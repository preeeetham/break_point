"""Reconcile agent. Predicted-vs-confirmed over synthetic reports: no Docker, no LLM, no network."""
from __future__ import annotations

from faultline.agents import reconcile as agent
from faultline.schemas import (
    PREDICTED_LABEL,
    Finding,
    PredictedWeakness,
    SuggestedExperiment,
    WeaknessReport,
)

PROJECT = "bosch-ditto"


def _weakness(
    weakness_id: str,
    *,
    category: str = "shallow_health_check",
    services: list[str] | None = None,
    severity: float = 7.5,
    level: str = "high",
    codes: list[str] | None = None,
) -> PredictedWeakness:
    return PredictedWeakness(
        weakness_id=weakness_id,
        category=category,  # type: ignore[arg-type]
        services=services if services is not None else ["policies"],
        title=f"{category} on {services or ['policies']}",
        evidence="healthcheck in docker-compose.yml probes /alive only",
        predicted_severity=severity,
        predicted_level=level,  # type: ignore[arg-type]
        factors={"criticality": 1.0},
        suggested_experiment=SuggestedExperiment(failure_type="container_stop", target_service="policies"),
        confirming_finding_codes=codes if codes is not None else ["health_check_masked_failure"],
        why_it_matters="an orchestrator would never restart the container",
    )


def _report(weaknesses: list[PredictedWeakness]) -> WeaknessReport:
    return WeaknessReport(project=PROJECT, compose_file="docker-compose.yml", weaknesses=weaknesses)


# ---------------------------------------------------------------- states
def test_confirmed_quotes_the_live_finding(make_report):
    w = _weakness("wk-001")
    exp = make_report(
        experiment_id="3f9a1c2b7d4e",
        service="policies",
        failure_type="container_stop",
        finding_codes=["health_check_masked_failure"],
    )
    rec = agent.reconcile(_report([w]), [exp], PROJECT)

    assert (rec.confirmed, rec.cleared, rec.untested) == (1, 0, 0)
    o = rec.outcomes[0]
    assert o.state == "confirmed"
    assert o.confirming_findings == ["3f9a1c2b7d4e:health_check_masked_failure"]
    assert o.experiments == ["3f9a1c2b7d4e"]
    assert "3f9a1c2b7d4e" in o.detail and "container_stop on policies" in o.detail
    assert "health_check_masked_failure" in o.detail
    assert rec.predicted_total == 1


def test_confirmed_via_observed_collateral(make_report):
    """A weakness on `gateway` is confirmed by an experiment that merely cascaded into it."""
    w = _weakness("wk-collateral", services=["gateway"], codes=["cascade_beyond_hypothesis"])
    exp = make_report(service="policies", affected=["gateway"], finding_codes=["cascade_beyond_hypothesis"])
    rec = agent.reconcile(_report([w]), [exp], PROJECT)
    assert rec.outcomes[0].state == "confirmed"


def test_cleared_by_completed_clean_experiments(make_report):
    w = _weakness("wk-002", services=["mongodb"], codes=["health_check_masked_failure"])
    exps = [
        make_report(service="mongodb", failure_type="network_delay", finding_codes=[], affected=[]),
        make_report(service="mongodb", failure_type="db_flood", finding_codes=[], affected=[]),
    ]
    rec = agent.reconcile(_report([w]), exps, PROJECT)

    assert (rec.confirmed, rec.cleared, rec.untested) == (0, 1, 0)
    o = rec.outcomes[0]
    assert o.state == "cleared"
    assert "2 completed experiments on mongodb" in o.detail
    assert "db_flood, network_delay" in o.detail  # failure types sorted, so the string is deterministic
    assert o.confirming_findings == []


def test_high_severity_other_finding_is_not_cleared(make_report):
    """A clean run clears; a run that produced an unrelated high-severity finding does not."""
    w = _weakness("wk-003", services=["policies"], codes=["health_check_masked_failure"])
    exp = make_report(service="policies", finding_codes=["data_loss_suspected"])
    rec = agent.reconcile(_report([w]), [exp], PROJECT)

    o = rec.outcomes[0]
    assert o.state == "untested"
    assert "data_loss_suspected" in o.detail


def test_info_severity_match_does_not_confirm(make_report):
    """`api_impact` fires on every experiment; at `info` it says the API was UNAFFECTED."""
    w = _weakness("wk-info", category="no_rate_limiting", services=["gateway"], codes=["api_impact"])
    benign = make_report(
        service="gateway",
        affected=[],
        findings=[Finding(code="api_impact", severity="info", title="API unaffected",
                          evidence="API unaffected (100% success, p95 41 ms)")],
    )
    rec = agent.reconcile(_report([w]), [benign], PROJECT)

    o = rec.outcomes[0]
    assert o.state == "cleared"  # an info match does not confirm, and does not block the clear either
    assert o.confirming_findings == []


def test_same_code_at_high_severity_does_confirm(make_report):
    w = _weakness("wk-impact", category="no_rate_limiting", services=["gateway"], codes=["api_impact"])
    real = make_report(service="gateway", affected=[], finding_codes=["api_impact"])  # factory builds these at "high"
    rec = agent.reconcile(_report([w]), [real], PROJECT)

    o = rec.outcomes[0]
    assert o.state == "confirmed"
    assert o.confirming_findings == [f"{real.experiment_id}:api_impact"]


def test_untested_when_nothing_bears_on_it(make_report):
    w = _weakness("wk-004", services=["connectivity"])
    exp = make_report(service="mongodb", affected=["gateway"])
    rec = agent.reconcile(_report([w]), [exp], PROJECT)

    o = rec.outcomes[0]
    assert o.state == "untested"
    assert o.experiments == []
    assert "no experiment targeted connectivity" in o.detail


def test_skipped_only_proves_nothing(make_report):
    w = _weakness("wk-005", services=["policies"])
    exp = make_report(experiment_id="skip01", service="policies", status="skipped", affected=[])
    rec = agent.reconcile(_report([w]), [exp], PROJECT)

    o = rec.outcomes[0]
    assert o.state == "untested"
    assert o.experiments == []  # a skipped experiment is not even recorded as bearing
    assert "skipped" in o.detail and "skip01" in o.detail
    assert rec.experiments_considered == 0


def test_aborted_only_is_untested_not_cleared(make_report):
    w = _weakness("wk-006", services=["policies"])
    exp = make_report(experiment_id="ab01", service="policies", status="aborted", finding_codes=[], affected=[])
    rec = agent.reconcile(_report([w]), [exp], PROJECT)

    o = rec.outcomes[0]
    assert o.state == "untested"
    assert o.experiments == ["ab01"]  # it did bear on the weakness, it just never finished
    assert "aborted" in o.detail and "safety monitor" in o.detail
    assert rec.experiments_considered == 1


def test_aborted_can_still_confirm(make_report):
    """Cut short or not, a finding that fired is evidence."""
    w = _weakness("wk-007", services=["policies"])
    exp = make_report(service="policies", status="aborted", finding_codes=["health_check_masked_failure"])
    assert agent.reconcile(_report([w]), [exp], PROJECT).outcomes[0].state == "confirmed"


def test_empty_store_leaves_everything_untested():
    rec = agent.reconcile(_report([_weakness("wk-a"), _weakness("wk-b", services=["things"])]), [], PROJECT)

    assert (rec.predicted_total, rec.confirmed, rec.cleared, rec.untested) == (2, 0, 0, 2)
    assert rec.experiments_considered == 0
    assert "no experiment has run yet" in rec.summary
    assert "hit rate" not in rec.summary  # no precision figure when nothing was exercised


def test_no_weaknesses_at_all():
    rec = agent.reconcile(_report([]), [], PROJECT)
    assert rec.outcomes == []
    assert rec.summary == "No predicted weaknesses on record; nothing to reconcile."


# ---------------------------------------------------------------- aggregation
def test_sort_order_confirmed_first_then_descending_severity(make_report):
    weaknesses = [
        _weakness("wk-low-untested", services=["connectivity"], severity=2.0, level="low"),
        _weakness("wk-high-untested", services=["things-search"], severity=9.0, level="critical"),
        _weakness("wk-confirmed-mild", services=["policies"], severity=3.0, level="low"),
        _weakness("wk-confirmed-bad", services=["gateway"], severity=8.0, level="high"),
    ]
    exps = [
        make_report(service="policies", finding_codes=["health_check_masked_failure"], affected=[]),
        make_report(service="gateway", finding_codes=["health_check_masked_failure"], affected=[]),
    ]
    rec = agent.reconcile(_report(weaknesses), exps, PROJECT)

    assert [o.weakness_id for o in rec.outcomes] == [
        "wk-confirmed-bad",     # confirmed, 8.0
        "wk-confirmed-mild",    # confirmed, 3.0
        "wk-high-untested",     # not confirmed, 9.0
        "wk-low-untested",      # not confirmed, 2.0
    ]


def test_summary_counts_and_precision_phrasing(make_report):
    weaknesses = [
        _weakness("wk-c", services=["policies"]),
        _weakness("wk-cl", services=["mongodb"]),
        _weakness("wk-u", services=["connectivity"]),
    ]
    exps = [
        make_report(service="policies", finding_codes=["health_check_masked_failure"], affected=[]),
        make_report(service="mongodb", finding_codes=[], affected=[]),
    ]
    rec = agent.reconcile(_report(weaknesses), exps, PROJECT)

    assert (rec.confirmed, rec.cleared, rec.untested) == (1, 1, 1)
    assert "Predicted 3 weaknesses from static configuration before any experiment ran" in rec.summary
    assert "50% held up" in rec.summary
    assert "not a measured severity" in rec.summary


def test_input_report_is_not_mutated(make_report):
    w = _weakness("wk-008")
    report = _report([w])
    agent.reconcile(report, [make_report(service="policies", finding_codes=["health_check_masked_failure"])], PROJECT)
    assert report.weaknesses[0].status == "predicted"
    assert report.weaknesses[0].label == PREDICTED_LABEL


def test_predicted_at_comes_from_the_scout_report(make_report):
    report = _report([_weakness("wk-009")])
    rec = agent.reconcile(report, [], PROJECT)
    assert rec.predicted_at == report.generated_at


def test_experiments_considered_excludes_skipped(make_report):
    exps = [
        make_report(service="policies", status="completed"),
        make_report(service="policies", status="skipped"),
        make_report(service="mongodb", status="aborted"),
    ]
    rec = agent.reconcile(_report([_weakness("wk-010")]), exps, PROJECT)
    assert rec.experiments_considered == 2


# ---------------------------------------------------------------- markdown
def test_markdown_labels_every_predicted_severity(make_report):
    weaknesses = [_weakness("wk-conf", services=["policies"]), _weakness("wk-open", services=["connectivity"])]
    exp = make_report(service="policies", finding_codes=["health_check_masked_failure"], affected=[])
    md = agent.render_markdown(agent.reconcile(_report(weaknesses), [exp], PROJECT))

    assert md.startswith(f"# Weakness reconciliation — {PROJECT}")
    assert "predicted severity (not measured)" in md.lower()
    # The open prediction keeps the unconfirmed label; the confirmed one is relabelled, not stripped.
    assert PREDICTED_LABEL in md
    assert agent.CONFIRMED_LABEL in md
    conf_row = next(line for line in md.splitlines() if "`wk-conf`" in line)
    open_row = next(line for line in md.splitlines() if "`wk-open`" in line)
    assert agent.CONFIRMED_LABEL in conf_row and PREDICTED_LABEL not in conf_row
    assert PREDICTED_LABEL in open_row
    # No severity is ever rendered bare.
    assert "7.5/10 high [" in open_row


def test_markdown_survives_no_outcomes():
    md = agent.render_markdown(agent.reconcile(_report([]), [], PROJECT))
    assert "nothing predicted" in md


def test_markdown_escapes_pipes_in_evidence(make_report):
    w = _weakness("wk-pipe")
    exp = make_report(service="policies", findings=[
        Finding(code="health_check_masked_failure", severity="high", title="masked",
                evidence="things kept reporting /ready=200 | while the API failed"),
    ])
    md = agent.render_markdown(agent.reconcile(_report([w]), [exp], PROJECT))
    row = next(line for line in md.splitlines() if "`wk-pipe`" in line)
    assert row.count("|") == 8  # 7 columns -> 8 delimiters, none smuggled in from the evidence


# ---------------------------------------------------------------- run()
def test_run_without_weaknesses_file(tmp_store):
    logs: list[str] = []
    assert agent.run(tmp_store, PROJECT, log=logs.append) is None
    assert logs == ["[reconcile] no reports/weaknesses.json yet; run `faultline scout` first"]
    assert tmp_store.reconciliation() is None
    assert not (tmp_store.root / "weaknesses_reconcile.md").exists()


def test_run_writes_json_and_markdown(tmp_store, make_report):
    tmp_store.write_weaknesses(_report([_weakness("wk-011"), _weakness("wk-012", services=["connectivity"])]))
    tmp_store.write_experiment(make_report(service="policies", finding_codes=["health_check_masked_failure"], affected=[]))

    logs: list[str] = []
    rec = agent.run(tmp_store, PROJECT, log=logs.append)

    assert rec is not None
    assert (rec.confirmed, rec.untested) == (1, 1)
    # Round-trips through the store unchanged.
    stored = tmp_store.reconciliation()
    assert stored is not None
    assert [o.weakness_id for o in stored.outcomes] == [o.weakness_id for o in rec.outcomes]
    md = (tmp_store.root / "weaknesses_reconcile.md").read_text()
    assert "wk-011" in md and "wk-012" in md
    # One line per outcome plus the tally, and no severity logged without its marker.
    assert len(logs) == 3
    assert all("not measured" in line for line in logs[:2])
    assert logs[-1].startswith("[reconcile] 1 confirmed, 0 cleared, 1 untested of 2 predicted")


def test_run_on_store_with_weaknesses_but_no_experiments(tmp_store):
    tmp_store.write_weaknesses(_report([_weakness("wk-013")]))
    rec = agent.run(tmp_store, PROJECT, log=lambda _: None)
    assert rec is not None and rec.untested == 1 and rec.experiments_considered == 0
