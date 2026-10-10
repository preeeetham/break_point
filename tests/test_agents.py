import pytest


def _level_for(sev: float) -> str:
    if sev < 3:
        return "low"
    if sev < 5.5:
        return "medium"
    if sev < 8:
        return "high"
    return "critical"


# ---------------------------------------------------------------- risk
def test_risk_severity_and_level_bands(make_report, topology):
    risk = pytest.importorskip("faultline.agents.risk")
    for during in (1.0, 0.9, 0.5, 0.1, 0.0):
        r = make_report(during_success=during)
        a = risk.assess(r, topology, None)
        assert 0 <= a.severity <= 10
        assert a.level == _level_for(a.severity)
        assert a.generated_by == "rules"
        assert a.target_service == "policies" and a.failure_type == "container_crash"
        assert "policies" in a.summary
    worst = risk.assess(make_report(during_success=0.0, recovered=False, remediation=["docker restart"],
                                    affected=list(topology.relevant_services()),
                                    finding_codes=["health_check_masked_failure", "manual_restart_required"]), topology, None)
    assert worst.level == "critical" and worst.severity >= 8
    mild = risk.assess(make_report(during_success=1.0, ttr=1.0, affected=[]), topology, None)
    assert mild.level == "low"
    assert mild.severity < worst.severity


def test_risk_data_loss_forces_critical(make_report, topology):
    risk = pytest.importorskip("faultline.agents.risk")
    a = risk.assess(make_report(during_success=0.99, ttr=1.0, affected=[], finding_codes=["data_loss_suspected"]), topology, None)
    assert a.severity < 3
    assert a.level == "critical"
    assert "data loss" in a.production_implication.lower()


def test_risk_skipped_report_has_zero_severity(make_report, topology):
    risk = pytest.importorskip("faultline.agents.risk")
    a = risk.assess(make_report(status="skipped", error="system unhealthy before injection"), topology, None)
    assert a.severity == 0 and a.level == "low" and a.blast_radius == 0
    assert "skipped" in a.summary


def test_risk_factors_sum_to_severity(make_report, topology):
    risk = pytest.importorskip("faultline.agents.risk")
    a = risk.assess(make_report(), topology, None)
    assert a.severity == pytest.approx(min(10.0, sum(a.factors.values())), abs=0.01)
    assert a.blast_radius == 2  # target + gateway
    assert a.recovery_s == 14.0


# ---------------------------------------------------------------- fix
def test_fix_is_advisory_with_recommendations(make_report, topology):
    risk = pytest.importorskip("faultline.agents.risk")
    fix = pytest.importorskip("faultline.agents.fix")
    report = make_report()
    rk = risk.assess(report, topology, None)
    out = fix.recommend(report, rk, None)
    assert out.advisory_only is True
    assert len(out.recommendations) >= 1
    assert out.generated_by == "rules"
    assert out.risk_level == rk.level and out.target_service == "policies"
    titles = [r.title for r in out.recommendations]
    assert len(titles) == len(set(titles))
    for r in out.recommendations:
        assert r.steps and r.verify and 0 <= r.confidence <= 1
        assert report.experiment_id in r.rationale
    assert fix.recommend(report, None, None).risk_level == "unknown"


def test_fix_health_check_masked_failure_references_issue_1865(make_report, topology):
    fix = pytest.importorskip("faultline.agents.fix")
    report = make_report(finding_codes=["health_check_masked_failure"])
    out = fix.recommend(report, None, None)
    assert any("1865" in " ".join([r.rationale, *r.references]) for r in out.recommendations)
    md = fix.render_markdown(out, None)
    assert "Advisory only" in md and report.experiment_id in md


# ---------------------------------------------------------------- scorecard
def test_scorecard_untested_and_ranking(make_report, topology):
    scorecard = pytest.importorskip("faultline.agents.scorecard")
    reports = [
        make_report(service="policies", during_success=0.1),
        make_report(service="things", during_success=0.9, failure_type="network_delay"),
        make_report(service="gateway", during_success=0.5, status="failed"),  # not scored
    ]
    sc = scorecard.compute(reports, {}, topology, "p")
    by = {s.service: s for s in sc.ranking}
    assert set(by) == set(topology.relevant_services())
    assert by["mongodb"].score is None and by["mongodb"].grade == "—" and by["mongodb"].experiments == 0
    assert by["gateway"].score is None  # failed experiments do not count
    assert by["policies"].score < by["things"].score
    assert sc.ranking[0].service == "policies"
    tested = [s for s in sc.ranking if s.score is not None]
    assert [s.score for s in tested] == sorted(s.score for s in tested)
    assert all(s.score is None for s in sc.ranking[len(tested):])
    assert sc.experiments_total == 3
    assert sc.overall_score == pytest.approx((by["policies"].score + by["things"].score) / 2, abs=0.1)
    assert "policies" in sc.summary
    assert by["things"].worst_success_rate == 0.9 and "tested: network_delay" in by["things"].notes


def test_scorecard_grades_and_empty(topology):
    scorecard = pytest.importorskip("faultline.agents.scorecard")
    assert scorecard.grade(None) == "—"
    assert scorecard.grade(95) == "A" and scorecard.grade(80) == "B" and scorecard.grade(70) == "C"
    assert scorecard.grade(50) == "D" and scorecard.grade(10) == "F"
    sc = scorecard.compute([], {}, topology, "p")
    assert sc.overall_score is None and all(s.score is None for s in sc.ranking)
    assert "nothing to score" in sc.summary.lower()


# ---------------------------------------------------------------- narrator
def test_narrator_mentions_target(make_report, topology):
    risk = pytest.importorskip("faultline.agents.risk")
    fix = pytest.importorskip("faultline.agents.fix")
    narrator = pytest.importorskip("faultline.agents.narrator")
    report = make_report()
    rk = risk.assess(report, topology, None)
    fx = fix.recommend(report, rk, None)
    lines = narrator.narrate(report, rk, fx, None, False)
    assert isinstance(lines, list) and lines
    assert all(isinstance(line, str) for line in lines)
    assert any("policies" in line for line in lines)
