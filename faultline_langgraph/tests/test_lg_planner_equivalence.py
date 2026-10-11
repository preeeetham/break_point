"""Deterministic equivalence: with no model, the LangGraph planner IS the original planner (test 1)."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from faultline.config import ROOT
from faultline.planning.planner import Policy
from faultline.schemas import ExperimentSpec
from faultline_langgraph.llm import ModelHandle
from faultline_langgraph.planner import plan_next

from .conftest import LABEL, spec_fields


def _run(board, model=None):
    planner = board.build()
    return plan_next(planner, ModelHandle(model, LABEL, "" if model else "LLM disabled"), board.store.baseline())


def test_ranking_identical_to_original(board):
    result = _run(board)
    original = board.build().candidates()
    assert original, "fixture must produce candidates"
    assert [asdict(c) for c in result.candidates] == [asdict(c) for c in original]


def test_deterministic_spec_identical_to_original(board):
    result = _run(board)
    original = board.build().plan_next()
    assert result.spec is not None and original is not None
    assert result.spec.planned_by == "deterministic"
    assert spec_fields(result.spec) == spec_fields(original)
    assert result.spec.experiment_id != original.experiment_id  # fresh uuid each time, the only difference
    assert ExperimentSpec.model_validate(result.spec.model_dump()) == result.spec
    assert result.tool_calls == []
    assert result.trace[-1] == "fallback: deterministic plan"  # no "(reason)": the agent was never tried


def test_reference_values_with_history(demo):
    """Literal reference values from the original planner on reports-demo (images {pumba, tc})."""
    result = _run(demo)
    assert [r.target["service"] for r in demo.build().history] == ["mongodb", "gateway", "things", "policies"]
    assert len(result.candidates) == 55
    assert [(c.service, c.failure_type, c.priority) for c in result.candidates[:6]] == [
        ("mongodb", "network_delay", 14.15), ("mongodb", "db_flood", 14.10), ("mongodb", "cpu_stress", 13.85),
        ("mongodb", "disk_pressure", 13.75), ("mongodb", "network_loss", 7.80), ("policies", "container_crash", 6.90),
    ]
    assert (result.spec.target_service, result.spec.failure_type) == ("mongodb", "network_delay")


def test_reference_values_without_history(empty):
    result = _run(empty)
    assert empty.build().history == []
    assert [(c.service, c.failure_type, c.priority) for c in result.candidates[:5]] == [
        ("mongodb", "network_delay", 14.15), ("mongodb", "db_flood", 14.10), ("mongodb", "network_loss", 14.05),
        ("mongodb", "cpu_stress", 13.85), ("mongodb", "disk_pressure", 13.75),
    ]


def test_no_candidates_gives_no_spec_like_the_original(demo, scripted):
    """Policy that skips every role: the original returns None, and the graph ends at rank without asking a model."""
    model = scripted()  # an empty script would raise if invoked
    planner = demo.build()
    planner.policy = Policy({"policy": {"skip_roles": ["app", "datastore", "proxy", "ui"]}})
    assert planner.plan_next() is None
    result = plan_next(planner, ModelHandle(model, LABEL), demo.store.baseline())
    assert result.spec is None and result.candidates == []
    assert model.invocations == []


def test_faultline_never_imports_faultline_langgraph():
    src = Path(ROOT) / "faultline"
    offenders = [str(p.relative_to(ROOT)) for p in sorted(src.rglob("*.py")) if "faultline_langgraph" in p.read_text()]
    assert list(src.rglob("*.py")), "source tree not found"
    assert offenders == []
