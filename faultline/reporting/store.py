"""The blackboard: a reports/ directory every agent reads and writes. No message bus needed."""
from __future__ import annotations

import json
import threading
import os
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from ..schemas import (
    ApprovalRequest,
    Baseline,
    ExperimentReport,
    FixRecommendation,
    RiskAssessment,
    Scorecard,
    Topology,
    WeaknessReconciliation,
    WeaknessReport,
)


class ReportStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        for sub in ("experiments", "risk", "fixes", "logs", "approvals", "proposals"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- generic
    def _write(self, path: Path, model: BaseModel | dict) -> Path:
        data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
        # unique per writer: two threads saving the same file (e.g. an email wait and an approval) must not
        # share one temp file, or the second rename finds it gone
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
        return path

    def _read(self, path: Path) -> Optional[dict]:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    # ---------------------------------------------------------- topology / baseline
    def write_topology(self, topo: Topology) -> Path:
        return self._write(self.root / "topology.json", topo)

    def topology(self) -> Optional[Topology]:
        d = self._read(self.root / "topology.json")
        return Topology.model_validate(d) if d else None

    # ---------------------------------------------------------- fix proposals (remediation)
    def write_proposal(self, p: "FixProposal") -> Path:
        from faultline.schemas import FixProposal
        return self._write(self.root / "proposals" / f"{p.proposal_id}.json", FixProposal.model_validate(p.model_dump()))

    def proposal(self, proposal_id: str) -> Optional["FixProposal"]:
        from faultline.schemas import FixProposal
        if not proposal_id or "/" in proposal_id or ".." in proposal_id:
            return None
        d = self._read(self.root / "proposals" / f"{proposal_id}.json")
        return FixProposal.model_validate(d) if d else None

    def proposals(self) -> list["FixProposal"]:
        """Every proposal, newest first."""
        from faultline.schemas import FixProposal
        out = []
        for f in (self.root / "proposals").glob("*.json"):
            d = self._read(f)
            if d:
                out.append(FixProposal.model_validate(d))
        return sorted(out, key=lambda p: p.created_at, reverse=True)

    @property
    def fix_lock_file(self) -> Path:
        return self.root / "FIX_IN_FLIGHT.json"

    def write_baseline(self, b: Baseline) -> Path:
        return self._write(self.root / "baseline.json", b)

    def baseline(self) -> Optional[Baseline]:
        d = self._read(self.root / "baseline.json")
        return Baseline.model_validate(d) if d else None

    # ---------------------------------------------------------- experiments
    def write_experiment(self, r: ExperimentReport) -> Path:
        return self._write(self.root / "experiments" / f"{r.experiment_id}.json", r)

    def experiment_ids(self) -> list[str]:
        files = sorted((self.root / "experiments").glob("*.json"), key=lambda p: p.stat().st_mtime)
        return [p.stem for p in files]

    def experiment(self, eid: str) -> Optional[ExperimentReport]:
        d = self._read(self.root / "experiments" / f"{eid}.json")
        return ExperimentReport.model_validate(d) if d else None

    def experiments(self) -> list[ExperimentReport]:
        out = []
        for eid in self.experiment_ids():
            r = self.experiment(eid)
            if r:
                out.append(r)
        return out

    def experiment_dir(self, eid: str) -> Path:
        p = self.root / "experiments" / eid
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------------------------------------------------------- risk / fix / scorecard
    def write_risk(self, r: RiskAssessment) -> Path:
        return self._write(self.root / "risk" / f"{r.experiment_id}.json", r)

    def risk(self, eid: str) -> Optional[RiskAssessment]:
        d = self._read(self.root / "risk" / f"{eid}.json")
        return RiskAssessment.model_validate(d) if d else None

    def write_fix(self, f: FixRecommendation) -> Path:
        return self._write(self.root / "fixes" / f"{f.experiment_id}.json", f)

    def fix(self, eid: str) -> Optional[FixRecommendation]:
        d = self._read(self.root / "fixes" / f"{eid}.json")
        return FixRecommendation.model_validate(d) if d else None

    def write_scorecard(self, s: Scorecard) -> Path:
        return self._write(self.root / "scorecard.json", s)

    def scorecard(self) -> Optional[Scorecard]:
        d = self._read(self.root / "scorecard.json")
        return Scorecard.model_validate(d) if d else None

    # ---------------------------------------------------------- weakness scout
    def write_weaknesses(self, w: WeaknessReport) -> Path:
        return self._write(self.root / "weaknesses.json", w)

    def weaknesses(self) -> Optional[WeaknessReport]:
        d = self._read(self.root / "weaknesses.json")
        return WeaknessReport.model_validate(d) if d else None

    def write_reconciliation(self, r: WeaknessReconciliation) -> Path:
        return self._write(self.root / "weaknesses_reconcile.json", r)

    def reconciliation(self) -> Optional[WeaknessReconciliation]:
        d = self._read(self.root / "weaknesses_reconcile.json")
        return WeaknessReconciliation.model_validate(d) if d else None

    # ---------------------------------------------------------- coordination files
    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"

    def stop_requested(self) -> bool:
        return self.stop_file.exists()

    def request_stop(self, reason: str = "operator") -> None:
        self.stop_file.write_text(f"{reason} {time.time()}\n")

    def clear_stop(self) -> None:
        if self.stop_file.exists():
            self.stop_file.unlink()

    @property
    def lock_file(self) -> Path:
        return self.root / "ACTIVE_EXPERIMENT.json"

    def set_active(self, data: dict[str, Any]) -> None:
        self._write(self.lock_file, data)

    def active(self) -> Optional[dict]:
        return self._read(self.lock_file)

    def clear_active(self) -> None:
        if self.lock_file.exists():
            self.lock_file.unlink()

    @property
    def pending_approval_file(self) -> Path:
        return self.root / "PENDING_APPROVAL.json"

    def set_pending_approval(self, req: ApprovalRequest) -> None:
        self._write(self.pending_approval_file, req)

    def pending_approval(self) -> Optional[ApprovalRequest]:
        d = self._read(self.pending_approval_file)
        if not d:
            return None
        try:
            return ApprovalRequest.model_validate(d)
        except ValidationError:
            return None

    def clear_pending_approval(self) -> None:
        if self.pending_approval_file.exists():
            self.pending_approval_file.unlink()

    def write_approval(self, req: ApprovalRequest) -> Path:
        """The decided record, kept for good: one file per decision, named by approval id."""
        return self._write(self.root / "approvals" / f"{req.approval_id}.json", req)

    def approval(self, approval_id: str) -> Optional[ApprovalRequest]:
        d = self._read(self.root / "approvals" / f"{approval_id}.json")
        return ApprovalRequest.model_validate(d) if d else None

    def approval_ids(self) -> list[str]:
        files = sorted((self.root / "approvals").glob("*.json"), key=lambda p: p.stat().st_mtime)
        return [p.stem for p in files]

    def approvals(self) -> list[ApprovalRequest]:
        out = []
        for aid in self.approval_ids():
            a = self.approval(aid)
            if a:
                out.append(a)
        return out

    def write_live(self, data: dict[str, Any]) -> None:
        data["updated_at"] = time.time()
        self._write(self.root / "live.json", data)

    def live(self) -> Optional[dict]:
        return self._read(self.root / "live.json")

    def write_heartbeat(self, name: str, data: dict[str, Any]) -> None:
        data["at"] = time.time()
        self._write(self.root / f"{name}.heartbeat.json", data)

    def heartbeat(self, name: str) -> Optional[dict]:
        return self._read(self.root / f"{name}.heartbeat.json")

    def append_log(self, name: str, line: str) -> None:
        with (self.root / "logs" / f"{name}.log").open("a") as f:
            f.write(line.rstrip("\n") + "\n")
