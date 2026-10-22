"""Narrator x Cascade Attribution: a "Cascade" section right after Impact, one describe_hop line per
hop, a timing caveat when a hop's order is unresolved, and nothing at all for reports without one."""
from __future__ import annotations

import json

from break_point.agents import narrator
from break_point.observe.cascade import describe_hop
from break_point.schemas import CascadeAttribution, CascadeHop


def _origin(service: str, evidence: str) -> CascadeHop:
    return CascadeHop(service=service, onset_earliest_s=0.0, onset_latest_s=0.0, onset_source="injection",
                      mechanism="injected_fault", mechanism_evidence=evidence, path=[service],
                      path_confidence="origin", hops_from_origin=0)


def things_crash() -> CascadeAttribution:
    """Kill `orders`; gateway's requests time out, but gateway -> orders and gateway -> orders-search -> orders
    both fit the timing, and gateway's own /ready stayed green."""
    gateway = CascadeHop(
        service="gateway", onset_earliest_s=0.6, onset_latest_s=3.1, onset_source="api_probe",
        mechanism="dependency_timeout", mechanism_evidence="GET /api/2/orders/{id}: 14/14 timed out",
        masked_by_health_check=True, upstream="orders", edge_kinds=["documented"], lag_s=3.1,
        path=["gateway", "orders"], candidate_paths=[["gateway", "orders"], ["gateway", "orders-search", "orders"]],
        path_confidence="ambiguous", hops_from_origin=1, beyond_hypothesis=False)
    return CascadeAttribution(
        origin="orders", health_interval_s=3.0, window_start_s=-10.0, window_end_s=45.0,
        hops=[_origin("orders", "docker kill shop-things-1"), gateway],
        summary="orders was killed; gateway then timed out on thing reads, most likely via orders.")


def policies_stop() -> CascadeAttribution:
    """Stop `auth`; gateway fails 4 s later (ordered), then nginx answers 5xx, beyond the hypothesis."""
    gateway = CascadeHop(
        service="gateway", onset_earliest_s=4.0, onset_latest_s=4.0, onset_source="health_probe",
        mechanism="dependency_timeout", mechanism_evidence="policy enforcement asks timed out",
        upstream="auth", edge_kinds=["depends_on"], lag_s=4.0, path=["gateway", "auth"],
        candidate_paths=[["gateway", "auth"]], path_confidence="ordered", hops_from_origin=1)
    nginx = CascadeHop(
        service="nginx", onset_earliest_s=6.0, onset_latest_s=7.5, onset_source="api_probe",
        mechanism="dependency_error", mechanism_evidence="502/503 from the proxy",
        upstream="gateway", edge_kinds=["proxy", "depends_on"], lag_s=3.5, path=["nginx", "gateway", "auth"],
        candidate_paths=[["nginx", "gateway", "auth"]], path_confidence="ordered", hops_from_origin=2,
        beyond_hypothesis=True)
    return CascadeAttribution(
        origin="auth", health_interval_s=3.0, window_start_s=-10.0, window_end_s=45.0,
        hops=[_origin("auth", "docker stop shop-policies-1"), gateway, nginx],
        hops_beyond_hypothesis=2, beyond_hypothesis=["nginx"],
        summary="auth stopped; gateway timed out 4.0s later; nginx then answered with server errors.")


def _section(lines: list[str]) -> list[str]:
    i = next(i for i, line in enumerate(lines) if line.startswith("  Impact:"))
    j = next(i for i, line in enumerate(lines) if line.startswith("  Recovery:"))
    return lines[i + 1:j]


def test_absent_cascade_leaves_output_unchanged(make_report):
    report = make_report(finding_codes=["health_check_masked_failure"])
    assert report.cascade is None and narrator._cascade(report) == []
    lines = narrator.narrate(report, None, None, None, False)
    # the pre-attribution order: header, timeline, impact, recovery, hypothesis, findings, risk, fix
    expected = [narrator._header(report), *narrator._timeline(report), narrator._impact(report),
                narrator._recovery(report), narrator._hypothesis(report), *narrator._findings(report),
                narrator._risk(None), narrator._fix(None)]
    assert lines == expected
    assert not any("Cascade" in line for line in lines)


def test_things_crash_ambiguous_masked(make_report):
    c = things_crash()
    report = make_report(service="orders").model_copy(update={"cascade": c})
    section = _section(narrator.narrate(report, None, None, None, False))
    assert section == narrator._cascade(report)
    assert section[0] == "  Cascade: from orders, in order of failure"
    assert section[1:3] == [f"    {describe_hop(h, 'orders')}" for h in c.hops]
    assert "most likely via orders" in section[2] and "orders → orders-search → gateway" in section[2]
    assert "health check stayed green" in section[2]
    assert section[3].startswith("    Timing: health is polled every 3s")
    assert len(section) == 4


def test_policies_stop_ordered_has_no_caveat(make_report):
    c = policies_stop()
    report = make_report(service="auth", failure_type="container_stop").model_copy(update={"cascade": c})
    section = _section(narrator.narrate(report, None, None, None, False))
    assert section[0] == "  Cascade: from auth, in order of failure; beyond the hypothesis: nginx"
    assert section[1:] == [f"    {describe_hop(h, 'auth')}" for h in c.hops]
    assert "4.0s after auth (path auth → gateway)" in section[2]
    assert section[3].endswith("[beyond the hypothesis]")
    assert not any("Timing:" in line for line in section)


class _FakeLLM:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str, max_tokens: int = 0) -> str:
        self.calls.append((system, json.loads(user)))
        return "A paragraph."


def test_llm_gets_cascade_as_established_facts(make_report):
    llm = _FakeLLM()
    plain = make_report()
    narrator.narrate(plain, None, None, llm, True)
    c = things_crash()
    lines = narrator.narrate(make_report(service="orders").model_copy(update={"cascade": c}), None, None, llm, True)
    (sys_plain, facts_plain), (sys_c, facts_c) = llm.calls
    assert sys_plain == narrator.LLM_SYSTEM and "cascade" not in facts_plain
    assert sys_c == narrator.LLM_SYSTEM + narrator.LLM_CASCADE_RULE
    assert "do not change the order" in sys_c and "ambiguous" in sys_c
    assert facts_c["cascade"]["summary"] == c.summary
    assert facts_c["cascade"]["hops_in_order"] == [
        {"sentence": describe_hop(h, "orders"), "confidence": h.path_confidence} for h in c.hops]
    assert lines[-1] == "  A paragraph."


def test_narrate_sections_carries_the_cascade(make_report):
    c = policies_stop()
    report = make_report(service="auth", failure_type="container_stop").model_copy(update={"cascade": c})
    out = narrator.narrate_sections(report, None, None, None, False)
    assert out["cascade"] == [line.strip() for line in narrator._cascade(report)]
    assert out["cascade"][0] == "Cascade: from auth, in order of failure; beyond the hypothesis: nginx"
    assert narrator.narrate_sections(make_report(), None, None, None, False)["cascade"] == []
