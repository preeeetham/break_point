"""Failure Injection agent: runs one experiment end to end.

preflight -> lock -> arm watchdog -> inject -> observe (safety every second) -> rollback -> wait for
recovery (with escalation) -> integrity check -> findings -> report on the blackboard.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..chaos.actions import ACTIONS, Context, Handle
from ..chaos.catalog import CATALOG
from ..chaos.docker_ctl import DockerController
from ..observe.probes import HealthProbe, ThingsApiProbe, summarize_health
from ..observe.window import SampleWindow
from ..reporting.store import ReportStore
from ..safety.monitor import SafetyMonitor, Watchdog
from ..schemas import (ExperimentReport, ExperimentSpec, Finding, HealthSummary, HypothesisResult, RecoveryOutcome,
                       SafetyOutcome, Thresholds, TimelineEvent, Topology, WindowStats)

Log = Callable[[str], None]


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class Injector:
    def __init__(self, settings, store: ReportStore, topology: Topology, docker: DockerController, window: SampleWindow,
                 api: ThingsApiProbe, health: HealthProbe, thresholds: Thresholds, policy, log: Log = print):
        self.s = settings
        self.store = store
        self.topo = topology
        self.docker = docker
        self.window = window
        self.api = api
        self.health = health
        self.t = thresholds
        self.policy = policy
        self.log = log
        self.soak_s = float(getattr(policy, "recovery_soak_s", 20.0))
        self._relapses: list[float] = []

    # ------------------------------------------------------------------ preflight
    def preflight(self, spec: ExperimentSpec) -> list[str]:
        issues = []
        if self.store.stop_requested():
            issues.append("STOP file present (run `faultline resume` to clear)")
        if self.store.active():
            issues.append("another experiment is active (reports/ACTIVE_EXPERIMENT.json)")
        st = self.docker.state(spec.target_service)
        if st.get("status") != "running":
            issues.append(f"target {spec.target_service} is {st.get('status')}, refusing to inject")
        for svc in self.topo.relevant_services():
            if not self.docker.is_running(svc):
                issues.append(f"{svc} is not running before the experiment")
        pre = self.window.stats_since(time.time() - self.policy.pre_window_s)
        if pre.sample_count < 5:
            issues.append(f"only {pre.sample_count} probe samples in the last {self.policy.pre_window_s:.0f}s; let the probe warm up")
        elif (pre.success_rate or 0) < self.policy.min_pre_success:
            issues.append(f"system is not steady: success rate {pre.success_rate:.0%} before injection")
        return issues

    # ------------------------------------------------------------------ main
    def run(self, spec: ExperimentSpec) -> ExperimentReport:
        t_start = time.time()
        timeline: list[TimelineEvent] = []

        def ev(event: str, detail: str = "") -> None:
            timeline.append(TimelineEvent(at=_utc(time.time()), t_plus_s=round(time.time() - t_start, 1), event=event, detail=detail))
            self.log(f"[{spec.experiment_id[:8]} +{time.time() - t_start:5.1f}s] {event}{': ' + detail if detail else ''}")

        ev("planned", f"{spec.failure_type} on {spec.target_service} ({spec.planned_by}) — {spec.rationale}")
        issues = self.preflight(spec)
        pre_stats = self.window.stats_since(t_start - self.policy.pre_window_s, t_start)
        if issues:
            ev("skipped", "; ".join(issues))
            return self._report(spec, t_start, pre_stats, WindowStats(), WindowStats(), [], timeline,
                                RecoveryOutcome(recovered=True), SafetyOutcome(thresholds=self.t), [], "skipped", "; ".join(issues))

        ft = CATALOG[spec.failure_type]
        duration = float(spec.parameters.get("duration_s", 30))
        max_s = min(duration, self.t.max_experiment_seconds)
        expected = set(spec.hypothesis.expected_affected) | {spec.target_service}
        protected = [s for s in self.topo.relevant_services() if s not in expected]
        remediation: list[str] = []
        safety = SafetyOutcome(thresholds=self.t)
        handle: Handle | None = None
        status = "completed"
        error = None

        self.store.set_active({"experiment_id": spec.experiment_id, "target_service": spec.target_service,
                               "container": spec.container, "failure_type": spec.failure_type, "started_at": t_start,
                               "deadline": t_start + max_s + self.t.watchdog_grace_seconds, "parameters": spec.parameters})

        def emergency() -> None:
            safety.watchdog_fired = True
            ev("watchdog_fired", "independent timer hit the hard deadline; forcing rollback")
            if handle:
                try:
                    handle.revert()
                except Exception as exc:
                    ev("rollback_error", str(exc))

        wd = Watchdog(max_s + self.t.watchdog_grace_seconds, emergency)
        wd.arm()
        ctx = Context(self.docker, spec.target_service, spec.container, spec.experiment_id, spec.parameters, self.s, self.log)
        injected_at = time.time()
        first_error_at: float | None = None
        condition_at: float | None = None
        revert_at = injected_at
        try:
            handle = ACTIONS[spec.failure_type](ctx)
            injected_at = time.time()
            ev("injected", handle.description)
            monitor = SafetyMonitor(self.t, self.window, self.health, protected, self.store.stop_requested, injected_at)
            while time.time() - injected_at < max_s:
                time.sleep(1.0)
                verdict = monitor.evaluate()
                cur = self.window.stats_since(injected_at)
                if first_error_at is None and cur.sample_count and (cur.success_rate or 1) < 1:
                    first_error_at = time.time()
                    ev("first_api_error", f"status counts {cur.status_counts}")
                st = self.docker.state(spec.target_service)
                if condition_at is None and (st.get("status") != "running" or st.get("health") == "unhealthy" or st.get("paused")):
                    condition_at = time.time()
                    ev("target_condition_observed", f"docker reports {st.get('status')}/{st.get('health')}")
                self._live("injecting", spec, injected_at, cur, verdict.detail)
                if verdict.abort:
                    safety.aborted_early, safety.abort_reason, safety.abort_detail = True, verdict.reason, verdict.detail
                    safety.operator_stop = verdict.reason == "operator_stop"
                    status = "aborted"
                    ev("safety_abort", f"{verdict.reason} {json.dumps(verdict.detail)}")
                    break
                if handle.finished():
                    ev("injection_ended", "fault expired on its own")
                    break
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            ev("error", error)
        finally:
            wd.disarm()
            revert_at = time.time()
            ev("rollback_started")
            if handle:
                try:
                    handle.revert()
                    ev("rollback_done", "fault removed" + (" (watchdog had already reverted)" if safety.watchdog_fired else ""))
                except Exception as exc:
                    ev("rollback_error", str(exc))
                    remediation.append(f"rollback raised {type(exc).__name__}; forcing restart of {spec.target_service}")
                    self._safe_restart(spec.target_service)
            if not self.docker.wait_running(spec.target_service, 30):
                remediation.append(f"started {spec.target_service} by hand after rollback")
                self._safe_start(spec.target_service)
            self.store.clear_active()

        during = self.window.stats_since(injected_at, revert_at)
        health_samples = self.health.between(injected_at, revert_at)
        recovery = self._wait_recovery(spec, revert_at, injected_at, first_error_at, remediation, ev)
        if handle is not None and handle.auto_restarted is not None:
            recovery.auto_restarted_by_docker = handle.auto_restarted
        ok, detail = self.api.integrity_check()
        recovery.data_integrity_ok, recovery.data_integrity_detail = ok, detail
        ev("integrity_check", detail)
        after_t0 = time.time()
        time.sleep(8)
        after = self.window.stats_since(after_t0)
        health_summary = summarize_health(health_samples, self.topo.relevant_services())
        findings = self._findings(spec, pre_stats, during, health_summary, recovery, safety, first_error_at, injected_at, revert_at)
        report = self._report(spec, t_start, pre_stats, during, after, health_summary, timeline, recovery, safety, findings, status, error)
        self._save_logs(spec, injected_at, report)
        self.store.write_experiment(report)
        self.store.write_live({"phase": "idle", "experiment_id": spec.experiment_id})
        ev("report_written", f"reports/experiments/{spec.experiment_id}.json")
        return report

    # ------------------------------------------------------------------ recovery
    def _wait_recovery(self, spec, revert_at, injected_at, first_error_at, remediation, ev) -> RecoveryOutcome:
        t = self.t
        deadline = revert_at + t.recovery_timeout_seconds
        watch = sorted(set(spec.hypothesis.expected_affected) | {spec.target_service})
        consecutive = 0
        escalation = 0
        relapses: list[float] = []
        self._relapses = relapses
        esc_target = getattr(self.policy, "escalate_target_at", 0.5)
        esc_deps = getattr(self.policy, "escalate_dependents_at", 0.8)
        while time.time() < deadline:
            time.sleep(2)
            recent = self.window.last(t.trailing_samples, after=revert_at)
            from ..observe.window import stats as _stats
            st = _stats(recent)
            healthy = self.health.all_healthy(watch)
            ok = (st.sample_count >= 5 and (st.success_rate or 0) >= t.recovery_success_rate_at_least
                  and (st.p95_ms is None or st.p95_ms <= t.recovery_p95_ms_below) and healthy)
            consecutive = consecutive + 1 if ok else 0
            self._live("recovering", spec, injected_at, st, {"consecutive_ok": consecutive, "healthy": healthy})
            if consecutive >= t.recovery_consecutive_checks:
                candidate = time.time()
                ev("recovery_candidate", f"success {st.success_rate:.0%}, p95 {st.p95_ms} ms; soaking {self.soak_s:.0f}s to rule out a relapse")
                if self._soak(spec, injected_at, candidate):
                    ev("recovered", f"stable for {self.soak_s:.0f}s after rollback, {len(remediation)} remediation actions")
                    return RecoveryOutcome(recovered=True, recovered_at=_utc(candidate), time_to_recover_s=round(candidate - revert_at, 1),
                                           outage_duration_s=round(candidate - (first_error_at or injected_at), 1),
                                           remediation_actions=remediation)
                relapses.append(round(time.time() - revert_at, 1))
                ev("relapse", f"API degraded again during the soak (relapse #{len(relapses)}); continuing recovery wait")
                consecutive = 0
            frac = (time.time() - revert_at) / t.recovery_timeout_seconds
            if escalation == 0 and frac >= esc_target:
                escalation = 1
                remediation.append(f"restart {spec.target_service} (no recovery after {frac:.0%} of timeout)")
                ev("escalation", remediation[-1])
                self._safe_restart(spec.target_service)
            elif escalation == 1 and frac >= esc_deps:
                escalation = 2
                deps = self.topo.direct_dependents(spec.target_service)
                remediation.append(f"restart dependents {deps} (Ditto #1861 pattern: clients did not reconnect)")
                ev("escalation", remediation[-1])
                for d in deps:
                    self._safe_restart(d)
        ev("recovery_timeout", f"not recovered after {t.recovery_timeout_seconds}s")
        return RecoveryOutcome(recovered=False, remediation_actions=remediation,
                               outage_duration_s=round(time.time() - (first_error_at or injected_at), 1))

    def _soak(self, spec, injected_at: float, since: float) -> bool:
        """Keep watching after a recovery candidate: Pekko clusters can look healed for a few seconds and then stall."""
        from ..observe.window import stats as _stats
        while time.time() - since < self.soak_s:
            time.sleep(2)
            st = _stats(self.window.between(since))
            self._live("soaking", spec, injected_at, st, {"soak_elapsed_s": round(time.time() - since)})
            if st.sample_count >= 5 and (st.success_rate or 0) < self.t.recovery_success_rate_at_least:
                return False
        return True

    def _safe_restart(self, svc: str) -> None:
        try:
            self.docker.restart(svc)
        except Exception as exc:
            self.log(f"restart {svc} failed: {exc}")

    def _safe_start(self, svc: str) -> None:
        try:
            self.docker.start(svc)
        except Exception as exc:
            self.log(f"start {svc} failed: {exc}")

    # ------------------------------------------------------------------ analysis
    def _findings(self, spec, pre: WindowStats, during: WindowStats, health: list[HealthSummary], rec: RecoveryOutcome,
                  safety: SafetyOutcome, first_error_at, injected_at, revert_at) -> list[Finding]:
        out: list[Finding] = []
        sr = during.success_rate
        if sr is not None and during.sample_count:
            sev = "high" if sr < 0.5 else "medium" if sr < 0.95 else "info"
            slower = pre.p95_ms and during.p95_ms and during.p95_ms > 2 * pre.p95_ms
            title = (f"API success dropped to {sr:.0%} during the fault" if sr < 0.99 else
                     f"API stayed available ({sr:.0%}) but p95 latency rose {pre.p95_ms} -> {during.p95_ms} ms" if slower else
                     f"API unaffected ({sr:.0%} success, p95 {during.p95_ms} ms)")
            if sr >= 0.99 and slower:
                sev = "low"
            out.append(Finding(code="api_impact", severity=sev, title=title,
                               evidence=f"{during.sample_count} requests, status counts {during.status_counts}, p95 {during.p95_ms} ms (baseline {pre.p95_ms} ms)"))
        if sr is not None and sr < 0.5:
            target_h = next((h for h in health if h.service == spec.target_service), None)
            target_gone = bool(target_h and target_h.container_down_seen)
            affected = set(spec.hypothesis.expected_affected) | {spec.target_service}
            blind = []
            for h in health:
                if h.service not in affected or h.container_down_seen:
                    continue
                green = h.ready_ok_fraction if h.ready_ok_fraction is not None else h.alive_ok_fraction
                if green is None or green < 0.9:
                    continue
                if h.service == spec.target_service or not target_gone:
                    out.append(Finding(code="health_check_masked_failure", severity="high",
                                       title=f"{h.service} kept reporting /ready=200 while the API was failing",
                                       evidence=f"/ready OK {green:.0%} of {h.ticks} checks while API success was {sr:.0%}; "
                                                f"cf. Ditto issue #1865 (health checks ignore subsystem health)"))
                else:
                    blind.append(f"{h.service} ({green:.0%})")
            if blind:
                out.append(Finding(code="dependency_blind_readiness", severity="low",
                                   title=f"Dependents stayed 'ready' while {spec.target_service} was down: {', '.join(blind)}",
                                   evidence="readiness only reflects the service's own process, not whether it can serve requests; "
                                            "an orchestrator would keep routing traffic to them"))
            if target_h and not target_h.container_down_seen and not target_h.docker_unhealthy_seen:
                out.append(Finding(code="no_automated_remediation", severity="medium",
                                   title="Nothing in the platform reacted to the outage",
                                   evidence="target container stayed 'running' and never became 'unhealthy' during the fault; "
                                            "compose `restart: always` only acts on exit, never on health"))
        if not rec.recovered:
            out.append(Finding(code="recovery_failed", severity="critical", title="System did not recover within the timeout",
                               evidence=f"timeout {self.t.recovery_timeout_seconds}s; actions tried: {rec.remediation_actions}"))
        elif rec.remediation_actions:
            out.append(Finding(code="manual_restart_required", severity="high", title="Recovery needed restarts beyond the rollback",
                               evidence="; ".join(rec.remediation_actions)))
        elif rec.time_to_recover_s is not None:
            out.append(Finding(code="auto_recovered", severity="info", title=f"Recovered on its own in {rec.time_to_recover_s:.0f}s",
                               evidence=f"outage as seen by clients: {rec.outage_duration_s}s" +
                                        ("; Docker auto-restarted the container" if rec.auto_restarted_by_docker else "")))
        if rec.time_to_recover_s is not None and rec.time_to_recover_s > spec.hypothesis.expected_recovery_s * 1.5:
            out.append(Finding(code="slow_recovery", severity="medium", title="Recovery slower than hypothesised",
                               evidence=f"{rec.time_to_recover_s:.0f}s vs expected {spec.hypothesis.expected_recovery_s:.0f}s"))
        if self._relapses:
            out.append(Finding(code="relapse_after_recovery", severity="high",
                               title=f"API recovered and then degraded again ({len(self._relapses)}x)",
                               evidence=f"relapses at +{self._relapses}s after rollback; typical of a Pekko cluster that re-admitted the restarted node "
                                        f"but left the old incarnation unreachable (no downing provider configured in compose)"))
        if rec.data_integrity_ok is False:
            out.append(Finding(code="data_loss_suspected", severity="critical", title="An acknowledged write was lost",
                               evidence=rec.data_integrity_detail))
        unexpected = [h.service for h in health if h.service not in set(spec.hypothesis.expected_affected) | {spec.target_service}
                      and (h.container_down_seen or (h.alive_ok_fraction is not None and h.alive_ok_fraction < 0.5))]
        if unexpected:
            out.append(Finding(code="cascade_beyond_hypothesis", severity="medium", title=f"Blast radius exceeded the hypothesis: {unexpected}",
                               evidence="these services lost health although the hypothesis did not expect them to"))
        if safety.aborted_early:
            out.append(Finding(code="safety_abort", severity="info" if safety.operator_stop else "medium",
                               title=f"Kill switch fired ({safety.abort_reason})", evidence=json.dumps(safety.abort_detail)))
        return out

    def _hypothesis_result(self, spec, during: WindowStats, health: list[HealthSummary], rec: RecoveryOutcome) -> HypothesisResult:
        h = spec.hypothesis
        err = 1 - during.success_rate if during.success_rate is not None else None
        expected = set(h.expected_affected) | {spec.target_service}
        unexpected = [x.service for x in health if x.service not in expected and (x.container_down_seen or (x.alive_ok_fraction or 1) < 0.5)]
        checks = []
        if err is not None:
            checks.append(err <= h.expected_max_error_rate + 0.05)
        checks.append(not unexpected)
        checks.append(rec.recovered and not rec.remediation_actions and (rec.time_to_recover_s or 0) <= h.expected_recovery_s * 1.5)
        held = all(checks) if checks else None
        notes = (f"error rate {err:.0%} vs ≤{h.expected_max_error_rate:.0%}; " if err is not None else "") + \
                f"unexpected services {unexpected or 'none'}; recovery {rec.time_to_recover_s}s vs ≤{h.expected_recovery_s * 1.5:.0f}s" + \
                ("; needed remediation" if rec.remediation_actions else "")
        return HypothesisResult(held=held, observed_max_error_rate=round(err, 3) if err is not None else None,
                                observed_recovery_s=rec.time_to_recover_s, unexpected_affected=unexpected, notes=notes)

    # ------------------------------------------------------------------ output
    def _report(self, spec, t_start, pre, during, after, health, timeline, recovery, safety, findings, status, error) -> ExperimentReport:
        affected = sorted({spec.target_service} | {h.service for h in health if h.container_down_seen or (h.alive_ok_fraction or 1) < 0.9})
        return ExperimentReport(
            experiment_id=spec.experiment_id, started_at=_utc(t_start), ended_at=_utc(time.time()),
            target={"service": spec.target_service, "container": spec.container}, failure_type=spec.failure_type,
            parameters=spec.parameters, rationale=spec.rationale, hypothesis=spec.hypothesis,
            hypothesis_result=self._hypothesis_result(spec, during, health, recovery) if status != "skipped" else HypothesisResult(),
            criticality_score=spec.criticality_score, planned_by=spec.planned_by, baseline=pre, during=during, after=after,
            health=health, recovery=recovery, safety=safety, affected_services_observed=affected, findings=findings,
            timeline=timeline, reproduce=spec.reproduce, raw_logs_ref=f"reports/experiments/{spec.experiment_id}/",
            status=status, error=error,
        )

    def _save_logs(self, spec, since: float, report: ExperimentReport) -> None:
        d = self.store.experiment_dir(spec.experiment_id)
        for svc in sorted(set(spec.hypothesis.expected_affected) | {spec.target_service}):
            (d / f"{svc}.log").write_text(self.docker.logs(svc, since - 5, tail=120))
        from ..reporting.markdown import experiment_markdown
        (d / "report.md").write_text(experiment_markdown(report))

    def _live(self, phase: str, spec, injected_at: float, st: WindowStats, extra: dict) -> None:
        latest = self.health.latest()
        self.store.write_live({
            "phase": phase, "experiment_id": spec.experiment_id, "target_service": spec.target_service,
            "failure_type": spec.failure_type, "elapsed_s": round(time.time() - injected_at, 1),
            "success_rate": st.success_rate, "p95_ms": st.p95_ms, "sample_count": st.sample_count,
            "hypothesis": spec.hypothesis.statement, "rationale": spec.rationale,
            "health": {svc: {"alive": h.alive, "ready": h.ready, "status": h.status} for svc, h in latest.items()},
            **extra,
        })
