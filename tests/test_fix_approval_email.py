"""Scenario C of the approval agent: "Can I fix this?" by email.

The SMTP and IMAP clients are the real `smtplib` / `imaplib`, driven against the loopback doubles in
`tests/fakemail.py`. The proposals are real FixProposal records on a real ReportStore, hashed with
`proposals.change_hash`. The rule under test: APPROVE approves exactly the change the email showed,
REJECT rejects, and every other ending (silence, garbage, a changed proposal, no configuration) leaves
the proposal pending. Nothing is ever approved by default.
"""
from __future__ import annotations

import threading
import time

import pytest

from faultline.config import Settings
from faultline.remediation import proposals as P

from .test_approval import ADMIN, answer, mail, mailbox, settings  # noqa: F401  (fixtures)
from .test_control import PREVIEW, make_proposal


@pytest.fixture
def agent(settings, tmp_store):  # noqa: F811
    from faultline.agents.approval import ApprovalAgent

    return ApprovalAgent(settings, tmp_store, log=lambda m: None)


@pytest.fixture
def proposal(tmp_store):
    p = make_proposal()
    tmp_store.write_proposal(p)
    return p


def sent_body(mailbox, i: int = 0) -> str:  # noqa: F811
    # SMTP puts CRLF on the wire; the content between the line breaks is what must be verbatim
    return mailbox.sent[i]["msg"].get_payload(decode=True).decode().replace("\r\n", "\n")


# ---------------------------------------------------------------- parsing (no network)
@pytest.mark.parametrize("line,expected", [
    ("APPROVE", "approve"), ("approve", "approve"), ("Approve.", "approve"), ("  REJECT ", "reject"),
    ("reject!", "reject"),
    ("APPROVE it", None), ("REJECT, too risky", None), ("yes", None), ("APPROVE REDUCED", None),
    ("FIX 1", None), ("CONTINUE", None), ("", None),
])
def test_fix_proposal_vocabulary_is_closed_and_exact(line, expected):
    from faultline.agents.approval import parse_reply

    parsed = parse_reply(line, "fix_proposal")
    assert (parsed.answer if parsed else None) == expected


def test_reply_reason_is_the_unquoted_text_after_the_answer():
    from faultline.agents.approval import reply_reason

    body = "\n> quoted first\nREJECT\nNot during the demo,\n  the judges are watching.\n\n" \
           "On Mon, Oct 12, Faultline wrote:\n> APPROVE  apply exactly the change\n"
    assert reply_reason(body) == "Not during the demo, the judges are watching."
    assert reply_reason("APPROVE") == ""
    assert reply_reason("APPROVE\n-- \nsent from my phone") == ""
    assert len(reply_reason("REJECT\n" + "x" * 2000)) == 500


# ---------------------------------------------------------------- the flow
def test_approve_by_email_approves_exactly_the_hash_that_was_shown(agent, mailbox, tmp_store, proposal):  # noqa: F811
    answer(mailbox, "APPROVE\nlooks right to me")
    decision = agent.request_fix_proposal(proposal)
    assert decision is not None and decision.answer == "approve" and decision.answered is True
    p = tmp_store.proposal(proposal.proposal_id)
    assert p.status == "approved" and p.decided_by == f"email:{ADMIN}" and p.reason == "looks right to me"
    assert p.audit[-1].actor == f"email:{ADMIN}" and p.audit[-1].to_status == "approved"
    assert any("emailed" in a.note and a.to_status == "pending" for a in p.audit)
    # the email carried the whole proposal: five parts, the verbatim change, the full hash, the replies
    msg = mailbox.sent[0]["msg"]
    body = sent_body(mailbox)
    record = tmp_store.approvals()[0]
    assert record.scenario == "fix_proposal" and record.status == "answered"
    assert record.token in msg["Subject"] and record.options == ["APPROVE", "REJECT"]
    assert record.default_on_timeout == "no_decision" and record.decision.answer == "approve"
    assert PREVIEW.rstrip("\n") in body and P.change_hash(PREVIEW) in body
    for heading in ("1. What is wrong", "2. Exactly what I will change", "3. Expected improvement",
                    "4. Risks", "5. Rollback"):
        assert heading in body, heading
    assert "APPROVE" in body and "REJECT" in body and "never approved automatically" in body
    assert "faultline approve" not in body          # the CLI hints are not the email vocabulary


def test_reject_by_email_keeps_the_reason(agent, mailbox, tmp_store, proposal):  # noqa: F811
    answer(mailbox, "REJECT\nNot during the demo.\nAsk again tomorrow.")
    decision = agent.request_fix_proposal(proposal)
    assert decision is not None and decision.answer == "reject"
    p = tmp_store.proposal(proposal.proposal_id)
    assert p.status == "rejected" and p.decided_by == f"email:{ADMIN}"
    assert p.reason == "Not during the demo. Ask again tomorrow."


def test_garbage_gets_a_clarification_and_then_stays_pending(agent, mailbox, tmp_store, proposal, settings):  # noqa: F811
    settings.approval_timeout_s = 1.0
    answer(mailbox, "APPROVE the readiness one please")
    t0 = time.time()
    assert agent.request_fix_proposal(proposal) is None
    assert time.time() - t0 < 4
    assert tmp_store.proposal(proposal.proposal_id).status == "pending"
    assert len(mailbox.sent) == 2 and "not one of the options" in sent_body(mailbox, 1)
    assert "APPROVE" in sent_body(mailbox, 1) and "REJECT" in sent_body(mailbox, 1)
    record = tmp_store.approvals()[0]
    assert record.status == "timed_out" and record.decision is None


def test_silence_times_out_to_no_decision(agent, mailbox, tmp_store, proposal, settings):  # noqa: F811
    settings.approval_timeout_s = 0.6
    assert agent.request_fix_proposal(proposal) is None
    assert tmp_store.proposal(proposal.proposal_id).status == "pending"
    assert len(mailbox.sent) == 1 and tmp_store.pending_approval() is None   # no gate lock was taken


def test_wait_is_capped_by_the_proposal_expiry(agent, mailbox, tmp_store, settings):  # noqa: F811
    settings.approval_timeout_s = 30.0
    p = make_proposal(expires_in=1.0)
    tmp_store.write_proposal(p)
    t0 = time.time()
    assert agent.request_fix_proposal(p) is None
    assert time.time() - t0 < 4
    assert tmp_store.approvals()[0].timeout_s <= 1.0
    # once past its deadline it can only expire: approving it by any channel is refused
    with pytest.raises(P.IllegalTransition):
        P.approve(tmp_store, p.proposal_id, p.change_hash, "late")
    assert tmp_store.proposal(p.proposal_id).status == "expired"


def test_approve_for_a_change_edited_after_the_email_is_not_applied(agent, mailbox, tmp_store, proposal):  # noqa: F811
    edited = PREVIEW + "+    retries: 10\n"

    def edit_then_approve() -> None:
        deadline = time.time() + 5
        while not mailbox.sent and time.time() < deadline:
            time.sleep(0.01)
        p = tmp_store.proposal(proposal.proposal_id)
        p.change_preview, p.change_hash = edited, P.change_hash(edited)
        tmp_store.write_proposal(p)
        mailbox.reply_to(0, "APPROVE", from_addr=ADMIN)

    threading.Thread(target=edit_then_approve, daemon=True).start()
    assert agent.request_fix_proposal(proposal) is None
    p = tmp_store.proposal(proposal.proposal_id)
    assert p.status == "pending" and p.change_hash == P.change_hash(edited)
    assert "reply not applied" in tmp_store.approvals()[0].send_error


def test_decided_elsewhere_stops_the_wait(agent, mailbox, tmp_store, proposal, settings):  # noqa: F811
    settings.approval_timeout_s = 5.0

    def reject_in_dashboard() -> None:
        deadline = time.time() + 5
        while not mailbox.sent and time.time() < deadline:
            time.sleep(0.01)
        P.reject(tmp_store, proposal.proposal_id, "dashboard", "done here")

    threading.Thread(target=reject_in_dashboard, daemon=True).start()
    t0 = time.time()
    assert agent.request_fix_proposal(proposal) is None
    assert time.time() - t0 < 3
    p = tmp_store.proposal(proposal.proposal_id)
    assert p.status == "rejected" and p.decided_by == "dashboard"   # the email side never overwrote it


def test_not_pending_or_unknown_is_never_emailed(agent, mailbox, tmp_store):  # noqa: F811
    gone = make_proposal("fx-gone")
    assert agent.request_fix_proposal(gone) is None                     # not on the blackboard
    late = make_proposal("fx-late", expires_in=-1)
    tmp_store.write_proposal(late)
    assert agent.request_fix_proposal(late) is None
    assert tmp_store.proposal("fx-late").status == "expired"
    done = make_proposal("fx-done", status="rejected")
    tmp_store.write_proposal(done)
    assert agent.request_fix_proposal(done) is None
    assert mailbox.sent == []


def test_unconfigured_channel_decides_nothing(tmp_store, proposal):
    from faultline.agents.approval import request_fix_approval

    lines: list[str] = []
    assert request_fix_approval(Settings(), tmp_store, proposal, log=lines.append) is None
    assert tmp_store.proposal(proposal.proposal_id).status == "pending"
    record = tmp_store.approvals()[0]
    assert record.status == "failed" and "ADMIN_EMAIL" in record.send_error and record.sent is False
    assert any("NOT CONFIGURED" in ln for ln in lines)


def test_send_failure_decides_nothing(mailbox, tmp_store, proposal, settings):  # noqa: F811
    from faultline.agents.approval import ApprovalAgent

    from .fakemail import FakeSMTPServer

    with FakeSMTPServer(mailbox, fail_with="refuse") as dead:
        settings.smtp_host, settings.smtp_port = dead.host, dead.port
        agent = ApprovalAgent(settings, tmp_store, log=lambda m: None)
        assert agent.request_fix_proposal(proposal) is None
    assert tmp_store.proposal(proposal.proposal_id).status == "pending"
    assert tmp_store.approvals()[0].status == "failed"


def test_credentials_never_reach_the_blackboard(agent, mailbox, tmp_store, proposal, settings):  # noqa: F811
    answer(mailbox, "REJECT")
    agent.request_fix_proposal(proposal)
    blob = "\n".join(p.read_text() for p in tmp_store.root.rglob("*.json"))
    assert settings.smtp_password not in blob and settings.imap_password not in blob


def test_cli_propose_email_without_config_leaves_proposals_pending(monkeypatch, tmp_path):
    """`faultline propose --email` with no mail config refuses clearly and approves nothing."""
    from faultline import cli
    from faultline.agents import approval as ap

    monkeypatch.setattr(ap, "preflight", lambda s: ["ADMIN_EMAIL is not set"])
    sent = []
    monkeypatch.setattr(ap, "request_fix_approval", lambda *a, **k: sent.append(a))

    class P:
        proposal_id = "fx-x"

    class Store:
        def proposal(self, pid):
            return None

    rc = cli._email_proposals(object(), Store(), [P()], apply_after=True, allow_protected=False)
    assert rc == 1 and sent == []
