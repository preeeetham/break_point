"""Agent 5, the Remediation Planner: proposes actions from the Action API's allowed list, with a reason
(REQUIREMENTS F4.1, F4.2, S5).

The model (or the rules, by diagnosis category) only chooses. Code validates every action and parameter against
the list the API returned, the API previews each one as a dry run, and the plan is hashed exactly as the human
will see it. If nothing in the runbook fits, the plan says so and the incident escalates.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import yaml

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


RUNBOOK_FILE = Path(__file__).resolve().parents[1] / "runbook.yaml"


def load_runbook(path: Path = RUNBOOK_FILE) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except OSError:
        return {}


def rule_actions(d: Diagnosis, evidence: list[Evidence], catalog: list[ActionDef], runbook: Optional[dict] = None,
                 normal_users: int = 5) -> tuple[list[ProposedAction], list[str]]:
    """(actions, notes). Notes explain root causes no allowed action can fix (they become the escalation reason)."""
    names = {a.name for a in catalog}
    rb = runbook if runbook is not None else load_runbook()
    flags_of = {k: list(v or []) for k, v in (rb.get("service_flags") or {}).items()}
    facts = extract(evidence)
    out: list[ProposedAction] = []
    notes: list[str] = []

    def add(action: str, reason: str, **params) -> bool:
        if action in names and not any(p.action == action and p.parameters == params for p in out):
            out.append(ProposedAction(action=action, parameters=params, reason=reason))
            return True
        return False

    def flags_off(svc: str, why: str) -> bool:
        added = False
        for f in flags_of.get(svc, []):
            added = add("toggle_feature_flag", f"{why}: switch off {f}, which guards that code path", flag=f,
                        enabled=False) or added
        return added

    for rc in d.root_causes:
        svc, cat = rc.component, rc.category
        c = latest(facts, "container", svc)
        r = latest(facts, "resources", svc)
        cpu_limit = c.values.get("cpu_limit") if c else None
        mem_limit = c.values.get("memory_limit_mb") if c else None
        if cat == "network" and c and c.values["networks"] == "none":
            notes.append(f"{svc} is detached from the network; no allowed action reattaches it")
        elif cat in ("container_down", "dependency_failure", "network") and not (
                c and c.values["oom"] and mem_limit and mem_limit <= rb.get("starved_memory_limit_mb", 64)):
            what = {"network": "its network interface carries injected latency/loss; a restart resets it",
                    "container_down": "it is not running", "dependency_failure": "its dependents cannot reach it"}[cat]
            add("restart_service", f"{svc}: {what}", service=svc)
        elif cat in ("resource_memory", "container_down") and mem_limit and mem_limit <= rb.get("starved_memory_limit_mb", 64):
            notes.append(f"{svc} is OOM-killed under a {mem_limit:.0f} MB limit; no allowed action raises limits")
        elif cat == "resource_memory":
            flags_off(svc, f"{svc}'s memory grows without bound")
            add("restart_service", f"{svc} has leaked memory; restart it to reclaim it", service=svc)
        elif cat == "resource_cpu":
            if r and r.values["throttled"] > 0 and cpu_limit and cpu_limit <= rb.get("starved_cpu_limit", 0.25):
                notes.append(f"{svc} is throttled under a {cpu_limit:g}-CPU limit; no allowed action raises limits"
                             + ("; scale_service needs Kubernetes" if "scale_service" in names else ""))
            elif not flags_off(svc, f"{svc} burns CPU in its own code"):
                add("restart_service", f"{svc} is CPU-bound; restart it", service=svc)
        elif cat == "application_error":
            if not flags_off(svc, f"{svc}'s own code fails requests"):
                add("restart_service", f"{svc} fails requests; restart it", service=svc)
        elif cat == "traffic":
            add("throttle_load_generator", f"a request flood enters through {svc}; bring the load generator back to "
                f"its normal {normal_users} users", users=normal_users)
            flags_off("load-generator", "the load generator floods the homepage")
        else:
            add("restart_service", f"{svc} is failing for an unclear reason; restart it", service=svc)
    return out, notes


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
            notes: list[str] = []
            if self.ctx.llm.enabled:
                actions = self._model(inc, d, evidence, catalog) or []
                produced_by = "llm" if actions else "rules"
            if not actions:
                actions, notes = rule_actions(d, evidence, catalog, normal_users=self.ctx.settings.normal_load_users)
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
            reason = "" if valid else ("; ".join(notes) or "no allowed action fits this diagnosis")
            if valid and notes:
                inc.limitations.extend(notes)
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
        rb = load_runbook()
        guards = {s: rb.get("service_flags", {}).get(s) for s in {r.component for r in d.root_causes}}
        user = (f"Diagnosis: {d.model_dump_json(include={'summary', 'root_causes'})}\n\nContainer facts:\n"
                + "\n".join(facts)
                + "\n\nFlags that guard each diagnosed service's code paths (from the demo's source; their current "
                  f"values are unknown and must not be assumed): {json.dumps(guards)}"
                + f"\nThe load generator's normal user count is {self.ctx.settings.normal_load_users}."
                + "\nIf a root cause is a resource limit or a detached network, no allowed action fixes it: say so in "
                  "no_action_reason.\n\nAllowed actions:\n" + json.dumps([a.model_dump() for a in catalog], indent=1))
        data = self.ctx.llm.json(SYSTEM, user, max_tokens=800)
        if not data or not isinstance(data.get("actions"), list):
            return None
        out = []
        for a in data["actions"]:
            if isinstance(a, dict) and a.get("action") and a.get("reason"):
                out.append(ProposedAction(action=str(a["action"]), parameters=dict(a.get("parameters") or {}),
                                          reason=str(a["reason"])[:300]))
        return out or None
