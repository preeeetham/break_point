"""FixProposal schema and lifecycle (faultline/remediation/proposals.py)."""
from datetime import timedelta

import pytest
from pydantic import ValidationError

from faultline.remediation import proposals as P
from faultline.reporting.store import ReportStore
from faultline.schemas import FixProposal, ProposalExplanation


def _proposal(status="pending", preview="services:\n  things:\n    healthcheck: {}\n", **kw):
    return FixProposal(
        proposal_id=kw.pop("proposal_id", P.new_proposal_id()), experiment_id="e1", finding_code="health_check_masked_failure",
        catalog_fix_id="add_or_tighten_healthcheck", target_services=["things"], change_preview=preview,
        change_hash=P.change_hash(preview),
        explanation=ProposalExplanation(problem="p", change="c", expected_improvement="i", risks="r", rollback="b"),
        status=status, expires_at=kw.pop("expires_at", P.expires_at(1800)), **kw)


def test_schema_refuses_unknown_status():
    with pytest.raises(ValidationError):
        _proposal(status="done")


def test_only_legal_transitions():
    p = _proposal()
    P.transition(p, "rejected", "test")
    for target in ("approved", "applying", "applied", "verified"):
        with pytest.raises(P.IllegalTransition):
            P.transition(p, target, "test")
    assert p.status == "rejected" and len(p.audit) == 1


def test_happy_path_is_legal_and_audited():
    p = _proposal()
    for s in ("approved", "applying", "applied", "verified"):
        P.transition(p, s, "test")
    assert [a.to_status for a in p.audit] == ["approved", "applying", "applied", "verified"]
    assert p.status in P.FINAL


def test_every_final_status_is_terminal():
    for s in P.FINAL:
        assert not P.TRANSITIONS[s]
    assert "applied" not in P.TRANSITIONS["rejected"] | P.TRANSITIONS["expired"]


def test_approve_with_stale_hash_is_refused(tmp_path):
    store = ReportStore(tmp_path)
    p = _proposal()
    store.write_proposal(p)
    with pytest.raises(P.HashMismatch):
        P.approve(store, p.proposal_id, P.change_hash("something else"), "test")
    assert store.proposal(p.proposal_id).status == "pending"
    ok = P.approve(store, p.proposal_id, p.change_hash, "test")
    assert ok.status == "approved" and store.proposal(p.proposal_id).decided_by == "test"


def test_tampered_preview_cannot_be_approved(tmp_path):
    store = ReportStore(tmp_path)
    p = _proposal()
    shown = p.change_hash
    p.change_preview += "    restart: always\n"          # preview changed after it was hashed
    store.write_proposal(p)
    with pytest.raises(P.HashMismatch):
        P.approve(store, p.proposal_id, shown, "test")


def test_expired_proposal_cannot_be_approved_or_applied(tmp_path):
    store = ReportStore(tmp_path)
    p = _proposal(expires_at=P.utcnow() - timedelta(seconds=1))
    store.write_proposal(p)
    with pytest.raises(P.IllegalTransition):
        P.approve(store, p.proposal_id, p.change_hash, "test")
    got = store.proposal(p.proposal_id)
    assert got.status == "expired"
    with pytest.raises(P.IllegalTransition):
        P.transition(got, "applying", "test")


def test_reject_records_reason(tmp_path):
    store = ReportStore(tmp_path)
    p = _proposal()
    store.write_proposal(p)
    r = P.reject(store, p.proposal_id, "cli", "not during the demo")
    assert r.status == "rejected" and r.reason == "not during the demo"


def test_store_round_trip_and_path_guard(tmp_path):
    store = ReportStore(tmp_path)
    a, b = _proposal(proposal_id="fx-a"), _proposal(proposal_id="fx-b")
    store.write_proposal(a)
    store.write_proposal(b)
    assert {p.proposal_id for p in store.proposals()} == {"fx-a", "fx-b"}
    assert store.proposal("../etc") is None
