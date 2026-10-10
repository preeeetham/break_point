"""Control plane behind the dashboard.

The CLI runs one command and exits. The dashboard instead keeps a long-lived session: the synthetic
API probe and the health monitor run continuously, telemetry is sampled at 1 Hz into a rolling
buffer, and operator actions ("map the app", "capture a baseline", "break things") are submitted as
jobs that run one at a time on a worker thread.

Everything the UI needs is assembled by :meth:`ControlPlane.state`. Nothing here talks HTTP; the
server module is a thin adapter over this class.
"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import Settings
from ..reporting.store import ReportStore

# pipeline order; the UI strip and docs/UI_CONTRACT.md §4 follow this sequence
AGENTS = ("onboarding", "topology", "scout", "planner", "injector", "guardian", "risk", "fix", "scorecard")
JOB_KINDS = ("topology", "scout", "baseline", "run", "analyze", "heal", "plan", "onboard")
ONBOARD_CACHE_DIR = ".cache/onboarding"   # same default as `faultline onboard --cache-dir`
KNOWLEDGE_SUFFIX = "_knowledge.yaml"
TELEMETRY_WINDOW_S = 300
TELEMETRY_SAMPLE_S = 1.0
MAX_EVENTS = 4000


# ---------------------------------------------------------------------- events
@dataclass
class Event:
    seq: int
    ts: float
    level: str
    source: str
    text: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "level": self.level, "source": self.source,
                "text": self.text, "data": self.data}


class EventLog:
    def __init__(self, maxlen: int = MAX_EVENTS):
        self._items: deque[Event] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, level: str, source: str, text: str, data: dict | None = None) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(self._seq, time.time(), level, source, str(text)[:2000], data or {})
            self._items.append(ev)
            return ev

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def since(self, after: int, limit: int = 300) -> list[dict]:
        with self._lock:
            items = [e for e in self._items if e.seq > after]
        return [e.as_dict() for e in items[-limit:]]


def infer_level(text: str, source: str = "") -> str:
    t = text.lower()
    if any(k in t for k in ("error", "failed", "failure to", "abort", "watchdog", "not recover", "lost", "relapse")):
        return "error" if "error" in t or "failed" in t else "warn"
    if any(k in t for k in ("recovered", "complete", "healthy", "held", "wrote", "done")):
        return "success"
    # only the injector actually applies faults; analysis agents merely describe them
    if source == "injector" and any(k in t for k in ("inject", "kill", "netem", "stress", "flood", "partition", "pause")):
        return "chaos"
    return "info"


# ---------------------------------------------------------------------- jobs
@dataclass
class Job:
    id: str
    kind: str
    label: str
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    error: Optional[str] = None
    result: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                "started_at": self.started_at, "ended_at": self.ended_at, "error": self.error,
                "result": self.result}


class JobBusy(RuntimeError):
    pass


# ---------------------------------------------------------------------- telemetry
class Telemetry:
    """1 Hz rolling buffer of what the synthetic probe sees, plus the experiment phase bands."""

    def __init__(self):
        self.points: deque[dict] = deque(maxlen=int(TELEMETRY_WINDOW_S / TELEMETRY_SAMPLE_S) + 60)
        self.phases: deque[dict] = deque(maxlen=40)
        self._open: Optional[dict] = None
        self._lock = threading.Lock()

    def sample(self, window, phase: str, experiment_id: Optional[str]) -> None:
        now = time.time()
        st = window.stats_since(now - 5.0)
        with self._lock:
            self.points.append({"t": round(now, 2), "success": st.success_rate, "p95": st.p95_ms,
                                "n": st.sample_count})
            active = phase and phase != "idle"
            if active and (self._open is None or self._open["phase"] != phase):
                if self._open:
                    self._open["to"] = now
                self._open = {"from": now, "to": None, "phase": phase, "experiment_id": experiment_id}
                self.phases.append(self._open)
            elif not active and self._open is not None:
                self._open["to"] = now
                self._open = None

    def as_dict(self) -> dict:
        with self._lock:
            cutoff = time.time() - TELEMETRY_WINDOW_S
            return {
                "points": [p for p in self.points if p["t"] >= cutoff],
                "phases": [dict(p) for p in self.phases if (p["to"] or time.time()) >= cutoff],
            }


# ---------------------------------------------------------------------- control plane
class ControlPlane:
    def __init__(self, settings: Settings, store: ReportStore):
        self.s = settings
        self.store = store
        self.events = EventLog()
        self.telemetry = Telemetry()
        self.session = None                      # faultline.cli.Session once armed
        self.session_error: Optional[str] = None
        self._job: Optional[Job] = None
        self._job_seq = 0
        self._job_lock = threading.Lock()
        # separate from _job_lock: job threads arm via _require_session() without holding it
        self._session_lock = threading.RLock()
        self._cancel = threading.Event()
        self._agents = {a: {"state": "idle", "at": None, "detail": ""} for a in AGENTS}
        self._run_meta: dict = {}
        self._onboarding: Optional[dict] = None   # last `onboard` job, surfaced in state()
        self._last_plan_sig: str = ""
        self._llm_cache: Optional[dict] = None
        self._stopping = threading.Event()
        self._sampler = threading.Thread(target=self._sample_loop, daemon=True, name="telemetry")
        self._sampler.start()
        self.events.add("info", "system", "Control room online. Arm the probes to begin.")

    # ------------------------------------------------------------------ logging
    def log(self, source: str, text: str, level: str | None = None, data: dict | None = None) -> None:
        self.events.add(level or infer_level(text, source), source, text, data)

    def _logger(self, source: str) -> Callable[[str], None]:
        def _log(text: str) -> None:
            self.log(source, str(text))
        return _log

    def _agent(self, name: str, state: str, detail: str = "") -> None:
        if name in self._agents:
            self._agents[name] = {"state": state, "at": time.time(), "detail": detail}

    # ------------------------------------------------------------------ session
    def arm(self) -> dict:
        if self.session is not None:
            return {"armed": True}
        with self._session_lock:
            if self.session is not None:
                return {"armed": True}
            return self._arm_locked()

    def _arm_locked(self) -> dict:
        from ..cli import Session

        try:
            self.log("system", "Arming session: building topology and starting probes ...")
            self._agent("topology", "working", "mapping services")
            session = Session(self.s, self.store, need_probes=True)
            self.session = session
            self.session_error = None
            svc = len(session.topology.services)
            self._agent("topology", "done", f"{svc} services mapped")
            self.log("probe", f"Synthetic API probe running at {self.s.probe_rps:g} req/s against {self.s.ditto_url}", "success")
            self.log("system", f"Session armed: {svc} services, health checks every {self.s.health_interval_s:g}s", "success")
            return {"armed": True}
        except Exception as exc:
            self.session_error = f"{type(exc).__name__}: {exc}"
            self._agent("topology", "error", self.session_error)
            self.log("system", f"Could not arm the session: {self.session_error}", "error")
            raise

    def disarm(self) -> dict:
        with self._session_lock:
            if self.session is not None:
                self.session.close()
                self.session = None
                self.log("system", "Session disarmed; probes stopped.", "warn")
        return {"armed": False}

    def _require_session(self):
        if self.session is None:
            self.arm()
        return self.session

    # ------------------------------------------------------------------ sampler
    def _sample_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                if self.session is not None:
                    live = self.store.live() or {}
                    self.telemetry.sample(self.session.window, live.get("phase") or "idle",
                                          live.get("experiment_id"))
            except Exception:
                pass
            self._stopping.wait(TELEMETRY_SAMPLE_S)

    def shutdown(self) -> None:
        self._stopping.set()
        if self.session is not None:
            self.session.close()

    # ------------------------------------------------------------------ jobs
    @property
    def job(self) -> Optional[Job]:
        return self._job

    def submit(self, kind: str, body: dict) -> Job:
        with self._job_lock:
            if self._job is not None and self._job.status == "running":
                raise JobBusy(f"'{self._job.label}' is already running")
            self._job_seq += 1
            job = Job(id=f"j{self._job_seq}", kind=kind, label=self._label(kind, body))
            self._job = job
            self._cancel.clear()
        self.log("system", f"Job started: {job.label}")
        threading.Thread(target=self._run_job, args=(job, body), daemon=True, name=f"job-{job.id}").start()
        return job

    def _label(self, kind: str, body: dict) -> str:
        if kind == "run":
            if body.get("mode") == "manual" and body.get("target"):
                return f"{body.get('failure', 'experiment')} on {body['target']}"
            n = int(body.get("experiments", 1) or 1)
            return f"AI-planned experiment{'s' if n > 1 else ''} (x{n})" if n > 1 else "AI-planned experiment"
        if kind == "onboard":
            repo = str(body.get("repo") or "").strip()
            return f"Onboard {repo[:60]}" if repo else "Onboard a repository"
        return {"topology": "Rebuild application map", "baseline": "Capture steady-state baseline",
                "analyze": "Run analysis agents", "heal": "Heal sandbox",
                "scout": "Scout for predicted weaknesses"}.get(kind, kind)

    def cancel(self) -> dict:
        job = self._job
        if job is None or job.status != "running":
            return {"ok": True, "cancelled": False}
        self._cancel.set()
        if job.kind == "run":
            self.store.request_stop("dashboard-cancel")
        self.log("system", f"Cancel requested for {job.label}", "warn")
        return {"ok": True, "cancelled": True}

    def _run_job(self, job: Job, body: dict) -> None:
        try:
            handler = {"topology": self._job_topology, "scout": self._job_scout,
                       "baseline": self._job_baseline, "run": self._job_run,
                       "analyze": self._job_analyze, "heal": self._job_heal,
                       "onboard": self._job_onboard}[job.kind]
            job.result = handler(body) or {}
            job.status = "cancelled" if self._cancel.is_set() and job.kind != "run" else "done"
            self.log("system", f"Job finished: {job.label}", "success")
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            self.log("system", f"Job failed: {job.label} -> {job.error}", "error",
                     {"traceback": traceback.format_exc()[-2000:]})
        finally:
            job.ended_at = time.time()

    # -- individual jobs -------------------------------------------------
    def _job_onboard(self, body: dict) -> dict:
        """Point Faultline at an unseen compose app and write its documented-knowledge file.

        Identical call shape to `faultline onboard`: resolve the reference to files on disk, make one
        bounded LLM pass, validate the answer in code, then write <out>/<slug>_knowledge.yaml. Every
        progress line goes through the event log, so the room narrates it exactly as the CLI prints it.
        """
        from pathlib import Path

        from ..cli import llm_client
        from ..onboarding.agent import compare, extract, onboard
        from ..onboarding.source import resolve

        repo = str(body.get("repo") or "").strip()
        if not repo:
            raise ValueError("a repository reference is required: a GitHub URL, owner/name, or a local path")
        out_dir = Path(str(body.get("out") or "").strip() or "config")
        compare_to = str(body.get("compare_to") or "").strip()
        log = self._logger("onboarding")
        try:
            self._agent("onboarding", "working", f"resolving {repo}")
            self.log("onboarding", f"Onboarding {repo}: locating the compose file, README and "
                                   f"architecture documentation ...")
            src = resolve(repo, Path(ONBOARD_CACHE_DIR), compose=(body.get("compose") or None),
                          ref=(body.get("ref") or None), log=log)
            self._agent("onboarding", "working", f"{src.slug}: reading {src.compose_path.name}")
            report = onboard(src, llm_client(self.s, not bool(body.get("no_llm"))),
                             Path(self.s.knowledge_file), out_dir, log=log)
            k = report.knowledge
            self.log("onboarding", f"{report.slug}: {len(report.services)} services, "
                                   f"{report.structural_edges} structural edges, "
                                   f"{len(k.documented_edges)} documented edges ({report.generated_by})",
                     "ai" if report.generated_by == "llm" else "success")
            for e in k.documented_edges:
                self.log("onboarding", f"documented edge {e.src} -> {e.dst}: {e.evidence}", "ai")
            if k.roles:
                self.log("onboarding", "role overrides: " +
                         ", ".join(f"{s}={r}" for s, r in sorted(k.roles.items())), "info")
            if k.health_port or k.cluster_alias:
                self.log("onboarding", f"health_port={k.health_port} cluster_alias={k.cluster_alias}", "info")
            # the rejections are the point: the model proposes, code decides what is admissible
            for r in report.rejected:
                self.log("onboarding", f"validation refused: {r}", "warn")
            for n in report.notes:
                self.log("onboarding", f"Note: {n}", "warn")
            self.log("onboarding", f"Wrote {report.output_path}. Use it with: faultline topology "
                                   f"--knowledge {report.output_path}", "success")
            comparison = None
            if compare_to:
                comparison = compare(k, Path(compare_to), extract(src.compose_path, src.nginx_conf_path))
                cov = comparison.get("final_graph_coverage") or {}
                edges = comparison.get("edges") or {}
                self.log("onboarding", f"Against the hand-authored {compare_to}: "
                                       f"{cov.get('covered_by_generated_or_structural')}/"
                                       f"{cov.get('truth_relationships')} documented relationships covered, "
                                       f"edge f1 {edges.get('f1')}, roles "
                                       f"{(comparison.get('roles') or {}).get('accuracy')}", "success")
            self._agent("onboarding", "done", f"{report.slug}: {len(k.documented_edges)} documented edges")
            result = {"report": report.model_dump(mode="json"), "comparison": comparison}
            self._onboarding = {"at": time.time(), "repo": repo, **result}
            return result
        except Exception as exc:
            self._agent("onboarding", "error", f"{type(exc).__name__}: {exc}")
            raise

    def _job_topology(self, body: dict) -> dict:
        from ..cli import build_topology

        self._agent("topology", "working", "scanning compose, env vars, nginx and live docker")
        topo = build_topology(self.s, self.store, live=True)
        if self.session is not None:
            self.session.topology = topo
        top = topo.criticality[0] if topo.criticality else None
        detail = f"{len(topo.services)} services, {len(topo.edges)} edges"
        self._agent("topology", "done", detail)
        self.log("topology", f"Application map rebuilt: {detail}" +
                 (f"; most critical: {top.service} ({top.score:g})" if top else ""), "success")
        for f in topo.findings:
            self.log("topology", f"Finding: {f}", "warn")
        return {"services": len(topo.services), "edges": len(topo.edges)}

    def _job_scout(self, body: dict) -> dict:
        """Static analysis only: reads compose + topology + knowledge, breaks nothing, arms nothing.

        Everything it produces is a PREDICTION. The log lines say so, because the feed sits next to
        measured findings and the two must never be confusable.
        """
        from ..agents import scout
        from ..cli import llm_client

        self._agent("scout", "working", "reading compose, health checks and declared limits")
        llm = llm_client(self.s, bool(body.get("llm", True)))
        report = scout.run(self.s, self.store, llm, log=self._logger("scout"))
        n = len(report.weaknesses)
        self._agent("scout", "done", f"{n} predicted weaknesses")
        # explicit levels throughout: infer_level() keyword-sniffs the text, and words like
        # "weakness"/"predicted" would otherwise be scored off whatever else is in the sentence
        self.log("scout", f"Static analysis of {report.compose_file} complete: {n} predicted "
                          f"weakness(es) {report.label} across {len(report.checks_run)} check(s). "
                          f"Nothing has been measured yet.", "success")
        for w in report.weaknesses:
            self.log("scout", f"Predicted {w.predicted_level} ({w.predicted_severity:g}/10) "
                              f"{report.label}: {w.title} [{', '.join(w.services) or 'system'}] "
                              f"- would be tested by {w.suggested_experiment.failure_type} on "
                              f"{w.suggested_experiment.target_service}", "ai")
        for sp in report.systemic_patterns:
            self.log("scout", f"Systemic pattern (LLM narrative, {report.label}): {sp.title}", "ai")
        for n_ in report.notes:
            self.log("scout", f"Note: {n_}", "info")
        return {"weaknesses": n, "patterns": len(report.systemic_patterns),
                "generated_by": report.generated_by, "label": report.label}

    def _job_baseline(self, body: dict) -> dict:
        from ..observe.baseline import capture, to_markdown

        session = self._require_session()
        duration = max(20, min(300, int(body.get("duration", 60) or 60)))
        self.log("probe", f"Measuring steady state for {duration}s ...")
        b = capture(session.api, session.health, session.window, duration, self.s.ditto_url,
                    session.policy_raw, log=self._logger("probe"))
        self.store.write_baseline(b)
        (self.store.root / "baseline.md").write_text(to_markdown(b))
        session.thresholds = b.thresholds
        self.log("probe", f"Baseline captured: success {100 * (b.api.success_rate or 0):.2f}%, "
                          f"p95 {b.api.p95_ms} ms. Abort thresholds are now calibrated.", "success")
        for n in b.notes:
            self.log("probe", f"Note: {n}", "warn")
        return {"p95_ms": b.api.p95_ms, "success_rate": b.api.success_rate}

    def _planner(self, use_llm: bool, allow_protected: bool, arm: bool = True):
        """arm=False keeps this read-only: previewing a plan must never start probes by surprise."""
        from ..cli import llm_client
        from ..planning.planner import Planner, Policy
        from ..safety.thresholds import load_policy

        session = self._require_session() if arm else self.session
        if session is not None:
            topo, policy, docker = session.topology, session.policy, session.docker
        else:
            topo = self.store.topology()
            if topo is None:
                raise RuntimeError("no application map yet — run the topology job first")
            policy, docker = Policy(load_policy(self.s.thresholds_file)), None
        images = {k for k, img in (("pumba", self.s.pumba_image), ("tc", self.s.tc_image))
                  if docker is None or docker.has_image(img)}
        return Planner(topo, policy, self.store.experiments(),
                       llm_client(self.s, use_llm), self.s, images, allow_protected)

    def plan(self, use_llm: bool = False, allow_protected: bool = False) -> dict:
        """Preview the ranked plan. The UI polls this, so it only narrates when the pick changes."""
        planner = self._planner(use_llm, allow_protected, arm=False)
        cands = planner.candidates()
        llm = planner.llm
        asking = use_llm and llm and llm.available()
        if asking and self._last_plan_sig != "pending-llm":
            self.log("planner", f"Asking {llm.describe()} to choose from the top candidates ...", "ai")
        spec = planner.plan_next()
        sig = f"{spec.target_service}/{spec.failure_type}/{spec.planned_by}" if spec else "none"
        if sig != self._last_plan_sig:
            self._last_plan_sig = sig
            if spec:
                self._agent("planner", "done", f"{spec.failure_type} on {spec.target_service} ({spec.planned_by})")
                self.log("planner", f"Next up: {spec.failure_type} on {spec.target_service} — {spec.rationale}",
                         "ai" if spec.planned_by == "llm" else "info")
            else:
                self._agent("planner", "done", "no candidates left")
                self.log("planner", "No candidates left under the current policy.", "warn")
        return {
            "candidates": [{"service": c.service, "failure_type": c.failure_type, "priority": c.priority,
                            "reasons": c.reasons,
                            "criticality": planner.topo.criticality_of(c.service)} for c in cands],
            "pick": self._spec_dict(spec) if spec else None,
            "llm": {"used": bool(spec and spec.planned_by == "llm"), "provider": self.s.llm_provider,
                    "model": llm.model if llm else "", "available": bool(llm and llm.available()),
                    "error": (llm.last_error if llm else "") or ""},
        }

    @staticmethod
    def _spec_dict(spec) -> dict:
        return {"experiment_id": spec.experiment_id, "target_service": spec.target_service,
                "container": spec.container, "failure_type": spec.failure_type, "parameters": spec.parameters,
                "rationale": spec.rationale, "hypothesis": spec.hypothesis.model_dump(mode="json"),
                "criticality_score": spec.criticality_score, "planned_by": spec.planned_by,
                "planner_notes": spec.planner_notes, "reproduce": spec.reproduce}

    def _job_run(self, body: dict) -> dict:
        from ..agents.injector import Injector

        session = self._require_session()
        if body.get("resume_stop", True):
            self.store.clear_stop()
        use_llm = bool(body.get("llm", True))
        allow_protected = bool(body.get("allow_protected", False))
        planner = self._planner(use_llm, allow_protected)
        injector = Injector(self.s, self.store, session.topology, session.docker, session.window,
                            session.api, session.health, session.thresholds, session.policy,
                            log=self._logger("injector"))
        manual = body.get("mode") == "manual" and body.get("target")
        count = 1 if manual else max(1, min(int(body.get("experiments", 1) or 1), session.policy.max_experiments))
        done = []
        for i in range(count):
            if self._cancel.is_set() or self.store.stop_requested():
                self.log("system", "STOP is latched; not starting another experiment.", "warn")
                break
            if manual:
                params = dict(body.get("params") or {})
                spec = planner.plan_operator(body["target"], body.get("failure", "container_crash"), params)
            else:
                self._agent("planner", "working", "choosing the next experiment")
                spec = planner.plan_next()
                if spec is None:
                    self.log("planner", "Nothing left to test under the current policy.", "warn")
                    break
                self._agent("planner", "done", f"{spec.failure_type} on {spec.target_service}")
            for n in spec.planner_notes:
                self.log("planner", n, "info")
            self._run_meta = {**self._spec_dict(spec), "max_s": min(
                float(spec.parameters.get("duration_s", 30)), session.thresholds.max_experiment_seconds)}
            self._agent("injector", "working", f"{spec.failure_type} on {spec.target_service}")
            self.log("injector", f"Experiment {spec.experiment_id[:8]}: {spec.failure_type} on "
                                 f"{spec.target_service} — {spec.rationale}", "chaos")
            self.log("injector", f"Hypothesis: {spec.hypothesis.statement}", "ai" if spec.planned_by == "llm" else "info")
            report = injector.run(spec)
            planner.history.append(report)
            done.append(report.experiment_id)
            self._run_meta = {}
            level = {"completed": "success", "aborted": "warn", "failed": "error", "skipped": "warn"}.get(report.status, "info")
            self._agent("injector", "done" if report.status == "completed" else "error", report.status)
            self.log("injector", f"Experiment {report.experiment_id[:8]} {report.status}: "
                                 f"success during {report.during.success_rate}, recovered in "
                                 f"{report.recovery.time_to_recover_s}s, findings "
                                 f"{[f.code for f in report.findings]}", level)
            if body.get("analyze", True):
                self._analyze(use_llm, only=[report.experiment_id])
            if i < count - 1 and not self._cancel.is_set():
                pause = session.policy.pause_between_s
                self.log("system", f"Settling for {pause:.0f}s before the next experiment ...")
                if self._cancel.wait(pause):
                    break
        return {"experiments": done}

    def _analyze(self, use_llm: bool, only: list[str] | None = None) -> dict:
        from ..agents import fix, risk, scorecard
        from ..cli import llm_client

        llm = llm_client(self.s, use_llm)
        topo = self.session.topology if self.session else self.store.topology()
        self._agent("risk", "working", "scoring severity")
        risks = risk.run(self.store, llm, topo, only=only, log=self._logger("risk"))
        self._agent("risk", "done", f"{len(risks)} assessed")
        self._agent("fix", "working", "matching remediations")
        fixes = fix.run(self.store, llm, only=only, log=self._logger("fix"))
        self._agent("fix", "done", f"{len(fixes)} report(s)")
        self._agent("scorecard", "working", "ranking services")
        sc = scorecard.run(self.store, topo, self.s.compose_project, log=self._logger("scorecard"))
        self._agent("scorecard", "done", f"overall {sc.overall_score}" if sc and sc.overall_score is not None else "no data")
        return {"risks": len(risks), "fixes": len(fixes)}

    def _job_analyze(self, body: dict) -> dict:
        return self._analyze(bool(body.get("llm", True)))

    def _job_heal(self, body: dict) -> dict:
        import argparse

        from ..cli import cmd_heal

        self.log("system", "Healing the sandbox: removing chaos leftovers and waiting for health ...", "warn")
        rc = cmd_heal(self.s, self.store, argparse.Namespace(restart=bool(body.get("restart", False))))
        if self.session is not None:
            self.session.api.ensure_canary()
        self.log("system", "Sandbox healthy again." if rc == 0 else "Sandbox did not come back healthy.",
                 "success" if rc == 0 else "error")
        return {"rc": rc}

    # ------------------------------------------------------------------ kill switch
    def stop(self) -> dict:
        self.store.request_stop("dashboard")
        self._cancel.set()
        self.log("system", "KILL SWITCH: stop requested. The running experiment rolls back within ~1 s "
                           "and the guardian restores anything still down.", "error")
        return {"ok": True, "stop_requested": True}

    def resume(self) -> dict:
        self.store.clear_stop()
        self._cancel.clear()
        self.log("system", "STOP cleared; experiments may run again.", "success")
        return {"ok": True, "stop_requested": False}

    # ------------------------------------------------------------------ catalog
    def catalog(self) -> dict:
        from ..chaos.catalog import CATALOG, applicable

        topo = self.session.topology if self.session else self.store.topology()
        policy = self.session.policy if self.session else None
        if policy is None:
            from ..planning.planner import Policy
            from ..safety.thresholds import load_policy
            policy = Policy(load_policy(self.s.thresholds_file))
        have = set()
        if self.session is not None:
            for key, img in (("pumba", self.s.pumba_image), ("tc", self.s.tc_image)):
                if self.session.docker.has_image(img):
                    have.add(key)
        else:
            have = {"pumba", "tc"}
        services = topo.services if topo else {}
        out = []
        for ft in CATALOG.values():
            missing = [r for r in ft.requires_images if r not in have]
            applies, blocked = [], []
            for name, node in services.items():
                if not applicable(ft, node):
                    continue
                ok, _why = policy.allows(name, ft.key, False)
                (applies if ok else blocked).append(name)
            out.append({
                "key": ft.key, "title": ft.title, "slide_category": ft.slide_category,
                "description": ft.description, "degrade_only": ft.degrade_only,
                "expected_recovery_s": ft.expected_recovery_s, "requires_images": list(ft.requires_images),
                "available": not missing,
                "unavailable_reason": (f"missing helper image(s): {', '.join(missing)}" if missing else None),
                "params": [{"name": p.name, "default": p.default, "lo": p.lo, "hi": p.hi, "unit": p.unit,
                            "desc": p.desc, "type": "bool" if isinstance(p.default, bool) else "number"}
                           for p in ft.params],
                "applies_to": sorted(applies), "policy_blocked_on": sorted(blocked),
            })
        base = self.store.baseline()
        return {"failures": out, "protected": policy.protected,
                "thresholds": (base.thresholds.model_dump(mode="json") if base else
                               (self.session.thresholds.model_dump(mode="json") if self.session else {}))}

    # ------------------------------------------------------------------ knowledge files
    def knowledge(self) -> dict:
        """The documented-knowledge files on disk, so the room can show what Faultline can be pointed at.

        One entry per `config/*_knowledge.yaml`. Each file is parsed through the same schema the
        topology mapper uses; a file that does not validate is skipped with a note rather than
        breaking the panel, because these files are hand-editable.
        """
        from pathlib import Path

        import yaml

        from ..config import ROOT
        from ..schemas import RepoKnowledge

        active = Path(self.s.knowledge_file)
        cfg = active.parent if active.parent.is_dir() else ROOT / "config"
        try:
            active_resolved = str(active.resolve())
        except OSError:
            active_resolved = str(active)
        files, notes = [], []
        for path in sorted(cfg.glob(f"*{KNOWLEDGE_SUFFIX}")):
            try:
                k = RepoKnowledge.model_validate(yaml.safe_load(path.read_text()) or {})
            except Exception as exc:
                notes.append(f"{path.name} is not a usable knowledge file: {type(exc).__name__}: {exc}")
                continue
            referenced = {s for e in k.documented_edges for s in (e.src, e.dst)} | set(k.roles)
            try:
                resolved, mtime = str(path.resolve()), path.stat().st_mtime
            except OSError:
                resolved, mtime = str(path), None
            files.append({
                "slug": path.name[: -len(KNOWLEDGE_SUFFIX)],
                "path": str(path),
                "documented_edges": len(k.documented_edges),
                "roles": dict(sorted(k.roles.items())),
                "health_port": k.health_port,
                "cluster_alias": k.cluster_alias,
                "services_referenced": sorted(referenced),
                "is_active": resolved == active_resolved,
                "mtime": mtime,
            })
        files.sort(key=lambda f: (not f["is_active"], f["slug"]))
        return {"dir": str(cfg), "active": str(active), "files": files, "notes": notes}

    # ------------------------------------------------------------------ state
    def _llm_state(self) -> dict:
        if self._llm_cache and time.time() - self._llm_cache["at"] < 30:
            return self._llm_cache["value"]
        from ..cli import llm_client

        c = llm_client(self.s)
        value = {"provider": c.provider, "model": c.model, "available": c.available(),
                 "enabled": c.enabled, "error": c.last_error or ""}
        self._llm_cache = {"at": time.time(), "value": value}
        return value

    def _run_state(self) -> dict:
        live = self.store.live() or {}
        active = self.store.active() or {}
        meta = dict(self._run_meta)
        phase = live.get("phase") or ("preflight" if meta and not live else "idle")
        job = self._job
        if phase == "idle" and job and job.kind == "run" and job.status == "running" and meta:
            phase = "preflight"
        if phase == "idle" and job and job.kind == "analyze" and job.status == "running":
            phase = "analyzing"
        out = {
            "phase": phase,
            "experiment_id": live.get("experiment_id") or meta.get("experiment_id"),
            "target_service": live.get("target_service") or meta.get("target_service"),
            "failure_type": live.get("failure_type") or meta.get("failure_type"),
            "parameters": meta.get("parameters") or {},
            "rationale": live.get("rationale") or meta.get("rationale"),
            "hypothesis": live.get("hypothesis") or (meta.get("hypothesis") or {}).get("statement"),
            "planned_by": meta.get("planned_by"),
            "elapsed_s": live.get("elapsed_s"),
            "success_rate": live.get("success_rate"),
            "p95_ms": live.get("p95_ms"),
            "sample_count": live.get("sample_count"),
            "health": live.get("health") or {},
            "max_s": meta.get("max_s"),
            "deadline_in_s": (round(active["deadline"] - time.time(), 1)
                              if active.get("deadline") and phase not in ("idle",) else None),
            "consecutive_ok": live.get("consecutive_ok"),
            "soak_elapsed_s": live.get("soak_elapsed_s"),
        }
        expected = list((meta.get("hypothesis") or {}).get("expected_affected") or [])
        observed = [svc for svc, h in (live.get("health") or {}).items()
                    if h and (h.get("status") != "running" or (h.get("alive") not in (None, 200)))]
        out["blast"] = {"expected": expected, "observed": observed}
        out["safety"] = {"abort_reason": live.get("abort_reason"), "detail": {}}
        return out

    def _agents_state(self) -> dict:
        out = {k: dict(v) for k, v in self._agents.items()}
        hb = self.store.heartbeat("guardian") or {}
        alive = hb.get("at", 0) > time.time() - 15
        out["guardian"] = {"state": "working" if alive else "idle", "at": hb.get("at"),
                           "detail": "armed, watching containers" if alive else "not running (faultline guardian)"}
        return out

    def _topology_state(self) -> Optional[dict]:
        topo = (self.session.topology if self.session else None) or self.store.topology()
        if topo is None:
            return None
        crit = {c.service: c for c in topo.criticality}
        return {
            "services": {n: {"role": s.role, "stateful": s.stateful, "entrypoint": s.entrypoint,
                             "container": s.container, "state": s.state, "health": s.health,
                             "chaos_relevant": s.chaos_relevant, "image": s.image,
                             "mem_limit_mb": s.mem_limit_mb,
                             "criticality": round(crit[n].score, 2) if n in crit else 0.0}
                         for n, s in topo.services.items()},
            "edges": [{"src": e.src, "dst": e.dst, "kind": e.kind} for e in topo.edges],
            "criticality": [{"service": c.service, "score": c.score, "fan_in": c.fan_in,
                             "transitive_dependents": c.transitive_dependents, "reasons": c.reasons}
                            for c in topo.criticality],
            "findings": topo.findings,
            "generated_at": topo.generated_at.isoformat() if topo.generated_at else None,
        }

    def _baseline_state(self) -> Optional[dict]:
        b = self.store.baseline()
        if b is None:
            return None
        return {"captured_at": b.captured_at.isoformat(), "duration_s": b.duration_s,
                "api": b.api.model_dump(mode="json"), "thresholds": b.thresholds.model_dump(mode="json"),
                "health_availability": b.health_availability, "notes": b.notes}

    def _experiments_state(self) -> list[dict]:
        out = []
        for r in self.store.experiments():
            risk = self.store.risk(r.experiment_id)
            fix = self.store.fix(r.experiment_id)
            out.append({
                "experiment_id": r.experiment_id, "status": r.status,
                "started_at": r.started_at.isoformat(), "ended_at": r.ended_at.isoformat(),
                "target_service": r.target.get("service"), "failure_type": r.failure_type,
                "parameters": r.parameters, "rationale": r.rationale, "planned_by": r.planned_by,
                "criticality_score": r.criticality_score,
                "hypothesis": r.hypothesis.model_dump(mode="json"),
                "hypothesis_held": r.hypothesis_result.held,
                "hypothesis_notes": r.hypothesis_result.notes,
                "baseline_success_rate": r.baseline.success_rate, "baseline_p95_ms": r.baseline.p95_ms,
                "during_success_rate": r.during.success_rate, "during_p95_ms": r.during.p95_ms,
                "after_success_rate": r.after.success_rate,
                "time_to_recover_s": r.recovery.time_to_recover_s,
                "outage_duration_s": r.recovery.outage_duration_s, "recovered": r.recovery.recovered,
                "auto_restarted_by_docker": r.recovery.auto_restarted_by_docker,
                "remediation_actions": r.recovery.remediation_actions,
                "data_integrity_ok": r.recovery.data_integrity_ok,
                "data_integrity_detail": r.recovery.data_integrity_detail,
                "aborted_early": r.safety.aborted_early, "abort_reason": r.safety.abort_reason,
                "watchdog_fired": r.safety.watchdog_fired, "operator_stop": r.safety.operator_stop,
                "affected_services_observed": r.affected_services_observed,
                "findings": [f.model_dump(mode="json") for f in r.findings],
                "risk": ({"severity": risk.severity, "level": risk.level, "summary": risk.summary,
                          "production_implication": risk.production_implication,
                          "blast_radius": risk.blast_radius, "factors": risk.factors,
                          "generated_by": risk.generated_by} if risk else None),
                "fix": ({"generated_by": fix.generated_by,
                         "recommendations": [r_.model_dump(mode="json") for r_ in fix.recommendations]}
                        if fix else None),
                "timeline": [{"t_plus_s": t.t_plus_s, "event": t.event, "detail": t.detail} for t in r.timeline],
                "reproduce": r.reproduce,
            })
        out.sort(key=lambda e: e["started_at"], reverse=True)
        return out

    def _scorecard_state(self) -> Optional[dict]:
        sc = self.store.scorecard()
        if sc is None:
            return None
        return {"overall_score": sc.overall_score, "summary": sc.summary,
                "experiments_total": sc.experiments_total,
                "ranking": [r.model_dump(mode="json") for r in sc.ranking]}

    def _weaknesses_state(self) -> Optional[dict]:
        """The Weakness Scout's PREDICTIONS. `label` is carried through untouched: the UI renders it
        verbatim so a prediction is never mistaken for a measured finding."""
        w = self.store.weaknesses()
        if w is None:
            return None
        return {"generated_at": w.generated_at.isoformat(), "project": w.project,
                "compose_file": w.compose_file, "knowledge_file": w.knowledge_file,
                "label": w.label, "checks_run": w.checks_run, "notes": w.notes,
                "generated_by": w.generated_by,
                "weaknesses": [x.model_dump(mode="json") for x in w.weaknesses],
                "systemic_patterns": [x.model_dump(mode="json") for x in w.systemic_patterns]}

    def state(self, since: int = 0) -> dict:
        safe = _safe
        probe = {"running": False, "rps": self.s.probe_rps, "canary": f"{self.s.probe_namespace}:canary",
                 "samples": 0}
        if self.session is not None:
            probe["running"] = bool(self.session.api and self.session.api.is_alive())
            probe["samples"] = len(self.session.window.all())
        job = self._job.as_dict() if self._job else None
        return {
            "now": time.time(),
            "seq": self.events.seq,
            "events": self.events.since(since),
            "session": {"armed": self.session is not None, "error": self.session_error, "probe": probe,
                        "project": self.s.compose_project, "ditto_url": self.s.ditto_url},
            "job": job,
            "run": safe(self._run_state, {"phase": "idle"}),
            "telemetry": self.telemetry.as_dict(),
            "stop_requested": bool(safe(self.store.stop_requested, False)),
            "guardian": safe(lambda: self.store.heartbeat("guardian")),
            "agents": safe(self._agents_state, {}),
            "llm": safe(self._llm_state, {}),
            "topology": safe(self._topology_state),
            "baseline": safe(self._baseline_state),
            "experiments": safe(self._experiments_state, []),
            "scorecard": safe(self._scorecard_state),
            "weaknesses": safe(self._weaknesses_state),
            "onboarding": self._onboarding,
        }

    def experiment_detail(self, eid: str) -> Optional[dict]:
        from ..reporting.markdown import experiment_markdown

        report = self.store.experiment(eid)
        if report is None:
            return None
        risk = self.store.risk(eid)
        fix = self.store.fix(eid)
        return {"experiment": report.model_dump(mode="json"),
                "risk": risk.model_dump(mode="json") if risk else None,
                "fix": fix.model_dump(mode="json") if fix else None,
                "markdown": experiment_markdown(report)}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default
