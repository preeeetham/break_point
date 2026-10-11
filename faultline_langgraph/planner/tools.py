"""What the planning agent may look at before it commits: read-only views over the blackboard (topology,
history, baseline) and the failure catalog, plus `submit_plan`, the one tool that ends its turn.

None of these can change anything. They read from the existing Planner instance (built exactly the way
`faultline plan` builds it), so the agent sees the same topology, policy and history the ranking used.
"""
from __future__ import annotations

import json
import math
from typing import Any

from langchain_core.tools import StructuredTool, tool
from pydantic import BaseModel, Field, field_validator

from faultline.chaos.catalog import CATALOG, clamp
from faultline.schemas import Baseline, Hypothesis

SUBMIT = "submit_plan"
HISTORY_LIMIT = 10


class PlanProposal(BaseModel):
    """Commit to the next experiment. Code validates it against the candidate list and clamps the parameters."""

    choice: int = Field(description="index of the chosen candidate, 0-4, from the ranked list")
    parameters: dict[str, Any] = Field(default_factory=dict,
                                       description="failure parameters by name, inside the candidate's bounds")
    rationale: str = Field(description="one sentence, plain English, for a live demo: why this experiment next")
    hypothesis: Hypothesis = Field(description="falsifiable prediction; expected_affected are service names")

    @field_validator("parameters")
    @classmethod
    def _finite(cls, v: dict[str, Any]) -> dict[str, Any]:
        # NaN slips through catalog.clamp() (it fails both bound comparisons), so it is stopped here; other
        # non-numbers are left to clamp(), which notes them and uses the default, as for the original planner
        for name, raw in v.items():
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(val):
                raise ValueError(f"{name}={raw!r} is not a finite number")
        return v


def _dump(data: Any) -> str:
    return json.dumps(data, default=str, sort_keys=True)


def build_tools(planner, top: list, baseline: Baseline | None) -> tuple[list[StructuredTool], StructuredTool]:
    """Returns (investigation tools, submit tool). `top` is the ranked top-5 the agent must choose from."""
    topo = planner.topo
    last_target = planner.history[-1].target["service"] if planner.history else None

    @tool
    def inspect_service(service: str) -> str:
        """Role, image, criticality, who depends on it (directly and transitively), what it depends on, and any
        protection policy for one service. Use it to judge blast radius before choosing."""
        node = topo.services.get(service)
        if node is None:
            return _dump({"error": f"unknown service {service!r}", "known": sorted(topo.services)})
        prot = planner.policy.protected.get(service)
        return _dump({
            "service": service, "role": node.role, "image": node.image, "chaos_relevant": node.chaos_relevant,
            "criticality": topo.criticality_of(service),
            "direct_dependents": topo.direct_dependents(service),
            "transitive_dependents": topo.transitive_dependents(service),
            "depends_on": topo.dependencies(service),
            "protected": ({"allowed_failure_types": prot.get("allowed_failure_types", []), "reason": prot.get("reason", "")}
                          if prot else None),
            "was_last_target": service == last_target,
        })

    @tool
    def experiment_history(service: str = "") -> str:
        """Past experiments this session, most recent last: target, failure, status, API success rate during the
        fault, recovery time, whether the hypothesis held, findings. Pass a service name to filter, or "" for all."""
        rows = [r for r in planner.history if not service or r.target.get("service") == service][-HISTORY_LIMIT:]
        return _dump([{
            "service": r.target.get("service"), "failure_type": r.failure_type, "status": r.status,
            "success_rate_during": r.during.success_rate, "time_to_recover_s": r.recovery.time_to_recover_s,
            "hypothesis_held": r.hypothesis_result.held, "findings": [f.code for f in r.findings],
        } for r in rows] or "no experiments yet" + (f" on {service}" if service else ""))

    @tool
    def failure_type_info(failure_type: str) -> str:
        """What a failure type does, its parameters with [min, max] bounds and defaults, whether it only degrades
        (vs takes the target away), and its typical damage and recovery time."""
        ft = CATALOG.get(failure_type)
        if ft is None:
            return _dump({"error": f"unknown failure type {failure_type!r}", "known": sorted(CATALOG)})
        return _dump({
            "failure_type": ft.key, "title": ft.title, "description": ft.description, "degrade_only": ft.degrade_only,
            "typical_max_error_rate": ft.severity_hint, "typical_recovery_s": ft.expected_recovery_s,
            "parameters": [{"name": p.name, "default": p.default, "min": p.lo, "max": p.hi, "unit": p.unit} for p in ft.params],
        })

    @tool
    def baseline_summary() -> str:
        """Steady-state API health measured before any chaos, and the abort thresholds the safety layer enforces."""
        if baseline is None:
            return "no baseline captured yet; safety uses floor thresholds"
        return _dump({
            "success_rate": baseline.api.success_rate, "p95_ms": baseline.api.p95_ms,
            "abort_error_rate_above": baseline.thresholds.abort_error_rate_above,
            "abort_p95_ms_above": baseline.thresholds.abort_p95_ms_above,
            "health_availability": baseline.health_availability,
        })

    @tool
    def preview_candidate(choice: int, parameters: dict[str, Any] | None = None) -> str:
        """Dry-run a choice: the parameters after code clamps them to bounds, and the hypothesis code would
        predict by default. Use it to check your parameters before submitting."""
        if not 0 <= choice < len(top):
            return _dump({"error": f"choice must be 0..{len(top) - 1}"})
        c = top[choice]
        ft = CATALOG[c.failure_type]
        params, notes = clamp(ft, parameters or {})
        return _dump({"service": c.service, "failure_type": c.failure_type, "clamped_parameters": params,
                      "clamp_notes": notes, "default_hypothesis": planner.default_hypothesis(c.service, ft, params).model_dump()})

    submit = StructuredTool.from_function(
        func=lambda **_: "received; validating", name=SUBMIT, args_schema=PlanProposal,
        description=PlanProposal.__doc__,
    )
    return [inspect_service, experiment_history, failure_type_info, baseline_summary, preview_candidate], submit
