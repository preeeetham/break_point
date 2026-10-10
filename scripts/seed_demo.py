"""Seed a fake but schema-valid blackboard so the control room can be shown without Docker.

    .venv/bin/python scripts/seed_demo.py [--out reports-demo]
    .venv/bin/python -m faultline.dashboard.server --reports reports-demo --port 8790

Topology comes from the real static builder (compose + env scan + nginx, no docker), every report is
built through faultline.schemas, and risk, fixes and the scorecard are derived by the real agents with
the LLM disabled. Experiment ids are fixed, so re-running overwrites instead of piling up.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from faultline.agents import fix, risk, scorecard  # noqa: E402
from faultline.chaos.catalog import CATALOG, clamp, reproduce_commands  # noqa: E402
from faultline.config import load_settings  # noqa: E402
from faultline.observe.baseline import to_markdown as baseline_markdown  # noqa: E402
from faultline.reporting.markdown import experiment_markdown  # noqa: E402
from faultline.reporting.store import ReportStore  # noqa: E402
from faultline.safety.thresholds import load_policy, thresholds_from  # noqa: E402
from faultline.schemas import (  # noqa: E402
    Baseline, ExperimentReport, Finding, HealthSummary, Hypothesis, HypothesisResult, RecoveryOutcome,
    SafetyOutcome, Thresholds, TimelineEvent, Topology, WindowStats,
)
from faultline.topology.graph import build as build_topology  # noqa: E402
from faultline.topology.render import markdown as topology_markdown  # noqa: E402

# mirrors the synthetic probe's operation mix (3:2:2:1) and its rough per-operation latency spread
OPS = (("get_thing", 3, 0.85, "200"), ("put_property", 2, 0.9, "204"), ("search", 2, 1.1, "200"), ("get_property", 1, 0.6, "200"))
MASKED = "/ready OK 100% of {n} checks while API success was 0%; cf. Ditto issue #1865 (health checks ignore subsystem health)"


def eid(name: str) -> str:
    return hashlib.sha1(f"faultline-demo:{name}".encode()).hexdigest()[:12]


def window(n: int, failures: int = 0, p50: Optional[float] = None, p95: Optional[float] = None,
           p99: Optional[float] = None, fail_status: str = "timeout") -> WindowStats:
    unit = n / 8
    by_op: dict[str, dict[str, Any]] = {}
    status: dict[str, int] = {}
    left_fail = failures
    for i, (op, w, spread, code) in enumerate(OPS):
        count = round(unit * w) if i < len(OPS) - 1 else n - sum(v["count"] for v in by_op.values())
        bad = min(count, round(failures * w / 8)) if i < len(OPS) - 1 else min(count, left_fail)
        left_fail -= bad
        good = count - bad
        by_op[op] = {"count": count, "success_rate": round(good / count, 4) if count else None,
                     "p95_ms": round(p95 * spread, 2) if good and p95 else None}
        if good:
            status[code] = status.get(code, 0) + good
        if bad:
            status[fail_status] = status.get(fail_status, 0) + bad
    ok = n - failures
    return WindowStats(sample_count=n, success_rate=round(ok / n, 4) if n else None,
                       p50_ms=p50 if ok else None, p95_ms=p95 if ok else None, p99_ms=p99 if ok else None,
                       by_operation=by_op, status_counts=status)


def impact(w: WindowStats, base: WindowStats) -> str:
    return f"{w.sample_count} requests, status counts {w.status_counts}, p95 {w.p95_ms} ms (baseline {base.p95_ms} ms)"


def health(services: list[str], ticks: int, **override: dict[str, Any]) -> list[HealthSummary]:
    out = []
    for svc in services:
        base = {"alive_ok_fraction": 1.0, "ready_ok_fraction": None if svc in ("mongodb", "nginx") else 1.0}
        if svc == "nginx":
            base["alive_ok_fraction"] = None
        base.update(override.get(svc.replace("-", "_"), {}))
        out.append(HealthSummary(service=svc, ticks=ticks, **base))
    return out


def timeline(t0: datetime, steps: list[tuple[float, str, str]]) -> list[TimelineEvent]:
    return [TimelineEvent(at=t0 + timedelta(seconds=t), t_plus_s=t, event=e, detail=d) for t, e, d in steps]


def hyp_notes(err: float, max_err: float, rec: float, expected_rec: float, unexpected: list[str]) -> str:
    return (f"error rate {err:.0%} vs <={max_err:.0%}; unexpected services {unexpected or 'none'}; "
            f"recovery {rec}s vs <={expected_rec * 1.5:.0f}s")


# ---------------------------------------------------------------------- the seeded story
def experiments(s, topo: Topology, thresholds: Thresholds, now: datetime) -> list[ExperimentReport]:
    project = topo.project
    relevant = topo.relevant_services()
    net = s.compose_network
    base = window(80, 0, 18.4, 41.2, 96.5)

    def container(svc: str) -> str:
        return topo.services[svc].container or f"{project}-{svc}-1"

    def params(ft: str, raw: dict[str, Any]) -> dict[str, Any]:
        return clamp(CATALOG[ft], raw)[0]

    def repro(ft: str, svc: str, p: dict[str, Any]) -> list[str]:
        return reproduce_commands(ft, container(svc), p, net, s.pumba_image, s.tc_image)

    def report(name: str, start: datetime, dur_s: float, svc: str, ft: str, p: dict[str, Any], **kw: Any) -> ExperimentReport:
        i = eid(name)
        return ExperimentReport(
            experiment_id=i, started_at=start, ended_at=start + timedelta(seconds=dur_s),
            target={"service": svc, "container": container(svc)}, failure_type=ft, parameters=p,
            criticality_score=topo.criticality_of(svc), baseline=base, reproduce=repro(ft, svc, p),
            raw_logs_ref=f"reports/experiments/{i}/", **kw)

    out: list[ExperimentReport] = []

    # 1. AI-planned: light packet loss on the hidden single point of failure; hypothesis held
    t = now - timedelta(minutes=82)
    p = params("network_loss", {"duration_s": 20, "loss_percent": 15})
    h = Hypothesis(statement="Dropping 15% of packets leaving mongodb for 20 s will slow reads and writes through every "
                             "Ditto service but keep the API error rate under 10%, and the API will be back to baseline "
                             "latency within 30 s of rollback with no intervention.",
                   expected_affected=["policies", "things", "things-search", "connectivity"],
                   expected_max_error_rate=0.10, expected_recovery_s=30.0)
    out.append(report(
        "mongodb-network-loss", t, 71.3, "mongodb", "network_loss", p,
        rationale="mongodb has the highest criticality (9.5) yet no compose depends_on points at it; the env-var scan "
                  "shows four services need it. It is protected, so a short degrade-only packet-loss fault is the "
                  "safest way to test that hidden dependency first.",
        hypothesis=h, planned_by="llm",
        hypothesis_result=HypothesisResult(held=True, observed_max_error_rate=0.014, observed_recovery_s=18.4,
                                           notes=hyp_notes(0.014, 0.10, 18.4, 30.0, [])),
        during=(d := window(72, 1, 190.2, 1238.7, 1611.0)), after=window(32, 0, 20.1, 44.3, 58.0),
        health=health(relevant, 7, mongodb={"alive_ok_fraction": 0.71}),
        recovery=RecoveryOutcome(recovered=True, recovered_at=t + timedelta(seconds=41.2), time_to_recover_s=18.4,
                                 outage_duration_s=2.1, data_integrity_ok=True,
                                 data_integrity_detail="stored seq=212, last acknowledged write seq=212"),
        safety=SafetyOutcome(thresholds=thresholds),
        affected_services_observed=["mongodb"],
        findings=[
            Finding(code="api_impact", severity="info", title="API success dropped to 99% during the fault",
                    evidence=impact(d, base)),
            Finding(code="auto_recovered", severity="info", title="Recovered on its own in 18s",
                    evidence="outage as seen by clients: 2.1s"),
        ],
        timeline=timeline(t, [
            (0.0, "planned", "network_loss on mongodb (llm): protected datastore, degrade-only fault"),
            (1.6, "injected", f"pumba netem loss 15% on {container('mongodb')} for 20s"),
            (6.9, "first_api_error", "status counts {'200': 14, '204': 5, 'timeout': 1}"),
            (21.7, "injection_ended", "fault expired on its own"),
            (21.8, "rollback_started", ""), (22.4, "rollback_done", "fault removed"),
            (41.2, "recovered", "success 100%, p95 44.3 ms, 0 remediation actions"),
            (41.3, "integrity_check", "stored seq=212, last acknowledged write seq=212"),
        ]),
        resources={"mongodb": {"cpu_pct_peak": 31.0, "mem_mb_peak": 412}}, status="completed"))

    # 2. deterministic: gateway latency, API stays up, only slower
    t = now - timedelta(minutes=61)
    p = params("network_delay", {"duration_s": 30, "delay_ms": 400, "jitter_ms": 100})
    h = Hypothesis(statement="Adding 400 +/- 100 ms of latency on gateway for 30 s will raise p95 latency well above "
                             "baseline without failing requests, and recover within 15 s of rollback.",
                   expected_affected=["nginx"], expected_max_error_rate=0.05, expected_recovery_s=15.0)
    out.append(report(
        "gateway-network-delay", t, 62.8, "gateway", "network_delay", p,
        rationale="gateway is the only path from nginx into the cluster and has not been tested with a slow network; "
                  "ranked first among untested degrade-only candidates.",
        hypothesis=h, planned_by="deterministic",
        hypothesis_result=HypothesisResult(held=True, observed_max_error_rate=0.0, observed_recovery_s=6.2,
                                           notes=hyp_notes(0.0, 0.05, 6.2, 15.0, [])),
        during=(d := window(112, 0, 452.0, 788.5, 902.3)), after=window(32, 0, 19.6, 42.8, 51.2),
        health=health(relevant, 10),
        recovery=RecoveryOutcome(recovered=True, recovered_at=t + timedelta(seconds=38.4), time_to_recover_s=6.2,
                                 outage_duration_s=0.0, data_integrity_ok=True,
                                 data_integrity_detail="stored seq=377, last acknowledged write seq=377"),
        safety=SafetyOutcome(thresholds=thresholds),
        affected_services_observed=[],
        findings=[
            Finding(code="api_impact", severity="low", title="API stayed available (100%) but p95 latency rose 41.2 -> 788.5 ms",
                    evidence=impact(d, base)),
            Finding(code="auto_recovered", severity="info", title="Recovered on its own in 6s",
                    evidence="outage as seen by clients: 0.0s"),
        ],
        timeline=timeline(t, [
            (0.0, "planned", "network_delay on gateway (deterministic)"),
            (1.4, "injected", f"pumba netem delay 400ms +/- 100ms on {container('gateway')} for 30s"),
            (31.5, "injection_ended", "fault expired on its own"),
            (31.6, "rollback_started", ""), (32.2, "rollback_done", "fault removed"),
            (38.4, "recovered", "success 100%, p95 42.8 ms, 0 remediation actions"),
            (38.5, "integrity_check", "stored seq=377, last acknowledged write seq=377"),
        ]),
        status="completed"))

    # 3. the money shot: freeze things, API at 0%, every dependent still reports /ready=200
    t = now - timedelta(minutes=40)
    p = params("container_pause", {"duration_s": 20})
    h = Hypothesis(statement="Freezing things for 20 s will fail thing reads and writes behind gateway; Ditto's health "
                             "checks will flag the outage, and the API will recover within 15 s of unpause.",
                   expected_affected=["gateway", "things-search", "connectivity"],
                   expected_max_error_rate=1.0, expected_recovery_s=15.0)
    abort = {"trailing_samples": 6, "error_rate": 1.0, "p95_ms": None, "threshold": thresholds.abort_error_rate_above}
    out.append(report(
        "things-container-pause", t, 58.6, "things", "container_pause", p,
        rationale="things owns every digital twin and has never been tested with a hung process; a SIGSTOP keeps TCP "
                  "connections open, so callers hang instead of failing fast.",
        hypothesis=h, planned_by="deterministic",
        hypothesis_result=HypothesisResult(held=False, observed_max_error_rate=1.0, observed_recovery_s=26.0,
                                           notes=hyp_notes(1.0, 1.0, 26.0, 15.0, [])),
        during=(d := window(24, 24)), after=window(32, 0, 23.0, 47.9, 66.1),
        health=health(relevant, 3, things={"alive_ok_fraction": None, "ready_ok_fraction": None}),
        recovery=RecoveryOutcome(recovered=True, recovered_at=t + timedelta(seconds=34.9), time_to_recover_s=26.0,
                                 outage_duration_s=33.2, auto_restarted_by_docker=False, data_integrity_ok=True,
                                 data_integrity_detail="stored seq=431, last acknowledged write seq=431"),
        safety=SafetyOutcome(aborted_early=True, abort_reason="error_rate", abort_detail=abort, thresholds=thresholds),
        affected_services_observed=["things"],
        findings=[
            Finding(code="api_impact", severity="high", title="API success dropped to 0% during the fault",
                    evidence=impact(d, base)),
            *[Finding(code="health_check_masked_failure", severity="high",
                      title=f"{svc} kept reporting /ready=200 while the API was failing", evidence=MASKED.format(n=3))
              for svc in ("things-search", "connectivity", "gateway")],
            Finding(code="no_automated_remediation", severity="medium", title="Nothing in the platform reacted to the outage",
                    evidence="target container stayed 'running' and never became 'unhealthy' during the fault; "
                             "compose `restart: always` only acts on exit, never on health"),
            Finding(code="auto_recovered", severity="info", title="Recovered on its own in 26s",
                    evidence="outage as seen by clients: 33.2s"),
            Finding(code="slow_recovery", severity="medium", title="Recovery slower than hypothesised",
                    evidence="26s vs expected 15s"),
            Finding(code="safety_abort", severity="medium", title="Kill switch fired (error_rate)",
                    evidence='{"trailing_samples": 6, "error_rate": 1.0, "p95_ms": null, "threshold": 0.3}'),
        ],
        timeline=timeline(t, [
            (0.0, "planned", "container_pause on things (deterministic)"),
            (0.6, "injected", f"SIGSTOP {container('things')} (docker pause)"),
            (1.7, "target_condition_observed", "docker reports paused/healthy"),
            (2.4, "first_api_error", "status counts {'timeout': 2}"),
            (8.3, "safety_abort", "error_rate " + str(abort).replace("'", '"').replace("None", "null")),
            (8.3, "rollback_started", ""), (8.5, "rollback_done", "fault removed"),
            (34.5, "recovered", "success 100%, p95 47.9 ms, 0 remediation actions"),
            (34.6, "integrity_check", "stored seq=431, last acknowledged write seq=431"),
        ]),
        status="aborted"))

    # 4. operator pressed the kill switch a few seconds into a policies stop
    t = now - timedelta(minutes=18)
    p = params("container_stop", {"duration_s": 30})
    deps = [d for d in topo.transitive_dependents("policies") if d in relevant]
    h = Hypothesis(statement=f"taking away policies (graceful stop (sigterm)) for 30s will push the synthetic API error "
                             f"rate to at most 100%, impact at most {deps}, and the API will recover within 90s after "
                             f"rollback without manual intervention",
                   expected_affected=deps, expected_max_error_rate=1.0, expected_recovery_s=90.0)
    out.append(report(
        "policies-container-stop", t, 66.0, "policies", "container_stop", p,
        rationale="operator-selected graceful stop (sigterm) on policies", hypothesis=h, planned_by="operator",
        hypothesis_result=HypothesisResult(held=True, observed_max_error_rate=0.25, observed_recovery_s=41.5,
                                           notes=hyp_notes(0.25, 1.0, 41.5, 90.0, [])),
        during=(d := window(16, 4, 31.0, 214.0, 402.0, fail_status="503")), after=window(32, 0, 21.2, 45.0, 61.7),
        health=health(relevant, 2, policies={"alive_ok_fraction": None, "ready_ok_fraction": None,
                                             "docker_unhealthy_seen": True, "container_down_seen": True}),
        recovery=RecoveryOutcome(recovered=True, recovered_at=t + timedelta(seconds=47.0), time_to_recover_s=41.5,
                                 outage_duration_s=12.3, auto_restarted_by_docker=False, data_integrity_ok=True,
                                 data_integrity_detail="stored seq=502, last acknowledged write seq=502"),
        safety=SafetyOutcome(aborted_early=True, abort_reason="operator_stop", operator_stop=True, thresholds=thresholds),
        affected_services_observed=["policies"],
        findings=[
            Finding(code="api_impact", severity="medium", title="API success dropped to 75% during the fault",
                    evidence=impact(d, base)),
            Finding(code="auto_recovered", severity="info", title="Recovered on its own in 42s",
                    evidence="outage as seen by clients: 12.3s"),
            Finding(code="safety_abort", severity="info", title="Kill switch fired (operator_stop)", evidence="{}"),
        ],
        timeline=timeline(t, [
            (0.0, "planned", "container_stop on policies (operator): operator-selected graceful stop (sigterm) on policies"),
            (1.1, "injected", f"docker stop {container('policies')}"),
            (2.3, "target_condition_observed", "docker reports exited/None"),
            (3.9, "first_api_error", "status counts {'200': 5, '204': 2, '503': 1}"),
            (5.2, "safety_abort", "operator_stop {}"),
            (5.2, "rollback_started", ""), (5.6, "rollback_done", "fault removed"),
            (47.0, "recovered", "success 100%, p95 45.0 ms, 0 remediation actions"),
            (47.1, "integrity_check", "stored seq=502, last acknowledged write seq=502"),
        ]),
        status="aborted"))
    return out


# ---------------------------------------------------------------------- main
def seed(out_dir: Path, now: datetime) -> list[str]:
    s = load_settings()
    store = ReportStore(out_dir)
    lines: list[str] = []

    topo = build_topology(s.compose_file, s.compose_project, s.nginx_conf, s.knowledge_file, live=False)
    topo.generated_at = now - timedelta(minutes=95)
    for name, node in topo.services.items():
        node.container = f"{topo.project}-{name}-1"
        node.state = "running"
        node.health = "healthy" if node.health_port else None
        node.restart_policy = "always"
    topo = Topology.model_validate(topo.model_dump())
    store.write_topology(topo)
    (store.root / "topology.md").write_text(topology_markdown(topo))
    lines.append(f"topology   {len(topo.services)} services, {len(topo.edges)} edges, "
                 f"#1 {topo.criticality[0].service} ({topo.criticality[0].score})")

    policy = load_policy(s.thresholds_file)
    api = window(240, 0, 17.9, 41.2, 98.3)
    b = Baseline(captured_at=now - timedelta(minutes=90), duration_s=60.0, ditto_url=s.ditto_url, api=api,
                 health_availability={svc: (None if svc == "nginx" else 1.0) for svc in topo.relevant_services()},
                 thresholds=thresholds_from(policy, api.p95_ms), notes=[])
    store.write_baseline(b)
    (store.root / "baseline.md").write_text(baseline_markdown(b))
    lines.append(f"baseline   {api.sample_count} requests, success {api.success_rate:.0%}, p95 {api.p95_ms} ms")

    reports = experiments(s, topo, b.thresholds, now)
    for r in reports:
        path = store.write_experiment(r)
        (store.experiment_dir(r.experiment_id) / "report.md").write_text(experiment_markdown(r))
        # the store orders experiments by mtime, so make it match the story
        ts = r.ended_at.timestamp()
        os.utime(path, (ts, ts))

    ids = [r.experiment_id for r in reports]
    quiet = lambda _msg: None  # noqa: E731
    risks = {x.experiment_id: x for x in risk.run(store, None, topo, only=ids, force=True, log=quiet)}
    fixes = {x.experiment_id: x for x in fix.run(store, None, only=ids, force=True, log=quiet)}
    sc = scorecard.run(store, topo, topo.project, log=quiet)

    for r in reports:
        rk, fx = risks[r.experiment_id], fixes[r.experiment_id]
        held = {True: "held", False: "violated", None: "n/a"}[r.hypothesis_result.held]
        lines.append(f"experiment {r.experiment_id}  {r.failure_type:<15} {r.target['service']:<9} {r.status:<9} "
                     f"by {r.planned_by:<13} hypothesis {held:<8} risk {rk.level} {rk.severity:.1f}  fixes {len(fx.recommendations)}")
    worst = next((x for x in sc.ranking if x.score is not None), None)
    lines.append(f"scorecard  overall {sc.overall_score}/100 over {sc.experiments_total} experiments"
                 + (f", weakest {worst.service} {worst.grade} ({worst.score})" if worst else ""))

    # round-trip everything the dashboard will read, so a schema drift fails here and not on stage
    assert store.topology() and store.baseline() and store.scorecard()
    for i in ids:
        assert store.experiment(i) and store.risk(i) and store.fix(i)
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Seed a demo blackboard for the Faultline control room (no Docker needed)")
    p.add_argument("--out", default="reports-demo", help="target directory (default reports-demo/)")
    a = p.parse_args(argv)
    out = Path(a.out)
    out = (out if out.is_absolute() else ROOT / out).resolve()
    for line in seed(out, datetime.now(timezone.utc)):
        print(line)
    print(f"wrote {out}")
    print(f"serve it: .venv/bin/python -m faultline.dashboard.server --reports {os.path.relpath(out, ROOT)} --port 8790")
    return 0


if __name__ == "__main__":
    sys.exit(main())
