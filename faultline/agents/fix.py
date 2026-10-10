"""Fix agent. Rules propose concrete Ditto remediations; the LLM may add up to two schema-validated extras.
Everything is advisory: nothing is ever applied to the running stack."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import ValidationError

from ..planning.llm import OllamaClient
from ..reporting.store import ReportStore
from ..schemas import ExperimentReport, FixRecommendation, Recommendation, RiskAssessment
from . import risk as risk_agent
from .knowledge import matching_rules

MAX_RULE_RECS = 6
MAX_LLM_EXTRAS = 2

LLM_SYSTEM = (
    "You are a senior SRE reviewing a chaos experiment on an Eclipse Ditto 3.9 docker-compose deployment "
    "(nginx -> gateway -> policies/things/things-search/connectivity -> single MongoDB). "
    "Given the experiment facts and the remediations already proposed, suggest at most two ADDITIONAL, concrete, "
    "non-overlapping remediations. Do not repeat existing titles. Be specific to Ditto/Docker/nginx/MongoDB. "
    'Reply with JSON only: {"additional": [{"title": str, "category": "configuration|architecture|observability|operations|code", '
    '"effort": "S|M|L", "confidence": 0..1, "rationale": str, "steps": [str], "verify": [str]}], "priority_note": str}.'
)


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def _evidence_sentence(report: ExperimentReport, risk: Optional[RiskAssessment]) -> str:
    rec = report.recovery
    ttr = f"{rec.time_to_recover_s:.0f} s" if rec.time_to_recover_s is not None else "no recovery"
    bits = [f"API success fell to {_pct(report.during.success_rate)} during injection", f"TTR {ttr}"]
    if rec.remediation_actions:
        bits.append("manual remediation was required")
    titles = [f.title for f in report.findings if f.severity in ("high", "critical")][:2]
    if titles:
        bits.append("findings: " + "; ".join(titles))
    level = f" (risk {risk.level.upper()} {risk.severity:.1f}/10)" if risk else ""
    return f"In experiment {report.experiment_id}{level}, " + ", ".join(bits) + "."


def _from_rule(rule: dict[str, Any], evidence: str) -> Recommendation:
    body = dict(rule["recommendation"])
    body["rationale"] = f"{evidence} {body['rationale']}"
    return Recommendation.model_validate(body)


def rule_recommendations(report: ExperimentReport, risk: Optional[RiskAssessment]) -> list[Recommendation]:
    evidence = _evidence_sentence(report, risk)
    return [_from_rule(r, evidence) for r in matching_rules(report)[:MAX_RULE_RECS]]


def _llm_extras(
    llm: Optional[OllamaClient], report: ExperimentReport, risk: Optional[RiskAssessment], existing: list[Recommendation]
) -> tuple[list[Recommendation], Optional[str]]:
    if llm is None or not llm.available():
        return [], None
    payload = {
        "target": risk.target_service if risk else report.target,
        "failure_type": report.failure_type,
        "parameters": report.parameters,
        "success_rate": {"baseline": report.baseline.success_rate, "during": report.during.success_rate, "after": report.after.success_rate},
        "p95_ms": {"baseline": report.baseline.p95_ms, "during": report.during.p95_ms},
        "recovery": report.recovery.model_dump(mode="json"),
        "affected_services": report.affected_services_observed,
        "findings": [{"code": f.code, "severity": f.severity, "title": f.title} for f in report.findings],
        "risk_summary": risk.summary if risk else "",
        "existing_recommendation_titles": [r.title for r in existing],
    }
    data = llm.chat_json(LLM_SYSTEM, json.dumps(payload, default=str), max_tokens=1200)
    if not data:
        return [], None
    taken = {r.title.strip().lower() for r in existing}
    extras: list[Recommendation] = []
    for item in data.get("additional") or []:
        if not isinstance(item, dict):
            continue
        item.setdefault("references", [])
        item["rationale"] = f"[LLM suggestion, unverified] {item.get('rationale', '')}".strip()
        try:
            rec = Recommendation.model_validate(item)
        except ValidationError:
            continue
        if rec.title.strip().lower() in taken or not rec.steps:
            continue
        rec.confidence = min(rec.confidence, 0.6)
        taken.add(rec.title.strip().lower())
        extras.append(rec)
        if len(extras) >= MAX_LLM_EXTRAS:
            break
    note = data.get("priority_note")
    return extras, (note.strip() if isinstance(note, str) and note.strip() else None)


def recommend(report: ExperimentReport, risk: Optional[RiskAssessment], llm: Optional[OllamaClient]) -> FixRecommendation:
    recs = rule_recommendations(report, risk)
    extras, _note = _llm_extras(llm, report, risk, recs)
    return FixRecommendation(
        experiment_id=report.experiment_id,
        target_service=risk.target_service if risk else risk_agent.target_service(report),
        failure_type=report.failure_type,
        risk_level=risk.level if risk else "unknown",
        recommendations=recs + extras,
        advisory_only=True,
        generated_by="llm+rules" if extras else "rules",
    )


def render_markdown(fix: FixRecommendation, risk: Optional[RiskAssessment]) -> str:
    lines = [
        f"# Fix recommendations — experiment `{fix.experiment_id}`",
        "",
        f"**Target:** `{fix.target_service}`  **Failure:** `{fix.failure_type}`  **Generated by:** {fix.generated_by}  ",
        "**Advisory only — nothing was applied.**",
        "",
    ]
    if risk:
        lines += [f"**Risk:** {risk.level.upper()} ({risk.severity:.1f}/10) — {risk.summary}", "", f"_{risk.production_implication}_", ""]
    if not fix.recommendations:
        lines.append("No matching recommendations.")
    for i, r in enumerate(fix.recommendations, 1):
        lines += [
            f"## {i}. {r.title}",
            "",
            f"`{r.category}` · effort **{r.effort}** · confidence {r.confidence:.0%}",
            "",
            r.rationale,
            "",
            "**Steps**",
            "",
            *[f"{n}. {s}" for n, s in enumerate(r.steps, 1)],
            "",
            "**Verify**",
            "",
            *[f"- {v}" for v in r.verify],
            "",
        ]
        if r.references:
            lines += ["**References**", "", *[f"- {ref}" for ref in r.references], ""]
    return "\n".join(lines).rstrip() + "\n"


def _write_markdown(store: ReportStore, fix: FixRecommendation, risk: Optional[RiskAssessment]) -> Path:
    path = store.root / "fixes" / f"{fix.experiment_id}.md"
    path.write_text(render_markdown(fix, risk))
    return path


def pending_ids(store: ReportStore, only: Optional[list[str]], force: bool) -> list[str]:
    ids = store.experiment_ids()
    if only:
        wanted = set(only)
        ids = [i for i in ids if i in wanted]
    if force:
        return ids
    return [i for i in ids if store.fix(i) is None]


def run(
    store: ReportStore,
    llm: Optional[OllamaClient],
    only: Optional[list[str]] = None,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> list[FixRecommendation]:
    topology = store.topology()
    out: list[FixRecommendation] = []
    for eid in pending_ids(store, only, force):
        report = store.experiment(eid)
        if report is None:
            log(f"[fix] {eid}: unreadable report, skipped")
            continue
        risk = store.risk(eid) or risk_agent.assess(report, topology, None)
        fix = recommend(report, risk, llm)
        store.write_fix(fix)
        md = _write_markdown(store, fix, risk)
        out.append(fix)
        top = fix.recommendations[0].title if fix.recommendations else "none"
        log(f"[fix] {eid}: {len(fix.recommendations)} recommendation(s) ({fix.generated_by}); top: {top} -> {md.name}")
    if not out:
        log("[fix] nothing to recommend")
    return out
