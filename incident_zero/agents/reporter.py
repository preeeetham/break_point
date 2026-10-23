"""Agent 9, the Reporter (REQUIREMENTS F7.1, F7.2).

Code builds every fact in the report from the recorded incident (timeline from events, root cause from the audited
diagnosis, evidence from the ledger, action/approval/execution/verification from their records) and validates it
against the jury's schema. The model may only reword the summary and suggest prevention; if it is unavailable,
prevention comes from rules per root-cause category.
"""
from __future__ import annotations

import json

from ..models import Event, Evidence, Incident
from ..report_schema import validate
from .context import Context

PREVENTION = {
    "container_down": ["Give {c} a restart policy and a readiness healthcheck so it is restarted and taken out of "
                       "rotation automatically", "Alert on container state and restart count, not only on error rate"],
    "dependency_failure": ["Give {c}'s callers timeouts, retries with backoff and a circuit breaker",
                           "Run {c} with redundancy (a replica or managed service) and alert on its own health"],
    "resource_memory": ["Bound {c}'s in-process caches (size limit and eviction) and alert at 80% of its memory limit",
                        "Load-test {c} for memory growth before release"],
    "resource_cpu": ["Size {c}'s CPU limit from load tests and alert on CPU throttling",
                     "Profile {c}'s hot path; gate releases on CPU regression"],
    "network": ["Set client timeouts on calls to {c} below the user-facing SLO",
                "Alert on client-to-server latency gaps between services, which isolate the network"],
    "application_error": ["Release {c} behind a canary that is rolled back automatically on error-rate regression",
                          "Add a contract test for the failing operation of {c}"],
    "traffic": ["Rate-limit the entry point ({c}) per client and autoscale the frontend on request rate"],
    "unknown": ["Add per-service alerting on {c}'s error ratio and latency against a learned baseline"],
}
TIMELINE_KINDS = ("alert", "phase", "approval", "action", "verify", "report")
APPROVAL_MAP = {"approved": "approved", "rejected": "rejected", "pending": "pending", "timed_out": "pending",
                "sent_back": "pending", "not_requested": "not_requested"}


def build_report(inc: Incident, evidence: list[Evidence], events: list[Event]) -> dict:
    d = inc.diagnosis
    rcs = sorted(d.root_causes, key=lambda r: -r.confidence) if d else []
    main = rcs[0] if rcs else None
    cited = []
    for rc in rcs:
        for c in rc.claims:
            if c.evidence_id not in cited:
                cited.append(c.evidence_id)
    alert_ev = [e.id for e in evidence if e.source == "sentinel" and e.id not in cited]
    by_id = {e.id: e for e in evidence}
    quotes = {c.evidence_id: c.quote for rc in rcs for c in rc.claims}
    ev_out = []
    for eid in cited + alert_ev:
        e = by_id.get(eid)
        if e is None:
            continue
        q = e.args.get("query") or (f"{e.tool}({', '.join(f'{k}={v}' for k, v in e.args.items() if v is not None)})")
        ev_out.append({"id": e.id, "source": e.source, "query": str(q),
                       "detail": (quotes.get(e.id) or e.summary)[:500]})
    timeline = [{"timestamp": ev.ts, "actor": ev.agent, "event": ev.title[:300]}
                for ev in events if ev.kind in TIMELINE_KINDS][:40]
    first = inc.plan.actions[0] if inc.plan and inc.plan.actions else None
    executed = inc.execution.results
    report = {
        "incident_id": inc.id,
        "title": inc.title,
        "status": inc.phase if inc.terminal else "open",
        "detected_at": inc.created_at,
        "resolved_at": inc.resolved_at,
        "summary": inc.report_summary or (d.summary if d else "No diagnosis was reached."),
        "timeline": timeline,
        "root_cause": {
            "component": main.component if main else "unknown",
            "category": main.category if main else "unknown",
            "description": main.cause if main else (inc.escalation_reason or "not established"),
            "confidence": main.confidence if main else 0.0,
            "additional_causes": [{"component": r.component, "category": r.category, "description": r.cause,
                                   "confidence": r.confidence} for r in rcs[1:]],
        },
        "evidence": ev_out,
        "proposed_action": ({"action": first.action, "parameters": first.parameters, "reason": first.reason,
                             "dry_run": bool(executed[0].dry_run) if executed else first.dry_run}
                            if first else {"action": "none", "parameters": {},
                                           "reason": (inc.plan.no_action_reason if inc.plan else "no plan was made"),
                                           "dry_run": True}),
        "approval": {"required": True, "decision": APPROVAL_MAP[inc.approval.decision],
                     "approver": inc.approval.approver, "timestamp": inc.approval.timestamp},
        "execution": {
            "attempted": inc.execution.attempted,
            "action_api_only": True,
            "action_result": "; ".join(f"{r.action}: {'ok' if r.ok else 'failed'} - {r.message}" for r in executed)
                             or None,
            "rollback_performed": inc.execution.rollback_performed,
            "notes": "; ".join(x for x in [
                inc.execution.notes,
                (f"plan had {len(inc.plan.actions)} actions: " + ", ".join(a.action for a in inc.plan.actions))
                if inc.plan and len(inc.plan.actions) > 1 else "",
                "; ".join(f"rollback {r.execution_id}: {r.message}" for r in inc.execution.rollback_results)] if x),
        },
        "verification": {"checked": inc.verification.checked, "recovered": inc.verification.recovered,
                         "signals_checked": inc.verification.signals_checked, "notes": inc.verification.notes},
        "prevention": inc.prevention,
        "limitations": inc.limitations,
    }
    validate(report)
    return report


def to_markdown(r: dict) -> str:
    rc = r["root_cause"]
    lines = [f"# {r['incident_id']}: {r['title']}", "", f"**Status:** {r['status']}  ",
             f"**Detected:** {r['detected_at']}  ", f"**Resolved:** {r['resolved_at'] or '-'}", "", r["summary"], "",
             "## Root cause", f"**{rc['component']}** ({rc['category']}, confidence {rc['confidence']:.2f}): "
             f"{rc['description']}"]
    for c in rc.get("additional_causes", []):
        lines.append(f"- also **{c['component']}** ({c['category']}, {c['confidence']:.2f}): {c['description']}")
    lines += ["", "## Evidence"] + [f"- `{e['id']}` {e['source']} — `{e['query']}`\n  > {e['detail']}"
                                    for e in r["evidence"]]
    pa, ap, ex, ve = r["proposed_action"], r["approval"], r["execution"], r["verification"]
    lines += ["", "## Action", f"- Proposed: `{pa['action']}` {json.dumps(pa.get('parameters', {}))} — {pa['reason']}"
              f" (dry run: {str(pa['dry_run']).lower()})",
              f"- Approval: {ap['decision']} by {ap['approver'] or '-'} at {ap['timestamp'] or '-'}",
              f"- Execution: {ex['action_result'] or 'not executed'}; rollback performed: "
              f"{str(ex['rollback_performed']).lower()}" + (f"; {ex['notes']}" if ex.get('notes') else ""),
              f"- Verification: checked {str(ve['checked']).lower()}, recovered {ve['recovered']}; {ve['notes']}",
              "", "## Timeline"] + [f"- {t['timestamp']} **{t['actor']}** {t['event']}" for t in r["timeline"]]
    lines += ["", "## Prevention"] + [f"- {p}" for p in r["prevention"]]
    if r["limitations"]:
        lines += ["", "## Limitations"] + [f"- {x}" for x in r["limitations"]]
    return "\n".join(lines) + "\n"


class Reporter:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self, inc: Incident) -> dict:
        with self.ctx.status.working("reporter", inc.id, "writing the incident report"):
            if not inc.prevention:
                inc.prevention = self._prevention(inc)
            if self.ctx.llm.enabled and inc.diagnosis and not inc.report_summary:
                data = self.ctx.llm.json(
                    "Write a concise incident summary (max 3 sentences) for an on-call report, using only the facts "
                    "given. Return {\"summary\": \"...\"}.",
                    json.dumps({"title": inc.title, "diagnosis": inc.diagnosis.summary, "phase": inc.phase,
                                "actions": [r.model_dump(include={"action", "ok", "message", "dry_run"})
                                            for r in inc.execution.results],
                                "verification": inc.verification.model_dump(include={"recovered", "notes"})}),
                    max_tokens=300)
                if data and isinstance(data.get("summary"), str):
                    inc.report_summary = data["summary"][:800]
            report = build_report(inc, self.ctx.store.evidence(inc.id), self.ctx.store.events(incident_id=inc.id))
            self.ctx.store.write_report(inc.id, report, to_markdown(report))
        self.ctx.bus.publish("reporter", "report", f"report written for {inc.id} ({report['status']})",
                             incident_id=inc.id)
        return report

    def _prevention(self, inc: Incident) -> list[str]:
        out: list[str] = []
        for rc in (inc.diagnosis.root_causes if inc.diagnosis else []):
            for p in PREVENTION.get(rc.category, PREVENTION["unknown"]):
                text = p.format(c=rc.component)
                if text not in out:
                    out.append(text)
        return out or ["Review alert coverage: this incident could not be diagnosed from the available telemetry"]
