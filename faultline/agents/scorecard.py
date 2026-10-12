"""Scorecard agent. Per-service resilience score 0–100 from completed/aborted experiments. Pure arithmetic."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Callable, Optional

from ..reporting.store import ReportStore
from ..schemas import ExperimentReport, FixState, RiskAssessment, Scorecard, ServiceScore, Topology
from .knowledge import target_service

SCORED_STATUSES = ("completed", "aborted")
TTR_CEILING_S = 120.0
MAX_SEVERE_FINDINGS = 3
GRADES = [(90.0, "A"), (80.0, "B"), (65.0, "C"), (50.0, "D")]
UNTESTED_GRADE = "—"


def penalty(report: ExperimentReport) -> tuple[float, dict[str, float]]:
    during = report.during.success_rate
    rec = report.recovery
    severe = sum(1 for f in report.findings if f.severity in ("high", "critical"))
    parts = {
        "availability": 40.0 * (1.0 - (during if during is not None else 0.0)),
        "recovery_time": 20.0 * min((rec.time_to_recover_s or 0.0) / TTR_CEILING_S, 1.0),
        "intervention": 20.0 if (not rec.recovered or rec.remediation_actions) else 0.0,
        "severe_findings": 10.0 * min(severe, MAX_SEVERE_FINDINGS),
        "cascade": 5.0 if any(f.code == "cascade_beyond_hypothesis" for f in report.findings) else 0.0,
    }
    return sum(parts.values()), parts


def grade(score: Optional[float]) -> str:
    if score is None:
        return UNTESTED_GRADE
    for floor, letter in GRADES:
        if score >= floor:
            return letter
    return "F"


def _score_service(service: str, reports: list[ExperimentReport]) -> ServiceScore:
    if not reports:
        return ServiceScore(service=service, score=None, grade=UNTESTED_GRADE, experiments=0, notes=["not tested"])
    penalties = [penalty(r)[0] for r in reports]
    score = round(max(0.0, 100.0 - mean(penalties)), 1)
    rates = [r.during.success_rate for r in reports if r.during.success_rate is not None]
    ttrs = [r.recovery.time_to_recover_s for r in reports if r.recovery.time_to_recover_s is not None]
    notes = _notes(reports)
    return ServiceScore(
        service=service,
        score=score,
        grade=grade(score),
        experiments=len(reports),
        worst_success_rate=round(min(rates), 4) if rates else None,
        slowest_recovery_s=round(max(ttrs), 1) if ttrs else None,
        findings=sum(len(r.findings) for r in reports),
        notes=notes,
    )


def _notes(reports: list[ExperimentReport]) -> list[str]:
    notes: list[str] = []
    codes: dict[str, int] = defaultdict(int)
    for r in reports:
        for f in r.findings:
            codes[f.code] += 1
    if codes.get("health_check_masked_failure"):
        notes.append("health checks stayed green during failure")
    if codes.get("manual_restart_required") or codes.get("recovery_failed"):
        notes.append("needed manual restart to recover")
    elif codes.get("no_automated_remediation"):
        notes.append("sat unhealthy with no automated remediation")
    if codes.get("data_loss_suspected"):
        notes.append("suspected data loss")
    if codes.get("cascade_beyond_hypothesis"):
        notes.append("cascaded beyond hypothesis")
    if codes.get("slow_recovery"):
        notes.append("slow recovery")
    unrecovered = sum(1 for r in reports if not r.recovery.recovered)
    if unrecovered:
        notes.append(f"{unrecovered} experiment(s) never recovered")
    types = sorted({r.failure_type for r in reports})
    notes.append("tested: " + ", ".join(types))
    return notes


def _rank_key(s: ServiceScore) -> tuple[int, float, str]:
    return (1 if s.score is None else 0, s.score if s.score is not None else 0.0, s.service)


def _weakest_reason(s: ServiceScore, reports: list[ExperimentReport]) -> str:
    reasons: list[str] = []
    if s.worst_success_rate is not None and s.worst_success_rate < 0.9:
        reasons.append(f"API success fell to {s.worst_success_rate:.0%}")
    if s.slowest_recovery_s is not None and s.slowest_recovery_s > 30:
        reasons.append(f"recovery took up to {s.slowest_recovery_s:.0f} s")
    if any(not r.recovery.recovered or r.recovery.remediation_actions for r in reports):
        reasons.append("recovery needed intervention")
    behavioural = [n for n in s.notes if not n.startswith("tested:") and not n.endswith("never recovered")]
    reasons.extend(behavioural[:2])
    return "; ".join(dict.fromkeys(reasons)) or "lowest score among tested services"


def fix_state(status: str) -> str:
    """Proposal status -> the word the scorecard shows (a pending proposal is "proposed")."""
    return "proposed" if status == "pending" else status


def _attach_fixes(ranking: list[ServiceScore], experiments: list[ExperimentReport], proposals: list) -> None:
    """Each finding's fix state, on the service the finding was raised against (the experiment's target)
    and on every service the fix changes."""
    target_of = {r.experiment_id: target_service(r) for r in experiments}
    by_service = {s.service: s for s in ranking}
    for p in sorted(proposals or [], key=lambda p: p.created_at):
        reason = p.verification.reason if p.verification and p.verification.reason else p.reason
        state = FixState(proposal_id=p.proposal_id, experiment_id=p.experiment_id, finding_code=p.finding_code,
                         catalog_fix_id=p.catalog_fix_id, state=fix_state(p.status), reason=reason or "")
        for svc in dict.fromkeys([target_of.get(p.experiment_id, ""), *p.target_services]):
            if svc in by_service:
                by_service[svc].fixes.append(state)


def compute(
    experiments: list[ExperimentReport],
    risks: dict[str, RiskAssessment],
    topology: Optional[Topology],
    project: str,
    proposals: Optional[list] = None,
) -> Scorecard:
    scored = [r for r in experiments if r.status in SCORED_STATUSES]
    by_service: dict[str, list[ExperimentReport]] = defaultdict(list)
    for r in scored:
        by_service[target_service(r) or "unknown"].append(r)

    services = list(topology.relevant_services()) if topology else []
    for name in [target_service(r) or "unknown" for r in experiments]:
        if name not in services:
            services.append(name)

    ranking = sorted((_score_service(s, by_service.get(s, [])) for s in services), key=_rank_key)
    _attach_fixes(ranking, experiments, proposals or [])
    tested = [s for s in ranking if s.score is not None]
    overall = round(mean(s.score for s in tested), 1) if tested else None  # type: ignore[misc]

    if tested:
        weakest = tested[0]
        high_risk = sum(1 for rk in risks.values() if rk.level in ("high", "critical"))
        summary = (
            f"{len(scored)} scored experiment(s) across {len(tested)} of {len(services)} services; overall {overall}/100. "
            f"Weakest: `{weakest.service}` ({weakest.grade}, {weakest.score}/100) — {_weakest_reason(weakest, by_service[weakest.service])}."
        )
        if high_risk:
            summary += f" {high_risk} experiment(s) rated high or critical risk."
    else:
        summary = "No completed experiments yet; nothing to score."

    return Scorecard(project=project, overall_score=overall, ranking=ranking, experiments_total=len(experiments), summary=summary)


def render_markdown(sc: Scorecard) -> str:
    overall = f"{sc.overall_score}/100" if sc.overall_score is not None else "n/a"
    lines = [
        f"# Resilience scorecard — {sc.project}",
        "",
        f"_Generated {sc.generated_at.strftime('%Y-%m-%d %H:%M UTC')} · {sc.experiments_total} experiment(s) · overall **{overall}**_",
        "",
        sc.summary,
        "",
        "| # | Service | Grade | Score | Experiments | Worst success | Slowest recovery | Findings | Notes |",
        "|---|---------|-------|-------|-------------|---------------|------------------|----------|-------|",
    ]
    for i, s in enumerate(sc.ranking, 1):
        score = f"{s.score}" if s.score is not None else "—"
        worst = f"{s.worst_success_rate:.0%}" if s.worst_success_rate is not None else "—"
        slowest = f"{s.slowest_recovery_s:.0f} s" if s.slowest_recovery_s is not None else "—"
        notes = "; ".join(s.notes).replace("|", "/")
        lines.append(f"| {i} | `{s.service}` | **{s.grade}** | {score} | {s.experiments} | {worst} | {slowest} | {s.findings} | {notes} |")
    lines += [
        "",
    ]
    fixed = [(s.service, f) for s in sc.ranking for f in s.fixes]
    if fixed:
        lines += ["", "## Fix states", "",
                  "Human-approved fix proposals for these findings (nothing is applied without an approval).", "",
                  "| Service | Finding | Fix | State | Proposal |", "|---|---|---|---|---|"]
        for svc, f in fixed:
            note = f" ({f.reason})".replace("|", "/") if f.reason and f.state in ("rolled_back", "verification_failed", "rejected") else ""
            lines.append(f"| `{svc}` | `{f.finding_code}` | `{f.catalog_fix_id}` | {f.state.replace('_', ' ')}{note} | "
                         f"[{f.proposal_id}](proposals/{f.proposal_id}.md) |")
    lines += [
        "",
        "Score = 100 − mean penalty; penalty = 40·(1−success during) + 20·min(TTR/120 s, 1) + 20·(manual recovery) "
        "+ 10·(high/critical findings, max 3) + 5·(cascade). Grades: A ≥ 90, B ≥ 80, C ≥ 65, D ≥ 50, else F.",
    ]
    return "\n".join(lines) + "\n"


def run(store: ReportStore, topology: Optional[Topology], project: str, log: Callable[[str], None] = print) -> Scorecard:
    experiments = store.experiments()
    risks = {r.experiment_id: rk for r in experiments if (rk := store.risk(r.experiment_id)) is not None}
    try:
        proposals = store.proposals()
    except Exception:  # a broken proposal file must never stop the scorecard
        proposals = []
    sc = compute(experiments, risks, topology, project, proposals)
    store.write_scorecard(sc)
    md: Path = store.root / "scorecard.md"
    md.write_text(render_markdown(sc))
    log(f"[scorecard] {sc.summary}")
    for s in sc.ranking:
        score = f"{s.score:>5}" if s.score is not None else "    —"
        log(f"[scorecard]   {s.grade:>1} {score}  {s.service} ({s.experiments} exp)")
    return sc
