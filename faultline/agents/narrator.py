"""Narrator agent. Plain-English play-by-play of each experiment from the blackboard, optionally watching live."""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Callable, Optional

from ..observe.cascade import describe_hop
from ..planning.llm import OllamaClient
from ..reporting.store import ReportStore
from ..schemas import ExperimentReport, FixRecommendation, RiskAssessment
from . import fix as fix_agent
from . import risk as risk_agent
from .knowledge import target_service

WATCH_INTERVAL_S = 3.0

LLM_SYSTEM = (
    "You are narrating a chaos-engineering experiment on an Eclipse Ditto deployment for an engineering audience. "
    "Write one paragraph of two or three plain-English sentences: what was done, what users experienced, and what it means. "
    "Use only the facts provided; no numbers that are not in the input; no bullet points; no headings."
)

# Appended to LLM_SYSTEM only when the report carries a cascade attribution.
LLM_CASCADE_RULE = (
    " The cascade facts are established and deterministic: you may rephrase them, but do not change the order of "
    "the hops, do not change any mechanism, and do not present a hop as certain unless its confidence is "
    "\"ordered\"; for \"same_interval\" or \"ambiguous\" hops keep the hedge (say which failed first cannot be "
    "told, or name the alternative path). An ordered hop may be told plainly in cause-and-effect form, e.g. "
    "\"policies failed, which took gateway down with it 4 seconds later because gateway's requests timed out "
    "waiting on policies\", using only the lag and mechanism its sentence states."
)

VERBS = {
    "container_crash": "killed",
    "container_stop": "stopped",
    "container_pause": "paused",
    "network_delay": "added latency to",
    "network_loss": "dropped packets on",
    "network_partition": "partitioned",
    "cpu_stress": "CPU-stressed",
    "disk_pressure": "filled the disk of",
    "db_flood": "flooded the database behind",
    "traffic_spike": "spiked traffic at",
}


def _clock(dt: Optional[datetime]) -> str:
    return dt.astimezone().strftime("%H:%M:%S") if dt else "--:--:--"


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def _ms(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.0f}"


def _short(eid: str) -> str:
    return eid[:8]


def _header(report: ExperimentReport) -> str:
    verb = VERBS.get(report.failure_type, "disrupted")
    params = ", ".join(f"{k}={v}" for k, v in (report.parameters or {}).items())
    params = f" [{params}]" if params else ""
    line = f"[{_clock(report.started_at)}] Experiment {_short(report.experiment_id)} — {verb} {target_service(report)} ({report.failure_type}){params}."
    if report.rationale:
        line += f" Why: {report.rationale.strip()}"
    return line


def _timeline(report: ExperimentReport) -> list[str]:
    lines = []
    for ev in report.timeline or []:
        detail = f" — {ev.detail.strip()}" if ev.detail else ""
        lines.append(f"  [+{ev.t_plus_s:.1f}s] {ev.event}{detail}")
    return lines


def _impact(report: ExperimentReport) -> str:
    b, d, a = report.baseline, report.during, report.after
    line = f"  Impact: success {_pct(b.success_rate)} → {_pct(d.success_rate)}, p95 {_ms(b.p95_ms)} → {_ms(d.p95_ms)} ms"
    if a.success_rate is not None:
        line += f"; after: {_pct(a.success_rate)} / p95 {_ms(a.p95_ms)} ms"
    if report.affected_services_observed:
        line += f". Affected: {', '.join(report.affected_services_observed)}"
    return line


def _cascade(report: ExperimentReport) -> list[str]:
    """One header line, one line per hop (describe_hop, verbatim), and a timing caveat when a hop's
    order could not be resolved. Empty when the report has no cascade attribution."""
    c = report.cascade
    if c is None:
        return []
    beyond = f"; beyond the hypothesis: {', '.join(c.beyond_hypothesis)}" if c.beyond_hypothesis else ""
    lines = [f"  Cascade: from {c.origin}, in order of failure{beyond}"]
    lines += [f"    {describe_hop(hop, c.origin)}" for hop in c.hops]
    if any(h.path_confidence in ("same_interval", "ambiguous") for h in c.hops):
        lines.append(f"    Timing: health is polled every {c.health_interval_s:g}s, so onsets inside one interval "
                     "cannot be ordered; hops marked so are likely, not certain.")
    return lines


def _recovery(report: ExperimentReport) -> str:
    rec = report.recovery
    if not rec.recovered:
        how = f"did NOT recover; actions tried: {', '.join(rec.remediation_actions)}" if rec.remediation_actions else "did NOT recover within the window"
        return f"  Recovery: {how}"
    ttr = f"{rec.time_to_recover_s:.0f} s" if rec.time_to_recover_s is not None else "unknown"
    if rec.remediation_actions:
        how = f"manual intervention ({', '.join(rec.remediation_actions)})"
    elif rec.auto_restarted_by_docker:
        how = "auto-restarted by Docker"
    else:
        how = "self-healed, no intervention"
    line = f"  Recovery: {ttr}, {how}"
    if rec.outage_duration_s is not None:
        line += f"; outage {rec.outage_duration_s:.0f} s"
    if rec.data_integrity_ok is False:
        line += "; DATA INTEGRITY CHECK FAILED"
    return line


def _hypothesis(report: ExperimentReport) -> str:
    hr = report.hypothesis_result
    verdict = "held" if hr.held else ("violated" if hr.held is False else "inconclusive")
    line = f"  Hypothesis: {verdict} — \"{report.hypothesis.statement}\""
    extras = []
    if hr.observed_max_error_rate is not None:
        extras.append(f"max error rate {hr.observed_max_error_rate:.0%} (expected ≤ {report.hypothesis.expected_max_error_rate:.0%})")
    if hr.observed_recovery_s is not None:
        extras.append(f"recovery {hr.observed_recovery_s:.0f} s (expected ≤ {report.hypothesis.expected_recovery_s:.0f} s)")
    if hr.unexpected_affected:
        extras.append(f"unexpected: {', '.join(hr.unexpected_affected)}")
    if hr.notes:
        extras.append(hr.notes.strip())
    return line + (" | " + "; ".join(extras) if extras else "")


def _safety(report: ExperimentReport) -> Optional[str]:
    s = report.safety
    if not s.aborted_early:
        return None
    who = "watchdog" if s.watchdog_fired else ("operator" if s.operator_stop else "guardian")
    return f"  Safety: aborted early by {who} — {s.abort_reason or 'threshold breached'}"


def _findings(report: ExperimentReport) -> list[str]:
    return [f"  Finding [{f.severity.upper()}] {f.title} ({f.code})" for f in report.findings]


def _risk(risk: Optional[RiskAssessment]) -> str:
    if risk is None:
        return "  Risk: not yet assessed"
    return f"  Risk: {risk.level.upper()} ({risk.severity:.1f}/10) — {risk.summary}"


def _fix(fix: Optional[FixRecommendation]) -> str:
    if fix is None or not fix.recommendations:
        return "  Top fix: not yet available"
    top = fix.recommendations[0]
    return f"  Top fix: {top.title} [{top.category}, effort {top.effort}]"


def _llm_paragraph(llm: Optional[OllamaClient], report: ExperimentReport, risk: Optional[RiskAssessment], fix: Optional[FixRecommendation]) -> Optional[str]:
    if llm is None or not llm.available():
        return None
    facts = {
        "target": target_service(report),
        "failure_type": report.failure_type,
        "status": report.status,
        "success_rate": {"baseline": report.baseline.success_rate, "during": report.during.success_rate, "after": report.after.success_rate},
        "p95_ms": {"baseline": report.baseline.p95_ms, "during": report.during.p95_ms},
        "recovery": report.recovery.model_dump(mode="json"),
        "hypothesis_held": report.hypothesis_result.held,
        "findings": [f.title for f in report.findings],
        "risk": {"level": risk.level, "severity": risk.severity} if risk else None,
        "top_fix": fix.recommendations[0].title if fix and fix.recommendations else None,
    }
    system = LLM_SYSTEM
    if report.cascade is not None:
        c = report.cascade
        facts["cascade"] = {
            "summary": c.summary,
            "hops_in_order": [{"sentence": describe_hop(h, c.origin), "confidence": h.path_confidence} for h in c.hops],
        }
        system += LLM_CASCADE_RULE
    text = llm.complete(system, json.dumps(facts, default=str), max_tokens=220)
    if not text or len(text) > 900:
        return None
    return "  " + " ".join(text.split())


def narrate(
    report: ExperimentReport,
    risk: Optional[RiskAssessment],
    fix: Optional[FixRecommendation],
    llm: Optional[OllamaClient],
    use_llm: bool,
) -> list[str]:
    lines = [_header(report)]
    if report.status == "skipped":
        lines.append(f"  Skipped: {report.error or 'no injection performed'}")
        return lines
    lines += _timeline(report)
    lines.append(_impact(report))
    lines += _cascade(report)
    lines.append(_recovery(report))
    lines.append(_hypothesis(report))
    safety = _safety(report)
    if safety:
        lines.append(safety)
    lines += _findings(report)
    lines.append(_risk(risk))
    lines.append(_fix(fix))
    if report.status == "failed" and report.error:
        lines.append(f"  Error: {report.error}")
    if use_llm:
        para = _llm_paragraph(llm, report, risk, fix)
        if para:
            lines.append(para)
    return lines


def narrate_sections(
    report: ExperimentReport,
    risk: Optional[RiskAssessment],
    fix: Optional[FixRecommendation],
    llm: Optional[OllamaClient],
    use_llm: bool,
) -> dict:
    """The same play-by-play as `narrate()`, one key per section, for JSON consumers. Strings are
    stripped of the console indent; absent sections are "" / [] / None."""
    out = {"header": _header(report), "timeline": [], "impact": "", "cascade": [], "recovery": "", "hypothesis": "",
           "safety": None, "findings": [], "risk": "", "fix": "", "error": None, "llm": None}
    if report.status == "skipped":
        out["impact"] = f"Skipped: {report.error or 'no injection performed'}"
        return out
    out["timeline"] = [t.strip() for t in _timeline(report)]
    out["impact"] = _impact(report).strip()
    out["cascade"] = [c.strip() for c in _cascade(report)]
    out["recovery"] = _recovery(report).strip()
    out["hypothesis"] = _hypothesis(report).strip()
    safety = _safety(report)
    out["safety"] = safety.strip() if safety else None
    out["findings"] = [f.strip() for f in _findings(report)]
    out["risk"] = _risk(risk).strip()
    out["fix"] = _fix(fix).strip()
    if report.status == "failed" and report.error:
        out["error"] = report.error
    if use_llm:
        para = _llm_paragraph(llm, report, risk, fix)
        out["llm"] = para.strip() if para else None
    return out


def _narrate_id(store: ReportStore, eid: str, llm: Optional[OllamaClient], use_llm: bool, log: Callable[[str], None]) -> None:
    report = store.experiment(eid)
    if report is None:
        return
    risk = store.risk(eid)
    fix = store.fix(eid)
    if risk is None:
        risk = risk_agent.assess(report, store.topology(), None)
    if fix is None:
        fix = fix_agent.recommend(report, risk, None)
    for line in narrate(report, risk, fix, llm, use_llm):
        log(line)
    log("")


def _live_line(live: dict) -> Optional[str]:
    phase = live.get("phase") or live.get("state")
    if not phase:
        return None
    target = live.get("target") or live.get("target_service") or live.get("service") or ""
    ftype = live.get("failure_type") or ""
    eid = live.get("experiment_id") or ""
    tag = f" ({_short(eid)})" if eid else ""
    phase_l = str(phase).lower()
    if "inject" in phase_l or "running" in phase_l:
        return f"▶ injecting {ftype} into {target} …{tag}"
    if "recover" in phase_l:
        return f"↺ recovering {target} after {ftype} …{tag}"
    if "baseline" in phase_l or "settl" in phase_l:
        return f"◦ measuring baseline{tag}"
    if "abort" in phase_l:
        return f"■ aborted {ftype} on {target}{tag}"
    if "done" in phase_l or "complete" in phase_l or "idle" in phase_l:
        return f"✓ {phase_l}{tag}"
    return f"· {phase_l} {ftype} {target}{tag}".rstrip()


def run(
    store: ReportStore,
    llm: Optional[OllamaClient],
    use_llm: bool,
    watch: bool,
    log: Callable[[str], None] = print,
) -> None:
    seen: set[str] = set()
    for eid in store.experiment_ids():
        _narrate_id(store, eid, llm, use_llm, log)
        seen.add(eid)
    if not seen:
        log("No experiments yet." + (" Watching …" if watch else ""))
    if not watch:
        return

    last_phase: Optional[str] = None
    try:
        while True:
            live = store.live() or {}
            phase = live.get("phase") or live.get("state")
            if phase != last_phase:
                line = _live_line(live)
                if line:
                    log(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")
                last_phase = phase
            for eid in store.experiment_ids():
                if eid not in seen:
                    _narrate_id(store, eid, llm, use_llm, log)
                    seen.add(eid)
            time.sleep(WATCH_INTERVAL_S)
    except KeyboardInterrupt:
        log("narrator stopped.")
