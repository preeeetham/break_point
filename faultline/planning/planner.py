"""Experiment planning: "LLM proposes, code disposes".

1. Code scores every (service, failure type) pair from the dependency graph + history.
2. The model picks one of the top candidates, tunes parameters *within bounds*, writes the rationale
   and a falsifiable hypothesis.
3. Code validates the pick, clamps parameters, applies policy. If the model is unavailable or answers
   nonsense, the deterministic top candidate runs with a templated rationale.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from ..chaos.catalog import CATALOG, FailureType, applicable, clamp, reproduce_commands
from ..schemas import ExperimentReport, ExperimentSpec, Hypothesis, Topology
from .llm import LLMClient

SYSTEM = """You are the planning brain of Faultline, a chaos-engineering agent for Eclipse Ditto (an IoT digital-twin
platform: nginx -> gateway -> policies/things/things-search/connectivity -> MongoDB, all on one Pekko cluster).
You choose the single most informative next experiment from a ranked candidate list produced by code.
Rules: you may only pick a listed candidate index; parameters must stay inside the given bounds (code clamps
them anyway); prefer untested, high-criticality targets; prefer shorter/lighter when the system looked fragile
in history; be concrete. Answer with ONE JSON object:
{"choice": <index>, "parameters": {<name>: <value>}, "rationale": "<one sentence, plain English, for a live demo>",
 "hypothesis": {"statement": "<falsifiable prediction>", "expected_affected": ["<service>", ...],
                "expected_max_error_rate": <0..1>, "expected_recovery_s": <seconds>}}"""


# tie-breaker between failure types on the same target: how much a first run usually teaches us
INFORMATIVENESS = {"network_delay": 0.4, "container_crash": 0.4, "db_flood": 0.35, "network_partition": 0.35,
                   "network_loss": 0.3, "container_stop": 0.3, "container_pause": 0.2, "traffic_spike": 0.2,
                   "cpu_stress": 0.1, "disk_pressure": 0.0}


@dataclass
class Candidate:
    service: str
    failure_type: str
    priority: float
    reasons: list[str]


class Policy:
    def __init__(self, raw: dict[str, Any]):
        p = raw.get("policy", {})
        self.protected: dict[str, dict] = p.get("protected", {}) or {}
        self.skip_roles = set(p.get("skip_roles", ["ui"]))
        self.max_experiments = int(p.get("max_experiments_per_run", 10))
        self.pause_between_s = float(p.get("pause_between_experiments_s", 30))
        self.pre_window_s = float(p.get("pre_experiment_window_s", 20))
        self.min_pre_success = float(p.get("min_pre_success_rate", 0.95))
        r = raw.get("recovery", {})
        self.escalate_target_at = float(r.get("escalate_restart_target_at", 0.5))
        self.escalate_dependents_at = float(r.get("escalate_restart_dependents_at", 0.8))
        self.recovery_soak_s = float(r.get("soak_seconds", 20))

    def allows(self, service: str, failure_type: str, override: bool = False) -> tuple[bool, str]:
        prot = self.protected.get(service)
        if prot and failure_type not in prot.get("allowed_failure_types", []):
            if override:
                return True, f"policy override: {service} is protected ({prot.get('reason', '')})"
            return False, f"{service} is protected: only {prot.get('allowed_failure_types')} allowed ({prot.get('reason', '')})"
        return True, ""


class Planner:
    def __init__(self, topology: Topology, policy: Policy, history: list[ExperimentReport], llm: LLMClient | None,
                 settings, available_images: set[str] | None = None, allow_protected: bool = False):
        self.topo = topology
        self.policy = policy
        self.history = history
        self.llm = llm
        self.settings = settings
        self.images = available_images if available_images is not None else {"pumba", "tc"}
        self.allow_protected = allow_protected

    # ------------------------------------------------------------------ ranking
    def candidates(self) -> list[Candidate]:
        tested: dict[tuple[str, str], int] = {}
        for r in self.history:
            if r.status in ("completed", "aborted"):
                k = (r.target["service"], r.failure_type)
                tested[k] = tested.get(k, 0) + 1
        last_target = self.history[-1].target["service"] if self.history else None
        out: list[Candidate] = []
        for name, node in self.topo.services.items():
            if node.role in self.policy.skip_roles or not node.chaos_relevant:
                continue
            crit = self.topo.criticality_of(name)
            for ft in CATALOG.values():
                if not applicable(ft, node):
                    continue
                ok, why = self.policy.allows(name, ft.key, self.allow_protected)
                if not ok:
                    continue
                if any(req not in self.images for req in ft.requires_images):
                    continue
                reasons = [f"criticality {crit:g}"]
                n = tested.get((name, ft.key), 0)
                prio = crit * (1.5 if n == 0 else 1.0) - 1.5 * n + INFORMATIVENESS.get(ft.key, 0.0)
                reasons.append("untested" if n == 0 else f"tested {n}x")
                if ft.degrade_only:
                    prio -= 0.5
                if name == last_target:
                    prio -= 1.0
                    reasons.append("same target as last run")
                if why:
                    reasons.append(why)
                out.append(Candidate(name, ft.key, round(prio, 2), reasons))
        out.sort(key=lambda c: (-c.priority, c.service, c.failure_type))
        return out

    # ------------------------------------------------------------------ hypotheses
    def default_hypothesis(self, service: str, ft: FailureType, params: dict[str, Any]) -> Hypothesis:
        dependents = self.topo.direct_dependents(service)
        node = self.topo.services[service]
        full_outage = not ft.degrade_only
        if full_outage:
            rate = {"things-search": 0.3, "connectivity": 0.05}.get(service, 1.0)
            if node.role == "datastore":
                rate = 1.0
        else:
            rate = ft.severity_hint
        affected = dependents if full_outage else [d for d in dependents if d != "nginx"]
        d = params.get("duration_s", 30)
        verb = "taking away" if full_outage else "degrading"
        statement = (f"{verb} {service} ({ft.title.lower()}) for {d}s will push the synthetic API error rate to at most "
                     f"{rate:.0%}, impact at most {affected or ['no other services']}, and the API will recover within "
                     f"{ft.expected_recovery_s:.0f}s after rollback without manual intervention")
        return Hypothesis(statement=statement, expected_affected=affected, expected_max_error_rate=rate,
                          expected_recovery_s=ft.expected_recovery_s)

    # ------------------------------------------------------------------ planning
    def plan_next(self) -> ExperimentSpec | None:
        cands = self.candidates()
        if not cands:
            return None
        top = cands[:5]
        pick, params, rationale, hyp, by, notes = top[0], {}, "", None, "deterministic", []
        if self.llm and self.llm.available():
            answer = self._ask_llm(top)
            if answer:
                pick, params, rationale, hyp, notes = answer
                by = "llm"
            else:
                notes.append(f"LLM ({self.llm.describe()}) gave no usable answer: {self.llm.last_error or 'invalid JSON'}; deterministic choice")
        ft = CATALOG[pick.failure_type]
        params, clamp_notes = clamp(ft, params)
        notes += clamp_notes
        if not rationale:
            rationale = (f"{pick.service} ranks #{cands.index(pick) + 1} by criticality ({self.topo.criticality_of(pick.service):g}: "
                         f"{', '.join(self.topo.transitive_dependents(pick.service)) or 'no dependents'}) and "
                         f"{ft.title.lower()} has not been tested on it yet")
        hyp = hyp or self.default_hypothesis(pick.service, ft, params)
        return self._spec(pick.service, ft, params, rationale, hyp, by, notes)  # type: ignore[arg-type]

    def plan_operator(self, service: str, failure_type: str, params: dict[str, Any]) -> ExperimentSpec:
        if service not in self.topo.services:
            raise ValueError(f"unknown service {service}; known: {sorted(self.topo.services)}")
        ft = CATALOG.get(failure_type)
        if not ft:
            raise ValueError(f"unknown failure type {failure_type}; known: {sorted(CATALOG)}")
        if not applicable(ft, self.topo.services[service]):
            raise ValueError(f"{failure_type} does not apply to {service} (role {self.topo.services[service].role})")
        ok, why = self.policy.allows(service, failure_type, self.allow_protected)
        if not ok:
            raise ValueError(why + " -- pass --allow-protected to override")
        params, notes = clamp(ft, params)
        if why:
            notes.append(why)
        return self._spec(service, ft, params, f"operator-selected {ft.title.lower()} on {service}",
                          self.default_hypothesis(service, ft, params), "operator", notes)

    def _spec(self, service, ft: FailureType, params, rationale, hyp, by, notes) -> ExperimentSpec:
        node = self.topo.services[service]
        container = node.container or f"{self.settings.compose_project}-{service}-1"
        return ExperimentSpec(
            experiment_id=uuid.uuid4().hex[:12], target_service=service, container=container, failure_type=ft.key,
            parameters=params, rationale=rationale, hypothesis=hyp, criticality_score=self.topo.criticality_of(service),
            planned_by=by, planner_notes=notes,
            reproduce=reproduce_commands(ft.key, container, params, self.settings.compose_network,
                                         self.settings.pumba_image, self.settings.tc_image),
        )

    # ------------------------------------------------------------------ llm
    def _ask_llm(self, top: list[Candidate]):
        lines = []
        for i, c in enumerate(top):
            ft = CATALOG[c.failure_type]
            bounds = ", ".join(f"{p.name}∈[{p.lo},{p.hi}] default {p.default}" if p.lo is not None else f"{p.name} default {p.default}"
                               for p in ft.params)
            lines.append(f"[{i}] target={c.service} failure={c.failure_type} priority={c.priority} ({'; '.join(c.reasons)}) bounds: {bounds}")
        hist = [f"- {r.target['service']}/{r.failure_type}: success during {r.during.success_rate}, recovered in "
                f"{r.recovery.time_to_recover_s}s, findings {[f.code for f in r.findings]}" for r in self.history[-5:]] or ["- none yet"]
        topo = [f"- {n}: dependents {self.topo.transitive_dependents(n) or []}" for n in self.topo.relevant_services()]
        user = ("Topology (service: who breaks if it breaks):\n" + "\n".join(topo) +
                "\n\nCandidates (pick exactly one index):\n" + "\n".join(lines) +
                "\n\nHistory of this session:\n" + "\n".join(hist) + "\n\nReturn the JSON object now.")
        data = self.llm.chat_json(SYSTEM, user)
        if not data:
            return None
        try:
            idx = int(data.get("choice", -1))
            if not 0 <= idx < len(top):
                return None
            pick = top[idx]
            params = data.get("parameters") or {}
            if not isinstance(params, dict):
                params = {}
            rationale = str(data.get("rationale") or "").strip()[:300]
            h = data.get("hypothesis") or {}
            ft = CATALOG[pick.failure_type]
            default = self.default_hypothesis(pick.service, ft, clamp(ft, params)[0])
            known = set(self.topo.services)
            hyp = Hypothesis(
                statement=str(h.get("statement") or default.statement)[:400],
                expected_affected=[s for s in (h.get("expected_affected") or default.expected_affected) if s in known] or default.expected_affected,
                expected_max_error_rate=min(1.0, max(0.0, float(h.get("expected_max_error_rate", default.expected_max_error_rate)))),
                expected_recovery_s=min(600.0, max(1.0, float(h.get("expected_recovery_s", default.expected_recovery_s)))),
            )
            return pick, params, rationale, hyp, [f"LLM ({self.llm.describe()}) chose candidate {idx}"]
        except (TypeError, ValueError, KeyError):
            return None


def describe_candidates(cands: list[Candidate], limit: int = 12) -> str:
    rows = [f"{'#':>2}  {'priority':>8}  {'service':<14} {'failure':<18} reasons", "-" * 78]
    for i, c in enumerate(cands[:limit], 1):
        rows.append(f"{i:>2}  {c.priority:>8.2f}  {c.service:<14} {c.failure_type:<18} {'; '.join(c.reasons)}")
    return "\n".join(rows)
