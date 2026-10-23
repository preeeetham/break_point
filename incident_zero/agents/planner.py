"""Agent 5, the Remediation Planner: proposes actions from the Action API's allowed list, with a reason
(REQUIREMENTS F4.1, F4.2, S5).

The model (or the rules, by diagnosis category) only chooses. Code validates every action and parameter against
the list the API returned, the API previews each one as a dry run, and the plan is hashed exactly as the human
will see it. If nothing in the runbook fits, the plan says so and the incident escalates.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from ..actions.catalog import ActionDef, validate
from ..actions.client import ActionAPIError
from ..models import Diagnosis, Evidence, Incident, Plan, ProposedAction
from .context import Context
from .facts import extract, latest

SYSTEM = """You choose remediation actions for a diagnosed incident. Choose ONLY from the allowed actions given,
with parameters that match their definitions. Prefer the least disruptive action that removes the root cause.
Return one JSON object: {"actions": [{"action": "<name>", "parameters": {...}, "reason": "<one sentence>"}],
"no_action_reason": "<set only when no allowed action fits>"}. At most 3 actions, in execution order."""


def plan_hash(incident_id: str, d: Diagnosis, actions: list[ProposedAction]) -> str:
    shown = {"incident": incident_id, "diagnosis": d.summary,
             "actions": [{"action": a.action, "parameters": a.parameters, "reason": a.reason} for a in actions]}
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()


def rule_actions(d: Diagnosis, evidence: list[Evidence], catalog: list[ActionDef]) -> list[ProposedAction]:
    names = {a.name for a in catalog}
    facts = extract(evidence)
    out: list[ProposedAction] = []

    def add(action: str, reason: str, **params) -> bool:
        if action in names and not any(p.action == action and p.parameters == params for p in out):
            out.append(ProposedAction(action=action, parameters=params, reason=reason))
            return True
        return False

    for rc in d.root_causes:
        svc, cat = rc.component, rc.category
        c = latest(facts, "container", svc)
        r = latest(facts, "resources", svc)
        if cat == "container_down" or (cat == "dependency_failure" and c and c.values["state"] != "running"):
            if c and c.values["oom"] and c.values.get("memory_limit_mb"):
                add("update_resources", f"{svc} is OOM-killed at {c.values['memory_limit_mb']:.0f} MB; give it headroom",
                    service=svc, memory_mb=int(min(4096, c.values["memory_limit_mb"] * 2)))
            add("start_service", f"{svc} is not running; start it", service=svc) or \
                add("restart_service", f"{svc} is not running; restart it", service=svc)
        elif cat == "dependency_failure":
            add("restart_service", f"{svc} is unreachable; restart it", service=svc)
        elif cat == "network":
            if c and c.values["networks"] == "none":
                add("reconnect_network", f"{svc} is detached from the network; reattach it", service=svc)
            else:
                add("clear_network_impairment", f"latency/loss on {svc}'s interface; remove the impairment", service=svc)
        elif cat == "resource_memory":
            if c and c.values["oom"] and c.values.get("memory_limit_mb"):
                add("update_resources", f"{svc} is OOM-killed; raise its memory limit", service=svc,
                    memory_mb=int(min(4096, c.values["memory_limit_mb"] * 2)))
            add("rollback_release", f"{svc}'s memory grows without bound; roll back the release that leaks",
                service=svc) or add("restart_service", f"{svc} leaks memory; restart it to reclaim it", service=svc)
        elif cat == "resource_cpu":
            limit = c.values.get("cpu_limit") if c else None
            if r and r.values["throttled"] > 0 and limit:
                add("update_resources", f"{svc} is throttled at {limit:g} CPUs; raise its limit", service=svc,
                    cpus=float(min(4, limit * 2)))
            else:
                add("rollback_release", f"{svc} burns CPU without a limit being the cause; roll back its release",
                    service=svc) or add("restart_service", f"{svc} is CPU-bound; restart it", service=svc)
        elif cat == "application_error":
            add("rollback_release", f"{svc}'s own code fails requests; roll back to the last good release",
                service=svc) or add("restart_service", f"{svc} fails requests; restart it", service=svc)
        elif cat == "traffic":
            add("rollback_release", f"the flood enters through {svc}; roll back the change that caused it",
                service=svc)
        else:
            add("restart_service", f"{svc} is failing for an unclear reason; restart it", service=svc)
    return out


class Planner:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self, inc: Incident, d: Diagnosis, evidence: list[Evidence]) -> Plan:
        with self.ctx.status.working("planner", inc.id, "choosing a runbook action"):
            try:
                catalog = self.ctx.actions.actions()
            except ActionAPIError as exc:
                return Plan(actions=[], change_hash="", no_action_reason=f"Action API unavailable: {exc}")
            produced_by = "rules"
            actions: list[ProposedAction] = []
            if self.ctx.llm.enabled:
                actions = self._model(inc, d, evidence, catalog) or []
                produced_by = "llm" if actions else "rules"
            if not actions:
                actions = rule_actions(d, evidence, catalog)
            services = self.ctx.services() or None
            valid: list[ProposedAction] = []
            for a in actions[: self.ctx.settings.max_plan_actions]:
                errs = validate(a.action, a.parameters, catalog, services)
                if errs:
                    self.ctx.bus.publish("planner", "step", f"dropped {a.action}: {'; '.join(errs)}", incident_id=inc.id)
                    continue
                a.reversible = next((c.reversible for c in catalog if c.name == a.action), False)
                try:
                    prev = self.ctx.actions.execute(a.action, a.parameters, a.reason, dry_run=True, incident_id=inc.id)
                except ActionAPIError as exc:
                    prev = None
                    self.ctx.bus.publish("planner", "error", f"preview of {a.action} failed: {exc}", incident_id=inc.id)
                if prev is not None and not prev.ok:
                    self.ctx.bus.publish("planner", "step", f"Action API refused {a.action}: {prev.message}",
                                         incident_id=inc.id)
                    continue
                a.preview = prev.message if prev else None
                a.dry_run = self.ctx.safety.dry_run
                valid.append(a)
            reason = "" if valid else "no allowed action fits this diagnosis"
            plan = Plan(actions=valid, change_hash=plan_hash(inc.id, d, valid), produced_by=produced_by,  # type: ignore[arg-type]
                        no_action_reason=reason)
        for a in valid:
            self.ctx.bus.publish("planner", "step", f"proposes {a.action} {json.dumps(a.parameters)}: {a.reason}",
                                 incident_id=inc.id, detail={"preview": a.preview})
        return plan

    def _model(self, inc: Incident, d: Diagnosis, evidence: list[Evidence],
               catalog: list[ActionDef]) -> Optional[list[ProposedAction]]:
        facts = [f.line for f in extract(evidence) if f.kind in ("container", "resources")
                 and f.service in {r.component for r in d.root_causes}][-6:]
        user = (f"Diagnosis: {d.model_dump_json(include={'summary', 'root_causes'})}\n\nContainer facts:\n"
                + "\n".join(facts) + "\n\nAllowed actions:\n" + json.dumps([a.model_dump() for a in catalog], indent=1))
        data = self.ctx.llm.json(SYSTEM, user, max_tokens=800)
        if not data or not isinstance(data.get("actions"), list):
            return None
        out = []
        for a in data["actions"]:
            if isinstance(a, dict) and a.get("action") and a.get("reason"):
                out.append(ProposedAction(action=str(a["action"]), parameters=dict(a.get("parameters") or {}),
                                          reason=str(a["reason"])[:300]))
        return out or None
