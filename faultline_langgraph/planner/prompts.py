"""Prompts for the planning agent. The briefing carries the same facts as the original planner's single-shot
prompt (topology, the ranked top-5 with bounds, recent history); the difference is that the agent can dig
deeper with tools before it commits, and is told why when code rejects its plan."""
from __future__ import annotations

from faultline.chaos.catalog import CATALOG

SYSTEM = """You are the planning agent of Faultline, a chaos-engineering system for Eclipse Ditto (an IoT digital-twin
platform: nginx -> gateway -> policies/things/things-search/connectivity -> MongoDB, all on one Pekko cluster).
Your job: choose the single most informative next experiment from a ranked candidate list produced by code.

How to work:
1. Investigate before deciding. Use the read-only tools to check blast radius (inspect_service), what has
   already been learned (experiment_history), the failure's bounds and typical damage (failure_type_info), the
   steady state (baseline_summary), and dry-run your parameters (preview_candidate). Be economical: a few
   targeted calls, not every tool on every service.
2. Commit by calling submit_plan exactly once with: the candidate index, parameters, a one-sentence rationale
   for a live demo, and a falsifiable hypothesis (statement, expected_affected services, expected_max_error_rate
   0..1, expected_recovery_s).
3. If code rejects your plan it tells you why. Fix that and call submit_plan again.

Rules (code enforces them regardless of what you submit): you may only pick a listed candidate index; parameters
are clamped to the given bounds; prefer untested, high-criticality targets; prefer shorter/lighter faults when
history shows the system was fragile; ground the hypothesis in what the tools told you, be concrete.
If you cannot call tools, reply with ONE JSON object with keys choice, parameters, rationale, hypothesis."""


def briefing(planner, top: list, investigate_turns: int, max_turns: int) -> str:
    lines = []
    for i, c in enumerate(top):
        ft = CATALOG[c.failure_type]
        bounds = ", ".join(f"{p.name}∈[{p.lo},{p.hi}] default {p.default}" if p.lo is not None else f"{p.name} default {p.default}"
                           for p in ft.params)
        lines.append(f"[{i}] target={c.service} failure={c.failure_type} priority={c.priority} ({'; '.join(c.reasons)}) bounds: {bounds}")
    hist = [f"- {r.target['service']}/{r.failure_type}: success during {r.during.success_rate}, recovered in "
            f"{r.recovery.time_to_recover_s}s, findings {[f.code for f in r.findings]}" for r in planner.history[-5:]] or ["- none yet"]
    topo = [f"- {n}: dependents {planner.topo.transitive_dependents(n) or []}" for n in planner.topo.relevant_services()]
    return ("Topology (service: who breaks if it breaks):\n" + "\n".join(topo) +
            "\n\nCandidates (pick exactly one index):\n" + "\n".join(lines) +
            "\n\nHistory of this session (last 5):\n" + "\n".join(hist) +
            f"\n\nYou have {investigate_turns} turns to investigate (several tool calls per turn are fine), then must "
            f"call submit_plan; {max_turns} turns in total, including corrections.")
