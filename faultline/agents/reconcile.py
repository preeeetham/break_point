"""Reconcile agent. Scores the Weakness Scout's static predictions against what the live experiments
actually found. Pure arithmetic over reports/ — no LLM is consulted anywhere in this module.

This is the whole-system analogue of `hypothesis_result.held`: the injector judges one hypothesis per
experiment, this judges every prediction made before any experiment ran. A predicted severity stays a
prediction forever — it is never rendered or logged without a marker saying so, because the only
measured severity in the system is the Risk agent's post-hoc one.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..reporting.store import ReportStore
from ..schemas import (
    PREDICTED_LABEL,
    ExperimentReport,
    PredictedWeakness,
    WeaknessOutcome,
    WeaknessReconciliation,
    WeaknessReport,
)
from .knowledge import target_service

# A skipped experiment never injected anything, so it can neither confirm nor clear a prediction.
PROVES_NOTHING = "skipped"
# Only a run that went the distance is allowed to clear a prediction: an aborted run was cut short by
# the safety monitor, so "nothing bad happened" is a statement about the monitor, not the system.
CLEARING_STATUS = "completed"
SEVERE = ("high", "critical")

# Findings that are ABOUT a particular service rather than about the run as a whole. The injector
# raises one `health_check_masked_failure` per service whose /ready stayed green, so a single
# experiment on `things` can carry three of them naming things-search, connectivity and gateway.
# Matching on the code alone would credit all three to the prediction about `things` and leave the
# three predictions they actually confirm sitting in the untested pile -- so for these codes the
# finding must also name one of the weakness's own services.
# `cascade_beyond_hypothesis` is deliberately NOT here: it names the services a failure spread INTO,
# and `touching()` already attributes that through `affected_services_observed`, so gating it would
# only produce false negatives.
SERVICE_ATTRIBUTED_CODES = frozenset({
    "health_check_masked_failure",
    "dependency_blind_readiness",
})


def _names_service(text: str, services: Iterable[str]) -> bool:
    """Does `text` name one of these services? Hyphen-aware, because a plain word-boundary match for
    `things` also matches inside `things-search` and would confirm the wrong prediction."""
    return any(re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text) for s in services)
CONFIRMED_LABEL = "[predicted, confirmed by experiment]"
MAX_QUOTED_EVIDENCE = 2
EVIDENCE_CHARS = 160


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _names(items: list[str]) -> str:
    return ", ".join(items) if items else "—"


def _types(reports: list[ExperimentReport]) -> str:
    return _names(sorted({r.failure_type for r in reports}))


def _targets(reports: list[ExperimentReport]) -> str:
    return _names(sorted({target_service(r) for r in reports if target_service(r)}))


def _ids(reports: list[ExperimentReport]) -> list[str]:
    """Experiment ids in store order, de-duplicated."""
    return list(dict.fromkeys(r.experiment_id for r in reports))


def _where(report: ExperimentReport) -> str:
    """`exp0001 (container_stop on policies)` — enough to find the report on disk."""
    return f"{report.experiment_id} ({report.failure_type} on {target_service(report) or 'unknown'})"


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EVIDENCE_CHARS else text[: EVIDENCE_CHARS - 1].rstrip() + "…"


# ---------------------------------------------------------------- which experiments bear on a prediction
def touching(weakness: PredictedWeakness, experiments: list[ExperimentReport]) -> list[ExperimentReport]:
    """Experiments that hit one of the weakness's services, as a target or as observed collateral.

    A weakness may also restrict WHICH failure types can speak to it (`confirming_failure_types`,
    empty = any). That gate matters: several of the nominated finding codes -- `api_impact` above all
    -- fire on any experiment that moves the error rate, so without it a `container_crash` would
    "confirm" a predicted missing CPU limit, and the predicted-vs-confirmed headline would inflate
    itself on experiments that had nothing to do with the prediction. A crash is a fast, clean error,
    which is precisely not evidence about retry and backoff behaviour.
    """
    services = set(weakness.services)
    types = set(weakness.confirming_failure_types or ())
    codes = set(weakness.confirming_finding_codes)

    def bears_on(r: ExperimentReport) -> bool:
        if target_service(r) in services or services & set(r.affected_services_observed or []):
            return True
        # A per-service finding that NAMES one of these services is itself evidence the experiment
        # bore on it. The injector fills `affected_services_observed` from health collapse, so an
        # experiment on `things` records only ['things'] even when it raised three
        # `health_check_masked_failure` findings naming things-search, connectivity and gateway --
        # which is precisely the evidence those three services' predictions were waiting for.
        return any(f.code in codes and f.code in SERVICE_ATTRIBUTED_CODES and f.severity != "info"
                   and _names_service(f"{f.title} {f.evidence}", services)
                   for f in r.findings)

    return [r for r in experiments if bears_on(r) and (not types or r.failure_type in types)]


def _confirmations(weakness: PredictedWeakness, bearing: list[ExperimentReport]) -> list[tuple[ExperimentReport, str, str]]:
    """(report, code, evidence) for every live finding the Scout nominated as proof of this weakness.

    The severity filter is load-bearing and not obvious. Several nominated codes are emitted on EVERY
    experiment, benign ones included: the injector writes `api_impact` at `info` precisely to record
    "API unaffected (100% success, p95 41 ms)", and `auto_recovered` / an operator-stop `safety_abort`
    are always `info` too. Counting those as proof would manufacture confirmations out of experiments
    that went fine and wreck the one number this agent exists to produce, so an `info` match is not a
    confirmation. It does not block a clear either — at `info`, `api_impact` says the API held up.
    """
    codes = set(weakness.confirming_finding_codes)
    out: list[tuple[ExperimentReport, str, str]] = []
    for r in bearing:
        for f in r.findings:
            if f.code not in codes or f.severity == "info":
                continue
            if f.code in SERVICE_ATTRIBUTED_CODES and not _names_service(
                    f"{f.title} {f.evidence}", weakness.services):
                continue
            out.append((r, f.code, f.evidence))
    return out


def _severe_findings(bearing: list[ExperimentReport]) -> list[tuple[ExperimentReport, str, str]]:
    return [(r, f.code, f.severity) for r in bearing for f in r.findings if f.severity in SEVERE]


# ---------------------------------------------------------------- detail strings
def _confirmed_detail(hits: list[tuple[ExperimentReport, str, str]]) -> str:
    quoted = [f"experiment {_where(r)} raised {code}: {_clip(evidence) or 'no evidence recorded'}"
              for r, code, evidence in hits[:MAX_QUOTED_EVIDENCE]]
    detail = "; ".join(quoted)
    extra = len(hits) - len(quoted)
    if extra:
        detail += f"; plus {extra} further confirming {_plural(extra, 'finding', 'findings')}"
    return detail


def _cleared_detail(weakness: PredictedWeakness, completed: list[ExperimentReport]) -> str:
    n = len(completed)
    codes = _names(sorted(set(weakness.confirming_finding_codes)))
    return (
        f"{n} completed {_plural(n, 'experiment', 'experiments')} on {_targets(completed)} ({_types(completed)}) "
        f"raised none of {codes} above info severity and no high- or critical-severity finding"
    )


def _untested_detail(
    weakness: PredictedWeakness,
    skipped: list[ExperimentReport],
    bearing: list[ExperimentReport],
    severe: list[tuple[ExperimentReport, str, str]],
) -> str:
    services = _names(weakness.services)
    if not bearing:
        if skipped:
            n = len(skipped)
            return (
                f"the only {_plural(n, 'experiment', f'{n} experiments')} touching {services} "
                f"({_names([_where(r) for r in skipped])}) {_plural(n, 'was', 'were')} skipped and injected "
                f"nothing, which proves nothing either way"
            )
        return f"no experiment targeted {services} or observed it as collateral"

    aborted = [r for r in bearing if r.status == "aborted"]
    completed = [r for r in bearing if r.status == CLEARING_STATUS]
    if severe:
        quoted = [f"{_where(r)} raised {sev}-severity {code}" for r, code, sev in severe[:MAX_QUOTED_EVIDENCE]]
        return (
            f"{_names(quoted)}, which is not one of this prediction's confirming codes "
            f"({_names(sorted(set(weakness.confirming_finding_codes)))}), so {services} is neither confirmed nor cleared"
        )
    if aborted and not completed:
        n = len(aborted)
        return (
            f"{n} bearing {_plural(n, 'experiment', 'experiments')} ({_names([_where(r) for r in aborted])}) "
            f"{_plural(n, 'was', 'were')} aborted early by the safety monitor, so {services} never got the chance "
            f"to confirm or clear this"
        )
    n = len(bearing)
    quoted = [f"{_where(r)} ended {r.status}" for r in bearing[:MAX_QUOTED_EVIDENCE]]
    return (
        f"{n} bearing {_plural(n, 'experiment', 'experiments')} ({_names(quoted)}) neither raised one of "
        f"{_names(sorted(set(weakness.confirming_finding_codes)))} nor completed cleanly, so {services} stays untested"
    )


# ---------------------------------------------------------------- per-weakness judgement
def judge(weakness: PredictedWeakness, experiments: list[ExperimentReport]) -> WeaknessOutcome:
    """One prediction against the record. The input weakness is never mutated: the verdict lives in `state`."""
    hit = touching(weakness, experiments)
    skipped = [r for r in hit if r.status == PROVES_NOTHING]
    bearing = [r for r in hit if r.status != PROVES_NOTHING]
    confirmations = _confirmations(weakness, bearing)
    completed = [r for r in bearing if r.status == CLEARING_STATUS]
    severe = _severe_findings(bearing)

    if confirmations:
        state = "confirmed"
        detail = _confirmed_detail(confirmations)
    elif completed and not severe:
        state = "cleared"
        detail = _cleared_detail(weakness, completed)
    else:
        state = "untested"
        detail = _untested_detail(weakness, skipped, bearing, severe)

    return WeaknessOutcome(
        weakness_id=weakness.weakness_id,
        category=weakness.category,
        services=list(weakness.services),
        predicted_severity=weakness.predicted_severity,
        predicted_level=weakness.predicted_level,
        state=state,
        experiments=_ids(bearing),
        confirming_findings=list(dict.fromkeys(f"{r.experiment_id}:{code}" for r, code, _ in confirmations)),
        detail=detail,
    )


def _sort_key(o: WeaknessOutcome) -> tuple[int, float, str]:
    """Confirmed first — the strongest result reads first — then worst prediction, then stable by id."""
    return (0 if o.state == "confirmed" else 1, -o.predicted_severity, o.weakness_id)


def _summary(total: int, confirmed: int, cleared: int, untested: int, considered: int) -> str:
    if total == 0:
        return "No predicted weaknesses on record; nothing to reconcile."
    head = (
        f"Predicted {total} {_plural(total, 'weakness', 'weaknesses')} from static configuration before any "
        f"experiment ran; {confirmed} {_plural(confirmed, 'was', 'were')} confirmed by live findings, "
        f"{cleared} {_plural(cleared, 'was', 'were')} cleared by experiments that ran clean, and "
        f"{untested} {_plural(untested, 'remains', 'remain')} untested"
    )
    head += " because no experiment has run yet." if considered == 0 else f" across {considered} {_plural(considered, 'experiment', 'experiments')}."
    exercised = confirmed + cleared
    if exercised > 0:
        # A hit rate for the static predictor, not a severity: it says how often a prediction survived
        # contact with a live experiment, and nothing at all about how bad the failures were.
        head += (
            f" Of the {exercised} {_plural(exercised, 'prediction', 'predictions')} an experiment actually "
            f"exercised, {confirmed / exercised:.0%} held up — that is the predictor's hit rate, not a measured severity."
        )
    return head


def reconcile(report: WeaknessReport, experiments: list[ExperimentReport], project: str) -> WeaknessReconciliation:
    considered = [r for r in experiments if r.status != PROVES_NOTHING]
    outcomes = sorted((judge(w, experiments) for w in report.weaknesses), key=_sort_key)
    counts = {state: sum(1 for o in outcomes if o.state == state) for state in ("confirmed", "cleared", "untested")}
    return WeaknessReconciliation(
        project=project,
        predicted_at=report.generated_at,
        predicted_total=len(report.weaknesses),
        confirmed=counts["confirmed"],
        cleared=counts["cleared"],
        untested=counts["untested"],
        experiments_considered=len(considered),
        outcomes=outcomes,
        summary=_summary(len(outcomes), counts["confirmed"], counts["cleared"], counts["untested"], len(considered)),
    )


# ---------------------------------------------------------------- markdown
STATE_CELL = {
    "confirmed": "**confirmed**",
    "cleared": "cleared",
    "untested": "untested",
}


def _severity_cell(o: WeaknessOutcome) -> str:
    """Never a bare number: every severity here was predicted from config, not measured in an experiment."""
    label = CONFIRMED_LABEL if o.state == "confirmed" else PREDICTED_LABEL
    return f"{o.predicted_severity:.1f}/10 {o.predicted_level} {label}"


def _cell(text: str) -> str:
    return text.replace("|", "/") or "—"


def render_markdown(rec: WeaknessReconciliation) -> str:
    predicted_at = rec.predicted_at.strftime("%Y-%m-%d %H:%M UTC") if rec.predicted_at else "unknown time"
    lines = [
        f"# Weakness reconciliation — {rec.project}",
        "",
        f"_Generated {rec.generated_at.strftime('%Y-%m-%d %H:%M UTC')} · predicted {predicted_at} · "
        f"{rec.predicted_total} predicted · **{rec.confirmed} confirmed** · {rec.cleared} cleared · "
        f"{rec.untested} untested · {rec.experiments_considered} experiment(s) considered_",
        "",
        rec.summary,
        "",
        "| State | Weakness | Category | Services | Predicted severity (not measured) | Confirming findings | Evidence |",
        "|-------|----------|----------|----------|-----------------------------------|---------------------|----------|",
    ]
    for o in rec.outcomes:
        services = ", ".join(f"`{s}`" for s in o.services) or "—"
        confirming = ", ".join(f"`{c}`" for c in o.confirming_findings) or "—"
        lines.append(
            f"| {STATE_CELL[o.state]} | `{o.weakness_id}` | {_cell(o.category)} | {services} | "
            f"{_cell(_severity_cell(o))} | {confirming} | {_cell(o.detail)} |"
        )
    if not rec.outcomes:
        lines.append("| — | — | — | — | — | — | nothing predicted |")
    lines += [
        "",
        f"Severities in this table were predicted from static configuration by the Weakness Scout before any "
        f"experiment ran; they are never the Risk agent's measured severity. {PREDICTED_LABEL} means no experiment "
        f"has yet borne on the prediction one way or the other; {CONFIRMED_LABEL} means a live experiment produced "
        f"one of the finding codes the Scout nominated as proof.",
        "",
        "Rules: **confirmed** = a bearing experiment produced a nominated finding code above `info` severity "
        "(`api_impact` at `info` means the API was unaffected). **cleared** = at least one bearing experiment "
        "completed, none produced a nominated code, and none produced a high- or critical-severity finding. "
        "**untested** = nothing bore on it, or only skipped/aborted runs did. "
        "A bearing experiment is one whose target, or whose observed collateral, is a service of the weakness.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- entry point
def run(store: ReportStore, project: str, log: Callable[[str], None] = print) -> Optional[WeaknessReconciliation]:
    report = store.weaknesses()
    if report is None:
        log("[reconcile] no reports/weaknesses.json yet; run `faultline scout` first")
        return None

    rec = reconcile(report, store.experiments(), project)
    store.write_reconciliation(rec)
    md: Path = store.root / "weaknesses_reconcile.md"
    md.write_text(render_markdown(rec))

    for o in rec.outcomes:
        log(
            f"[reconcile] {o.weakness_id}: {o.state.upper()} ({o.category}, predicted "
            f"{o.predicted_severity:.1f}/10 {o.predicted_level}, not measured) — {o.detail}"
        )
    log(
        f"[reconcile] {rec.confirmed} confirmed, {rec.cleared} cleared, {rec.untested} untested of "
        f"{rec.predicted_total} predicted across {rec.experiments_considered} experiment(s) -> {md.name}"
    )
    return rec
