import pytest

from faultline.chaos.catalog import CATALOG
from faultline.planning.planner import (
    WEAKNESS_EXACT_MATCH_BONUS,
    WEAKNESS_WEIGHT,
    Candidate,
    Planner,
    Policy,
    describe_candidates,
)
from faultline.schemas import PREDICTED_LABEL, PredictedWeakness, SuggestedExperiment, WeaknessReport


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


def _planner(topology, policy, history=None, llm=None, images=None, allow_protected=False, weaknesses=None):
    return Planner(topology, policy, history or [], llm, FakeSettings(),
                   available_images={"pumba", "tc"} if images is None else images, allow_protected=allow_protected,
                   weaknesses=weaknesses)


def _weakness(services, failure_type, severity=6.2, category="shallow_health_check", weakness_id="w1"):
    """One PREDICTED weakness, as the Weakness Scout would write it into reports/weaknesses.json."""
    return PredictedWeakness(
        weakness_id=weakness_id, category=category, services=list(services),
        title=f"{category} on {', '.join(services)}", evidence="compose: healthcheck probes the process, not its deps",
        predicted_severity=severity, predicted_level="high", factors={"fan_in": 2.0, "stateful": 0.0},
        suggested_experiment=SuggestedExperiment(failure_type=failure_type, target_service=services[0],
                                                 reason="breaking this is what would confirm or clear the prediction"),
        confirming_finding_codes=["dependent_unhealthy"], why_it_matters="callers hang instead of failing fast",
    )


def _weakness_report(*weaknesses):
    return WeaknessReport(project="bosch-ditto", compose_file="docker-compose.yml", weaknesses=list(weaknesses))


def _by_pair(cands):
    return {(c.service, c.failure_type): c for c in cands}


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


# ------------------------------------------------- predicted weaknesses (Weakness Scout feedback)
# Priorities below are the ones the formula
#   crit * (1.5 if untested else 1.0) - 1.5 * tested + INFORMATIVENESS[ft] (- 0.5 degrade_only)
# produces for the `topology` fixture; they are hand-derived from criticality (mongodb 9, policies 5,
# things 4, connectivity/gateway/things-search 2, nginx 1), not snapshotted from a run.
BASELINE_PRIORITIES = {
    ("mongodb", "network_delay"): 9 * 1.5 + 0.4 - 0.5,          # 13.4, protected -> degrade-only types only
    ("mongodb", "db_flood"): 9 * 1.5 + 0.35 - 0.5,              # 13.35
    ("mongodb", "disk_pressure"): 9 * 1.5 + 0.0 - 0.5,          # 13.0
    ("policies", "container_crash"): 5 * 1.5 + 0.4,             # 7.9
    ("policies", "network_partition"): 5 * 1.5 + 0.35,          # 7.85
    ("policies", "container_pause"): 5 * 1.5 + 0.2,             # 7.7
    ("things", "container_crash"): 4 * 1.5 + 0.4,               # 6.4
    ("things", "network_loss"): 4 * 1.5 + 0.3 - 0.5,            # 5.8
    ("connectivity", "container_crash"): 2 * 1.5 + 0.4,         # 3.4
    ("gateway", "container_crash"): 2 * 1.5 + 0.4,              # 3.4
    ("things-search", "container_crash"): 2 * 1.5 + 0.4,        # 3.4
    ("gateway", "traffic_spike"): 2 * 1.5 + 0.2 - 0.5,          # 2.7
    ("nginx", "disk_pressure"): 1 * 1.5 + 0.0 - 0.5,            # 1.0
}


def test_candidates_without_weaknesses_are_byte_identical(topology, policy):
    """No weakness report -> the ranking is exactly what it was before the bonus existed."""
    cands = _planner(topology, policy).candidates()
    assert len(cands) == 55
    by_pair = _by_pair(cands)
    for pair, expected in BASELINE_PRIORITIES.items():
        assert by_pair[pair].priority == round(expected, 2), pair
    for c in cands:
        assert c.reasons == [f"criticality {topology.criticality_of(c.service):g}", "untested"]
    assert [(c.service, c.failure_type) for c in cands[:3]] == [
        ("mongodb", "network_delay"), ("mongodb", "db_flood"), ("mongodb", "network_loss")]


def test_predicted_weakness_outranks_identical_service_without_one(topology, policy):
    """connectivity / gateway / things-search are all criticality 2: only the prediction separates them."""
    report = _weakness_report(_weakness(["things-search"], failure_type="container_pause", severity=6.2))
    flagged = _by_pair(_planner(topology, policy, weaknesses=report).candidates())
    pick = flagged[("things-search", "container_crash")]
    assert pick.priority == round(3.4 + WEAKNESS_WEIGHT * 0.62, 2)
    assert pick.priority > flagged[("connectivity", "container_crash")].priority
    assert pick.priority > flagged[("gateway", "container_crash")].priority
    assert flagged[("connectivity", "container_crash")].priority == 3.4  # unflagged services untouched
    reason = next(r for r in pick.reasons if r.startswith("predicted weakness"))
    assert reason == f"predicted weakness: shallow_health_check 6.2/10 {PREDICTED_LABEL}"
    assert "exact match" not in reason


def test_predicted_weakness_plus_criticality_outranks_criticality_alone(topology, policy, make_report):
    """The headline: `things` (criticality 4, flagged) beats `policies` (criticality 5, unflagged).

    Both pairs carry one past run so the untested multiplier is the same on each side, and the last
    run targeted nginx so neither gets the same-target-as-last-run penalty.
    """
    history = [make_report(service="policies", failure_type="container_crash"),
               make_report(service="things", failure_type="container_crash"),
               make_report(service="nginx", failure_type="container_crash")]
    plain = _by_pair(_planner(topology, policy, history).candidates())
    assert plain[("policies", "container_crash")].priority == 3.9   # 5 - 1.5 + 0.4
    assert plain[("things", "container_crash")].priority == 2.9     # 4 - 1.5 + 0.4, loses on criticality
    report = _weakness_report(_weakness(["things"], failure_type="container_crash", severity=8.0))
    flagged = _by_pair(_planner(topology, policy, history, weaknesses=report).candidates())
    assert flagged[("policies", "container_crash")].priority == 3.9
    assert flagged[("things", "container_crash")].priority == round(2.9 + 0.8 + WEAKNESS_EXACT_MATCH_BONUS, 2)
    assert flagged[("things", "container_crash")].priority > flagged[("policies", "container_crash")].priority


def test_exact_match_bonus_only_applies_to_the_suggested_failure_type(topology, policy):
    report = _weakness_report(_weakness(["things"], failure_type="container_pause", severity=6.0))
    flagged = _by_pair(_planner(topology, policy, weaknesses=report).candidates())
    exact = flagged[("things", "container_pause")]
    other = flagged[("things", "container_crash")]
    assert exact.priority == round(6.2 + 0.6 + WEAKNESS_EXACT_MATCH_BONUS, 2)   # 6.2 baseline + 0.6 + 0.5
    assert other.priority == round(6.4 + 0.6, 2)                                # severity part only
    assert round(exact.priority - other.priority, 2) == WEAKNESS_EXACT_MATCH_BONUS - 0.2  # 0.2 = informativeness gap
    assert "exact match: container_pause is the experiment suggested for predicted shallow_health_check" in \
        next(r for r in exact.reasons if r.startswith("predicted weakness"))
    assert "exact match" not in next(r for r in other.reasons if r.startswith("predicted weakness"))
    assert PREDICTED_LABEL in next(r for r in other.reasons if r.startswith("predicted weakness"))


def test_weaknesses_for_unknown_or_filtered_services_change_nothing(topology, policy):
    report = _weakness_report(
        _weakness(["mars"], failure_type="container_crash", severity=10.0, weakness_id="w-unknown"),
        _weakness(["ditto-ui"], failure_type="container_crash", severity=10.0, weakness_id="w-skipped"),
    )
    plain = _planner(topology, policy).candidates()
    flagged = _planner(topology, policy, weaknesses=report).candidates()
    assert [(c.service, c.failure_type, c.priority, c.reasons) for c in flagged] == \
        [(c.service, c.failure_type, c.priority, c.reasons) for c in plain]
    assert not any("predicted weakness" in r for c in flagged for r in c.reasons)


def test_weakness_bonus_is_bounded(topology, policy):
    """A maximal 10/10 exact-match prediction can add 1.5 and no more."""
    report = _weakness_report(
        _weakness(["things"], failure_type="container_crash", severity=10.0),
        _weakness(["things"], failure_type="container_crash", severity=3.0, weakness_id="w2", category="no_redundancy"),
    )
    plain = _by_pair(_planner(topology, policy).candidates())
    flagged = _by_pair(_planner(topology, policy, weaknesses=report).candidates())
    deltas = {pair: round(c.priority - plain[pair].priority, 2) for pair, c in flagged.items()}
    assert max(deltas.values()) == WEAKNESS_WEIGHT + WEAKNESS_EXACT_MATCH_BONUS == 1.5
    assert deltas[("things", "container_crash")] == 1.5          # worst severity wins, bonuses do not stack
    assert deltas[("things", "network_delay")] == WEAKNESS_WEIGHT
    assert all(d == 0.0 for pair, d in deltas.items() if pair[0] != "things")


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
