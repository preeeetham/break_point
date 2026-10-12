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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import ROOT, Settings
from ..reporting.store import ReportStore

# pipeline order; the UI strip and docs/UI_CONTRACT.md §4 follow this sequence
AGENTS = ("onboarding", "topology", "scout", "baseline", "planner", "injector", "guardian", "risk", "fix",
          "scorecard", "reconcile")
JOB_KINDS = ("topology", "scout", "baseline", "run", "analyze", "heal", "plan", "onboard", "reconcile", "fix")
# Kinds a bare POST /api/jobs/<kind> may start. `plan` is GET-only; `fix` applies a change to the stack
# and is only ever started through POST /api/proposals/<id>/approve|apply, against a proposal a human
# approved (ControlPlane.submit_fix). apply.preflight re-checks the status regardless.
GENERIC_JOB_KINDS = tuple(k for k in JOB_KINDS if k not in ("plan", "fix"))
# how many decided (final) proposals state() carries next to every open one
PROPOSALS_DECIDED_IN_STATE = 6
ONBOARD_CACHE_DIR = ".cache/onboarding"   # same default as `faultline onboard --cache-dir`
KNOWLEDGE_SUFFIX = "_knowledge.yaml"
# keys the safety monitor's verdict detail spreads flat into live.json (injector._live)
SAFETY_DETAIL_KEYS = ("trailing_samples", "error_rate", "threshold", "threshold_ms", "consecutive_failures")
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
        # whatever the operator configured at startup stays adoptable even if it lives elsewhere
        self._startup_targets = {Path(p).resolve() for p in (settings.compose_file, settings.knowledge_file) if p}
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
        if kind == "fix":
            return f"Apply approved fix {str(body.get('proposal_id') or '')[:24]}".rstrip()
        return {"topology": "Rebuild application map", "baseline": "Capture steady-state baseline",
                "analyze": "Run analysis agents", "heal": "Heal sandbox",
                "scout": "Scout for predicted weaknesses",
                "reconcile": "Reconcile predictions against experiments"}.get(kind, kind)

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
                       "onboard": self._job_onboard, "reconcile": self._job_reconcile,
                       "fix": self._job_fix}[job.kind]
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

    # -- target files ------------------------------------------------------
    def _target_path(self, raw: Any, kind: str) -> Optional[Path]:
        """Resolve an operator-supplied compose/knowledge path and refuse anything outside deploy/, config/
        or the onboarding cache. Not the whole repo: a parse error would echo a line of `.env` to the browser."""
        text = str(raw or "").strip()
        if not text:
            return None
        p = Path(text).expanduser()
        candidates = [p] if p.is_absolute() else [ROOT / p, Path.cwd() / p]
        path = next((c.resolve() for c in candidates if c.is_file()), None)
        if path is None:
            raise ValueError(f"{kind} file not found: {text}")
        roots = (ROOT / "deploy", ROOT / "config", ROOT / ONBOARD_CACHE_DIR)
        if path not in self._startup_targets and not any(path.is_relative_to(r.resolve()) for r in roots):
            raise ValueError(f"{kind} file must live under deploy/, config/ or {ONBOARD_CACHE_DIR}/: {text}")
        return path

    def _target_state(self) -> dict:
        return {"compose_file": self.s.compose_file, "knowledge_file": self.s.knowledge_file,
                "project": self.s.compose_project}

    def adopt_target(self, body: dict) -> dict:
        """Point the room at another compose file and/or knowledge file. Disarms first: the probes and
        the in-memory map belong to the old target."""
        compose = self._target_path(body.get("compose"), "compose")
        knowledge = self._target_path(body.get("knowledge"), "knowledge")
        if compose is None and knowledge is None:
            raise ValueError("nothing to adopt: give a compose and/or knowledge path")
        self.disarm()
        if compose is not None:
            self.s.compose_file = str(compose)
        if knowledge is not None:
            self.s.knowledge_file = str(knowledge)
        self._last_plan_sig = ""
        for name in ("topology", "scout"):
            self._agent(name, "idle", "target changed")
        target = self._target_state()
        self.log("system", f"Target adopted: compose {target['compose_file']}, knowledge "
                           f"{target['knowledge_file']}. Rebuild the map before running anything.", "warn", target)
        return target

    def _job_topology(self, body: dict) -> dict:
        from ..cli import build_topology

        compose = self._target_path(body.get("compose"), "compose")
        knowledge = self._target_path(body.get("knowledge"), "knowledge")
        foreign = compose is not None and compose != Path(self.s.compose_file).resolve()
        live = bool(body.get("live", not foreign))
        self._agent("topology", "working", "scanning compose, env vars, nginx" + (" and live docker" if live else ""))
        topo = build_topology(self.s, self.store, live=live,
                              knowledge=str(knowledge) if knowledge else None,
                              compose=str(compose) if compose else None)
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

        duration = max(20, min(300, int(body.get("duration", 60) or 60)))
        self._agent("baseline", "working", f"measuring steady state for {duration}s")
        try:
            session = self._require_session()
            self.log("probe", f"Measuring steady state for {duration}s ...")
            b = capture(session.api, session.health, session.window, duration, self.s.ditto_url,
                        session.policy_raw, log=self._logger("probe"))
            self.store.write_baseline(b)
            (self.store.root / "baseline.md").write_text(to_markdown(b))
            session.thresholds = b.thresholds
        except Exception as exc:
            self._agent("baseline", "error", f"{type(exc).__name__}: {exc}")
            raise
        self._agent("baseline", "done", f"success {100 * (b.api.success_rate or 0):.2f}%, p95 {b.api.p95_ms} ms")
        self.log("probe", f"Baseline captured: success {100 * (b.api.success_rate or 0):.2f}%, "
                          f"p95 {b.api.p95_ms} ms. Abort thresholds are now calibrated.", "success")
        for n in b.notes:
            self.log("probe", f"Note: {n}", "warn")
        return {"p95_ms": b.api.p95_ms, "success_rate": b.api.success_rate}

    def _planner(self, use_llm: bool, allow_protected: bool, arm: bool = True, approval_gate: bool = False):
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
        # same extra ranking signal the CLI passes (cli._planner): predictions break near-ties only
        return Planner(topo, policy, self.store.experiments(),
                       llm_client(self.s, use_llm), self.s, images, allow_protected, approval_gate,
                       weaknesses=self.store.weaknesses())

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

    def _set_run_meta(self, spec, session) -> None:
        self._run_meta = {**self._spec_dict(spec), "max_s": min(
            float(spec.parameters.get("duration_s", 30)), session.thresholds.max_experiment_seconds)}

    def _gate(self, spec, session) -> str:
        """Hold the experiment until a human decides. Returns the answer the run loop must act on.

        This blocks the single job slot for up to APPROVAL_TIMEOUT_SECONDS, deliberately: nothing is
        injected while a decision is open. The kill switch is unaffected — /api/stop, /api/resume and
        /api/jobs/cancel are served on HTTP handler threads, and the agent's own poll loop returns the
        safe default as soon as the STOP file appears. The agent logs on send, on reply and once per
        unreachable mailbox, never per poll, so a long wait cannot flush the event feed.
        """
        from ..agents import approval

        planned = dict(spec.parameters)
        self._agent("injector", "working", "held: waiting for an admin decision")
        self.log("approval", f"Experiment {spec.experiment_id[:8]} is held pending an admin decision: "
                             f"{spec.approval_reason or 'the policy requires one'}", "warn")
        decision = approval.gate(self.s, self.store, spec, log=self._logger("approval"))
        if decision.answer in ("skip", "stop_run"):
            # nothing will be injected for this spec, so the launcher must not keep showing it as
            # preflight: _run_meta is only cleared after a completed injection.
            self._run_meta = {}
        if decision.answer == "skip":
            self._agent("injector", "idle", "skipped: no approval")
            self.log("approval", f"Not injecting {spec.failure_type} on {spec.target_service} — "
                                 f"{decision.reason}", "warn")
        elif decision.answer == "stop_run":
            # gate() has already written reports/STOP; the run ends here rather than at the next guard.
            self._agent("injector", "idle", "run aborted by the admin")
            self.log("approval", f"Run aborted before anything was injected — {decision.reason}. STOP is "
                                 f"latched; Resume clears it.", "error")
        else:
            # gate() mutated the spec in place — halved parameters on APPROVE REDUCED, a planner note
            # on both — so the launcher must stop showing what the planner asked for.
            self._set_run_meta(spec, session)
            delta = ", ".join(f"{k} {planned.get(k)} -> {v}" for k, v in spec.parameters.items()
                              if planned.get(k) != v)
            self._agent("injector", "working", f"{spec.failure_type} on {spec.target_service}"
                                               + (" (reduced)" if delta else ""))
            self.log("approval", f"Admin approved {spec.failure_type} on {spec.target_service} "
                                 + (f"with {delta}" if delta else f"as planned — {decision.reason}"), "success")
        return decision.answer

    def _job_run(self, body: dict) -> dict:
        from ..agents.injector import Injector

        session = self._require_session()
        if body.get("resume_stop", True):
            self.store.clear_stop()
        use_llm = bool(body.get("llm", True))
        allow_protected = bool(body.get("allow_protected", False))
        # Without this the gate below is unreachable from the browser: nothing would ever set
        # `requires_approval` on a spec the control room planned.
        from ..agents.approval import ApprovalAgent, gate_available

        gate_on = gate_available(self.s)
        if gate_on:
            self.log("approval", f"Approval gate on: {ApprovalAgent(self.s, self.store).describe()}")
        planner = self._planner(use_llm, allow_protected, approval_gate=gate_on)
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
            self._set_run_meta(spec, session)
            self._agent("injector", "working", f"{spec.failure_type} on {spec.target_service}")
            self.log("injector", f"Experiment {spec.experiment_id[:8]}: {spec.failure_type} on "
                                 f"{spec.target_service} — {spec.rationale}", "chaos")
            self.log("injector", f"Hypothesis: {spec.hypothesis.statement}", "ai" if spec.planned_by == "llm" else "info")
            if spec.requires_approval:
                answer = self._gate(spec, session)
                if answer == "skip":
                    continue
                if answer == "stop_run":
                    break
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
            self._narrate(report)
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
        # close the loop the scout opened; never let it fail the analysis that just ran
        if self.store.weaknesses() is not None:
            try:
                self._reconcile()
            except Exception as exc:
                self.log("reconcile", f"Reconcile skipped: {type(exc).__name__}: {exc}", "warn")
        # "Can I fix this?" requests for what was just analysed; guarded the same way
        proposed = self._propose(llm if use_llm else None, only)
        return {"risks": len(risks), "fixes": len(fixes), "proposals": proposed}

    def _job_analyze(self, body: dict) -> dict:
        return self._analyze(bool(body.get("llm", True)))

    def _reconcile(self):
        """Pure arithmetic over reports/: predicted vs confirmed. None when the scout has not run."""
        from ..agents import reconcile

        self._agent("reconcile", "working", "scoring predictions against experiments")
        try:
            rec = reconcile.run(self.store, self.s.compose_project, log=self._logger("reconcile"))
        except Exception as exc:
            self._agent("reconcile", "error", f"{type(exc).__name__}: {exc}")
            raise
        if rec is None:
            self._agent("reconcile", "idle", "no predictions yet")
            return None
        self._agent("reconcile", "done", f"{rec.confirmed}/{rec.predicted_total} confirmed")
        self.log("reconcile", f"Reconciled {rec.predicted_total} prediction(s) across "
                              f"{rec.experiments_considered} experiment(s): {rec.confirmed} confirmed, "
                              f"{rec.cleared} cleared, {rec.untested} untested.", "success")
        return rec

    def _job_reconcile(self, body: dict) -> dict:
        rec = self._reconcile()
        if rec is None:
            msg = "no predicted weaknesses yet: run the scout job before reconciling"
            self._agent("reconcile", "error", msg)
            raise RuntimeError(msg)
        return {"predicted": rec.predicted_total, "confirmed": rec.confirmed, "cleared": rec.cleared,
                "untested": rec.untested, "experiments_considered": rec.experiments_considered}

    # ------------------------------------------------------------------ fix proposals
    def _propose(self, llm, only: list[str] | None = None) -> int:
        """Build FixProposals for analysed experiments whose findings go above `info`.

        Never fails the analysis that called it: a missing remediation module, a catalog with nothing
        that matches, or a proposal builder error is one warn line in the feed. An experiment that
        already has proposals is skipped, so re-running analysis never duplicates a pending request.
        Nothing here approves or applies anything; proposals start `pending` and wait for a human.
        """
        try:
            from ..remediation import propose
        except ImportError:
            return 0
        made = 0
        try:
            already = {p.experiment_id for p in self.store.proposals()}
            ids = list(only) if only else self.store.experiment_ids()
        except Exception as exc:
            self.log("fix", f"Fix proposals skipped: {type(exc).__name__}: {exc}", "warn")
            return 0
        for eid in ids:
            if eid in already:
                continue
            try:
                report = self.store.experiment(eid)
                if report is None or not any(f.severity != "info" for f in report.findings):
                    continue
                out = list(propose.build_proposals(self.s, self.store, eid, llm=llm,
                                                   log=self._logger("fix")) or [])
            except Exception as exc:
                self.log("fix", f"Fix proposals skipped for {eid[:8]}: {type(exc).__name__}: {exc}", "warn")
                continue
            made += len(out)
            if out:
                self.log("fix", f"Can I fix this? {len(out)} proposal(s) for experiment {eid[:8]} are waiting "
                                f"for a human ({', '.join(p.catalog_fix_id for p in out)}). Nothing is applied "
                                f"until someone approves the exact change shown.", "info")
        return made

    @staticmethod
    def _proposal_summary(p) -> dict:
        left = (p.expires_at - datetime.now(timezone.utc)).total_seconds()
        return {
            "id": p.proposal_id, "experiment_id": p.experiment_id, "finding_code": p.finding_code,
            "catalog_fix_id": p.catalog_fix_id, "target_services": list(p.target_services),
            "status": p.status, "created_at": p.created_at.isoformat(), "expires_at": p.expires_at.isoformat(),
            "seconds_left": round(left, 1),
            "requires_allow_protected": p.requires_allow_protected,
            "explanation": p.explanation.model_dump(mode="json"),
            "change_preview": p.change_preview, "change_hash": p.change_hash,
            "verification": p.verification.model_dump(mode="json") if p.verification else None,
            "decided_by": p.decided_by, "decided_at": p.decided_at.isoformat() if p.decided_at else None,
            "reason": p.reason,
        }

    def _fix_running(self) -> bool:
        job = self._job
        return bool(job and job.kind == "fix" and job.status == "running")

    def _sweep_proposals(self) -> list:
        """Expire overdue proposals, newest first. Not while a fix job runs: the apply engine is then
        the only writer of the proposal it holds, and a sweep holding a stale copy could overwrite it."""
        from ..remediation import proposals as P

        return self.store.proposals() if self._fix_running() else P.sweep_expired(self.store)

    def proposals(self) -> dict:
        """GET /api/proposals: every proposal, newest first, after expiring the overdue ones."""
        return {"proposals": [self._proposal_summary(p) for p in self._sweep_proposals()]}

    def proposal_detail(self, proposal_id: str) -> Optional[dict]:
        from ..remediation import proposals as P

        p = self.store.proposal(proposal_id)
        if p is None:
            return None
        if not self._fix_running():
            P.expire_if_due(self.store, p)
        out = p.model_dump(mode="json")
        out["seconds_left"] = round((p.expires_at - datetime.now(timezone.utc)).total_seconds(), 1)
        return out

    def approve_proposal(self, proposal_id: str, change_hash: str, reason: str = "",
                         allow_protected: bool = False, actor: str = "dashboard", use_llm: bool = False) -> dict:
        """Record a human approval of exactly the change they were shown, then try to start the apply.

        Raises KeyError (unknown), proposals.HashMismatch (the hash quoted is not the current change)
        or proposals.IllegalTransition (expired, already decided). The approval stands even when the
        job slot is busy: the response says so and POST /api/proposals/<id>/apply retries it.
        """
        from ..remediation import proposals as P

        p = P.approve(self.store, proposal_id, change_hash, actor, reason)
        self.log("approval", f"Fix proposal {p.proposal_id} ({p.catalog_fix_id} on "
                             f"{', '.join(p.target_services) or 'the stack'}) approved by {actor} for change "
                             f"{p.change_hash[:12]}" + (f": {reason}" if reason else ""), "success")
        return {"ok": True, "proposal": self._proposal_summary(p),
                "apply": self._start_fix(p.proposal_id, allow_protected, use_llm)}

    def reject_proposal(self, proposal_id: str, reason: str = "", actor: str = "dashboard") -> dict:
        from ..remediation import proposals as P

        p = P.reject(self.store, proposal_id, actor, reason)
        self.log("approval", f"Fix proposal {p.proposal_id} rejected by {actor}"
                             + (f": {reason}" if reason else "") + ". Nothing will be applied.", "warn")
        return {"ok": True, "proposal": self._proposal_summary(p)}

    def _start_fix(self, proposal_id: str, allow_protected: bool, use_llm: bool = False) -> dict:
        import importlib.util

        if importlib.util.find_spec("faultline.remediation.apply") is None:
            return {"started": False, "job": None,
                    "reason": "the apply engine (faultline.remediation.apply) is not installed; "
                              "the approval is recorded and nothing was applied"}
        try:
            job = self.submit_fix(proposal_id, allow_protected, use_llm)
        except JobBusy as exc:
            return {"started": False, "job": self._job.as_dict() if self._job else None,
                    "reason": f"{exc}; the approval stands, start the apply with "
                              f"POST /api/proposals/{proposal_id}/apply once the slot is free"}
        return {"started": True, "job": job.as_dict(), "reason": ""}

    def submit_fix(self, proposal_id: str, allow_protected: bool = False, use_llm: bool = False) -> Job:
        """Start the `fix` job for an APPROVED proposal. The only way that job kind is ever submitted."""
        from ..remediation import proposals as P

        p = self.store.proposal(proposal_id)
        if p is None:
            raise KeyError(f"no proposal {proposal_id}")
        if not self._fix_running():
            P.expire_if_due(self.store, p)
        if p.status != "approved":
            raise P.IllegalTransition(f"proposal {proposal_id} is {p.status}; only an approved proposal "
                                      f"can be applied")
        return self.submit("fix", {"proposal_id": proposal_id, "allow_protected": bool(allow_protected),
                                   "llm": bool(use_llm)})

    def _job_fix(self, body: dict) -> dict:
        """Apply one approved catalog fix and verify it by re-running the experiment (apply.py).

        Every refusal (not approved, expired, hash drift, protected service without allow_protected,
        STOP latched) surfaces as the job's error and in the proposal's own audit trail.
        """
        from ..remediation import apply

        pid = str(body.get("proposal_id") or "").strip()
        if not pid:
            raise ValueError("a fix job needs the id of an approved proposal")
        if self.store.stop_requested():
            raise RuntimeError("STOP is latched; resume before applying a fix")
        allow_protected = bool(body.get("allow_protected", False))
        self._agent("fix", "working", f"applying approved fix {pid}")
        self.log("fix", f"Applying approved fix proposal {pid}; the stack is health-checked afterwards "
                        f"and rolled back if it does not come back.", "warn")
        try:
            deps = self._fix_deps(allow_protected, bool(body.get("llm", False)))
            p = apply.apply_and_verify(self.s, self.store, pid, allow_protected=allow_protected,
                                       log=self._logger("fix"), deps=deps)
        except Exception as exc:
            self._agent("fix", "error", f"{pid}: {type(exc).__name__}: {exc}")
            raise
        if p.status == "approved":
            # apply.py returns a refusal instead of raising: status unchanged, reason in the audit trail
            note = next((a.note for a in reversed(p.audit) if a.note.startswith("apply refused")),
                        "apply refused; see the proposal's audit trail")
            self._agent("fix", "error", f"{pid}: refused")
            raise RuntimeError(note)
        v = p.verification
        level = {"verified": "success", "applied": "success"}.get(p.status, "warn")
        self._agent("fix", "done" if level == "success" else "error", f"{pid}: {p.status}")
        self.log("fix", f"Fix proposal {pid} ended {p.status}"
                        + (f": {v.reason}" if v and v.reason else ""), level)
        return {"proposal_id": pid, "status": p.status,
                "verification": v.model_dump(mode="json") if v else None}

    def _fix_deps(self, allow_protected: bool, use_llm: bool):
        """apply.Deps built from the room's own armed session, mirroring apply.live_deps.

        live_deps would open a second cli.Session (a second docker client and a second pair of probes
        next to the dashboard's); this reuses the session the room already armed, the same way _job_run
        does. `close` stays the Deps no-op: the session belongs to the room, not to this job.
        """
        from ..agents import risk, scorecard
        from ..agents.injector import Injector
        from ..cli import llm_client
        from ..remediation import apply

        session = self._require_session()
        # live_deps warms the probes up before preflight reads the window; only wait when the room's
        # probes have not been running long enough, and let cancel/STOP cut the wait short
        need = max(float(getattr(session.policy, "pre_window_s", 20.0)), 15.0)
        try:
            have = session.window.stats_since(time.time() - need).sample_count
        except Exception:
            have = 0
        if have < 5:
            self.log("fix", f"Warming up the probes for {need:.0f}s so the stack's steady state is measured "
                            f"before anything changes ...")
            self._cancel.wait(need)
        planner = self._planner(False, allow_protected)   # operator planner: same clamping and policy
        injector = Injector(self.s, self.store, session.topology, session.docker, session.window, session.api,
                            session.health, session.thresholds, session.policy, log=self._logger("injector"))

        def analyze() -> None:
            topo = session.topology
            self._agent("risk", "working", "scoring the verification re-run")
            risk.run(self.store, llm_client(self.s, use_llm), topo, log=self._logger("risk"))
            self._agent("risk", "done", "verification re-run scored")
            self._agent("scorecard", "working", "ranking services")
            sc = scorecard.run(self.store, topo, self.s.compose_project, log=self._logger("scorecard"))
            self._agent("scorecard", "done", f"overall {sc.overall_score}"
                        if sc and sc.overall_score is not None else "no data")

        return apply.Deps(docker=session.docker, window=session.window, planner=planner, inject=injector.run,
                          analyze=analyze, topology=session.topology, policy=session.policy)

    def _proposals_state(self) -> dict:
        """Read-only for state(): no sweep and no write on the poll path. `seconds_left` <= 0 on a still
        pending record means it is overdue and will be expired by the next list call or decision."""
        from ..remediation.proposals import OPEN

        everything = self.store.proposals()
        in_flight = OPEN | {"applying", "applied"}
        live = [p for p in everything if p.status in in_flight]
        done = [p for p in everything if p.status not in in_flight][:PROPOSALS_DECIDED_IN_STATE]
        now = datetime.now(timezone.utc)
        pending = [p for p in everything if p.status == "pending" and p.expires_at > now]
        return {"open": len([p for p in live if p.status in OPEN and p.expires_at > now]), "pending": len(pending),
                "total": len(everything),
                "latest": [self._proposal_summary(p) for p in sorted(live + done, key=lambda p: p.created_at,
                                                                     reverse=True)]}

    # ------------------------------------------------------------------ narrator
    def _narration(self, report, use_llm: bool = False) -> tuple:
        """Inputs for the narrator, with the CLI's fallbacks: a missing risk or fix is computed in memory
        without an LLM, never written."""
        from ..agents import fix as fix_agent
        from ..agents import risk as risk_agent
        from ..cli import llm_client

        eid = report.experiment_id
        topo = self.session.topology if self.session else self.store.topology()
        risk = self.store.risk(eid) or risk_agent.assess(report, topo, None)
        fix = self.store.fix(eid) or fix_agent.recommend(report, risk, None)
        return report, risk, fix, (llm_client(self.s, True) if use_llm else None), use_llm

    def _narrate(self, report) -> None:
        from ..agents import narrator

        for line in narrator.narrate(*self._narration(report)):
            self.log("narrator", line.strip())

    def narrative(self, eid: str, use_llm: bool = False) -> Optional[dict]:
        from ..agents import narrator

        report = self.store.experiment(eid)
        if report is None:
            return None
        return narrator.narrate_sections(*self._narration(report, use_llm))

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
        out["blast"] = {"expected": expected, "observed": observed,
                        **{k: _records(live.get(k)) for k in ("cascade_edges", "cascade_hops")}}
        out["timeline"] = list(live.get("timeline") or [])
        detail = live.get("safety_detail") or {k: live[k] for k in SAFETY_DETAIL_KEYS if k in live}
        out["safety"] = {"abort_reason": live.get("abort_reason"), "detail": dict(detail)}
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
                "cascade_hops_beyond_hypothesis": r.cascade.hops_beyond_hypothesis if r.cascade else None,
                "cascade_beyond": list(r.cascade.beyond_hypothesis) if r.cascade else [],
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

    def _pending_approval_state(self) -> Optional[dict]:
        """The decision a human is being waited on, or None when nothing is open.

        `seconds_left` goes negative once the deadline has passed: that is the interesting case, since
        it means the safe default should already have fired and the UI should say so rather than hide it.
        """
        req = self.store.pending_approval()
        if req is None:
            return None
        out = req.model_dump(mode="json")
        out["seconds_left"] = round((req.deadline - datetime.now(timezone.utc)).total_seconds(), 1)
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

    def _reconciliation_state(self) -> Optional[dict]:
        """Predicted vs confirmed, straight off reports/weaknesses_reconcile.json. Severities inside are
        still the Scout's predictions; only `state` per outcome is measured."""
        rec = self.store.reconciliation()
        return rec.model_dump(mode="json") if rec else None

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
            "pending_approval": safe(self._pending_approval_state),
            "guardian": safe(lambda: self.store.heartbeat("guardian")),
            "agents": safe(self._agents_state, {}),
            "llm": safe(self._llm_state, {}),
            "topology": safe(self._topology_state),
            "baseline": safe(self._baseline_state),
            "experiments": safe(self._experiments_state, []),
            "scorecard": safe(self._scorecard_state),
            "weaknesses": safe(self._weaknesses_state),
            "reconciliation": safe(self._reconciliation_state),
            "proposals": safe(self._proposals_state, {"open": 0, "pending": 0, "total": 0, "latest": []}),
            "onboarding": self._onboarding,
            "target": self._target_state(),
        }

    def approvals(self, limit: int = 20) -> dict:
        """What is waiting on a human, and how the last decisions went. Read-only: see server.do_GET.

        A request that is still open lives in PENDING_APPROVAL.json and is reported as `pending`; it is
        only copied into approvals/ once it ends, so a resent request never shows up in both lists.
        """
        decided = [a.model_dump(mode="json") for a in (_safe(self.store.approvals, []) or [])
                   if a.status != "pending"]
        return {"pending": _safe(self._pending_approval_state), "decided": list(reversed(decided))[:limit]}

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


def _records(value) -> list:
    """A list of dicts from live.json (cascade_edges / cascade_hops), [] when absent or malformed:
    older injectors never write them, and the map then falls back to its health-only blast radius."""
    return [dict(x) for x in value if isinstance(x, dict)] if isinstance(value, list) else []
