"""Offline fixtures for the LangGraph planner tests: no Docker, no network, no real LLM.

These tests live outside tests/ on purpose: pyproject's testpaths=["tests"] never collects them, so the base
suite never needs langgraph. Run them with `python -m pytest faultline_langgraph/tests -q`.

The blackboard fixture is fixtures/reports-demo/: topology.json, baseline.json and experiments/*.json copied
byte-for-byte from reports-demo/ at commit 887b85f (this branch has no reports-demo/).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

pytest.importorskip("langgraph")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import ConfigDict, Field  # noqa: E402

from faultline.config import ROOT, Settings  # noqa: E402
from faultline.planning.planner import Planner, Policy  # noqa: E402
from faultline.reporting.store import ReportStore  # noqa: E402
from faultline.safety.thresholds import load_policy  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "reports-demo"
IMAGES = frozenset({"pumba", "tc"})
LABEL = "langgraph:scripted:fake"


# ---------------------------------------------------------------- blackboard + planner
def stamp_history_mtimes(reports_dir: Path) -> list[str]:
    """ReportStore orders history by file mtime (store.py experiment_ids), and history[-1] drives the
    "same target as last run" penalty. Git scrambles mtimes, so stamp each report with its started_at."""
    order = []
    for f in (reports_dir / "experiments").glob("*.json"):
        ts = datetime.fromisoformat(json.loads(f.read_text())["started_at"].replace("Z", "+00:00")).timestamp()
        os.utime(f, (ts, ts))
        order.append((ts, f.stem))
    return [eid for _, eid in sorted(order)]


def _copy_fixture(dst: Path, history: bool) -> Path:
    shutil.copytree(FIXTURE, dst)
    if history:
        stamp_history_mtimes(dst)
    else:
        shutil.rmtree(dst / "experiments")
    return dst


def _settings(reports_dir: Path) -> Settings:
    # A plain Settings object (no load_settings): no .env, no environment, LLM off for the original client.
    return Settings(reports_dir=str(reports_dir), thresholds_file=str(ROOT / "config" / "thresholds.yaml"),
                    llm_enabled=False)


class Blackboard:
    """One copy of the fixture; build() returns a fresh Planner wired the way `faultline plan` wires it."""

    def __init__(self, root: Path, name: str):
        self.root, self.name = root, name
        self.settings = _settings(root)
        self.store = ReportStore(root)

    def build(self, llm: Any = None, images=IMAGES, allow_protected: bool = False) -> Planner:
        s = self.settings
        return Planner(self.store.topology(), Policy(load_policy(s.thresholds_file)), self.store.experiments(), llm,
                       s, set(images), allow_protected)


@pytest.fixture
def demo(tmp_path) -> Blackboard:
    return Blackboard(_copy_fixture(tmp_path / "reports-demo", history=True), "history")


@pytest.fixture
def empty(tmp_path) -> Blackboard:
    return Blackboard(_copy_fixture(tmp_path / "reports-empty", history=False), "empty")


@pytest.fixture(params=["history", "empty"])
def board(request, tmp_path) -> Blackboard:
    return Blackboard(_copy_fixture(tmp_path / request.param, history=request.param == "history"), request.param)


@pytest.fixture
def planner(demo) -> Planner:
    return demo.build()


# ---------------------------------------------------------------- the original LLM seam
class ScriptedLLM:
    """Duck-types the LLMClient surface the ORIGINAL Planner touches; returns one fixed chat_json answer."""

    def __init__(self, answer: Any, label: str = LABEL):
        self.answer, self.label, self.last_error = answer, label, ""

    def available(self) -> bool:
        return True

    def describe(self) -> str:
        return self.label

    def chat_json(self, system: str, user: str, max_tokens: int = 700):
        return copy.deepcopy(self.answer)


# ---------------------------------------------------------------- scripted LangChain chat model
_ids = iter(range(10**9))


def call(name: str, **args: Any) -> dict:
    """A tool call as a chat model emits it."""
    return {"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def ai(*calls: dict, content: str = "") -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls))


def submit(choice: Any = 0, parameters: Optional[dict] = None, rationale: Any = "scripted rationale",
           hypothesis: Any = "default", **extra: Any) -> dict:
    """A submit_plan call. Pass rationale=None / hypothesis=None to omit the key entirely."""
    args: dict[str, Any] = {"choice": choice, "parameters": parameters or {}}
    if rationale is not None:
        args["rationale"] = rationale
    if hypothesis == "default":
        hypothesis = {"statement": "scripted statement", "expected_affected": ["gateway"],
                      "expected_max_error_rate": 0.4, "expected_recovery_s": 30}
    if hypothesis is not None:
        args["hypothesis"] = hypothesis
    args.update(extra)
    return call("submit_plan", **args)


class ScriptedChatModel(BaseChatModel):
    """Each invoke returns the next scripted item (an AIMessage, or raises it if it is an exception, or calls it
    with the turn number if it is a callable). When the script runs out the last item repeats. Every
    invocation's messages are recorded.

    bind_tools(tools, **kwargs) records the tool names and kwargs and returns a _Binding that delegates to this
    model, so tests can see which binding (plain, or tool_choice="submit_plan") answered each turn.
    `bind_error` raises from bind_tools when a tool_choice is requested; `forced_error` raises whenever the
    forced binding is invoked (a provider rejecting tool_choice at call time)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list = Field(default_factory=list)
    invocations: list = Field(default_factory=list)   # messages of every answered call
    tool_choices: list = Field(default_factory=list)  # tool_choice of the binding behind each answered call
    attempts: list = Field(default_factory=list)      # tool_choice of every binding.invoke, including failed ones
    bound_tools: list = Field(default_factory=list)
    binds: list = Field(default_factory=list)         # kwargs of every bind_tools call
    bind_error: Optional[Any] = None
    forced_error: Optional[Any] = None

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools, **kwargs):
        self.binds.append(dict(kwargs))
        if kwargs.get("tool_choice") and self.bind_error is not None:
            raise self.bind_error
        self.bound_tools = [getattr(t, "name", None) or t.get("name") for t in tools]
        return _Binding(self, kwargs.get("tool_choice"))

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        n = len(self.invocations)
        self.invocations.append(list(messages))
        item = self.script[min(n, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        if callable(item) and not isinstance(item, BaseMessage):
            item = item(n)
        msg = item.model_copy(deep=True)
        # fresh ids per turn when a scripted message repeats, so the conversation stays well-formed
        if n >= len(self.script):
            msg.tool_calls = [{**c, "id": f"{c['id']}_r{n}"} for c in msg.tool_calls]
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _Binding:
    """What bind_tools returns: invoke() goes to the scripted model, tagged with this binding's tool_choice."""

    def __init__(self, model: ScriptedChatModel, tool_choice: Optional[str]):
        self.model, self.tool_choice = model, tool_choice

    def invoke(self, messages, config=None, **kwargs):
        self.model.attempts.append(self.tool_choice)
        if self.tool_choice and self.model.forced_error is not None:
            raise self.model.forced_error
        self.model.tool_choices.append(self.tool_choice)
        return self.model.invoke(messages, config, **kwargs)


@pytest.fixture
def scripted() -> Callable[..., ScriptedChatModel]:
    return lambda *items, **kw: ScriptedChatModel(script=list(items), **kw)


# ---------------------------------------------------------------- comparisons
def spec_fields(spec, drop_notes_after: Optional[int] = None) -> dict:
    """Every ExperimentSpec field except the uuid experiment_id; optionally keep only the first N notes."""
    d = spec.model_dump(mode="json")
    d.pop("experiment_id")
    if drop_notes_after is not None:
        d["planner_notes"] = d["planner_notes"][:drop_notes_after]
    return d
