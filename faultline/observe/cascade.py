"""Cascade attribution: an ordered, per-hop account of how a failure moved through the dependency
graph, built from samples the probes already take. Purely observational and deterministic: it never
changes what the injector does, and no LLM decides a path or a mechanism.

Two evidence sources, because one of them lies:

* the health probe (/alive, /ready, docker state, every `health_interval_s`). In every real Ditto run
  only the injected target ever flipped; dependents kept answering 200 while serving nothing
  (Ditto #1865). So health onsets alone would show no cascade at all.
* the API probe (~4 req/s through nginx -> gateway -> things / things-search). Its routes are in
  `probes.OP_ROUTES`; a service on the route of an operation that failed is implicated, but only if
  the topology says it transitively depends on the origin (a failed request does not prove it ever
  reached a service that has nothing to do with the broken one).

Timing is always an interval: the failure began after the last good observation and no later than the
first bad one. Two services whose intervals overlap are reported as `same_interval`, never as an order.
All times in the output are seconds relative to `injected_at`, rounded to 0.1 s.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Optional

from ..schemas import (
    CascadeAttribution,
    CascadeEdge,
    CascadeHop,
    HealthTransition,
    OperationSignal,
    Topology,
    WindowStats,
)
from .probes import OP_ROUTES, HealthSample
from .window import Sample, percentile

# Plain-English verb phrase per mechanism. `{upstream}` is filled with the hop's upstream service.
MECHANISM_TEXT: dict[str, str] = {
    "injected_fault": "had the fault injected",
    "process_down": "went down (container not running)",
    "process_paused": "froze (container paused)",
    "liveness_failed": "failed its liveness check (/alive, or mongosh ping for MongoDB)",
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
    seen = "first seen slow" if hop.mechanism == "latency_degraded" else "first seen failing"
    when = (f"at {_s(hop.onset_latest_s)}" if hop.onset_earliest_s == hop.onset_latest_s
            else f"between {_s(hop.onset_earliest_s)} and {_s(hop.onset_latest_s)}")
    out = f"{hop.service} {verb}, {seen} {when}"
    if hop.phase == "after_rollback":
        out += " (after the fault had been rolled back)"
    if hop.path_confidence == "ordered" and hop.upstream:
        # A single lag figure is only honest when this hop's onset is a point; otherwise the interval
        # above already says when, and a number here would claim more precision than the samples have.
        exact = hop.lag_s is not None and hop.onset_earliest_s == hop.onset_latest_s
        lag = f"{hop.lag_s:.1f}s after {hop.upstream}" if exact else f"after {hop.upstream}"
        out += f", {lag} (path {flow(hop.path)})"
    elif hop.path_confidence == "same_interval" and hop.upstream:
        out += (f"; within the same observation interval as {hop.upstream}, so which failed first cannot be told "
                f"(path {flow(hop.path)})")
    elif hop.path_confidence == "ambiguous":
        others = [flow(p) for p in hop.candidate_paths if p != hop.path]
        shown = "; ".join(others[:2]) + (f"; and {len(others) - 2} more" if len(others) > 2 else "")
        out += f"; most likely via {hop.upstream} (path {flow(hop.path)}), but the timing also fits {shown}"
    elif hop.path_confidence == "no_graph_path":
        out += f"; topology.json has no dependency route from it to {origin}: an undeclared dependency or a coincidence"
    if hop.masked_by_health_check:
        out += "; its own health check stayed green throughout"
    if hop.beyond_hypothesis:
        out += " [beyond the hypothesis]"
    return out


# ---------------------------------------------------------------------------------------------- rules
MAX_PATH_NODES = 6
SIGNIFICANT_MIN_FAILURES = 2
SIGNIFICANT_MIN_FRACTION = 0.10
MINORITY_FRACTION = 0.25           # below this share of requests failing -> elevated_error_rate
DOMINANT_MECHANISM_SHARE = 0.60    # one mechanism must explain this share of failures to be named
LATENCY_FACTOR = 2.0
LATENCY_MIN_EXTRA_MS = 200.0
LATENCY_MIN_SLOW_REQUESTS = 2      # same noise bar as failures: one slow request is not a symptom

AMBIGUITY_RULE = ("the most likely path is chosen by: fewest hops, then the upstream whose own onset is latest "
                  "without being after this service's, then alphabetical order")


def _r(x: float) -> float:
    return round(x, 1) + 0.0  # + 0.0 turns -0.0 into 0.0


def _health_state(s: HealthSample) -> str:
    if s.status == "paused":
        return "process_paused"
    if s.status != "running":
        return "process_down"
    if s.alive is not None and s.alive != 200:
        return "liveness_failed"
    if s.ready is not None and s.ready != 200:
        return "readiness_failed"
    return "healthy"


def _health_detail(s: HealthSample) -> str:
    parts = []
    if s.status != "running":
        parts.append(f"docker={s.status}")
    if s.alive is not None:
        parts.append(f"alive={s.alive}")
    if s.ready is not None:
        parts.append(f"ready={s.ready}")
    if s.docker_health:
        parts.append(f"docker_health={s.docker_health}")
    return " ".join(parts) or "running, no health endpoint"


_CONTAINER_LEVEL = {"process_down", "process_paused"}


def _kind(s: Sample) -> str:
    if s.status == 0:
        return s.error or "error"
    return str(s.status)


def _kind_mechanism(kind: str) -> str:
    if kind in ("timeout", "504"):
        return "dependency_timeout"
    if kind in ("connection_error", "502"):
        return "dependency_unreachable"
    if kind in ("401", "403"):
        return "auth_failure"
    if kind.isdigit() and 500 <= int(kind) < 600:
        return "dependency_error"
    return "elevated_error_rate"


def _kinds_text(kinds: dict[str, int]) -> str:
    return ", ".join(f"{k}×{n}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))


@dataclass
class _Onset:
    earliest: float                 # relative to injection, rounded
    latest: float
    source: str                     # injection | health_probe | api_probe
    mechanism: str
    evidence: str
    masked: bool = False
    notes: list[str] = field(default_factory=list)
    no_graph_path_op: Optional[str] = None   # set for rule 4(c) hops


@dataclass
class _OpOnset:
    op: str
    latest: float
    earliest: float
    mechanism: str
    failures: int
    evidence: str


# ---------------------------------------------------------------------------------------------- API
def _operations(api: list[Sample], injected_at: float, window_end: float, services: set[str],
                baseline: Optional[WindowStats]
                ) -> tuple[list[OperationSignal], dict[str, _OpOnset], list[str], dict[str, float]]:
    """OperationSignals for every op seen in the window, the significant ones with their onset, noise
    notes, and the typical gap between consecutive requests per op (the timing resolution of API onsets)."""
    by_op: dict[str, list[Sample]] = {}
    for s in api:
        if injected_at <= s.ts <= window_end:
            by_op.setdefault(s.op, []).append(s)
    signals: list[OperationSignal] = []
    significant: dict[str, _OpOnset] = {}
    notes: list[str] = []
    gaps: dict[str, float] = {}
    for op in sorted(by_op):
        items = sorted(by_op[op], key=lambda s: s.ts)
        route = [svc for svc in OP_ROUTES.get(op, []) if svc in services]
        fails = [s for s in items if not s.ok]
        kinds: dict[str, int] = {}
        for s in fails:
            kinds[_kind(s)] = kinds.get(_kind(s), 0) + 1
        if len(items) > 1:
            gaps[op] = median(b.ts - a.ts for a, b in zip(items, items[1:]))
        first_fail = fails[0] if fails else None
        last_ok_rel: Optional[float] = None
        if first_fail is not None:
            oks_before = [s for s in items if s.ok and s.ts < first_fail.ts]
            last_ok_rel = max(0.0, _r(oks_before[-1].ts - injected_at)) if oks_before else 0.0
        n, f = len(items), len(fails)
        mechanism: Optional[str] = None
        evidence = ""
        if f:
            mech_count: dict[str, int] = {}
            for k, c in kinds.items():
                m = _kind_mechanism(k)
                mech_count[m] = mech_count.get(m, 0) + c
            dominant_kind = max(kinds.items(), key=lambda kv: (kv[1], kv[0]))[0]
            top_mech, top_n = max(mech_count.items(), key=lambda kv: (kv[1], kv[0]))
            if f / n < MINORITY_FRACTION:
                mechanism = "elevated_error_rate"
                evidence = f"only {f}/{n} requests failed (mostly {dominant_kind})"
            elif top_n / f >= DOMINANT_MECHANISM_SHARE:
                mechanism = top_mech
                evidence = f"{f}/{n} requests failed ({_kinds_text(kinds)})"
            else:
                mechanism = "elevated_error_rate"
                evidence = f"{f}/{n} requests failed with no dominant kind ({_kinds_text(kinds)})"
        sig = OperationSignal(
            op=op, route=route, requests=n, failures=f,
            first_failure_s=_r(first_fail.ts - injected_at) if first_fail else None,
            last_ok_before_failure_s=last_ok_rel, error_kinds=kinds, mechanism=mechanism)
        if f >= SIGNIFICANT_MIN_FAILURES and f / n >= SIGNIFICANT_MIN_FRACTION:
            significant[op] = _OpOnset(op, sig.first_failure_s, last_ok_rel or 0.0, mechanism, f,
                                       f"{op} {evidence}, first failure at {_s(sig.first_failure_s)}, "
                                       f"last success before it at {_s(last_ok_rel or 0.0)}")
        else:
            if f:
                notes.append(f"{op}: {f} failure(s) out of {n} requests ({_kinds_text(kinds)}) is below the significance "
                             f"bar (≥{SIGNIFICANT_MIN_FAILURES} failures and ≥{SIGNIFICANT_MIN_FRACTION:.0%} of requests); "
                             f"treated as noise, no hop created from it.")
            lat = _latency_onset(op, items, injected_at, baseline)
            if lat is not None:
                sig.mechanism = "latency_degraded"
                significant[op] = lat
        signals.append(sig)
    return signals, significant, notes, gaps


def _latency_onset(op: str, items: list[Sample], injected_at: float,
                   baseline: Optional[WindowStats]) -> Optional[_OpOnset]:
    if baseline is None:
        return None
    try:
        base_p95 = ((baseline.by_operation or {}).get(op) or {}).get("p95_ms")
        if base_p95 is None:
            return None
        base_p95 = float(base_p95)
    except (TypeError, ValueError, AttributeError):
        return None
    ok = [s for s in items if s.ok]
    p95 = percentile([s.latency_ms for s in ok], 95)
    bound = max(LATENCY_FACTOR * base_p95, base_p95 + LATENCY_MIN_EXTRA_MS)
    if p95 is None or p95 <= bound:
        return None
    slow = [s for s in ok if s.latency_ms > bound]
    if len(slow) < LATENCY_MIN_SLOW_REQUESTS:
        return None
    first = slow[0]
    before = [s for s in items if s.ts < first.ts]
    earliest = max(0.0, _r(before[-1].ts - injected_at)) if before else 0.0
    latest = _r(first.ts - injected_at)
    return _OpOnset(op, latest, earliest, "latency_degraded", 0,
                    f"{op} p95 {p95:.0f} ms vs baseline {base_p95:.0f} ms (bound {bound:.0f} ms), "
                    f"{len(slow)}/{len(ok)} successful requests above it, first slow request at {_s(latest)}")


# ---------------------------------------------------------------------------------------------- paths
def _paths(adj: dict[str, list[str]], src: str, dst: str) -> list[list[str]]:
    """Every simple dependency path src -> ... -> dst of at most MAX_PATH_NODES nodes."""
    out: list[list[str]] = []

    def walk(path: list[str]) -> None:
        cur = path[-1]
        if cur == dst:
            out.append(list(path))
            return
        if len(path) >= MAX_PATH_NODES:
            return
        for nxt in adj.get(cur, []):
            if nxt not in path:
                path.append(nxt)
                walk(path)
                path.pop()

    walk([src])
    return out


def _timing_ok(path: list[str], onsets: dict[str, _Onset]) -> bool:
    """A dependent cannot have PROVABLY failed before its dependency (checked between consecutive
    affected nodes; an unaffected intermediate has no onset to compare)."""
    aff = [n for n in path if n in onsets]
    return all(not (onsets[a].latest < onsets[b].earliest) for a, b in zip(aff, aff[1:]))


def _strictly_ordered(path: list[str], onsets: dict[str, _Onset]) -> bool:
    aff = [n for n in path if n in onsets]
    return all(onsets[a].earliest >= onsets[b].latest for a, b in zip(aff, aff[1:]))


# ---------------------------------------------------------------------------------------------- main
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
    """Deterministic hop-by-hop attribution. Never raises on odd input (empty samples, unknown target,
    unknown ops); see the module docstring for what the evidence can and cannot say."""
    services = set(topology.services)
    expected = set(expected_affected or [])
    notes: list[str] = []

    def rel(ts: float) -> float:
        return _r(ts - injected_at)

    # ---- 1. health timeline
    per_svc: dict[str, list[HealthSample]] = {}
    for s in health_samples or []:
        if s.service in services:
            per_svc.setdefault(s.service, []).append(s)
    for items in per_svc.values():
        items.sort(key=lambda s: s.ts)
    transitions: list[HealthTransition] = []
    for svc in sorted(per_svc):
        prev = None
        for s in per_svc[svc]:
            st = _health_state(s)
            if st != prev:
                transitions.append(HealthTransition(service=svc, t_plus_s=rel(s.ts), state=st, detail=_health_detail(s)))
                prev = st
    transitions.sort(key=lambda t: (t.t_plus_s, t.service))

    pre_existing: set[str] = set()
    for svc, items in sorted(per_svc.items()):
        before = [s for s in items if s.ts < injected_at]
        if before and _health_state(before[-1]) != "healthy":
            if svc == target:
                notes.append(f"{target} was already {_health_state(before[-1])} before the injection "
                             f"(at {_s(rel(before[-1].ts))}); its onset is still reported as the injection.")
            else:
                pre_existing.add(svc)
                notes.append(f"{svc} was already {_health_state(before[-1])} before the injection "
                             f"(last sample before it, at {rel(before[-1].ts):.1f}s); excluded from the cascade.")

    def in_window(svc: str) -> list[HealthSample]:
        return [s for s in per_svc.get(svc, []) if injected_at <= s.ts <= window_end]

    # ---- 2. health onsets (non-origin): a persistent or container-level unhealthy state
    health_onsets: dict[str, _Onset] = {}
    for svc, items in per_svc.items():
        if svc == target or svc in pre_existing:
            continue
        for i, s in enumerate(items):
            if s.ts < injected_at or s.ts > window_end:
                continue
            st = _health_state(s)
            if st == "healthy":
                continue
            nxt = items[i + 1] if i + 1 < len(items) else None
            if not (st in _CONTAINER_LEVEL or nxt is None or _health_state(nxt) != "healthy"):
                continue  # a one-tick blip that cleared on the next sample
            prev_ts = items[i - 1].ts if i > 0 else None
            earliest = max(prev_ts, injected_at) if prev_ts is not None else injected_at
            health_onsets[svc] = _Onset(
                earliest=rel(earliest), latest=rel(s.ts), source="health_probe", mechanism=st,
                evidence=f"health probe saw {st} at {_s(rel(s.ts))} ({_health_detail(s)})"
                         + (f", previous sample at {rel(prev_ts):.1f}s" if prev_ts is not None else ""))
            break

    # ---- 3. API operations
    operations, sig_ops, op_notes, gaps = _operations(api_samples or [], injected_at, window_end, services, baseline)
    notes.extend(op_notes)

    # ---- origin
    target_known = target in services
    if not target_known:
        notes.append(f"{target} is not in topology.json, so no dependency path can be matched; origin-only attribution.")
    first_bad = next((s for s in per_svc.get(target, []) if s.ts >= injected_at and _health_state(s) != "healthy"), None)
    if first_bad is not None:
        origin_ev = f"health probe saw {_health_state(first_bad)} at {_s(rel(first_bad.ts))}"
    elif per_svc.get(target):
        origin_ev = "its own health check never showed a failure during the window"
        through = [o for o in sig_ops.values() if target in OP_ROUTES.get(o.op, [])]
        if through:
            origin_ev += "; traffic through it: " + "; ".join(o.evidence for o in sorted(through, key=lambda o: o.op))
    else:
        origin_ev = "no health samples of it were taken during the window"
    t_items = in_window(target)
    if target_known and 0 < len(t_items) < 3:
        notes.append(f"Only {len(t_items)} health sample(s) of {target} were taken after the injection "
                     f"(the run ended early), so health-based timing here is coarse.")
    onsets: dict[str, _Onset] = {target: _Onset(0.0, 0.0, "injection", "injected_fault", origin_ev)}

    # ---- 4. affected set
    if target_known:
        dependents = set(topology.transitive_dependents(target))

        def masked(svc: str) -> bool:
            has_endpoint = any(s.alive is not None for s in per_svc.get(svc, []))
            win = in_window(svc)
            return has_endpoint and bool(win) and all(_health_state(s) == "healthy" for s in win)

        def api_onset(svc: str, ops: list[_OpOnset]) -> _Onset:
            best = min(ops, key=lambda o: (o.latest, -o.failures, o.op))
            ordered_ops = sorted(ops, key=lambda o: (o.latest, o.op))
            return _Onset(earliest=min(best.earliest, best.latest), latest=best.latest, source="api_probe",
                          mechanism=best.mechanism,
                          evidence="traffic through it failed: " + "; ".join(o.evidence for o in ordered_ops),
                          masked=masked(svc))

        # (a) health onset and/or (b) on the route of a significant op AND a dependent of the origin
        for svc in sorted(services):
            if svc == target or svc in pre_existing:
                continue
            h = health_onsets.get(svc)
            ops = [o for o in sig_ops.values() if svc in OP_ROUTES.get(o.op, []) and svc in dependents]
            a = api_onset(svc, ops) if ops else None
            if h and a:
                use = a if a.latest < h.latest else h
                onsets[svc] = _Onset(earliest=use.earliest, latest=use.latest, source=use.source,
                                     mechanism=h.mechanism, evidence=h.evidence + "; " + a.evidence, masked=False)
            elif h:
                onsets[svc] = h
            elif a:
                onsets[svc] = a

        # (c) a significant op the graph cannot explain: nothing on its route depends on the origin
        for o in sorted(sig_ops.values(), key=lambda o: o.op):
            route = [svc for svc in OP_ROUTES.get(o.op, []) if svc in services]
            if target in route or any(svc in dependents for svc in route):
                continue
            if not route:
                notes.append(f"{o.op} failed significantly but its route through the system is unknown; "
                             f"no service can be implicated from it.")
                continue
            last = route[-1]
            if last in pre_existing:
                continue
            msg = (f"{o.op} failed although nothing on its route ({' → '.join(route)}) depends on {target} "
                   f"in topology.json")
            if last in onsets:
                onsets[last].notes.append(msg + ".")
                continue
            on = api_onset(last, [o])
            on.no_graph_path_op = o.op
            on.notes.append(msg + ": a possible undeclared dependency.")
            onsets[last] = on
            notes.append(msg + f"; attributed to {last} with no graph path (possible undeclared dependency).")

    # ---- 5. path matching along real dependency edges (src depends on dst)
    adj: dict[str, list[str]] = {}
    edge_kinds: dict[tuple[str, str], set[str]] = {}
    for e in topology.edges:
        edge_kinds.setdefault((e.src, e.dst), set()).add(e.kind)
        if e.kind != "observed" and e.dst not in adj.setdefault(e.src, []):
            adj[e.src].append(e.dst)
    for k in adj:
        adj[k].sort()

    def rank(path: list[str], me: _Onset) -> tuple:
        v1 = onsets.get(path[1])
        if v1 is not None and v1.latest <= me.latest:
            return (len(path), 0, -v1.latest, path)
        return (len(path), 1, 0.0, path)

    revert_rel = _r(revert_at - injected_at)
    hops: list[CascadeHop] = []
    unattributed: list[str] = []
    for svc, on in onsets.items():
        if svc == target:
            hops.append(CascadeHop(
                service=target, onset_earliest_s=0.0, onset_latest_s=0.0, onset_source="injection",
                mechanism="injected_fault", mechanism_evidence=on.evidence, path=[target], candidate_paths=[[target]],
                path_confidence="origin", hops_from_origin=0, note=" ".join(on.notes)))
            continue
        hop_notes = list(on.notes)
        all_paths = _paths(adj, svc, target) if on.no_graph_path_op is None else []
        cands = [p for p in all_paths if all(n in onsets for n in p[1:-1]) and _timing_ok(p, onsets)]
        if not cands:
            cands = [p for p in all_paths if _timing_ok(p, onsets)]
            for n in sorted({n for p in cands for n in p[1:-1] if n not in onsets}):
                hop_notes.append(f"Passes through {n}, which " + (
                    "was already unhealthy before the injection." if n in pre_existing
                    else "showed no symptom of its own."))
        upstream: Optional[str] = None
        lag: Optional[float] = None
        kinds: list[str] = []
        path: list[str] = []
        if not cands:
            conf = "no_graph_path"
            unattributed.append(svc)
            if on.no_graph_path_op is None:
                hop_notes.append(f"No dependency path from {svc} to {target} in topology.json "
                                 + ("fits the timing." if all_paths else "exists at all."))
        else:
            cands.sort(key=lambda p: rank(p, on))
            path = cands[0]
            if len(cands) > 1:
                conf = "ambiguous"
                hop_notes.append(f"{len(cands)} graph paths fit the timing; {AMBIGUITY_RULE}.")
            else:
                conf = "ordered" if _strictly_ordered(path, onsets) else "same_interval"
            upstream = path[1]
            kinds = sorted(edge_kinds.get((svc, upstream), set()))
            if upstream in onsets:
                lag = _r(on.latest - onsets[upstream].latest)
        hops.append(CascadeHop(
            service=svc, onset_earliest_s=on.earliest, onset_latest_s=on.latest, onset_source=on.source,
            # Only when it provably began after the revert: an onset interval straddling it is "during".
            phase="after_rollback" if on.earliest >= revert_rel else "during_fault",
            mechanism=on.mechanism, mechanism_evidence=on.evidence, masked_by_health_check=on.masked,
            upstream=upstream, edge_kinds=kinds, lag_s=lag, path=path, candidate_paths=cands, path_confidence=conf,
            hops_from_origin=len(path) - 1 if path else None,
            beyond_hypothesis=svc not in expected and svc != target, note=" ".join(hop_notes)))

    # ---- 6. assemble
    hops.sort(key=lambda h: (h.path_confidence != "origin", h.onset_latest_s or 0.0, h.service))
    edges = [CascadeEdge(src=h.service, dst=h.upstream, mechanism=h.mechanism, confidence=h.path_confidence, lag_s=h.lag_s)
             for h in hops if h.path_confidence != "origin" and h.upstream]
    beyond = sorted(h.service for h in hops if h.beyond_hypothesis)
    hbh = max((h.hops_from_origin or 0 for h in hops if h.beyond_hypothesis), default=0)

    if len(hops) == 1:
        summary = f"The failure stayed at {target}: no other service showed a symptom."
    else:
        summary = ". ".join(describe_hop(h, target) for h in hops) + "."

    gap_txt = ""
    if gaps:
        lo, hi = min(gaps.values()), max(gaps.values())
        gap_txt = f" (here about {lo:.1f} s" + (f" to {hi:.1f} s" if _r(hi) != _r(lo) else "") + ")"
    notes.append(f"Timing resolution: a health-probe onset is only known to within one polling interval "
                 f"({health_interval_s:.1f} s); an API onset to within the gap between consecutive probe requests of "
                 f"that operation{gap_txt}. Onsets whose intervals overlap are reported as same_interval, never as an order.")
    notes.append("Route attribution uses the probe's fixed operation routes (OP_ROUTES): it says traffic through a "
                 "service failed, not that the service's own health check failed, and implicates a service only if "
                 f"topology.json says it depends on {target}.")
    masked_names = [h.service for h in hops if h.masked_by_health_check]
    if masked_names:
        notes.append(f"Health checks stayed green while traffic through them failed: {', '.join(masked_names)} "
                     f"(cf. Ditto #1865).")

    first_health = min((s.ts for items in per_svc.values() for s in items), default=injected_at)
    return CascadeAttribution(
        origin=target, health_interval_s=health_interval_s,
        window_start_s=min(0.0, rel(first_health)), window_end_s=rel(window_end),
        hops=hops, edges=edges, transitions=transitions, operations=operations,
        hops_beyond_hypothesis=hbh, beyond_hypothesis=beyond, unattributed=sorted(unattributed),
        summary=summary, notes=notes)
