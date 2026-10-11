"""The safety property (test 8): whatever the agent submits, the spec that comes out targets a top-5 candidate,
with every parameter inside the catalog bounds and a sane hypothesis. Each adversarial proposal is repeated on
every turn, so the run ends either accepted (sanitised) or in the deterministic fallback."""
from __future__ import annotations

import math

import pytest

from faultline.chaos.catalog import CATALOG
from faultline.schemas import ExperimentSpec
from faultline_langgraph.llm import ModelHandle
from faultline_langgraph.planner import plan_next
from faultline_langgraph.planner.tools import SUBMIT

from .conftest import LABEL, ai, call

HYP = {"statement": "s", "expected_affected": ["gateway"], "expected_max_error_rate": 0.5, "expected_recovery_s": 30}
HUGE = {"duration_s": 1e12, "delay_ms": 1e9, "jitter_ms": -1e9, "loss_percent": 1e6, "workers": 99, "writers": -4,
        "size_mb": 1e15, "rps": 10**9, "block_restart": "definitely"}


def proposal(choice=0, parameters=None, hypothesis=None, rationale="r"):
    return {"choice": choice, "parameters": parameters if parameters is not None else {}, "rationale": rationale,
            "hypothesis": {**HYP, **(hypothesis or {})}}


ADVERSARIAL = {
    "negative_choice": proposal(-1),
    "huge_choice": proposal(10**9),
    "choice_as_bool": proposal(True),
    "choice_as_float": proposal(4.0),
    "huge_parameters_0": proposal(0, HUGE),
    "huge_parameters_1": proposal(1, HUGE),
    "huge_parameters_2": proposal(2, HUGE),
    "huge_parameters_3": proposal(3, HUGE),
    "huge_parameters_4": proposal(4, HUGE),
    "negative_parameters": proposal(0, {k: -abs(v) if isinstance(v, (int, float)) else v for k, v in HUGE.items()}),
    "unknown_parameter_names": proposal(1, {"rm_rf": "/", "target": "nginx", "duration_s": "forever", "privileged": True}),
    "infinite_parameter": proposal(0, {"duration_s": "Infinity", "delay_ms": "-inf"}),
    "parameter_types_wrong": proposal(2, {"duration_s": [1, 2], "workers": {"n": 1}, "size_mb": None}),
    "error_rate_5": proposal(0, hypothesis={"expected_max_error_rate": 5}),
    "error_rate_negative": proposal(0, hypothesis={"expected_max_error_rate": -3, "expected_recovery_s": -10}),
    "recovery_huge": proposal(1, hypothesis={"expected_recovery_s": 1e9}),
    "unknown_affected_services": proposal(0, hypothesis={"expected_affected": ["mars", "venus", "; rm -rf /"]}),
    "affected_not_a_list": proposal(0, hypothesis={"expected_affected": "gateway"}),
    "everything_at_once": proposal(-7, HUGE, {"expected_max_error_rate": 1e9, "expected_affected": ["mars"]}),
    # NaN fails both bound checks in catalog.clamp(); PlanProposal rejects non-finite parameters before it
    "nan_parameter": proposal(0, {"duration_s": "nan", "delay_ms": float("nan")}),
}


def _bounded(spec: ExperimentSpec):
    ft = CATALOG[spec.failure_type]
    assert set(spec.parameters) == {p.name for p in ft.params}, spec.parameters
    for p in ft.params:
        v = spec.parameters[p.name]
        if isinstance(p.default, bool):
            assert isinstance(v, bool)
            continue
        assert isinstance(v, (int, float)) and not isinstance(v, bool), (p.name, v)
        assert math.isfinite(v), (p.name, v)
        if p.lo is not None:
            assert v >= p.lo, (p.name, v)
        if p.hi is not None:
            assert v <= p.hi, (p.name, v)


@pytest.mark.parametrize("prop", list(ADVERSARIAL.values()), ids=list(ADVERSARIAL))
def test_spec_is_always_safe(board, scripted, prop):
    planner = board.build()
    top = {(c.service, c.failure_type) for c in planner.candidates()[:5]}
    model = scripted(ai(call(SUBMIT, **prop)))
    result = plan_next(planner, ModelHandle(model, LABEL), board.store.baseline())
    spec = result.spec

    assert spec is not None
    assert (spec.target_service, spec.failure_type) in top
    _bounded(spec)
    h = spec.hypothesis
    assert 0.0 <= h.expected_max_error_rate <= 1.0
    assert 1.0 <= h.expected_recovery_s <= 600.0
    assert set(h.expected_affected) <= set(planner.topo.services)
    assert len(spec.rationale) <= 300 and len(h.statement) <= 400
    assert ExperimentSpec.model_validate(spec.model_dump()) == spec
