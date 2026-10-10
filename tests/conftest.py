"""Shared fixtures. Everything here is built in code: no Docker, no network, no running Ditto."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from faultline.reporting.store import ReportStore
from faultline.safety.thresholds import load_policy, thresholds_from
from faultline.schemas import (
    Criticality,
    Edge,
    ExperimentReport,
    Finding,
    HealthSummary,
    Hypothesis,
    HypothesisResult,
    RecoveryOutcome,
    SafetyOutcome,
    ServiceNode,
    Thresholds,
    TimelineEvent,
    Topology,
    WindowStats,
    utcnow,
)

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "bosch-ditto"


# ---------------------------------------------------------------- topology
def _node(name: str, image: str, role: str = "app", **kw: Any) -> ServiceNode:
    return ServiceNode(name=name, image=image, role=role, container=f"{PROJECT}-{name}-1", **kw)


def _criticality(services: dict[str, ServiceNode], edges: list[Edge]) -> list[Criticality]:
    """Tiny stand-in for topology.graph.criticality so the fixture does not depend on that module."""
    topo = Topology(project=PROJECT, services=services, edges=edges, criticality=[])
    out = []
    for name, node in services.items():
        deps = topo.transitive_dependents(name)
        score = float(len(deps))
        reasons = [f"{len(deps)} transitive dependents"]
        if node.stateful:
            score += 2
            reasons.append("stateful")
        if node.entrypoint:
            score += 1
            reasons.append("entrypoint")
        if node.role == "datastore":
            score += 1
            reasons.append("datastore")
        out.append(Criticality(service=name, fan_in=len(topo.direct_dependents(name)), transitive_dependents=deps,
                               score=score, reasons=reasons))
    out.sort(key=lambda c: (-c.score, c.service))
    return out


@pytest.fixture
def topology() -> Topology:
    services = {
        "mongodb": _node("mongodb", "mongo:7.0", "datastore", stateful=True),
        "policies": _node("policies", "eclipse/ditto-policies:3.9.0", health_port=7626),
        "things": _node("things", "eclipse/ditto-things:3.9.0", health_port=7626),
        "things-search": _node("things-search", "eclipse/ditto-things-search:3.9.0", health_port=7626),
        "connectivity": _node("connectivity", "eclipse/ditto-connectivity:3.9.0", health_port=7626),
        "gateway": _node("gateway", "eclipse/ditto-gateway:3.9.0", entrypoint=True, health_port=7626),
        "nginx": _node("nginx", "nginx:1.27", "proxy", entrypoint=True),
        "ditto-ui": _node("ditto-ui", "eclipse/ditto-ui:3.9.0", "ui", chaos_relevant=False),
    }
    edges: list[Edge] = []

    def add(kind: str, pairs: list[tuple[str, str]], evidence: str = "") -> None:
        for src, dst in pairs:
            edges.append(Edge(src=src, dst=dst, kind=kind, evidence=evidence or kind))

    add("depends_on", [("things", "policies"), ("things-search", "policies"), ("connectivity", "policies"),
                       ("gateway", "policies"), ("nginx", "gateway")])
    add("env", [(s, "mongodb") for s in ("policies", "things", "things-search", "connectivity")],
        "env MONGO_DB_HOSTNAME=mongodb")
    add("proxy", [("nginx", "gateway"), ("nginx", "ditto-ui")], "proxy_pass")
    add("documented", [("gateway", "things"), ("gateway", "things-search"), ("gateway", "connectivity"),
                       ("things", "policies"), ("things-search", "things"), ("connectivity", "things"),
                       ("connectivity", "policies"), ("things-search", "policies")], "Ditto architecture docs")
    return Topology(project=PROJECT, services=services, edges=edges, criticality=_criticality(services, edges))


# ---------------------------------------------------------------- policy / thresholds
@pytest.fixture(scope="session")
def policy_raw() -> dict:
    return load_policy(str(ROOT / "config" / "thresholds.yaml"))


@pytest.fixture
def thresholds(policy_raw) -> Thresholds:
    return thresholds_from(policy_raw, 60.0)


# ---------------------------------------------------------------- reports
@pytest.fixture
def make_report(thresholds):
    """Factory for a valid ExperimentReport.

    Convenience keys: service, failure_type, during_success, after_success, recovered, ttr,
    remediation, finding_codes (list of codes -> high-severity findings), affected (list), status.
    Any other keyword is applied as a top-level ExperimentReport field override.
    """
    counter = {"n": 0}

    def _make(**overrides: Any) -> ExperimentReport:
        counter["n"] += 1
        service = overrides.pop("service", "policies")
        failure_type = overrides.pop("failure_type", "container_crash")
        during_success = overrides.pop("during_success", 0.1)
        after_success = overrides.pop("after_success", 1.0)
        recovered = overrides.pop("recovered", True)
        ttr = overrides.pop("ttr", 14.0)
        remediation = overrides.pop("remediation", [])
        codes = overrides.pop("finding_codes", [])
        affected = overrides.pop("affected", ["gateway"])
        status = overrides.pop("status", "completed")
        started = utcnow() - timedelta(seconds=120)
        fields: dict[str, Any] = dict(
            experiment_id=overrides.pop("experiment_id", f"exp{counter['n']:04d}"),
            started_at=started,
            ended_at=started + timedelta(seconds=90),
            target={"service": service, "container": f"{PROJECT}-{service}-1"},
            failure_type=failure_type,
            parameters={"duration_s": 30},
            rationale="test",
            hypothesis=Hypothesis(statement="it will degrade", expected_affected=["gateway"],
                                  expected_max_error_rate=1.0, expected_recovery_s=90),
            hypothesis_result=HypothesisResult(held=True, observed_max_error_rate=1 - during_success,
                                               observed_recovery_s=ttr),
            criticality_score=5.0,
            planned_by="deterministic",
            baseline=WindowStats(sample_count=80, success_rate=1.0, p50_ms=20, p95_ms=60, p99_ms=90),
            during=WindowStats(sample_count=120, success_rate=during_success, p50_ms=30, p95_ms=800, p99_ms=1200),
            after=WindowStats(sample_count=60, success_rate=after_success, p50_ms=22, p95_ms=70, p99_ms=95),
            health=[HealthSummary(service=service, ticks=30, alive_ok_fraction=0.5, ready_ok_fraction=0.5)],
            recovery=RecoveryOutcome(recovered=recovered, recovered_at=started + timedelta(seconds=44) if recovered else None,
                                     time_to_recover_s=ttr if recovered else None, remediation_actions=list(remediation),
                                     data_integrity_ok=True),
            safety=SafetyOutcome(thresholds=thresholds),
            affected_services_observed=list(affected),
            # The title names the service, as the injector's own findings do: it raises one
            # `health_check_masked_failure` per service whose /ready stayed green, and consumers
            # (reconcile) attribute a per-service finding by the service it names.
            findings=[Finding(code=c, severity="high", title=f"{service} {c.replace('_', ' ')}",
                              evidence=f"synthetic, observed on {service}") for c in codes],
            timeline=[TimelineEvent(at=started, t_plus_s=0.0, event="inject", detail=failure_type)],
            reproduce=[f"docker kill {PROJECT}-{service}-1"],
            status=status,
        )
        fields.update(overrides)
        return ExperimentReport(**fields)

    return _make


@pytest.fixture
def tmp_store(tmp_path) -> ReportStore:
    return ReportStore(tmp_path / "reports")
