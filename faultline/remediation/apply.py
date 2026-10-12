"""Apply one human-approved FixProposal, re-run the experiment that found the problem, keep it or roll it back.

    preflight (refuse, never transition) -> FIX_IN_FLIGHT + ACTIVE_EXPERIMENT locks -> approved -> applying
    -> backup the override -> write the override + recreate the services (or run the runtime action)
    -> wait for health (bounded) -> applying -> applied | rolled_back
    -> release the experiment lock -> re-run the SAME experiment through the real Injector -> risk + scorecard
    -> compare before/after -> applied -> verified | verification_failed -> rolled_back

Everything that touches Docker or time is injected through `Deps`, so the whole sequence is testable without
Docker. The default (`live_deps`) builds the real pieces the CLI uses: a `cli.Session` (docker controller and
the two probes), the operator planner (`plan_operator`, so clamping and the protected-service policy apply to
the re-run exactly as to any operator experiment) and the `Injector` with all its existing safety limits.

Guarantees:
- a refusal only adds an audit note; it never changes status and never writes the override;
- every status change goes through proposals.transition();
- FIX_IN_FLIGHT.json and our ACTIVE_EXPERIMENT.json are released in a `finally`, whatever happens;
- once the override has been written, any failure (unhealthy, worse re-run, exception) restores the
  override byte-for-byte from the backup and recreates the same services.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from faultline.remediation import proposals as P
from faultline.reporting.store import ReportStore
from faultline.schemas import ExperimentReport, FixProposal, FixVerification, MetricComparison

Log = Callable[[str], None]
ACTOR = "faultline"

# A FIX_IN_FLIGHT.json older than this, or whose owning process is gone, no longer blocks a new apply.
FIX_LOCK_STALE_S = 3 * 3600.0
# Experiment-lock deadline slack on top of the health timeout, for `docker compose up` itself.
RECREATE_BUDGET_S = 300.0

SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# metric -> (better_is, absolute tolerance, relative tolerance): inside the tolerance it is "same", so
# probe noise (one slow request, one second of polling granularity) never decides a verdict.
METRICS: dict[str, tuple[str, float, float]] = {
    "time_to_recover_s": ("lower", 2.0, 0.10),
    "error_rate": ("lower", 0.02, 0.0),
    "p95_ms": ("lower", 50.0, 0.10),
    "service_score": ("higher", 1.0, 0.0),
}
METRIC_WORDS = {
    "time_to_recover_s": ("time to recover", " s"),
    "error_rate": ("error rate during the fault", ""),
    "p95_ms": ("p95 latency during the fault", " ms"),
    "service_score": ("service score", ""),
}


class Refused(Exception):
    """A preflight reason found after the locks were taken (e.g. the change no longer matches)."""


# ---------------------------------------------------------------------- injectables
@dataclass
class Deps:
    """Everything apply_and_verify needs from the outside world.

    docker   .state(svc) -> {"status", "health", "paused", ...}; .restart(svc, timeout=)
    window   .stats_since(t0, t1=None) -> WindowStats (the live API probe), or None
    planner  .plan_operator(service, failure_type, params) -> ExperimentSpec
    inject   ExperimentSpec -> ExperimentReport (the real Injector.run)
    analyze  () -> None: risk + scorecard over the store, so the re-run gets a comparable score
    runner   subprocess.run-compatible, handed to override.recreate
    """
    docker: Any = None
    window: Any = None
    planner: Any = None
    inject: Optional[Callable[[Any], ExperimentReport]] = None
    analyze: Optional[Callable[[], None]] = None
    runner: Callable[..., Any] = subprocess.run
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    topology: Any = None
    policy: Any = None
    health_interval_s: float = 3.0
    healthy_checks: int = 2              # consecutive good checks before "healthy"
    close: Callable[[], None] = field(default=lambda: None)


def live_deps(settings, store: ReportStore, allow_protected: bool, log: Log = print) -> Deps:
    """The real thing: one Session (docker + probes) shared by the health wait and the re-run."""
    from faultline.agents import risk, scorecard
    from faultline.agents.injector import Injector
    from faultline.cli import Session, _planner, llm_client

    sess = Session(settings, store)
    try:
        sess.warmup(max(sess.policy.pre_window_s, 15))
        planner = _planner(sess, False, allow_protected)
        inj = Injector(settings, store, sess.topology, sess.docker, sess.window, sess.api, sess.health,
                       sess.thresholds, sess.policy, log=log)
    except Exception:
        sess.close()
        raise

    def analyze() -> None:
        # Deterministic numbers either way; the LLM would only reword prose, so it stays off here.
        risk.run(store, llm_client(settings, False), sess.topology, log=log)
        scorecard.run(store, sess.topology, settings.compose_project, log=log)

    return Deps(docker=sess.docker, window=sess.window, planner=planner, inject=inj.run, analyze=analyze,
                topology=sess.topology, policy=sess.policy, close=sess.close)


def _policy(settings, policy=None):
    if policy is not None:
        return policy
    from faultline.planning.planner import Policy
    from faultline.safety.thresholds import load_policy
    try:
        return Policy(load_policy(settings.thresholds_file))
    except (OSError, ValueError):
        return Policy({})


# ---------------------------------------------------------------------- logging
def _logger(store: ReportStore, log: Log, proposal_id: str) -> Log:
    def _log(msg: str) -> None:
        line = f"[fix {proposal_id}] {msg}"
        try:
            store.append_log("remediation", f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}")
        except OSError:
            pass
        log(line)
    return _log


# ---------------------------------------------------------------------- the change, rebuilt from code
@dataclass
class Rebuilt:
    fix: Any
    change: Any
    before_text: str
    preview: str


def _base_config(settings, docker=None, runner=subprocess.run) -> dict:
    """Same rule as propose.base_compose: the merged live config (read-only `docker compose config`) when it can
    be had, else the yaml of settings.compose_file. Docker client and runner are injectable here."""
    from faultline.remediation import override
    try:
        fn = getattr(override, "base_config", None)
        if fn is not None:
            cfg = fn(settings, docker=docker, runner=runner)
            if isinstance(cfg, dict) and cfg:
                return cfg
    except Exception:
        pass
    return override.load(settings.compose_file) or {}


def _inputs(settings, store: ReportStore, topology=None, docker=None, runner=subprocess.run
            ) -> tuple[dict, dict, str, str, Any]:
    """(base config, current override dict, current override text, preview label, topology), computed the way
    propose.py computed them when the proposal was made, so the preview can be reproduced byte-for-byte."""
    from faultline.remediation import override, propose
    base = _base_config(settings, docker, runner)
    text = override.read_text(settings.remediation_override_file) or ""
    current = override.load(settings.remediation_override_file) or {}
    label = propose._path_label(settings)
    topo = topology if topology is not None else propose._topology(settings, store)
    return base, current, text, label, topo


def _test_catalog_on(catalog) -> bool:
    fn = getattr(catalog, "_test_enabled", None)
    return bool(fn(False)) if fn else False


def rebuild(settings, store: ReportStore, p: FixProposal, include_test: bool = False, topology=None, docker=None,
            runner=subprocess.run) -> Rebuilt:
    """Build the change again from the catalog against the override as it is NOW. Raises Refused."""
    from faultline.remediation import catalog
    fix = catalog.get(p.catalog_fix_id, include_test=True)
    if fix is None:
        raise Refused(f"catalog fix '{p.catalog_fix_id}' is unknown; only code-defined catalog fixes can be applied")
    if getattr(fix, "test_only", False) and not (include_test or _test_catalog_on(catalog)):
        raise Refused(f"catalog fix '{p.catalog_fix_id}' is test-only and cannot be applied to a real stack")
    base, current, before_text, label, topo = _inputs(settings, store, topology, docker, runner)
    change = fix.build(copy.deepcopy(base), copy.deepcopy(current), list(p.target_services), topo)
    if change is None:
        raise Refused(f"catalog fix '{p.catalog_fix_id}' no longer produces a change for "
                      f"{', '.join(p.target_services)} (already applied, or the services changed)")
    preview = catalog.preview(change, before_text, label)
    if P.change_hash(preview) != p.change_hash:
        raise Refused("the change differs from the one approved (the override or the compose files changed since); "
                      "nothing applied; ask for a fresh proposal")
    return Rebuilt(fix, change, before_text, preview)


# ---------------------------------------------------------------------- preflight
def _fix_lock_holder(store: ReportStore, now: Optional[float] = None) -> Optional[dict]:
    """The live FIX_IN_FLIGHT record, or None when there is none or it is stale."""
    f = store.fix_lock_file
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        data = {}
    started = float(data.get("started_at") or 0)
    if started and (now or time.time()) - started > FIX_LOCK_STALE_S:
        return None
    pid = data.get("pid")
    if isinstance(pid, int) and pid > 0 and pid != os.getpid():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except (PermissionError, OSError):
            pass
    return data or {"proposal_id": "?"}


def preflight(settings, store: ReportStore, proposal: FixProposal, allow_protected: bool, docker=None, window=None,
              include_test: bool = False, policy=None, topology=None, runner=subprocess.run,
              client=None) -> list[str]:
    """Every reason not to apply this proposal now, in plain words. Empty = go.

    `docker` / `window` switch on the stack gate (services running, API success over the 95% gate);
    without them only the file-level checks run. `client` is the Docker handle used read-only to rebuild
    the change from the live config (defaults to `docker`)."""
    reasons: list[str] = []
    p = proposal
    if store.stop_requested():
        reasons.append("STOP file present (the kill switch is on); run `faultline resume` first")
    active = store.active()
    if active:
        reasons.append(f"an experiment or fix is active ({store.lock_file.name}: {active.get('experiment_id', '?')}); "
                       "one change to the stack at a time")
    holder = _fix_lock_holder(store)
    if holder is not None:
        reasons.append(f"another fix is in flight ({store.fix_lock_file.name}: {holder.get('proposal_id', '?')})")
    if p.status != "approved":
        reasons.append(f"proposal is {p.status}, not approved; only an approved proposal can be applied")
    try:
        P.check_hash(p, p.change_hash)
    except P.HashMismatch as exc:
        reasons.append(f"{exc}; nothing applied")
    if P.is_expired(p):
        reasons.append(f"proposal expired at {p.expires_at.isoformat(timespec='seconds')}; approve a fresh one")
    pol = _policy(settings, policy)
    # Not only the stored flag: the catalog decides independently whether these services are protected.
    prot = [s for s in p.target_services if s in pol.protected]
    if (p.requires_allow_protected or prot) and not allow_protected:
        prot = prot or p.target_services
        reasons.append(f"this fix touches protected service(s) {', '.join(prot)}; re-run with --allow-protected "
                       "to apply it on purpose")
    original = store.experiment(p.experiment_id)
    if original is None:
        reasons.append(f"original experiment {p.experiment_id} not found; there is nothing to re-run to verify the fix")
    else:
        ok, why = pol.allows(original.target.get("service", ""), original.failure_type, allow_protected)
        if not ok:
            reasons.append(f"re-running the original experiment is not allowed: {why}; pass --allow-protected")
    try:
        rebuild(settings, store, p, include_test, topology, client if client is not None else docker, runner)
    except Refused as exc:
        reasons.append(str(exc))
    except Exception as exc:  # a broken compose file is a reason, not a crash
        reasons.append(f"could not rebuild the change: {type(exc).__name__}: {exc}")
    if docker is not None:
        topo = topology if topology is not None else store.topology()
        services = list(topo.relevant_services()) if topo else []
        for svc in dict.fromkeys(services + list(p.target_services)):
            st = docker.state(svc) or {}
            if st.get("status") != "running" or st.get("paused"):
                reasons.append(f"{svc} is {st.get('status')}{' (paused)' if st.get('paused') else ''}; "
                               "refusing to change a stack that is already unhealthy")
    if window is not None:
        pre = window.stats_since(time.time() - pol.pre_window_s)
        if pre.sample_count < 5:
            reasons.append(f"only {pre.sample_count} probe samples in the last {pol.pre_window_s:.0f}s; "
                           "let the probe warm up")
        elif (pre.success_rate or 0) < pol.min_pre_success:
            reasons.append(f"the stack is not steady: API success {pre.success_rate:.0%} over the last "
                           f"{pol.pre_window_s:.0f}s, below the {pol.min_pre_success:.0%} gate")
    return reasons


# ---------------------------------------------------------------------- locks
def _take_fix_lock(store: ReportStore, proposal_id: str, clock: Callable[[], float]) -> bool:
    f = store.fix_lock_file
    if f.exists() and _fix_lock_holder(store) is None:
        f.unlink(missing_ok=True)       # stale: its owner is gone
    try:
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps({"proposal_id": proposal_id, "started_at": clock(), "pid": os.getpid()}) + "\n")
    return True


def _release_fix_lock(store: ReportStore, proposal_id: str) -> None:
    f = store.fix_lock_file
    try:
        data = json.loads(f.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        data = {}
    if data.get("proposal_id") in (proposal_id, None):
        f.unlink(missing_ok=True)


def _lock_id(proposal_id: str) -> str:
    return f"fix:{proposal_id}"


def _set_experiment_lock(settings, store: ReportStore, p: FixProposal, services: list[str], clock) -> None:
    """ACTIVE_EXPERIMENT.json for the apply/rollback phase: the Guardian expects these services to restart."""
    now = clock()
    store.set_active({"experiment_id": _lock_id(p.proposal_id), "kind": "fix_apply", "proposal_id": p.proposal_id,
                      "target_service": services[0] if services else "", "services": list(services),
                      "failure_type": "fix_apply", "container": "", "started_at": now, "parameters": {},
                      "deadline": now + float(settings.fix_health_timeout_s) + RECREATE_BUDGET_S})


def _clear_experiment_lock(store: ReportStore, p: FixProposal) -> None:
    if (store.active() or {}).get("experiment_id") == _lock_id(p.proposal_id):
        store.clear_active()


# ---------------------------------------------------------------------- health
def _service_ok(st: dict) -> tuple[bool, str]:
    status, health = st.get("status"), st.get("health")
    if status != "running" or st.get("paused"):
        return False, f"{status}{' (paused)' if st.get('paused') else ''}"
    # No healthcheck declared (health None, e.g. the live MongoDB): running is all Docker can say; the
    # API probe below covers whether it actually serves.
    if health in (None, "", "none", "healthy"):
        return True, "running" + (f"/{health}" if health else "")
    return False, f"running/{health}"


def _wait_healthy(settings, store: ReportStore, deps: Deps, services: list[str], log: Log) -> tuple[bool, str]:
    """Bounded by settings.fix_health_timeout_s. Healthy = every service running and (if it has one) its
    Docker healthcheck healthy, AND, when a probe window exists, API success back over the 95% gate,
    for `deps.healthy_checks` checks in a row. STOP ends the wait as unhealthy."""
    pol = _policy(settings, deps.policy)
    deadline = deps.clock() + float(settings.fix_health_timeout_s)
    good, last = 0, "not checked"
    while True:
        if store.stop_requested():
            return False, "STOP requested during the health wait"
        problems = []
        for svc in services:
            ok, what = _service_ok((deps.docker.state(svc) or {}) if deps.docker else {"status": "running"})
            if not ok:
                problems.append(f"{svc} {what}")
        if deps.window is not None:
            st = deps.window.stats_since(deps.clock() - min(pol.pre_window_s, 10.0))
            if st.sample_count < 3:
                problems.append(f"only {st.sample_count} probe samples")
            elif (st.success_rate or 0) < pol.min_pre_success:
                problems.append(f"API success {st.success_rate:.0%} < {pol.min_pre_success:.0%}")
        last = "; ".join(problems) or "all healthy"
        good = 0 if problems else good + 1
        if good >= max(1, deps.healthy_checks):
            return True, last
        if deps.clock() >= deadline:
            return False, f"not healthy after {float(settings.fix_health_timeout_s):.0f}s: {last}"
        deps.sleep(deps.health_interval_s)


# ---------------------------------------------------------------------- apply / rollback
def _apply_change(settings, deps: Deps, rb: Rebuilt, log: Log) -> tuple[bool, str]:
    from faultline.remediation import override
    services = list(rb.change.services)
    if getattr(rb.fix, "runtime_only", False):
        for svc in services:
            log(f"runtime action: restarting {svc} ({rb.change.action})")
            deps.docker.restart(svc, timeout=20)
        return True, f"restarted {', '.join(services)}"
    override.write_atomic(settings.remediation_override_file, override.render(rb.change.override_after))
    log(f"wrote {settings.remediation_override_file}; recreating {', '.join(services)}")
    return override.recreate(settings, services, runner=deps.runner, docker=deps.docker)


def _rollback(settings, store: ReportStore, p: FixProposal, deps: Deps, services: list[str], log: Log) -> str:
    """Restore the override byte-for-byte, recreate the same services, wait for health. Returns what happened."""
    from faultline.remediation import override
    if not p.override_backup:
        return "no override backup was taken; nothing to restore"
    _set_experiment_lock(settings, store, p, services, deps.clock)
    try:
        override.restore(settings.remediation_override_file, p.override_backup)
        log(f"restored {settings.remediation_override_file} from {p.override_backup}")
        ok, out = override.recreate(settings, services, runner=deps.runner, docker=deps.docker)
        if not ok:
            return f"override restored, but recreating {', '.join(services)} failed: {_tail(out)}"
        healthy, detail = _wait_healthy(settings, store, deps, services, log)
        return "override restored and services recreated" + ("; healthy again" if healthy else f"; still {detail}")
    finally:
        _clear_experiment_lock(store, p)


def _tail(text: Any, n: int = 300) -> str:
    s = str(text or "").strip()
    return s[-n:] if len(s) > n else s


# ---------------------------------------------------------------------- re-run and compare
def _rerun(original: ExperimentReport, deps: Deps, log: Log) -> tuple[Optional[ExperimentReport], str]:
    """Same target, failure type and parameters, through plan_operator (clamping, policy) and the Injector."""
    target = original.target.get("service", "")
    try:
        spec = deps.planner.plan_operator(target, original.failure_type, dict(original.parameters))
    except ValueError as exc:
        return None, f"the re-run was refused by policy: {exc}"
    if getattr(spec, "requires_approval", False):
        return None, f"the re-run needs an admin decision ({spec.approval_reason}); not injected"
    log(f"re-running {original.failure_type} on {target} {spec.parameters} as {spec.experiment_id}")
    report = deps.inject(spec)
    if report.status == "skipped":
        return report, f"the re-run was skipped by the injector's preflight: {report.error or 'no reason given'}"
    if report.status == "failed":
        return report, f"the re-run failed: {report.error or 'no reason given'}"
    if report.status == "aborted" or report.safety.aborted_early:
        same = (original.status == "aborted" or original.safety.aborted_early) and \
            (report.safety.abort_reason or "") == (original.safety.abort_reason or "") and \
            not report.safety.operator_stop
        if same:
            # The original was cut short by the same safety limit, so the two runs are still like for like;
            # compare their numbers instead of failing the fix for the safety system doing its job.
            log(f"the re-run was aborted by safety ({report.safety.abort_reason}), as the original was; comparing")
            return report, ""
        return report, f"the re-run was aborted by safety ({report.safety.abort_reason or 'abort'})"
    return report, ""


def _metric_value(r: ExperimentReport, metric: str) -> Optional[float]:
    if metric == "time_to_recover_s":
        return r.recovery.time_to_recover_s if r.recovery.recovered else None
    if metric == "error_rate":
        return None if r.during.success_rate is None else round(1.0 - r.during.success_rate, 4)
    if metric == "p95_ms":
        return r.during.p95_ms
    return None


def _verdict(metric: str, before: Optional[float], after: Optional[float], none_is_worst: bool = False) -> str:
    better_is, abs_tol, rel_tol = METRICS[metric]
    if before is None or after is None:
        if none_is_worst and (before is not None or after is not None):
            return "better" if after is not None else "worse"
        return "unknown"
    tol = max(abs_tol, rel_tol * abs(before))
    delta = after - before if better_is == "lower" else before - after
    if abs(delta) <= tol:
        return "same"
    return "worse" if delta > 0 else "better"


def _service_score(store: ReportStore, services: list[str]) -> Optional[float]:
    """Same rule propose.py used for the `before` value: the weakest score among the fix's target services."""
    sc = store.scorecard()
    if sc is None:
        return None
    scores = [r.score for r in sc.ranking if r.service in services and r.score is not None]
    return min(scores) if scores else None


# What each catalog fix is meant to move. Healthcheck fixes change visibility, not recovery, so they are judged
# on the error rate (and on their finding disappearing, which every verdict checks); everything else on TTR.
TARGETED_METRIC = {
    "add_or_tighten_healthcheck": "error_rate",
    "add_dependency_healthcheck": "error_rate",
    "break_healthcheck_for_test": "error_rate",
}


def _targeted_metric(fix, finding_code: str) -> str:
    explicit = getattr(fix, "targeted_metric", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return TARGETED_METRIC.get(getattr(fix, "id", ""), "time_to_recover_s")


def _worst_severity(r: ExperimentReport, code: str) -> int:
    return max((SEVERITY_RANK.get(f.severity, 0) + 1 for f in r.findings if f.code == code), default=0)


def _compare(p: FixProposal, fix, original: ExperimentReport, rerun: ExperimentReport,
             score_after: Optional[float]) -> FixVerification:
    comps = []
    for m in ("time_to_recover_s", "error_rate", "p95_ms"):
        b, a = _metric_value(original, m), _metric_value(rerun, m)
        comps.append(MetricComparison(metric=m, before=b, after=a, better_is=METRICS[m][0],
                                      verdict=_verdict(m, b, a, none_is_worst=(m == "time_to_recover_s"))))
    score_before = next((e.before for e in p.expected_metrics if e.metric == "service_score"), None)
    comps.append(MetricComparison(metric="service_score", before=score_before, after=score_after, better_is="higher",
                                  verdict=_verdict("service_score", score_before, score_after)))
    # The finding the fix was proposed for, as a severity rank (0 = gone, 1 = info ... 5 = critical).
    fb, fa = _worst_severity(original, p.finding_code), _worst_severity(rerun, p.finding_code)
    comps.append(MetricComparison(metric=f"finding:{p.finding_code}", before=float(fb), after=float(fa),
                                  better_is="lower", verdict="same" if fa == fb else ("better" if fa < fb else "worse")))
    old_codes = {f.code for f in original.findings}
    new_findings = sorted({f.code for f in rerun.findings if f.severity != "info" and f.code not in old_codes})

    targeted = _targeted_metric(fix, p.finding_code)
    tcomp = next((c for c in comps if c.metric == targeted), comps[0])
    finding_comp = comps[-1]
    reasons = []
    if tcomp.verdict not in ("better", "same"):
        reasons.append(_describe(tcomp))
    if new_findings:
        reasons.append(f"the re-run raised new finding(s): {', '.join(new_findings)}")
    if finding_comp.verdict == "worse":
        reasons.append(f"the targeted finding {p.finding_code} got worse")
    outcome = "verification_failed" if reasons else "verified"
    reason = "; ".join(reasons) if reasons else (
        f"{_describe(tcomp)}; no new findings; {p.finding_code} "
        + ("is gone" if fa == 0 else "no worse"))
    return FixVerification(rerun_experiment_id=rerun.experiment_id, comparisons=comps, new_findings=new_findings,
                           targeted_metric=tcomp.metric, outcome=outcome, reason=reason)


def _fmt(metric: str, v: Optional[float]) -> str:
    if v is None:
        return "never" if metric == "time_to_recover_s" else "n/a"
    if metric == "error_rate":
        return f"{v:.0%}"
    unit = METRIC_WORDS.get(metric, (metric, ""))[1]
    return f"{v:g}{unit}"


def _describe(c: MetricComparison) -> str:
    words = METRIC_WORDS.get(c.metric, (c.metric, ""))[0]
    if c.verdict == "unknown":
        return f"{words} could not be compared ({_fmt(c.metric, c.before)} -> {_fmt(c.metric, c.after)})"
    return f"{words} went from {_fmt(c.metric, c.before)} to {_fmt(c.metric, c.after)} ({c.verdict})"


# ---------------------------------------------------------------------- the whole thing
def _refuse(store: ReportStore, p: FixProposal, reasons: list[str], log: Log) -> FixProposal:
    text = "apply refused: " + "; ".join(reasons)
    P.note(p, ACTOR, text)
    store.write_proposal(p)
    log(text)
    return p


def apply_and_verify(settings, store: ReportStore, proposal_id: str, allow_protected: bool = False, log: Log = print,
                     include_test: bool = False, deps: Optional[Deps] = None) -> FixProposal:
    """Apply an approved proposal, verify it by re-running its experiment, keep it or roll it back.

    Refusals are returned (status unchanged, audit note added), never raised. Raises KeyError only for an
    unknown proposal id. Returns the proposal in its final state."""
    p = store.proposal(proposal_id)
    if p is None:
        raise KeyError(f"no proposal {proposal_id}")
    say = _logger(store, log, proposal_id)

    # a. cheap checks first, so a STOP or a stale approval never spins up Docker clients or probes
    reasons = preflight(settings, store, p, allow_protected, include_test=include_test,
                        policy=deps.policy if deps else None, topology=deps.topology if deps else None,
                        runner=deps.runner if deps else subprocess.run, client=deps.docker if deps else None)
    if reasons:
        return _refuse(store, p, reasons, say)
    own_deps = deps is None
    if own_deps:
        try:
            deps = live_deps(settings, store, allow_protected, log=say)
        except Exception as exc:
            return _refuse(store, p, [f"could not reach the stack: {type(exc).__name__}: {exc}"], say)
    assert deps is not None
    try:
        reasons = preflight(settings, store, p, allow_protected, docker=deps.docker, window=deps.window,
                            include_test=include_test, policy=deps.policy, topology=deps.topology, runner=deps.runner)
        if reasons:
            return _refuse(store, p, reasons, say)
        if not _take_fix_lock(store, p.proposal_id, deps.clock):
            return _refuse(store, p, ["another fix is in flight (it took the lock first)"], say)
        try:
            return _locked_apply(settings, store, p, allow_protected, include_test, deps, say)
        finally:
            _clear_experiment_lock(store, p)
            _release_fix_lock(store, p.proposal_id)
    finally:
        if own_deps:
            try:
                deps.close()
            except Exception:
                pass


def _locked_apply(settings, store: ReportStore, p: FixProposal, allow_protected: bool, include_test: bool,
                  deps: Deps, log: Log) -> FixProposal:
    from faultline.remediation import override

    # b/c. rebuild under the lock: the change must still be byte-for-byte what was approved
    try:
        rb = rebuild(settings, store, p, include_test, deps.topology, deps.docker, deps.runner)
    except Refused as exc:
        return _refuse(store, p, [str(exc)], log)
    services = list(rb.change.services) or list(p.target_services)
    runtime_only = bool(getattr(rb.fix, "runtime_only", False))
    _set_experiment_lock(settings, store, p, services, deps.clock)
    P.transition(p, "applying", ACTOR, f"applying {p.catalog_fix_id} to {', '.join(services)}")
    store.write_proposal(p)
    log(f"applying {p.catalog_fix_id} to {', '.join(services)} ({'runtime action' if runtime_only else 'override'})")

    try:
        if not runtime_only:
            backup_dir = store.root / "remediation" / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            p.override_backup = str(override.backup(settings.remediation_override_file, backup_dir, p.proposal_id))
            P.note(p, ACTOR, f"override backed up to {p.override_backup}")
            store.write_proposal(p)

        # d. apply
        ok, out = _apply_change(settings, deps, rb, log)
        if not ok:
            log(f"apply failed: {_tail(out)}")
            what = _rollback(settings, store, p, deps, services, log) if not runtime_only else "nothing to roll back"
            return _finish(store, p, "rolled_back", f"applying failed ({_tail(out, 200)}); {what}", log)

        # e. health
        healthy, detail = _wait_healthy(settings, store, deps, services, log)
        if not healthy:
            log(f"unhealthy after the change: {detail}; rolling back")
            what = _rollback(settings, store, p, deps, services, log) if not runtime_only else \
                "runtime action, nothing to roll back"
            return _finish(store, p, "rolled_back", f"stack unhealthy after applying ({detail}); {what}", log)
        p.applied_at = P.utcnow()
        _finish(store, p, "applied", f"applied; {detail}", log)
        _clear_experiment_lock(store, p)          # the Injector takes its own lock for the re-run

        # f. re-run the same experiment
        original = store.experiment(p.experiment_id)
        rerun, why = _rerun(original, deps, log)
        if rerun is not None and why == "":
            if deps.analyze:
                deps.analyze()
            # g. compare
            p.verification = _compare(p, rb.fix, original, rerun,
                                      _service_score(store, list(p.target_services)))
        else:
            p.verification = FixVerification(rerun_experiment_id=rerun.experiment_id if rerun else None,
                                             targeted_metric=_targeted_metric(rb.fix, p.finding_code),
                                             outcome="verification_failed", reason=why)
        store.write_proposal(p)
        v = p.verification
        for c in v.comparisons:
            log(f"  {c.metric}: {_fmt(c.metric, c.before)} -> {_fmt(c.metric, c.after)} ({c.verdict})")
        if v.outcome == "verified":
            return _finish(store, p, "verified", v.reason, log)
        _finish(store, p, "verification_failed", v.reason, log)
        if runtime_only:
            P.note(p, ACTOR, "runtime action, nothing to roll back")
            store.write_proposal(p)
            return p
        log("verification failed; rolling back")
        what = _rollback(settings, store, p, deps, services, log)
        return _finish(store, p, "rolled_back", f"{v.reason}; {what}", log)
    except Exception as exc:
        # h. anything unexpected after the change started: put the override back and say so
        err = f"{type(exc).__name__}: {exc}"
        log(f"error while applying: {err}\n{traceback.format_exc()}")
        what = "runtime action, nothing to roll back"
        if not runtime_only and p.override_backup:
            try:
                what = _rollback(settings, store, p, deps, services, log)
            except Exception as exc2:
                what = f"rollback ALSO failed ({type(exc2).__name__}: {exc2}); restore {p.override_backup} by hand"
        if p.status in ("applying", "applied", "verification_failed"):
            return _finish(store, p, "rolled_back", f"error: {err}; {what}", log)
        P.note(p, ACTOR, f"error after {p.status}: {err}; {what}")
        store.write_proposal(p)
        return p


def _finish(store: ReportStore, p: FixProposal, status: str, reason: str, log: Log) -> FixProposal:
    P.transition(p, status, ACTOR, reason)
    store.write_proposal(p)
    log(f"{status}: {reason}")
    return p
