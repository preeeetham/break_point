"""Fix states on the scorecard and verified fixes in the narrator."""
from break_point.agents import narrator, scorecard
from break_point.remediation import proposals as P
from break_point.schemas import FixProposal, FixVerification, MetricComparison, ProposalExplanation


def _prop(eid, status, svc="orders", **kw):
    return FixProposal(proposal_id=kw.pop("pid", P.new_proposal_id()), experiment_id=eid, finding_code="health_check_masked_failure",
                       catalog_fix_id="add_or_tighten_healthcheck", target_services=[svc], change_preview="x", change_hash=P.change_hash("x"),
                       explanation=ProposalExplanation(problem="p", change="c", expected_improvement="i", risks="r", rollback="b"),
                       status=status, expires_at=P.expires_at(60), **kw)


def test_scorecard_shows_fix_state_per_finding(make_report, topology):
    r = make_report(experiment_id="e1", service="orders", failure_type="container_crash", finding_codes=["health_check_masked_failure"])
    props = [_prop("e1", "pending", pid="fx-a"), _prop("e1", "rolled_back", pid="fx-b", reason="time to recover got worse")]
    sc = scorecard.compute([r], {}, topology, "p", props)
    orders = next(s for s in sc.ranking if s.service == "orders")
    assert [(f.proposal_id, f.state) for f in orders.fixes] == [("fx-a", "proposed"), ("fx-b", "rolled_back")]
    md = scorecard.render_markdown(sc)
    assert "## Fix states" in md and "(proposals/fx-a.md)" in md and "rolled back (time to recover got worse)" in md


def test_scorecard_without_proposals_is_unchanged(make_report, topology):
    r = make_report(experiment_id="e1", service="orders", failure_type="container_crash")
    sc = scorecard.compute([r], {}, topology, "p")
    assert all(not s.fixes for s in sc.ranking) and "Fix states" not in scorecard.render_markdown(sc)


def test_narrator_mentions_verified_fix(make_report):
    r = make_report(experiment_id="e1", service="orders", failure_type="container_crash")
    v = _prop("e1", "verified", decided_by="cli:daksh",
              verification=FixVerification(outcome="verified", comparisons=[
                  MetricComparison(metric="time_to_recover_s", before=26.5, after=12.0, better_is="lower", verdict="better")]))
    lines = narrator.narrate(r, None, None, None, False, [v, _prop("other", "verified")])
    fix_lines = [l for l in lines if "Fix verified" in l]
    assert fix_lines == ["  Fix verified: add_or_tighten_healthcheck on orders, approved by cli:daksh (time to recover 26.5 s -> 12 s)"]
