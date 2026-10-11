"""The agent path with a scripted chat model: investigation, acceptance, self-correction, fallbacks (tests 2-7, 9)."""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from faultline.chaos.catalog import CATALOG, clamp
from faultline.schemas import ExperimentSpec
from faultline_langgraph.llm import ModelHandle
from faultline_langgraph.planner import plan_next
from faultline_langgraph.planner.graph import INVESTIGATE_TURNS, MAX_AGENT_TURNS, MAX_SUBMISSIONS
from faultline_langgraph.planner.prompts import SYSTEM
from faultline_langgraph.planner.tools import SUBMIT

from .conftest import LABEL, ScriptedLLM, ai, call, spec_fields, submit

INVESTIGATION_TOOLS = {"inspect_service", "experiment_history", "failure_type_info", "baseline_summary", "preview_candidate"}
FULL_HYPOTHESIS = {"statement": "mongodb under cpu starvation slows writes but the API stays up",
                   "expected_affected": ["policies", "things", "mars"],  # "mars" is not a service: filtered
                   "expected_max_error_rate": 0.25, "expected_recovery_s": 40}


def run(board, model, **kw):
    return plan_next(board.build(), ModelHandle(model, LABEL), board.store.baseline(), **kw)


def tool_messages(messages) -> dict[str, ToolMessage]:
    return {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}


def assert_deterministic_fallback(board, result, *fragments: str):
    """planned_by deterministic, identical to the original --no-llm plan except one final 'LLM (...)' note."""
    original = board.build().plan_next()
    spec = result.spec
    assert spec is not None and spec.planned_by == "deterministic"
    assert spec_fields(spec, drop_notes_after=len(original.planner_notes)) == spec_fields(original)
    assert len(spec.planner_notes) == len(original.planner_notes) + 1
    note = spec.planner_notes[-1]
    assert note.startswith(f"LLM ({LABEL})") and "gave no usable answer" in note
    for f in fragments:
        assert f in note, (f, note)
    assert ExperimentSpec.model_validate(spec.model_dump()) == spec
    return note


# ---------------------------------------------------------------- 2. happy path
def test_happy_path_investigates_then_submits_clamped_plan(board, scripted):
    top = board.build().candidates()[:5]
    target = top[2]
    params = {"duration_s": 9999}
    proposal = {"choice": 2, "parameters": params, "rationale": "lighter fault on the busiest datastore",
                "hypothesis": FULL_HYPOTHESIS}
    inspect = call("inspect_service", service=target.service)
    history = call("experiment_history", service="")
    model = scripted(ai(inspect, history), ai(call(SUBMIT, **proposal)))

    result = run(board, model)
    spec = result.spec

    # the model saw the briefing + system prompt, and every tool (investigation + submit) was bound
    assert set(model.bound_tools) == INVESTIGATION_TOOLS | {SUBMIT}
    assert model.binds == [{}, {"tool_choice": SUBMIT}]  # a plain and a forced binding, same tools
    assert model.tool_choices == [None, None]  # both turns inside the investigation window
    first = model.invocations[0]
    assert isinstance(first[0], SystemMessage) and first[0].content.startswith(SYSTEM)
    assert f"turn 1 of {MAX_AGENT_TURNS}" in first[0].content
    assert isinstance(first[1], HumanMessage)
    for i, c in enumerate(top):
        assert f"[{i}] target={c.service} failure={c.failure_type}" in first[1].content

    # the investigation calls were answered with ToolMessages before the next turn
    assert len(model.invocations) == 2
    answers = tool_messages(model.invocations[1])
    assert set(answers) == {inspect["id"], history["id"]}
    inspected = json.loads(answers[inspect["id"]].content)
    assert inspected["service"] == target.service and "transitive_dependents" in inspected
    hist = json.loads(answers[history["id"]].content)
    if board.name == "history":
        assert {h["service"] for h in hist} == {"mongodb", "gateway", "things", "policies"}
    else:
        assert hist == "no experiments yet"

    # the plan: the chosen candidate, clamped by the catalog, recorded as the LLM's
    assert spec.planned_by == "llm"
    assert (spec.target_service, spec.failure_type) == (target.service, target.failure_type)
    expected_params, clamp_notes = clamp(CATALOG[target.failure_type], params)
    assert spec.parameters == expected_params and spec.parameters["duration_s"] == 90
    assert clamp_notes and all(n in spec.planner_notes for n in clamp_notes)
    assert any("lowered from 9999 to maximum 90" in n for n in spec.planner_notes)
    assert spec.planner_notes[-1].startswith("agent investigated with 2 tool call(s)")
    assert f"inspect_service({target.service})" in spec.planner_notes[-1]
    assert "experiment_history" in spec.planner_notes[-1]
    assert result.tool_calls == [f"inspect_service({target.service})", "experiment_history()"]
    assert spec.hypothesis.expected_affected == ["policies", "things"]
    assert ExperimentSpec.model_validate(spec.model_dump()) == spec

    # ...and it is exactly what the ORIGINAL planner builds when its LLM returns the same proposal
    original = board.build(llm=ScriptedLLM(proposal)).plan_next()
    assert original.planned_by == "llm"
    assert spec_fields(spec, drop_notes_after=len(original.planner_notes)) == spec_fields(original)
    assert len(spec.planner_notes) == len(original.planner_notes) + 1  # only the agent's own note is extra


# ports of the reference harness's LLM_SCENARIOS that are complete proposals: both planners must agree
EQUIVALENT_PROPOSALS = {
    "valid_pick_1_clamped": {"choice": 1, "parameters": {"duration_s": 9999}, "rationale": "scripted rationale",
                             "hypothesis": {"statement": "scripted statement", "expected_affected": ["mars", "gateway"],
                                            "expected_max_error_rate": 7, "expected_recovery_s": 99999}},
    "valid_pick_0_bad_params": {"choice": 0, "parameters": {"duration_s": "lots", "bogus": 1}, "rationale": "r",
                                "hypothesis": FULL_HYPOTHESIS},
    "choice_as_string": {"choice": "3", "parameters": {"duration_s": 1}, "rationale": "r", "hypothesis": FULL_HYPOTHESIS},
    "long_texts_truncated": {"choice": 4, "parameters": {}, "rationale": "x" * 1000,
                             "hypothesis": {**FULL_HYPOTHESIS, "statement": "y" * 1000}},
    "negative_rates": {"choice": 1, "parameters": {"duration_s": -50}, "rationale": "r",
                       "hypothesis": {**FULL_HYPOTHESIS, "expected_max_error_rate": -2, "expected_recovery_s": -9}},
}


@pytest.mark.parametrize("name", sorted(EQUIVALENT_PROPOSALS))
def test_accepted_proposal_matches_original_llm_path(board, scripted, name):
    proposal = EQUIVALENT_PROPOSALS[name]
    result = run(board, scripted(ai(call(SUBMIT, **proposal))))
    original = board.build(llm=ScriptedLLM(proposal)).plan_next()
    assert original.planned_by == "llm" and result.spec.planned_by == "llm"
    assert spec_fields(result.spec, drop_notes_after=len(original.planner_notes)) == spec_fields(original)
    assert result.spec.planner_notes[len(original.planner_notes):] == ["agent investigated with 0 tool call(s)"]


# regression: omitted hypothesis fields fall back to the original per-failure defaults, not schema defaults
def test_partial_hypothesis_uses_original_defaults(board, scripted):
    proposal = {"choice": 1, "parameters": {}, "rationale": "r", "hypothesis": {"statement": "only a statement"}}
    result = run(board, scripted(ai(call(SUBMIT, **proposal))))
    original = board.build(llm=ScriptedLLM(proposal)).plan_next()
    assert result.spec.hypothesis == original.hypothesis


# ---------------------------------------------------------------- 3. self-correction
def test_rejected_choice_is_fed_back_and_corrected(board, scripted):
    top = board.build().candidates()[:5]
    bad = submit(7)
    model = scripted(ai(bad), ai(submit(1, hypothesis=FULL_HYPOTHESIS)))
    result = run(board, model)

    assert len(model.invocations) == 2
    feedback = model.invocations[1][-1]
    assert isinstance(feedback, ToolMessage) and feedback.tool_call_id == bad["id"]
    assert "REJECTED" in feedback.content and "not a listed candidate" in feedback.content
    assert "choice 7" in feedback.content and "0..4" in feedback.content

    spec = result.spec
    assert spec.planned_by == "llm"
    assert (spec.target_service, spec.failure_type) == (top[1].service, top[1].failure_type)
    assert spec.planner_notes[-1] == "agent corrected its plan after 1 rejection(s)"
    assert any("rejected -- choice 7 is not a listed candidate" in t for t in result.trace)


# ---------------------------------------------------------------- 4. malformed submissions
MALFORMED = {
    "hypothesis_is_a_string": (submit(0, hypothesis="oops"),
                               "hypothesis: Input should be a valid dictionary or instance of Hypothesis"),
    "choice_not_a_number": (submit("abc"), "choice: Input should be a valid integer"),
    "missing_rationale": (submit(0, rationale=None), "rationale: Field required"),
    "missing_hypothesis_statement": (submit(0, hypothesis={"expected_max_error_rate": 0.5}),
                                     "hypothesis.statement: Field required"),
    "parameters_not_a_dict": (call(SUBMIT, choice=0, parameters=[1, 2], rationale="r", hypothesis=FULL_HYPOTHESIS),
                              "parameters: Input should be a valid dictionary"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_submission_rejected_with_precise_reason(demo, scripted, name):
    bad, reason = MALFORMED[name]
    model = scripted(ai(bad), ai(submit(0)))
    result = run(demo, model)
    feedback = model.invocations[1][-1]
    assert isinstance(feedback, ToolMessage) and feedback.tool_call_id == bad["id"]
    assert feedback.content.startswith("REJECTED: malformed plan (") and reason in feedback.content, feedback.content
    assert result.spec.planned_by == "llm"
    assert result.spec.planner_notes[-1] == "agent corrected its plan after 1 rejection(s)"


def test_repeated_malformed_submissions_fall_back(board, scripted):
    reasons = [MALFORMED[k][1] for k in ("hypothesis_is_a_string", "choice_not_a_number", "missing_rationale")]
    model = scripted(*(ai(MALFORMED[k][0]) for k in ("hypothesis_is_a_string", "choice_not_a_number", "missing_rationale")),
                     ai(submit(0)))  # never reached
    result = run(board, model)

    assert MAX_SUBMISSIONS == 3 and len(model.invocations) == 3
    rejections = [t for t in result.trace if "rejected --" in t]
    assert len(rejections) == 3
    for line, reason in zip(rejections, reasons):
        assert reason in line, (reason, line)
    for i in (1, 2):  # the model saw the first two rejections as ToolMessages
        assert reasons[i - 1] in model.invocations[i][-1].content
    note = assert_deterministic_fallback(board, result, "plan rejected 3x", reasons[-1])
    assert note.endswith("; deterministic choice")


def test_string_hypothesis_is_an_intentional_divergence_from_the_original(demo, scripted):
    """The original _ask_llm only catches TypeError/ValueError/KeyError, so a string hypothesis escapes as an
    AttributeError and crashes `faultline plan`. The agent path must reject it and keep going instead."""
    answer = {"choice": 0, "hypothesis": "oops"}
    with pytest.raises(AttributeError):
        demo.build(llm=ScriptedLLM(answer)).plan_next()
    result = run(demo, scripted(ai(call(SUBMIT, **answer))))  # repeated every turn
    assert_deterministic_fallback(demo, result, "plan rejected 3x")


# ---------------------------------------------------------------- 5. model raises
@pytest.mark.parametrize("exc", [ConnectionError("connection refused"), TimeoutError("read timed out"),
                                 RuntimeError("model does not support tools")])
def test_model_exception_falls_back_without_raising(board, scripted, exc):
    model = scripted(exc)
    result = run(board, model)
    assert len(model.invocations) == 1
    assert_deterministic_fallback(board, result, type(exc).__name__, str(exc))
    assert any("model call failed" in t for t in result.trace)


def test_model_exception_after_investigation_falls_back(demo, scripted):
    model = scripted(ai(call("baseline_summary")), ConnectionError("dropped"))
    result = run(demo, model)
    assert len(model.invocations) == 2
    assert_deterministic_fallback(demo, result, "ConnectionError: dropped")


# ---------------------------------------------------------------- 6. never submits
def test_model_that_never_submits_stops_at_turn_budget(board, scripted):
    model = scripted(ai(call("inspect_service", service="mongodb")))  # repeats forever
    result = run(board, model)  # a GraphRecursionError would propagate here
    assert MAX_AGENT_TURNS == 6 and len(model.invocations) == MAX_AGENT_TURNS
    assert_deterministic_fallback(board, result, f"{MAX_AGENT_TURNS}-turn budget")


def test_forced_submit_binding_after_investigation_window(demo, scripted):
    """A model that keeps investigating is switched to the tool_choice=submit_plan binding from turn
    INVESTIGATE_TURNS+1 on, and is told so in the system prompt."""
    model = scripted(ai(call("inspect_service", service="mongodb")))
    result = run(demo, model)
    assert INVESTIGATE_TURNS == 3
    free, forced = INVESTIGATE_TURNS, MAX_AGENT_TURNS - INVESTIGATE_TURNS
    assert model.tool_choices == [None] * free + [SUBMIT] * forced
    for turn, msgs in enumerate(model.invocations, 1):
        system = msgs[0].content
        assert f"turn {turn} of {MAX_AGENT_TURNS}" in system
        assert ("Investigation time is over" in system) == (turn > INVESTIGATE_TURNS)
    assert_deterministic_fallback(demo, result, f"{MAX_AGENT_TURNS}-turn budget")


@pytest.mark.parametrize("investigate_turns", [1, 2])
def test_investigate_turns_is_configurable(demo, scripted, investigate_turns):
    model = scripted(ai(call("baseline_summary")))
    run(demo, model, investigate_turns=investigate_turns)
    assert model.tool_choices == [None] * investigate_turns + [SUBMIT] * (MAX_AGENT_TURNS - investigate_turns)


def test_forced_submit_after_investigation_is_accepted(demo, scripted):
    top = demo.build().candidates()[:5]
    look = ai(call("baseline_summary"))
    model = scripted(look, look, look, ai(submit(2)))
    result = run(demo, model)
    assert model.tool_choices == [None, None, None, SUBMIT]
    assert result.spec.planned_by == "llm" and result.spec.failure_type == top[2].failure_type


def test_forced_invoke_failure_falls_back_to_unforced_binding(demo, scripted):
    """A provider that rejects tool_choice at call time: that turn is retried on the plain binding."""
    look = ai(call("baseline_summary"))
    model = scripted(look, look, look, ai(submit(1)), forced_error=ValueError("tool_choice not supported"))
    result = run(demo, model)
    assert model.attempts == [None, None, None, SUBMIT, None]  # forced tried on turn 4, then the plain binding
    assert model.tool_choices == [None, None, None, None]
    assert len(model.invocations) == 4
    assert result.spec.planned_by == "llm"
    assert not any("model call failed" in t for t in result.trace)


def test_forced_invoke_failure_every_turn_still_ends_at_budget(demo, scripted):
    model = scripted(ai(call("baseline_summary")), forced_error=ValueError("tool_choice not supported"))
    result = run(demo, model)
    assert model.attempts.count(SUBMIT) == MAX_AGENT_TURNS - INVESTIGATE_TURNS
    assert model.tool_choices == [None] * MAX_AGENT_TURNS
    assert_deterministic_fallback(demo, result, f"{MAX_AGENT_TURNS}-turn budget")


def test_forced_and_plain_both_failing_falls_back_with_the_plain_error(demo, scripted):
    look = ai(call("baseline_summary"))
    model = scripted(look, look, look, ConnectionError("down"), forced_error=ValueError("tool_choice not supported"))
    result = run(demo, model)
    assert model.attempts == [None, None, None, SUBMIT, None]
    assert_deterministic_fallback(demo, result, "ConnectionError: down")


def test_bind_tools_rejecting_tool_choice_uses_plain_binding(demo, scripted):
    look = ai(call("baseline_summary"))
    model = scripted(look, look, look, ai(submit(0)), bind_error=NotImplementedError("no tool_choice"))
    result = run(demo, model)
    assert model.binds == [{}, {"tool_choice": SUBMIT}]
    assert model.tool_choices == [None, None, None, None]
    assert result.spec.planned_by == "llm"


@pytest.mark.parametrize("max_turns", [1, 3])
def test_turn_budget_is_configurable(demo, scripted, max_turns):
    model = scripted(ai(call("experiment_history", service="")))
    result = run(demo, model, max_turns=max_turns)
    assert len(model.invocations) == max_turns
    assert_deterministic_fallback(demo, result, f"{max_turns}-turn budget")


def test_rejection_on_the_last_turn_falls_back(demo, scripted):
    model = scripted(ai(call("baseline_summary")), ai(submit(9)))
    result = run(demo, model, max_turns=2)
    assert len(model.invocations) == 2
    assert_deterministic_fallback(demo, result, "2-turn budget is spent", "choice 9 is not a listed candidate")


# ---------------------------------------------------------------- 7. text-only models
def test_text_only_json_answer_is_accepted(board, scripted):
    top = board.build().candidates()[:5]
    proposal = {"choice": 3, "parameters": {"duration_s": 20}, "rationale": "text answer",
                "hypothesis": FULL_HYPOTHESIS}
    text = ("<think>maybe {\"choice\": 0} ... no, the datastore {is} the point</think>\n"
            "Here is my plan:\n```json\n" + json.dumps(proposal) + "\n```\nThanks.")
    model = scripted(ai(content=text))
    result = run(board, model)
    spec = result.spec
    assert spec.planned_by == "llm"
    assert (spec.target_service, spec.failure_type) == (top[3].service, top[3].failure_type)
    assert spec.rationale == "text answer" and spec.parameters["duration_s"] == 20
    original = board.build(llm=ScriptedLLM(proposal)).plan_next()
    assert spec_fields(spec, drop_notes_after=len(original.planner_notes)) == spec_fields(original)
    assert any("answered in text" in t for t in result.trace)


def test_text_only_without_json_gets_human_feedback(demo, scripted):
    model = scripted(ai(content="I would probably break mongodb."),
                     ai(content=json.dumps({"choice": 0, "parameters": {}, "rationale": "r", "hypothesis": FULL_HYPOTHESIS})))
    result = run(demo, model)
    feedback = model.invocations[1][-1]
    assert isinstance(feedback, HumanMessage)  # no tool_call_id to answer
    assert "REJECTED" in feedback.content and "no plan submitted" in feedback.content
    assert result.spec.planned_by == "llm"


def test_text_only_model_that_never_answers_falls_back(demo, scripted):
    model = scripted(ai(content="hmm"))
    result = run(demo, model)
    assert len(model.invocations) == MAX_SUBMISSIONS
    assert_deterministic_fallback(demo, result, "plan rejected 3x", "no plan submitted")


# ---------------------------------------------------------------- 9. unknown tools
def test_unknown_tool_gets_error_message_not_a_crash(demo, scripted):
    rogue = call("delete_everything", confirm=True)
    model = scripted(ai(rogue), ai(submit(1)))
    result = run(demo, model)
    answer = tool_messages(model.invocations[1])[rogue["id"]]
    assert "delete_everything is not a valid tool" in answer.content
    assert result.spec.planned_by == "llm"


def test_unknown_tool_alongside_submit_is_answered(demo, scripted):
    rogue = call("delete_everything")
    peek = call("failure_type_info", failure_type="network_delay")
    bad = submit(9)
    model = scripted(ai(rogue, peek, bad), ai(submit(0)))
    result = run(demo, model)
    answers = tool_messages(model.invocations[1])
    assert set(answers) == {rogue["id"], peek["id"], bad["id"]}  # every call answered: conversation well-formed
    assert answers[rogue["id"]].content == "unknown tool delete_everything"
    assert json.loads(answers[peek["id"]].content)["failure_type"] == "network_delay"
    assert "REJECTED" in answers[bad["id"]].content
    assert result.spec.planned_by == "llm"


def test_tool_with_bad_arguments_gets_error_message(demo, scripted):
    broken = call("inspect_service")  # missing required `service`
    model = scripted(ai(broken), ai(submit(0)))
    result = run(demo, model)
    answer = tool_messages(model.invocations[1])[broken["id"]]
    assert "service" in answer.content and "Field required" in answer.content
    assert result.spec.planned_by == "llm"


# regression: bad arguments to a tool called alongside submit_plan become an error ToolMessage, not a crash
def test_bad_tool_arguments_alongside_submit_do_not_raise(demo, scripted):
    model = scripted(ai(call("inspect_service"), submit(1)))
    result = run(demo, model)
    assert result.spec is not None
