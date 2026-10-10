"""Risk agent. Deterministic 0–10 severity from the experiment numbers; the LLM may only reword the prose."""
from __future__ import annotations

import json
from typing import Callable, Optional

from ..planning.llm import OllamaClient
from ..reporting.store import ReportStore
from ..schemas import ExperimentReport, Finding, RiskAssessment, Topology
from .knowledge import target_service

Level = str
FALLBACK_SERVICE_COUNT = 7
TTR_CEILING_S = 120.0

FINDING_BONUS: dict[str, float] = {
    "health_check_masked_failure": 1.0,
    "data_loss_suspected": 1.0,
    "cascade_beyond_hypothesis": 0.5,
    "no_automated_remediation": 0.5,
    "manual_restart_required": 0.5,
}

LLM_SYSTEM = (
    "You are an SRE writing a terse incident risk note for a chaos experiment on an Eclipse Ditto deployment. "
    "Write exactly two sentences for each field. Use only the numbers and findings given; do not speculate beyond them. "
    'Reply with JSON only: {"summary": str, "production_implication": str}.'
)


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def _level(severity: float) -> Level:
    if severity < 3:
        return "low"
    if severity < 5.5:
        return "medium"
    if severity < 8:
        return "high"
    return "critical"


def _service_count(topology: Optional[Topology]) -> int:
    if topology is None:
        return FALLBACK_SERVICE_COUNT
    return max(len(topology.relevant_services()), 1)


def _factors(report: ExperimentReport, topology: Optional[Topology]) -> tuple[dict[str, float], float, int, float]:
    during = report.during.success_rate
    user_impact = 1.0 - (during if during is not None else 0.0)
    user_impact = min(max(user_impact, 0.0), 1.0)
    blast_radius = len(set(report.affected_services_observed or []) | {target_service(report)} - {""})
    ttr = report.recovery.time_to_recover_s
    codes = {f.code for f in report.findings}

    factors = {
        "impact": round(4.0 * user_impact, 3),
        "blast": round(2.0 * min(blast_radius / _service_count(topology), 1.0), 3),
        "recovery": round(2.0 * min((ttr or 0.0) / TTR_CEILING_S, 1.0), 3),
        "intervention": 1.5 if (not report.recovery.recovered or report.recovery.remediation_actions) else 0.0,
        "findings": round(sum(bonus for code, bonus in FINDING_BONUS.items() if code in codes), 3),
    }
    return factors, user_impact, blast_radius, ttr


def _verb(failure_type: str) -> str:
    return {
        "container_crash": "Killing",
        "container_stop": "Stopping",
        "container_pause": "Pausing",
        "network_delay": "Adding latency to",
        "network_loss": "Dropping packets on",
        "network_partition": "Partitioning",
        "cpu_stress": "CPU-stressing",
        "disk_pressure": "Filling the disk of",
        "db_flood": "Flooding the database behind",
        "traffic_spike": "Spiking traffic at",
    }.get(failure_type, "Disrupting")


def _recovery_phrase(report: ExperimentReport) -> str:
    rec = report.recovery
    if not rec.recovered:
        return "did not recover within the experiment window"
    ttr = f"{rec.time_to_recover_s:.0f} s" if rec.time_to_recover_s is not None else "an unknown time"
    if rec.remediation_actions:
        return f"recovered in {ttr} only after manual remediation ({', '.join(rec.remediation_actions)})"
    if rec.auto_restarted_by_docker:
        return f"recovered in {ttr} after Docker restarted the container"
    return f"recovered in {ttr} without intervention"


def template_summary(report: ExperimentReport, blast_radius: int) -> str:
    target = target_service(report) or "unknown"
    svc = "service" if blast_radius == 1 else "services"
    abort = ""
    if report.safety.aborted_early:
        abort = f" The safety monitor aborted early ({report.safety.abort_reason or 'threshold breached'})."
    return (
        f"{_verb(report.failure_type)} `{target}` ({report.failure_type}) moved API success from "
        f"{_pct(report.baseline.success_rate)} to {_pct(report.during.success_rate)} across "
        f"{report.during.sample_count} samples; {blast_radius} {svc} affected; {_recovery_phrase(report)}.{abort}"
    )


def template_implication(report: ExperimentReport, level: Level) -> str:
    codes = {f.code for f in report.findings}
    target = target_service(report) or "this service"
    parts: list[str] = []
    if level == "critical":
        parts.append(f"In production this is a full outage: a single {report.failure_type} on {target} takes the API down for every tenant.")
    elif level == "high":
        parts.append(f"In production most users would see errors for the whole duration of a {report.failure_type} on {target}.")
    elif level == "medium":
        parts.append(f"In production a {report.failure_type} on {target} would be a visible degradation but not a full outage.")
    else:
        parts.append(f"In production a {report.failure_type} on {target} would be absorbed with little or no user-visible impact.")
    if "data_loss_suspected" in codes:
        parts.append("Suspected data loss makes this a stop-ship issue regardless of availability numbers.")
    if "health_check_masked_failure" in codes:
        parts.append("Health checks stayed green while the API failed, so no orchestrator or alert would have reacted.")
    if "manual_restart_required" in codes or "recovery_failed" in codes:
        parts.append("Recovery needed a human; expect a page and minutes of downtime instead of seconds.")
    elif "no_automated_remediation" in codes:
        parts.append("The container sat unhealthy with nothing acting on it; Docker's restart policy does not cover this state.")
    if "cascade_beyond_hypothesis" in codes:
        parts.append("The failure reached services outside the hypothesis, so the dependency map underestimates the blast radius.")
    return " ".join(parts[:3])


def _skipped(report: ExperimentReport) -> RiskAssessment:
    return RiskAssessment(
        experiment_id=report.experiment_id,
        target_service=target_service(report),
        failure_type=report.failure_type,
        severity=0.0,
        level="low",
        blast_radius=0,
        user_impact=0.0,
        recovery_s=None,
        factors={"impact": 0.0, "blast": 0.0, "recovery": 0.0, "intervention": 0.0, "findings": 0.0},
        findings=list(report.findings),
        summary=f"Experiment {report.experiment_id} was skipped ({report.error or 'no injection performed'}); no risk computed.",
        production_implication="No conclusion can be drawn from a skipped experiment.",
        generated_by="rules",
    )


def _llm_prose(
    llm: Optional[OllamaClient], report: ExperimentReport, severity: float, level: Level, factors: dict[str, float]
) -> Optional[tuple[str, str]]:
    if llm is None or not llm.available():
        return None
    payload = {
        "target": target_service(report),
        "failure_type": report.failure_type,
        "severity_0_to_10": severity,
        "level": level,
        "factors": factors,
        "success_rate": {"baseline": report.baseline.success_rate, "during": report.during.success_rate, "after": report.after.success_rate},
        "p95_ms": {"baseline": report.baseline.p95_ms, "during": report.during.p95_ms},
        "recovery": {
            "recovered": report.recovery.recovered,
            "time_to_recover_s": report.recovery.time_to_recover_s,
            "remediation_actions": report.recovery.remediation_actions,
        },
        "affected_services": report.affected_services_observed,
        "aborted_early": report.safety.aborted_early,
        "findings": [{"code": f.code, "severity": f.severity, "title": f.title} for f in report.findings],
    }
    data = llm.chat_json(LLM_SYSTEM, json.dumps(payload, default=str))
    if not data:
        return None
    summary, implication = data.get("summary"), data.get("production_implication")
    ok = all(isinstance(s, str) and 0 < len(s.strip()) < 600 for s in (summary, implication))
    return (summary.strip(), implication.strip()) if ok else None


def assess(report: ExperimentReport, topology: Optional[Topology], llm: Optional[OllamaClient]) -> RiskAssessment:
    if report.status == "skipped":
        return _skipped(report)

    factors, user_impact, blast_radius, ttr = _factors(report, topology)
    severity = round(min(max(sum(factors.values()), 0.0), 10.0), 2)
    level = _level(severity)
    if any(f.code == "data_loss_suspected" for f in report.findings):
        level = "critical"

    summary = template_summary(report, blast_radius)
    implication = template_implication(report, level)
    generated_by = "rules"
    prose = _llm_prose(llm, report, severity, level, factors)
    if prose:
        summary, implication = prose
        generated_by = "llm+rules"

    return RiskAssessment(
        experiment_id=report.experiment_id,
        target_service=target_service(report),
        failure_type=report.failure_type,
        severity=severity,
        level=level,
        blast_radius=blast_radius,
        user_impact=round(user_impact, 4),
        recovery_s=ttr,
        factors=factors,
        findings=list(report.findings),
        summary=summary,
        production_implication=implication,
        generated_by=generated_by,
    )


def pending_ids(store: ReportStore, only: Optional[list[str]], force: bool) -> list[str]:
    ids = store.experiment_ids()
    if only:
        wanted = set(only)
        ids = [i for i in ids if i in wanted]
    if force:
        return ids
    return [i for i in ids if store.risk(i) is None]


def run(
    store: ReportStore,
    llm: Optional[OllamaClient],
    topology: Optional[Topology],
    only: Optional[list[str]] = None,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> list[RiskAssessment]:
    out: list[RiskAssessment] = []
    for eid in pending_ids(store, only, force):
        report = store.experiment(eid)
        if report is None:
            log(f"[risk] {eid}: unreadable report, skipped")
            continue
        risk = assess(report, topology, llm)
        store.write_risk(risk)
        out.append(risk)
        log(
            f"[risk] {eid}: {risk.target_service}/{risk.failure_type} -> {risk.level.upper()} "
            f"({risk.severity:.1f}/10, blast {risk.blast_radius}, impact {risk.user_impact:.0%}, {risk.generated_by})"
        )
    if not out:
        log("[risk] nothing to assess")
    return out
