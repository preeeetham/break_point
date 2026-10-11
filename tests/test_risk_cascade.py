"""Risk agent x Cascade Attribution: the attribution adds cascade_depth / cascade_masked factors and
subsumes the bare cascade_beyond_hypothesis bonus; reports without `cascade` score exactly as before."""
from __future__ import annotations

import json

import pytest

from faultline.agents import risk
from faultline.schemas import CascadeAttribution, CascadeHop


def _origin(service: str = "policies") -> CascadeHop:
    return CascadeHop(service=service, onset_earliest_s=0.0, onset_latest_s=0.0, onset_source="injection",
                      mechanism="injected_fault", mechanism_evidence="docker kill", path=[service],
                      path_confidence="origin", hops_from_origin=0)


def _hop(service: str, upstream: str, path: list[str], *, confidence: str = "ordered", beyond: bool = True,
         masked: bool = False, mechanism: str = "dependency_timeout") -> CascadeHop:
    return CascadeHop(service=service, onset_earliest_s=3.0, onset_latest_s=6.0, onset_source="health_probe",
                      mechanism=mechanism, mechanism_evidence="synthetic", masked_by_health_check=masked,
                      upstream=upstream, lag_s=4.0, path=path, candidate_paths=[path],
                      path_confidence=confidence,
                      hops_from_origin=len(path) - 1 if confidence != "no_graph_path" else None,
                      beyond_hypothesis=beyond)


def _cascade(*hops: CascadeHop, origin: str = "policies", hops_beyond: int = 0) -> CascadeAttribution:
    return CascadeAttribution(origin=origin, health_interval_s=3.0, window_start_s=-10.0, window_end_s=60.0,
                              hops=[_origin(origin), *hops], hops_beyond_hypothesis=hops_beyond,
                              beyond_hypothesis=[h.service for h in hops if h.beyond_hypothesis],
                              summary="policies failed; gateway timed out waiting on it 4.0s later.")


# Two hops deep beyond the hypothesis, both on a graph path: policies -> gateway -> nginx.
TWO_DEEP = (
    _hop("gateway", "policies", ["gateway", "policies"]),
    _hop("nginx", "gateway", ["nginx", "gateway", "policies"], mechanism="dependency_error"),
)


def _without_cascade(factors: dict[str, float]) -> dict[str, float]:
    return {k: v for k, v in factors.items() if not k.startswith("cascade_")}


def test_old_report_without_cascade_scores_exactly_as_before(make_report, topology):
    """Regression: absent cascade -> same factor keys, values, severity, level and summary as the
    pre-attribution formula (the cascade finding still earns its 0.5 bonus)."""
    report = make_report(finding_codes=["cascade_beyond_hypothesis", "health_check_masked_failure"],
                         affected=["gateway", "nginx"])
    assert report.cascade is None
    a = risk.assess(report, topology, None)
    assert set(a.factors) == {"impact", "blast", "recovery", "intervention", "findings"}
    expected = {"impact": 3.6, "blast": round(2.0 * 3 / 7, 3), "recovery": round(2.0 * 14 / 120, 3),
                "intervention": 0.0, "findings": 1.5}
    assert a.factors == expected
    assert a.severity == round(sum(expected.values()), 2)
    assert a.level == "high"
    assert "cascaded" not in a.summary and "Cascade" not in a.summary


def test_level_thresholds_unchanged_for_old_reports(make_report, topology):
    for during in (1.0, 0.8, 0.5, 0.2, 0.0):
        a = risk.assess(make_report(during_success=during), topology, None)
        assert a.level == risk._level(a.severity)
        assert not any(k.startswith("cascade_") for k in a.factors)
    assert [risk._level(x) for x in (2.99, 3.0, 5.49, 5.5, 7.99, 8.0)] == \
        ["low", "medium", "medium", "high", "high", "critical"]


def test_two_hops_beyond_hypothesis_on_graph_path_adds_one(make_report, topology):
    base = make_report(affected=["gateway", "nginx"], finding_codes=["cascade_beyond_hypothesis"])
    withc = base.model_copy(update={"cascade": _cascade(*TWO_DEEP, hops_beyond=2)})
    old, new = risk.assess(base, topology, None), risk.assess(withc, topology, None)
    assert new.factors["cascade_depth"] == 1.0
    assert new.factors["cascade_masked"] == 0.0
    # the bare 0.5 finding bonus is subsumed, not added on top
    assert old.factors["findings"] == 0.5 and new.factors["findings"] == 0.0
    assert _without_cascade(new.factors) == {**old.factors, "findings": 0.0}
    assert new.severity == pytest.approx(old.severity - 0.5 + 1.0, abs=0.01)
    assert new.severity == pytest.approx(sum(new.factors.values()), abs=0.01)
    assert "cascaded beyond the hypothesis along policies → gateway → nginx." in new.summary


def test_masked_beyond_hop_adds_half(make_report, topology):
    base = make_report(affected=["gateway"])
    one_deep = base.model_copy(update={"cascade": _cascade(_hop("gateway", "policies", ["gateway", "policies"]))})
    masked = base.model_copy(update={"cascade": _cascade(_hop("gateway", "policies", ["gateway", "policies"], masked=True))})
    a, b = risk.assess(one_deep, topology, None), risk.assess(masked, topology, None)
    assert a.factors["cascade_depth"] == b.factors["cascade_depth"] == 0.5
    assert a.factors["cascade_masked"] == 0.0 and b.factors["cascade_masked"] == 0.5
    assert b.severity == pytest.approx(a.severity + 0.5, abs=0.01)
    # a masked hop inside the hypothesis does not count
    inside = base.model_copy(update={"cascade": _cascade(_hop("gateway", "policies", ["gateway", "policies"],
                                                              masked=True, beyond=False))})
    c = risk.assess(inside, topology, None)
    assert c.factors["cascade_masked"] == 0.0 and c.factors["cascade_depth"] == 0.0


def test_no_graph_path_hops_do_not_deepen(make_report, topology):
    stray = _hop("connectivity", None, [], confidence="no_graph_path", mechanism="elevated_error_rate")
    # even if the stored int claims depth, it is recomputed from the hops
    report = make_report(affected=["connectivity"], finding_codes=["cascade_beyond_hypothesis"])
    report = report.model_copy(update={"cascade": _cascade(stray, hops_beyond=3)})
    a = risk.assess(report, topology, None)
    assert a.factors["cascade_depth"] == 0.0
    assert a.factors["findings"] == 0.0
    assert "no dependency route to policies" in a.summary


def test_depth_cap_holds(make_report, topology):
    deep = (
        _hop("things", "policies", ["things", "policies"]),
        _hop("gateway", "things", ["gateway", "things", "policies"]),
        _hop("nginx", "gateway", ["nginx", "gateway", "things", "policies"]),
    )
    report = make_report(affected=["things", "gateway", "nginx"]).model_copy(
        update={"cascade": _cascade(*deep, hops_beyond=3)})
    a = risk.assess(report, topology, None)
    assert a.factors["cascade_depth"] == risk.CASCADE_DEPTH_CAP == 1.0


def test_hedged_path_in_summary_when_not_ordered(make_report, topology):
    hop = _hop("gateway", "policies", ["gateway", "policies"], confidence="same_interval")
    report = make_report().model_copy(update={"cascade": _cascade(hop)})
    a = risk.assess(report, topology, None)
    assert a.factors["cascade_depth"] == 0.5  # same_interval still has a graph path
    assert "along policies → gateway, order not certain." in a.summary


def test_cascade_without_beyond_hops_adds_no_clause(make_report, topology):
    report = make_report().model_copy(update={"cascade": _cascade(
        _hop("gateway", "policies", ["gateway", "policies"], beyond=False))})
    a = risk.assess(report, topology, None)
    assert a.summary == risk.template_summary(make_report(), a.blast_radius)
    assert a.factors["cascade_depth"] == 0.0 and a.factors["cascade_masked"] == 0.0


class _FakeLLM:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def available(self) -> bool:
        return True

    def chat_json(self, system: str, user: str):
        self.calls.append((system, json.loads(user)))
        return {"summary": "s.", "production_implication": "p."}


def test_cascade_summary_reaches_llm_only_when_present(make_report, topology):
    llm = _FakeLLM()
    risk.assess(make_report(), topology, llm)
    report = make_report().model_copy(update={"cascade": _cascade(*TWO_DEEP)})
    out = risk.assess(report, topology, llm)
    (sys_old, old), (sys_new, new) = llm.calls
    assert "cascade_summary" not in old and sys_old == risk.LLM_SYSTEM
    assert new["cascade_summary"] == report.cascade.summary
    assert "do not change its order" in sys_new
    assert new["factors"]["cascade_depth"] == 1.0
    assert out.generated_by == "llm+rules"
