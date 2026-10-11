"""The Planner as a LangGraph agent: "LLM proposes, code disposes", with the agent doing real investigation.

    START -> rank --(no model)--------------------------------------------> fallback -> END
               |                                                              ^
               +--> agent <--> tools          (read-only investigation loop)  |
                      |                                                       |
                      +--> validate --(accepted)--> END                       |
                              |  \\--(rejected, attempts left)--> agent        |
                              +----(rejected too often / budget spent)--------+

rank      code   the ORIGINAL ranking, Planner.candidates(), unchanged; the agent only ever sees the top 5
agent     LLM    a LangChain chat model with tools bound: investigates (up to INVESTIGATE_TURNS turns, several
                 tools per turn), then calls submit_plan -- forced via tool_choice once investigation time is up
tools     code   executes the read-only investigation tools (tools.py)
validate  code   hands the proposal to the ORIGINAL Planner.plan_next() through a replay client, so the existing
                 index check, clamping and hypothesis sanitising decide -- verbatim, not reimplemented. A
                 rejection goes back to the agent as the tool result, so it can correct itself.
fallback  code   the ORIGINAL deterministic plan (Planner.plan_next() with no LLM)

So the safety property is the original one by construction: whatever the agent says, the spec that comes out
was produced by faultline.planning.planner from a top-5 candidate with clamped parameters.
"""
from __future__ import annotations

import copy
import json
import operator
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Optional, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError

from faultline.schemas import Baseline, ExperimentSpec

from ..llm import ModelHandle
from .prompts import SYSTEM, briefing
from .tools import SUBMIT, PlanProposal, build_tools

TOP_N = 5              # same restriction as the original planner
MAX_AGENT_TURNS = 6    # LLM calls per plan, investigation included
INVESTIGATE_TURNS = 3  # after this many turns every call must be submit_plan (forced via tool_choice)
MAX_SUBMISSIONS = 3    # first try + 2 corrections, then the deterministic fallback

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class PlannerState(TypedDict, total=False):
    candidates: list                                  # every ranked Candidate
    top: list                                         # the top 5: the only choices the agent has
    messages: Annotated[list[AnyMessage], add_messages]
    turns: int                                        # agent (LLM) turns taken
    submissions: int                                  # submit_plan attempts validated so far
    spec: Optional[ExperimentSpec]
    fallback_reason: str                              # set when the agent path gives up
    trace: Annotated[list[str], operator.add]         # human-readable step log


@dataclass
class PlanResult:
    spec: Optional[ExperimentSpec]
    candidates: list
    trace: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)


class _ProposalReplay:
    """Stands in for the LLMClient inside the original Planner: "available", and its one chat_json answer is the
    agent's proposal. Planner.plan_next() then validates, clamps and builds the spec with its own code."""

    def __init__(self, proposal: dict, label: str):
        self.proposal = proposal
        self.label = label
        self.last_error = ""

    def available(self) -> bool:
        return True

    def describe(self) -> str:
        return self.label

    def chat_json(self, system: str, user: str, max_tokens: int = 700) -> Optional[dict]:
        return copy.deepcopy(self.proposal)


def _with_llm(planner, llm):
    p = copy.copy(planner)
    p.llm = llm
    return p


def _json_from_text(content: Any) -> Optional[dict]:
    """For models that answer in text instead of calling submit_plan (same leniency as LLMClient.chat_json)."""
    text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content if isinstance(b, dict))
    text = _THINK.sub("", text).strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _short(call: dict) -> str:
    args = ", ".join(f"{v}" if k in ("service", "failure_type", "choice") else f"{k}={v}" for k, v in (call.get("args") or {}).items())
    return f"{call['name']}({args})"


def build_graph(planner, handle: ModelHandle, baseline: Baseline | None = None,
                max_turns: int = MAX_AGENT_TURNS, max_submissions: int = MAX_SUBMISSIONS,
                investigate_turns: int = INVESTIGATE_TURNS):
    """Compile the planning graph around an existing faultline Planner (built the way `faultline plan` builds it)."""
    model = handle.model
    # tools close over the top-5, which only exists once rank has run, so they are bound there
    bound = forced = tools_by_name = tool_node = None

    # ------------------------------------------------------------------ nodes
    def rank(state: PlannerState) -> dict:
        nonlocal bound, forced, tools_by_name, tool_node
        cands = planner.candidates()
        top = cands[:TOP_N]
        out: dict = {"candidates": cands, "top": top,
                     "trace": [f"rank: {len(cands)} candidates; top {len(top)}: " +
                               ", ".join(f"[{i}] {c.service}/{c.failure_type} {c.priority:g}" for i, c in enumerate(top))]}
        if cands and model is not None:
            investigate, submit = build_tools(planner, top, baseline)
            tools_by_name = {t.name: t for t in investigate}
            tool_node = ToolNode(investigate)
            bound = model.bind_tools(investigate + [submit])
            try:  # same tools, but the model must call submit_plan
                forced = model.bind_tools(investigate + [submit], tool_choice=SUBMIT)
            except Exception:  # noqa: BLE001 -- provider without forced tool choice: rely on the prompt
                forced = bound
            out["messages"] = [HumanMessage(briefing(planner, top, investigate_turns, max_turns))]
        return out

    def agent(state: PlannerState) -> dict:
        turns = state.get("turns", 0) + 1
        must_submit = turns > investigate_turns
        clock = (f"\n\nThis is turn {turns} of {max_turns}. " +
                 (f"Investigation time is over: call {SUBMIT} now." if must_submit else
                  f"You may investigate for {investigate_turns - turns + 1} more turn(s) including this one; "
                  "call several tools in one turn, or submit as soon as you are confident."))
        prompt = [SystemMessage(SYSTEM + clock)] + state["messages"]
        try:
            try:
                ai = (forced if must_submit else bound).invoke(prompt)
            except Exception:  # noqa: BLE001 -- a provider may reject the forced tool_choice at call time
                if not must_submit or forced is bound:
                    raise
                ai = bound.invoke(prompt)
        except Exception as exc:  # noqa: BLE001 -- network, auth, model without tool support: never raise
            return {"turns": turns, "fallback_reason": f"{type(exc).__name__}: {str(exc)[:240]}",
                    "trace": [f"agent turn {turns}: model call failed ({type(exc).__name__})"]}
        calls = getattr(ai, "tool_calls", None) or []
        out: dict = {"messages": [ai], "turns": turns,
                     "trace": [f"agent turn {turns}: " + (", ".join(_short(c) for c in calls) if calls else "answered in text")]}
        if turns >= max_turns and not any(c["name"] == SUBMIT for c in calls) and calls:
            out["fallback_reason"] = f"agent used its {max_turns}-turn budget without submitting a plan"
        return out

    def validate(state: PlannerState) -> dict:
        ai: AIMessage = state["messages"][-1]
        top = state["top"]
        replies: list[AnyMessage] = []
        proposal, call_id = None, None
        for call in getattr(ai, "tool_calls", None) or []:
            if call["name"] == SUBMIT and call_id is None:
                proposal, call_id = call.get("args") or {}, call["id"]
            elif call["name"] == SUBMIT:
                replies.append(ToolMessage("ignored: only the first submit_plan per turn counts", tool_call_id=call["id"]))
            elif call["name"] in tools_by_name:  # answered so the conversation stays well-formed
                try:
                    result = str(tools_by_name[call["name"]].invoke(call.get("args") or {}))
                except Exception as exc:  # noqa: BLE001 -- bad tool arguments are the model's problem, not a crash
                    result = f"error: {type(exc).__name__}: {exc}"
                replies.append(ToolMessage(result, tool_call_id=call["id"]))
            else:
                replies.append(ToolMessage(f"unknown tool {call['name']}", tool_call_id=call["id"]))
        if call_id is None:
            proposal = _json_from_text(ai.content)

        submissions = state.get("submissions", 0) + 1
        problem, spec = _dispose(proposal, top, planner, handle.label)
        if spec is not None:
            ack = [ToolMessage(f"accepted: {spec.failure_type} on {spec.target_service} {spec.parameters}", tool_call_id=call_id)] if call_id else []
            return {"spec": spec, "submissions": submissions, "messages": replies + ack,
                    "trace": [f"validate #{submissions}: accepted [{spec.planner_notes[0] if spec.planner_notes else ''}]"]}
        feedback = f"REJECTED: {problem}. Fix this and call {SUBMIT} again (choice must be an index 0..{len(top) - 1})."
        msg = ToolMessage(feedback, tool_call_id=call_id) if call_id else HumanMessage(feedback)
        out: dict = {"submissions": submissions, "messages": replies + [msg],
                     "trace": [f"validate #{submissions}: rejected -- {problem}"]}
        if submissions >= max_submissions:
            out["fallback_reason"] = f"plan rejected {submissions}x, last: {problem}"
        elif state.get("turns", 0) >= max_turns:
            out["fallback_reason"] = f"plan rejected ({problem}) and the {max_turns}-turn budget is spent"
        return out

    def fallback(state: PlannerState) -> dict:
        spec = _with_llm(planner, None).plan_next()  # the original deterministic path, verbatim
        reason = state.get("fallback_reason")
        if spec is not None and reason:  # only when the agent was actually tried, as the original notes it
            spec.planner_notes.append(f"LLM ({handle.label}) gave no usable answer: {reason}; deterministic choice")
        return {"spec": spec, "trace": ["fallback: deterministic plan" + (f" ({reason})" if reason else "")]}

    # ------------------------------------------------------------------ edges
    def after_rank(state: PlannerState) -> str:
        if not state["candidates"]:
            return END
        return "agent" if model is not None else "fallback"

    def after_agent(state: PlannerState) -> str:
        if state.get("fallback_reason"):
            return "fallback"
        calls = getattr(state["messages"][-1], "tool_calls", None) or []
        if any(c["name"] == SUBMIT for c in calls) or not calls:
            return "validate"
        return "tools"

    def after_validate(state: PlannerState) -> str:
        if state.get("spec") is not None:
            return END
        return "fallback" if state.get("fallback_reason") else "agent"

    g = StateGraph(PlannerState)
    g.add_node("rank", rank)
    g.add_node("agent", agent)
    g.add_node("tools", lambda s: tool_node.invoke(s))
    g.add_node("validate", validate)
    g.add_node("fallback", fallback)
    g.add_edge(START, "rank")
    g.add_conditional_edges("rank", after_rank, {"agent": "agent", "fallback": "fallback", END: END})
    g.add_conditional_edges("agent", after_agent, {"tools": "tools", "validate": "validate", "fallback": "fallback"})
    g.add_edge("tools", "agent")
    g.add_conditional_edges("validate", after_validate, {"agent": "agent", "fallback": "fallback", END: END})
    g.add_edge("fallback", END)
    return g.compile()


def _dispose(proposal: Any, top: list, planner, label: str) -> tuple[str, Optional[ExperimentSpec]]:
    """(problem, None) or ("", spec). Schema errors are caught here so the agent gets a precise reason; the
    acceptance decision itself is the original Planner's."""
    if not isinstance(proposal, dict) or not proposal:
        return f"no plan submitted; call {SUBMIT} with choice, parameters, rationale, hypothesis", None
    try:
        p = PlanProposal.model_validate(proposal)
    except ValidationError as exc:
        errs = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])
        return f"malformed plan ({errs})", None
    if not 0 <= p.choice < len(top):
        return f"choice {p.choice} is not a listed candidate", None
    try:
        spec = _with_llm(planner, _ProposalReplay(p.model_dump(exclude_unset=True), label)).plan_next()
    except Exception as exc:  # noqa: BLE001 -- the original code raising on odd input is still a rejection
        return f"planner validation failed ({type(exc).__name__}: {exc})", None
    if spec is None or spec.planned_by != "llm":
        return "the planner's validation rejected it", None
    return "", spec


def plan_next(planner, handle: ModelHandle, baseline: Baseline | None = None, *,
              max_turns: int = MAX_AGENT_TURNS, max_submissions: int = MAX_SUBMISSIONS,
              investigate_turns: int = INVESTIGATE_TURNS) -> PlanResult:
    """Run the graph once. Never raises on LLM trouble: the worst case is the original deterministic plan."""
    graph = build_graph(planner, handle, baseline, max_turns, max_submissions, investigate_turns)
    final = graph.invoke({}, config={"recursion_limit": 4 * max_turns + 10})
    spec = final.get("spec")
    calls = [_short(c) for m in final.get("messages", []) if isinstance(m, AIMessage) for c in (m.tool_calls or [])
             if c["name"] != SUBMIT]
    if spec is not None and spec.planned_by == "llm":
        spec.planner_notes.append(f"agent investigated with {len(calls)} tool call(s)" + (f": {', '.join(calls)}" if calls else ""))
        if final.get("submissions", 1) > 1:
            spec.planner_notes.append(f"agent corrected its plan after {final['submissions'] - 1} rejection(s)")
    return PlanResult(spec, final.get("candidates", []), final.get("trace", []), calls)
