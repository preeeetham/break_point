import pytest

from faultline.chaos.catalog import CATALOG
from faultline.planning.planner import Candidate, Planner, Policy, describe_candidates


class FakeSettings:
    compose_project = "bosch-ditto"
    compose_network = "bosch-ditto_default"
    pumba_image = "p"
    tc_image = "t"


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.last_error = ""
        self.calls = []

    def available(self):
        return True

    def describe(self):
        return "fake"

    def chat_json(self, system, user, max_tokens=700):
        self.calls.append((system, user))
        return self.answer


@pytest.fixture
def policy(policy_raw):
    return Policy(policy_raw)


def _planner(topology, policy, history=None, llm=None, images=None, allow_protected=False):
    return Planner(topology, policy, history or [], llm, FakeSettings(),
                   available_images={"pumba", "tc"} if images is None else images, allow_protected=allow_protected)


# ---------------------------------------------------------------- policy
def test_policy_allows_blocks_protected_unless_override(policy):
    ok, why = policy.allows("mongodb", "container_crash")
    assert not ok and "protected" in why
    ok, why = policy.allows("mongodb", "container_crash", override=True)
    assert ok and "override" in why
    ok, why = policy.allows("mongodb", "network_delay")
    assert ok and why == ""
    ok, why = policy.allows("things", "container_crash")
    assert ok and why == ""
    assert policy.skip_roles == {"ui"}


# ---------------------------------------------------------------- candidates
def test_candidates_ranking_and_exclusions(topology, policy):
    cands = _planner(topology, policy).candidates()
    assert cands
    assert cands[0].service in ("mongodb", "policies")
    assert all(c.service != "ditto-ui" for c in cands)
    assert not any(c.service == "mongodb" and c.failure_type == "container_crash" for c in cands)
    assert [c.priority for c in cands] == sorted((c.priority for c in cands), reverse=True)
    for c in cands:
        assert c.failure_type in CATALOG
        assert any(r.startswith("criticality") for r in c.reasons)
    table = describe_candidates(cands, limit=3)
    assert cands[0].service in table and table.count("\n") == 4  # header + rule + 3 rows


def test_history_lowers_priority_of_tested_pair(topology, policy, make_report):
    fresh = _planner(topology, policy).candidates()
    top = fresh[0]
    history = [make_report(service=top.service, failure_type=top.failure_type)]
    again = {(c.service, c.failure_type): c for c in _planner(topology, policy, history).candidates()}
    tested = again[(top.service, top.failure_type)]
    assert tested.priority < top.priority
    assert "tested 1x" in tested.reasons
    assert "untested" in top.reasons


def test_missing_images_remove_netem_candidates(topology, policy):
    cands = _planner(topology, policy, images=set()).candidates()
    assert cands
    assert not any(c.failure_type in ("network_delay", "network_loss") for c in cands)


# ---------------------------------------------------------------- deterministic planning
def test_plan_next_deterministic(topology, policy):
    planner = _planner(topology, policy)
    spec = planner.plan_next()
    top = planner.candidates()[0]
    assert spec is not None
    assert spec.planned_by == "deterministic"
    assert (spec.target_service, spec.failure_type) == (top.service, top.failure_type)
    ft = CATALOG[spec.failure_type]
    for p in ft.params:
        assert p.name in spec.parameters
        if p.lo is not None:
            assert p.lo <= spec.parameters[p.name] <= p.hi
    assert set(spec.hypothesis.expected_affected) <= set(topology.services)
    assert spec.reproduce
    assert spec.container == f"bosch-ditto-{spec.target_service}-1"
    assert spec.criticality_score == topology.criticality_of(spec.target_service)
    assert spec.rationale


def test_plan_next_returns_none_without_candidates(topology, policy):
    for node in topology.services.values():
        node.chaos_relevant = False
    assert _planner(topology, policy).plan_next() is None


# ---------------------------------------------------------------- operator planning
def test_plan_operator_protected_requires_override(topology, policy):
    planner = _planner(topology, policy)
    with pytest.raises(ValueError) as exc:
        planner.plan_operator("mongodb", "network_partition", {})
    assert "protected" in str(exc.value)
    spec = _planner(topology, policy, allow_protected=True).plan_operator("mongodb", "network_partition", {})
    assert spec.planned_by == "operator"
    assert any("policy override" in n for n in spec.planner_notes)


def test_plan_operator_mongodb_container_crash_needs_allow_protected(topology, policy):
    with pytest.raises(ValueError) as exc:
        _planner(topology, policy).plan_operator("mongodb", "container_crash", {})
    assert "protected" in str(exc.value) and "--allow-protected" in str(exc.value)
    spec = _planner(topology, policy, allow_protected=True).plan_operator("mongodb", "container_crash", {})
    assert spec.target_service == "mongodb" and any("override" in n for n in spec.planner_notes)


def test_plan_operator_clamps_and_rejects_unknowns(topology, policy):
    planner = _planner(topology, policy)
    spec = planner.plan_operator("things", "network_delay", {"delay_ms": 10_000, "duration_s": 20})
    assert spec.parameters["delay_ms"] == 3000 and spec.parameters["duration_s"] == 20
    assert any("lowered" in n for n in spec.planner_notes)
    with pytest.raises(ValueError, match="unknown service"):
        planner.plan_operator("nope", "network_delay", {})
    with pytest.raises(ValueError, match="unknown failure type"):
        planner.plan_operator("things", "meteor_strike", {})
    with pytest.raises(ValueError, match="does not apply"):
        planner.plan_operator("things", "traffic_spike", {})


# ---------------------------------------------------------------- llm path
def test_plan_next_with_llm_pick_is_validated_and_clamped(topology, policy):
    llm = FakeLLM({"choice": 1, "parameters": {"duration_s": 9999}, "rationale": "x",
                   "hypothesis": {"expected_max_error_rate": 7}})
    planner = _planner(topology, policy, llm=llm)
    expected = planner.candidates()[1]
    spec = planner.plan_next()
    assert spec.planned_by == "llm"
    assert (spec.target_service, spec.failure_type) == (expected.service, expected.failure_type)
    ft = CATALOG[spec.failure_type]
    assert spec.parameters["duration_s"] == next(p.hi for p in ft.params if p.name == "duration_s")
    assert spec.hypothesis.expected_max_error_rate == 1.0
    assert spec.rationale == "x"
    assert any("chose candidate 1" in n for n in spec.planner_notes)
    assert any("lowered" in n for n in spec.planner_notes)
    assert len(llm.calls) == 1 and "Candidates" in llm.calls[0][1]


def test_plan_next_llm_bad_choice_falls_back(topology, policy):
    planner = _planner(topology, policy, llm=FakeLLM({"choice": 99}))
    spec = planner.plan_next()
    assert spec.planned_by == "deterministic"
    top = planner.candidates()[0]
    assert (spec.target_service, spec.failure_type) == (top.service, top.failure_type)
    assert any("no usable answer" in n for n in spec.planner_notes)


def test_plan_next_llm_none_answer_falls_back(topology, policy):
    spec = _planner(topology, policy, llm=FakeLLM(None)).plan_next()
    assert spec.planned_by == "deterministic"


def test_ask_llm_filters_unknown_affected_services(topology, policy):
    llm = FakeLLM({"choice": 0, "hypothesis": {"expected_affected": ["mars", "gateway"], "expected_recovery_s": 99999}})
    planner = _planner(topology, policy, llm=llm)
    top = planner.candidates()[:5]
    pick, params, rationale, hyp, notes = planner._ask_llm(top)
    assert isinstance(pick, Candidate) and pick == top[0]
    assert hyp.expected_affected == ["gateway"]
    assert hyp.expected_recovery_s == 600.0
    assert params == {} and rationale == ""
