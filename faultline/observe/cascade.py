"""Cascade attribution: an ordered, per-hop account of how a failure moved through the dependency
graph, built from samples the probes already take. Purely observational and deterministic: it never
changes what the injector does, and no LLM decides a path or a mechanism.

CONTRACT STUB — `attribute()` is being implemented. Its signature, MECHANISM_TEXT and describe_hop()
are fixed; consumers (injector, markdown, risk, narrator, dashboard) may rely on them now.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..schemas import CascadeAttribution, CascadeHop, Topology, WindowStats
from .probes import HealthSample
from .window import Sample

# Plain-English verb phrase per mechanism. `{upstream}` is filled with the hop's upstream service.
MECHANISM_TEXT: dict[str, str] = {
    "injected_fault": "had the fault injected",
    "process_down": "went down (container not running)",
    "process_paused": "froze (container paused)",
    "liveness_failed": "stopped answering /alive",
    "readiness_failed": "kept answering /alive but failed /ready (a subsystem failed)",
    "dependency_timeout": "had requests time out waiting on {upstream}",
    "dependency_unreachable": "could not connect to {upstream}",
    "dependency_error": "answered with server errors while {upstream} was unavailable",
    "auth_failure": "rejected requests as unauthorised",
    "elevated_error_rate": "returned errors on part of its traffic",
    "latency_degraded": "slowed down without failing",
}


def flow(path: list[str]) -> str:
    """`path` runs dependent -> ... -> origin; failures flow the other way, so print it reversed."""
    return " → ".join(reversed(path))


def _s(x: Optional[float]) -> str:
    return "?" if x is None else f"+{x:.1f}s"


def describe_hop(hop: CascadeHop, origin: str) -> str:
    """One deterministic sentence per hop. The Narrator and the Markdown report use this verbatim;
    an LLM may rephrase the result but never re-derive it."""
    verb = MECHANISM_TEXT.get(hop.mechanism, hop.mechanism).format(upstream=hop.upstream or "its dependency")
    if hop.path_confidence == "origin":
        return f"{hop.service} {verb} at +0.0s ({hop.mechanism_evidence})"
    when = (f"at {_s(hop.onset_latest_s)}" if hop.onset_earliest_s == hop.onset_latest_s
            else f"between {_s(hop.onset_earliest_s)} and {_s(hop.onset_latest_s)}")
    out = f"{hop.service} {verb}, first seen failing {when}"
    if hop.path_confidence == "ordered" and hop.upstream:
        lag = f"{hop.lag_s:.1f}s after {hop.upstream}" if hop.lag_s is not None else f"after {hop.upstream}"
        out += f", {lag} (path {flow(hop.path)})"
    elif hop.path_confidence == "same_interval" and hop.upstream:
        out += (f"; within the same observation interval as {hop.upstream}, so which failed first cannot be told "
                f"(path {flow(hop.path)})")
    elif hop.path_confidence == "ambiguous":
        others = [flow(p) for p in hop.candidate_paths if p != hop.path]
        out += f"; most likely via {hop.upstream} (path {flow(hop.path)}), but the timing also fits " + "; ".join(others)
    elif hop.path_confidence == "no_graph_path":
        out += f"; topology.json has no dependency route from it to {origin}: an undeclared dependency or a coincidence"
    if hop.masked_by_health_check:
        out += "; its own health check stayed green throughout"
    if hop.beyond_hypothesis:
        out += " [beyond the hypothesis]"
    return out


def attribute(
    *,
    topology: Topology,
    target: str,
    expected_affected: Iterable[str],
    health_samples: list[HealthSample],
    api_samples: list[Sample],
    injected_at: float,
    revert_at: float,
    window_end: float,
    health_interval_s: float = 3.0,
    baseline: Optional[WindowStats] = None,
) -> CascadeAttribution:
    raise NotImplementedError
