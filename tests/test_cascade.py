"""Cascade attribution: offline, synthetic probe samples shaped like the 13 real Ditto runs."""
from __future__ import annotations

import json
from typing import Callable, Optional

from faultline.observe.cascade import attribute, describe_hop
from faultline.observe.probes import HealthSample, ThingsApiProbe
from faultline.observe.window import Sample
from faultline.schemas import (
    CASCADE_MECHANISMS,
    PATH_CONFIDENCE,
    CascadeAttribution,
    Edge,
    ExperimentReport,
    ServiceNode,
    Topology,
    WindowStats,
)

T0 = 1000.0  # injected_at
HEALTH_PORT_SERVICES = {"policies", "things", "things-search", "connectivity", "gateway"}


# ---------------------------------------------------------------- topologies
def ditto_topology() -> Topology:
    """Same services and edges as the real reports/topology.json for the Ditto deployment."""
    roles = {"mongodb": "datastore", "nginx": "proxy", "ditto-ui": "ui"}
    names = ["mongodb", "policies", "things", "things-search", "connectivity", "gateway", "ditto-ui", "nginx"]
    services = {n: ServiceNode(name=n, image="mongo:7.0" if n == "mongodb" else f"img/{n}", role=roles.get(n, "app"),
                               health_port=7626 if n in HEALTH_PORT_SERVICES else None,
                               chaos_relevant=n != "ditto-ui") for n in names}
    raw = [
        ("things", "policies", "depends_on"), ("things-search", "policies", "depends_on"),
        ("connectivity", "policies", "depends_on"), ("gateway", "policies", "depends_on"),
        ("nginx", "gateway", "depends_on"),
        ("policies", "mongodb", "env"), ("things", "mongodb", "env"), ("things-search", "mongodb", "env"),
        ("connectivity", "mongodb", "env"),
        ("nginx", "gateway", "proxy"), ("nginx", "ditto-ui", "proxy"),
        ("gateway", "policies", "documented"), ("gateway", "things", "documented"),
        ("gateway", "things-search", "documented"), ("gateway", "connectivity", "documented"),
        ("things", "policies", "documented"), ("things-search", "policies", "documented"),
        ("things-search", "things", "documented"), ("connectivity", "things", "documented"),
        ("connectivity", "policies", "documented"),
    ]
    edges = [Edge(src=s, dst=d, kind=k) for s, d, k in raw]
    return Topology(project="bosch-ditto", services=services, edges=edges, criticality=[])


def chain_topology(extra: Optional[list[tuple[str, str]]] = None) -> Topology:
    """a -> b -> c (a depends on b, b depends on c), plus an isolated service z."""
    services = {n: ServiceNode(name=n, health_port=7626) for n in ("a", "b", "c", "z")}
    pairs = [("a", "b"), ("b", "c")] + (extra or [])
    return Topology(project="t", services=services, edges=[Edge(src=s, dst=d, kind="depends_on") for s, d in pairs],
                    criticality=[])


# ---------------------------------------------------------------- sample builders
State = tuple[str, Optional[int], Optional[int]]  # docker status, alive, ready
GREEN: State = ("running", 200, 200)


def health(services: list[str], state_of: Callable[[str, float], State] = lambda s, t: GREEN,
           start: float = -4.9, end: float = 60.0, interval: float = 3.0,
           no_endpoint: tuple[str, ...] = ("nginx",)) -> list[HealthSample]:
    out = []
    t = start
    while t <= end:
        for svc in services:
            status, alive, ready = state_of(svc, t)
            if svc in no_endpoint and status == "running":
                alive = ready = None
            if svc == "mongodb" and status == "running":
                ready = None
            out.append(HealthSample(T0 + t, svc, status, None, alive, ready))
        t = round(t + interval, 6)
    return out


Outcome = tuple[int, float, str]  # status, latency_ms, error


def api(outcome: Callable[[str, float], Outcome], start: float = 0.05, end: float = 60.0,
        step: float = 0.25) -> list[Sample]:
    out, i, t = [], 0, start
    while t <= end:
        op = ThingsApiProbe.OPS[i % len(ThingsApiProbe.OPS)]
        status, lat, err = outcome(op, t)
        out.append(Sample(T0 + t, op, status, lat, err))
        i += 1
        t = round(t + step, 6)
    return out


OK: Outcome = (200, 20.0, "")
TIMEOUT: Outcome = (0, 5000.0, "timeout")
DITTO = ["mongodb", "policies", "things", "things-search", "connectivity", "gateway", "nginx"]


def run(topo: Topology, target: str, hs: list[HealthSample], aps: list[Sample], expected=(),
        revert: float = 30.0, end: float = 60.0, baseline: Optional[WindowStats] = None) -> CascadeAttribution:
    return attribute(topology=topo, target=target, expected_affected=list(expected), health_samples=hs,
                     api_samples=aps, injected_at=T0, revert_at=T0 + revert, window_end=T0 + end,
                     health_interval_s=3.0, baseline=baseline)


def hop(attr: CascadeAttribution, svc: str):
    return next(h for h in attr.hops if h.service == svc)


def well_formed(attr: CascadeAttribution) -> None:
    assert attr.hops and attr.hops[0].path_confidence == "origin" and attr.hops[0].service == attr.origin
    for h in attr.hops:
        assert h.mechanism in CASCADE_MECHANISMS
        assert h.path_confidence in PATH_CONFIDENCE
        if h.path:
            assert h.path[0] == h.service and h.path[-1] == attr.origin and h.path in h.candidate_paths
    assert any("polling interval" in n for n in attr.notes)
    assert any("OP_ROUTES" in n for n in attr.notes)


# ---------------------------------------------------------------- fixtures shaped like real runs
def things_crash() -> CascadeAttribution:
    def state(svc, t):
        return ("exited", None, None) if svc == "things" and 0 < t < 30 else GREEN
    aps = api(lambda op, t: TIMEOUT if 0.3 <= t < 33 else OK)
    return run(ditto_topology(), "things", health(DITTO, state), aps, expected=["gateway"])


def policies_stop() -> CascadeAttribution:
    def state(svc, t):
        return ("exited", None, None) if svc == "policies" and 0 < t < 30 else GREEN
    aps = api(lambda op, t: (503, 25.0, "") if 0.3 <= t < 33 else OK)
    return run(ditto_topology(), "policies", health(DITTO, state), aps, expected=["gateway", "things"])


def test_things_crash_masked_gateway_and_ambiguous_path():
    attr = things_crash()
    well_formed(attr)
    origin = attr.hops[0]
    assert origin.service == "things" and origin.onset_latest_s == 0.0
    assert "process_down at +1.1s" in origin.mechanism_evidence

    gw = hop(attr, "gateway")
    assert gw.onset_source == "api_probe"
    assert gw.mechanism == "dependency_timeout"
    assert gw.masked_by_health_check is True
    assert gw.path_confidence == "ambiguous"
    assert gw.path == ["gateway", "things"]  # fewest hops
    assert sorted(gw.candidate_paths) == [["gateway", "things"], ["gateway", "things-search", "things"]]
    assert gw.upstream == "things" and gw.edge_kinds == ["documented"]
    assert gw.onset_latest_s == 0.3 and gw.onset_earliest_s == 0.0
    assert gw.beyond_hypothesis is False

    ts = hop(attr, "things-search")
    assert ts.onset_source == "api_probe" and ts.mechanism == "dependency_timeout"
    assert ts.masked_by_health_check is True
    assert ts.path == ["things-search", "things"] and ts.path_confidence == "ordered"
    assert ts.lag_s == ts.onset_latest_s and ts.beyond_hypothesis is True
    assert "search" in ts.mechanism_evidence

    ng = hop(attr, "nginx")  # no health endpoint, so it cannot be "masked"
    assert ng.masked_by_health_check is False and ng.hops_from_origin == 2

    names = {h.service for h in attr.hops}
    assert "policies" not in names and "connectivity" not in names and "mongodb" not in names
    assert attr.hops_beyond_hypothesis == 2 and attr.beyond_hypothesis == ["nginx", "things-search"]
    assert {(e.src, e.dst) for e in attr.edges} == {("gateway", "things"), ("things-search", "things"),
                                                    ("nginx", "gateway")}
    assert not attr.unattributed
    ops = {o.op: o for o in attr.operations}
    assert ops["search"].route == ["nginx", "gateway", "things-search"]
    assert ops["get_thing"].error_kinds == {"timeout": ops["get_thing"].failures}
    assert any(t.service == "things" and t.state == "process_down" for t in attr.transitions)
    assert "its own health check stayed green throughout" in attr.summary


def test_policies_stop_503_is_dependency_error_and_ambiguous():
    attr = policies_stop()
    well_formed(attr)
    gw = hop(attr, "gateway")
    assert gw.mechanism == "dependency_error"
    assert gw.path_confidence == "ambiguous"
    assert gw.path == ["gateway", "policies"]
    assert ["gateway", "things", "policies"] in gw.candidate_paths
    assert sorted(gw.edge_kinds) == ["depends_on", "documented"]
    assert "fewest hops" in gw.note
    th = hop(attr, "things")
    assert th.mechanism == "dependency_error" and th.path[-1] == "policies"
    assert "connectivity" not in {h.service for h in attr.hops}  # no traffic, no health symptom
    ops = {o.op: o for o in attr.operations}
    assert ops["get_thing"].error_kinds.keys() == {"503"}


def test_clean_chain_is_ordered_with_lag():
    def state(svc, t):
        if svc == "c" and t > 0:
            return ("exited", None, None)
        if svc == "b" and t > 3:
            return ("running", 503, 503)
        if svc == "a" and t > 9:
            return ("running", 503, 503)
        return GREEN
    attr = run(chain_topology(), "c", health(["a", "b", "c", "z"], state), [], expected=["b"])
    well_formed(attr)
    b, a = hop(attr, "b"), hop(attr, "a")
    assert (b.onset_earliest_s, b.onset_latest_s) == (1.1, 4.1)
    assert (a.onset_earliest_s, a.onset_latest_s) == (7.1, 10.1)
    assert b.path_confidence == "ordered" and b.lag_s == 4.1
    assert a.path_confidence == "ordered" and a.path == ["a", "b", "c"] and a.lag_s == 6.0
    assert a.mechanism == "liveness_failed" and a.onset_source == "health_probe"
    assert a.beyond_hypothesis and attr.hops_beyond_hypothesis == 2
    assert [h.service for h in attr.hops] == ["c", "b", "a"]
    assert "first seen failing between +7.1s and +10.1s, after b (path c → b → a)" in attr.summary
    assert a.lag_s == 6.0 and "6.0s after" not in attr.summary  # interval onset: no false-precision lag in prose


def test_onsets_in_one_polling_interval_are_same_interval():
    def state(svc, t):
        if svc == "c" and t > 0:
            return ("exited", None, None)
        if svc in ("a", "b") and t > 3:
            return ("running", 200, 503)
        return GREEN
    attr = run(chain_topology(), "c", health(["a", "b", "c"], state), [])
    a = hop(attr, "a")
    assert a.path_confidence == "same_interval"
    assert a.mechanism == "readiness_failed"
    assert hop(attr, "b").path_confidence == "ordered"
    assert "cannot be told" in describe_hop(a, "c")


def test_isolated_single_timeout_creates_no_hop():
    aps = api(lambda op, t: TIMEOUT if op == "get_thing" and 5.0 <= t < 5.6 else OK, end=18.0)
    assert sum(1 for s in aps if not s.ok) == 1
    attr = run(ditto_topology(), "mongodb", health(DITTO), aps)
    assert [h.service for h in attr.hops] == ["mongodb"]
    assert attr.summary == "The failure stayed at mongodb: no other service showed a symptom."
    assert any("below the significance bar" in n for n in attr.notes)
    assert not attr.edges


def test_one_tick_health_blip_is_not_an_onset():
    def state(svc, t):
        if svc == "c" and t > 0:
            return ("exited", None, None)
        if svc == "b" and 3 < t < 5:
            return ("running", 503, 503)
        return GREEN
    attr = run(chain_topology(), "c", health(["a", "b", "c"], state), [])
    assert [h.service for h in attr.hops] == ["c"]
    assert any(t.service == "b" and t.state == "liveness_failed" for t in attr.transitions)


def test_pre_existing_unhealthy_service_is_excluded():
    def state(svc, t):
        if svc == "things" and 0 < t < 30:
            return ("exited", None, None)
        if svc == "connectivity":
            return ("running", 503, 503)
        return GREEN
    attr = run(ditto_topology(), "things", health(DITTO, state), api(lambda op, t: TIMEOUT if 0.3 <= t < 33 else OK))
    assert "connectivity" not in {h.service for h in attr.hops}
    assert any("connectivity was already liveness_failed before the injection" in n for n in attr.notes)


def test_health_onset_with_no_graph_path_is_unattributed():
    def state(svc, t):
        if svc == "c" and t > 0:
            return ("exited", None, None)
        if svc == "z" and t > 3:
            return ("exited", None, None)
        return GREEN
    attr = run(chain_topology(), "c", health(["a", "b", "c", "z"], state), [])
    z = hop(attr, "z")
    assert z.path_confidence == "no_graph_path" and z.mechanism == "process_down"
    assert z.path == [] and z.upstream is None and z.hops_from_origin is None
    assert attr.unattributed == ["z"]
    assert "no dependency route" in describe_hop(z, "c")


def test_failing_op_unrelated_to_origin_is_no_graph_path():
    services = {n: ServiceNode(name=n, health_port=7626 if n != "nginx" else None)
                for n in ("nginx", "gateway", "things", "things-search", "prometheus")}
    edges = [Edge(src="nginx", dst="gateway", kind="proxy"), Edge(src="gateway", dst="things", kind="documented"),
             Edge(src="gateway", dst="things-search", kind="documented")]
    topo = Topology(project="t", services=services, edges=edges, criticality=[])
    aps = api(lambda op, t: (500, 30.0, "") if op == "search" and t >= 2 else OK, end=20.0)
    attr = run(topo, "prometheus", health(list(services)), aps)
    ts = hop(attr, "things-search")
    assert ts.path_confidence == "no_graph_path" and ts.mechanism == "dependency_error"
    assert "search failed although nothing on its route" in ts.note
    assert {h.service for h in attr.hops} == {"prometheus", "things-search"}  # gateway/nginx not implicated
    assert attr.unattributed == ["things-search"]


def test_onset_after_rollback_phase():
    def state(svc, t):
        if svc == "c" and 0 < t < 5:
            return ("paused", None, None)
        if svc == "b" and t > 9:
            return ("running", 200, 503)
        return GREEN
    attr = run(chain_topology(), "c", health(["a", "b", "c"], state), [], revert=5.0)
    assert "process_paused at +1.1s" in attr.hops[0].mechanism_evidence
    b = hop(attr, "b")
    assert b.phase == "after_rollback" and (b.onset_earliest_s, b.onset_latest_s) == (7.1, 10.1)
    assert "after the fault had been rolled back" in describe_hop(b, "c")


def test_onset_interval_straddling_the_revert_is_not_called_after_rollback():
    # Live run 841f241dfa92: mongodb's onset interval (+14.9..+21.4s) contained the revert (+21.2s).
    def state(svc, t):
        if svc == "c" and 0 < t < 5:
            return ("paused", None, None)
        if svc == "b" and t > 6:
            return ("running", 200, 503)
        return GREEN
    attr = run(chain_topology(), "c", health(["a", "b", "c"], state), [], revert=5.0)
    b = hop(attr, "b")
    assert (b.onset_earliest_s, b.onset_latest_s) == (4.1, 7.1) and b.phase == "during_fault"


def test_latency_degraded_against_baseline():
    def outcome(op, t):
        return (200, 900.0, "") if op == "search" and t >= 4 else OK
    baseline = WindowStats(sample_count=100, success_rate=1.0, p95_ms=60,
                           by_operation={op: {"count": 25, "success_rate": 1.0, "p95_ms": 100.0}
                                         for op in ("get_thing", "put_property", "get_property", "search")})
    attr = run(ditto_topology(), "mongodb", health(DITTO), api(outcome, end=30.0), baseline=baseline)
    ts = hop(attr, "things-search")
    assert ts.mechanism == "latency_degraded" and ts.onset_source == "api_probe"
    first_slow = min(s.ts for s in api(outcome, end=30.0) if s.op == "search" and s.latency_ms > 300) - T0
    assert ts.onset_latest_s == round(first_slow, 1)
    assert ts.path_confidence in ("ordered", "ambiguous") and ts.path[-1] == "mongodb"
    assert "things" not in {h.service for h in attr.hops}  # only search slowed
    assert next(o for o in attr.operations if o.op == "search").mechanism == "latency_degraded"
    assert "first seen slow" in describe_hop(ts, "mongodb")
    # no baseline -> nothing to compare against -> no hop
    no_base = run(ditto_topology(), "mongodb", health(DITTO), api(outcome, end=30.0))
    assert [h.service for h in no_base.hops] == ["mongodb"]


def test_minority_failures_are_elevated_error_rate():
    seen = {"n": 0}

    def outcome(op, t):  # every 7th get_thing times out: ~14% of that op
        if op != "get_thing":
            return OK
        seen["n"] += 1
        return TIMEOUT if seen["n"] % 7 == 3 else OK
    aps = api(outcome, end=40.0)
    attr = run(ditto_topology(), "things", health(DITTO), aps)
    op = next(o for o in attr.operations if o.op == "get_thing")
    assert 0.10 <= op.failures / op.requests < 0.25
    assert op.mechanism == "elevated_error_rate"
    gw = hop(attr, "gateway")
    assert gw.mechanism == "elevated_error_rate" and "mostly timeout" in gw.mechanism_evidence


def test_empty_input_and_unknown_target_never_raise():
    attr = run(ditto_topology(), "things", [], [])
    assert [h.service for h in attr.hops] == ["things"]
    assert attr.summary.startswith("The failure stayed at things")
    assert attr.window_end_s == 60.0 and attr.window_start_s == 0.0
    missing = run(ditto_topology(), "nope", health(DITTO), api(lambda op, t: TIMEOUT))
    assert [h.service for h in missing.hops] == ["nope"]
    assert any("not in topology.json" in n for n in missing.notes)
    unknown_op = run(ditto_topology(), "things", [], [Sample(T0 + 1, "mystery", 0, 5000, "timeout"),
                                                      Sample(T0 + 2, "mystery", 0, 5000, "timeout")])
    assert next(o for o in unknown_op.operations if o.op == "mystery").route == []


def test_attribution_is_deterministic():
    assert things_crash().model_dump() == things_crash().model_dump()


def test_round_trips_through_experiment_report(make_report):
    attr = things_crash()
    again = CascadeAttribution.model_validate(json.loads(attr.model_dump_json()))
    assert again == attr
    report = make_report(cascade=attr)
    loaded = ExperimentReport.model_validate(json.loads(report.model_dump_json()))
    assert loaded.cascade == attr
