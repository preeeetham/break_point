"""Approval agent. The SMTP and IMAP clients under test are the real `smtplib` / `imaplib` ones,
driven against the loopback doubles in `tests/fakemail.py`: no network, no inbox, no credentials.

Where an address is needed it is `admin@example.invalid`. The real ADMIN_EMAIL is not known yet and
is deliberately absent from this repository.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from faultline.config import Settings
from faultline.schemas import ExperimentSpec, Hypothesis

from .conftest import PROJECT, ROOT
from .fakemail import FakeIMAPServer, FakeMailbox, FakeSMTPServer

ADMIN = "admin@example.invalid"


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def mailbox() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
def mail(mailbox):
    """Both servers on one mailbox, with the Settings that point the agent at them."""
    with FakeSMTPServer(mailbox) as smtp, FakeIMAPServer(mailbox) as imap:
        yield smtp, imap


@pytest.fixture
def settings(tmp_path, mail) -> Settings:
    smtp, imap = mail
    return Settings(
        compose_project=PROJECT, reports_dir=str(tmp_path / "reports"),
        thresholds_file=str(ROOT / "config" / "thresholds.yaml"), llm_enabled=False,
        admin_email=ADMIN, smtp_host=smtp.host, smtp_port=smtp.port, smtp_user="faultline",
        smtp_password="secret", smtp_from="faultline@example.invalid", smtp_starttls=False,
        imap_host=imap.host, imap_port=imap.port, imap_user="faultline", imap_password="secret",
        imap_ssl=False, approval_timeout_s=5.0, approval_poll_s=0.05,
    )


@pytest.fixture
def agent(settings, tmp_store):
    from faultline.agents.approval import ApprovalAgent

    # tmp_store's root and settings.reports_dir are different tmp dirs; use the store explicitly.
    return ApprovalAgent(settings, tmp_store, log=lambda m: None)


def _spec(**kw: Any) -> ExperimentSpec:
    fields: dict[str, Any] = dict(
        experiment_id="exp-approval", target_service="mongodb", container=f"{PROJECT}-mongodb-1",
        failure_type="container_crash", parameters={"duration_s": 45, "block_restart": False},
        rationale="mongodb is the sole datastore and has never been crashed",
        hypothesis=Hypothesis(statement="everything breaks", expected_affected=["things"],
                              expected_max_error_rate=1.0, expected_recovery_s=90.0),
        criticality_score=9.5, planned_by="operator", requires_approval=True,
        approval_reason="mongodb is protected: only degrade-only failures are allowed",
    )
    fields.update(kw)
    return ExperimentSpec(**fields)


def answer(mailbox: FakeMailbox, body: str, *, after: float = 0.0) -> threading.Thread:
    """Reply to the agent's email from another thread, so the agent's own wait loop is what finds it."""

    def _reply() -> None:
        deadline = time.time() + 5
        while not mailbox.sent and time.time() < deadline:
            time.sleep(0.01)
        if after:
            time.sleep(after)
        mailbox.reply_to(0, body, from_addr=ADMIN)

    t = threading.Thread(target=_reply, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- configuration check
def test_preflight_names_every_unset_variable():
    approval = pytest.importorskip("faultline.agents.approval")
    issues = approval.preflight(Settings())
    joined = " | ".join(issues)
    for var in ("ADMIN_EMAIL", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "IMAP_HOST", "IMAP_USER",
                "IMAP_PASSWORD"):
        assert var in joined, var
    assert "not hardcoded anywhere" in joined        # the admin address is deliberately absent
    assert approval.preflight(Settings(admin_email=ADMIN, smtp_host="h", smtp_user="u", smtp_password="p",
                                       imap_host="h", imap_user="u", imap_password="p")) == []


def test_unconfigured_gate_fails_safe_to_skip_and_never_approves(tmp_store):
    approval = pytest.importorskip("faultline.agents.approval")
    agent = approval.ApprovalAgent(Settings(), tmp_store, log=lambda m: None)
    assert agent.available() is False
    decision = agent.request_pre_injection(_spec())
    assert decision.answer == "skip" and decision.answered is False
    assert "not configured" in decision.reason and "ADMIN_EMAIL" in decision.reason
    # The record exists and says it failed, and nothing is left pending.
    assert tmp_store.pending_approval() is None
    stored = tmp_store.approvals()
    assert len(stored) == 1 and stored[0].status == "failed" and stored[0].sent is False


# ---------------------------------------------------------------- SMTP send (build step 2)
def test_request_sends_a_real_email_carrying_every_planner_fact(agent, mailbox, tmp_store):
    answer(mailbox, "SKIP")
    decision = agent.request_pre_injection(_spec())
    assert decision.answered is True and decision.answer == "skip"

    assert len(mailbox.sent) == 1
    sent = mailbox.sent[0]
    assert sent["rcpttos"] == [ADMIN]
    msg = sent["msg"]
    assert msg["To"] == ADMIN and msg["Message-ID"]
    body = msg.get_payload(decode=True).decode()
    # the planner's own rationale, hypothesis and parameters, not a re-derivation
    assert "mongodb is the sole datastore and has never been crashed" in body
    assert "everything breaks" in body
    assert "duration_s=45" in body
    assert "mongodb is protected" in body
    for option in ("APPROVE", "APPROVE REDUCED", "SKIP", "STOP RUN"):
        assert option in body
    assert "SKIP is applied" in body                 # the timeout default is stated up front
    # the correlation token is in the subject, and is what the reply was matched on
    token = tmp_store.approvals()[0].token
    assert token in msg["Subject"] and token.startswith("FL-")


def test_credentials_never_reach_the_blackboard(agent, mailbox, tmp_store, settings):
    answer(mailbox, "CONTINUE")        # not a valid scenario-A answer; it will time out
    agent.request_pre_injection(_spec())
    blob = "\n".join(p.read_text() for p in tmp_store.root.rglob("*.json"))
    assert settings.smtp_password not in blob and settings.imap_password not in blob
    assert "secret" not in blob
    record = tmp_store.approvals()[0]
    assert record.to == ADMIN          # who was asked is recorded; what was used to ask is not


def test_send_failure_fails_safe_rather_than_approving(mailbox, tmp_store, settings):
    approval = pytest.importorskip("faultline.agents.approval")
    with FakeSMTPServer(mailbox, fail_with="refuse") as dead:
        settings.smtp_host, settings.smtp_port = dead.host, dead.port
        agent = approval.ApprovalAgent(settings, tmp_store, log=lambda m: None)
        decision = agent.request_pre_injection(_spec())
    assert decision.answer == "skip" and decision.answered is False
    assert "could not send" in decision.reason
    assert tmp_store.approvals()[0].status == "failed"
    assert tmp_store.pending_approval() is None


def test_unreachable_mailbox_times_out_to_the_safe_default(mailbox, tmp_store, settings):
    """Sending works, reading does not. Scenario A must still refuse to proceed."""
    approval = pytest.importorskip("faultline.agents.approval")
    with FakeIMAPServer(mailbox, fail_with="refuse") as dead:
        settings.imap_host, settings.imap_port = dead.host, dead.port
        settings.approval_timeout_s = 1.0
        agent = approval.ApprovalAgent(settings, tmp_store, log=lambda m: None)
        decision = agent.request_pre_injection(_spec())
    assert decision.answer == "skip" and decision.answered is False
    assert "no valid reply" in decision.reason
    assert len(mailbox.sent) == 1          # the request did go out


# ---------------------------------------------------------------- IMAP poll and parsing (step 3)
@pytest.mark.parametrize("reply,expected", [
    ("APPROVE", "approve"),
    ("approve", "approve"),
    ("Approve.", "approve"),
    ("APPROVE REDUCED", "approve_reduced"),
    ("approve reduced\nsorry, be careful", "approve_reduced"),
    ("SKIP", "skip"),
    ("STOP RUN", "stop_run"),
])
def test_every_valid_scenario_a_reply_round_trips(agent, mailbox, reply, expected):
    answer(mailbox, reply)
    decision = agent.request_pre_injection(_spec())
    assert decision.answer == expected and decision.answered is True
    assert decision.raw_first_line.strip() == reply.splitlines()[0].strip()


def test_quoted_original_text_is_not_read_as_an_answer(agent, mailbox):
    """A bottom-posted reply starts with the quoted mail. The answer is the first unquoted line."""
    answer(mailbox, "> APPROVE            run it exactly as planned\n> SKIP   reject this\n\nSKIP")
    assert agent.request_pre_injection(_spec()).answer == "skip"


@pytest.mark.parametrize("reply", [
    "yes do it",
    "APPROVE the mongodb one please",
    "STOP",                                   # not STOP RUN
    "FIX 1",                                  # the other scenario's vocabulary
    "",
])
def test_a_malformed_reply_decides_nothing_and_re_asks_once(agent, mailbox, reply):
    agent.s.approval_timeout_s = 2.0
    answer(mailbox, reply)
    decision = agent.request_pre_injection(_spec())
    # Not an answer: the request times out to the safe default instead of guessing.
    assert decision.answer == "skip" and decision.answered is False
    assert "no valid reply" in decision.reason
    assert decision.clarifications_sent == 1
    clarification = mailbox.sent[1]["msg"]
    text = clarification.get_payload(decode=True).decode()
    assert "not one of the options" in text
    for option in ("APPROVE", "APPROVE REDUCED", "SKIP", "STOP RUN"):
        assert option in text
    assert " ".join(clarification["Subject"].split()).startswith("Re: ")
    assert len(f"Subject: {clarification['Subject']}") <= 78      # never folded


def test_a_valid_reply_after_a_malformed_one_is_still_honoured(agent, mailbox):
    agent.s.approval_timeout_s = 5.0
    answer(mailbox, "no idea, ask Dirk")
    answer(mailbox, "APPROVE", after=0.6)
    decision = agent.request_pre_injection(_spec())
    assert decision.answer == "approve" and decision.answered is True
    assert decision.clarifications_sent == 1


def test_a_reply_to_another_decision_is_ignored(agent, mailbox):
    """Matching is by correlation token, never by "the newest unread mail"."""
    agent.s.approval_timeout_s = 1.5
    unrelated = ("From: someone@example.invalid\r\nTo: faultline@example.invalid\r\n"
                 "Subject: [Faultline FL-00000000] approval needed: something else\r\n"
                 "Message-ID: <other@example.invalid>\r\n\r\nAPPROVE\r\n").encode()
    mailbox.append(unrelated)
    decision = agent.request_pre_injection(_spec())
    assert decision.answered is False and decision.answer == "skip"
    assert decision.clarifications_sent == 0      # the unrelated mail was never even considered


# ---------------------------------------------------------------- timeout watchdog (step 4)
def test_no_reply_times_out_to_skip_for_scenario_a(agent):
    agent.s.approval_timeout_s = 1.0
    started = time.time()
    decision = agent.request_pre_injection(_spec())
    assert decision.answer == "skip" and decision.answered is False
    assert "no valid reply within 1 s" in decision.reason
    assert 1.0 <= time.time() - started < 5.0     # the deadline is actually honoured


def test_the_deadline_fires_even_when_a_poll_is_stuck(agent, make_report, tmp_store):
    """The watchdog is an independent timer, so a wedged mailbox read cannot outlive the deadline."""
    class WedgedMailbox:
        def find_replies(self, token, message_id=""):
            time.sleep(30)                         # never returns before the deadline
            return []

        def mark_seen(self, uid):
            pass

    agent.mailbox = WedgedMailbox()
    agent.s.approval_timeout_s = 1.0
    started = time.time()
    decision = agent.request_pre_injection(_spec())
    elapsed = time.time() - started
    assert decision.answer == "skip" and decision.answered is False
    # The wedged call is still blocking; what matters is that the decision did not wait for it.
    assert elapsed < 25, f"the deadline did not fire independently (took {elapsed:.1f}s)"


def test_stop_during_the_wait_is_treated_as_a_timeout(agent, tmp_store):
    agent.s.approval_timeout_s = 10.0
    threading.Timer(0.3, lambda: tmp_store.request_stop("test")).start()
    decision = agent.request_pre_injection(_spec())
    assert decision.answer == "skip" and decision.answered is False
    assert "STOP was latched" in decision.reason


# ---------------------------------------------------------------- the pending-decision file
def test_pending_file_is_written_while_waiting_and_cleared_after(agent, mailbox, tmp_store):
    seen: list[Any] = []

    class Watching:
        """Records what the blackboard looked like from another process's point of view."""

        def __init__(self, inner):
            self.inner = inner

        def find_replies(self, token, message_id=""):
            seen.append(tmp_store.pending_approval())
            return self.inner.find_replies(token, message_id)

        def mark_seen(self, uid):
            self.inner.mark_seen(uid)

    agent.mailbox = Watching(agent.mailbox)
    answer(mailbox, "APPROVE")
    agent.request_pre_injection(_spec())

    pending = next(p for p in seen if p is not None)
    assert pending.scenario == "pre_injection" and pending.status == "pending"
    assert pending.experiment_id == "exp-approval" and pending.sent is True
    assert pending.default_on_timeout == "skip" and pending.deadline > pending.created_at
    assert pending.to == ADMIN and pending.token in pending.subject
    # cleared once decided, and kept for good under approvals/
    assert tmp_store.pending_approval() is None
    assert tmp_store.approval(pending.approval_id).status == "answered"


def test_resend_keeps_the_same_token(agent, mailbox, tmp_store, settings):
    approval = pytest.importorskip("faultline.agents.approval")
    req = agent._open("pre_injection", "exp-approval", "mongodb", "container_crash")
    req.subject, req.sent = f"[Faultline {req.token}] approval needed", True
    tmp_store.set_pending_approval(req)
    assert agent.resend(req.approval_id) is True
    assert agent.resend("not-the-pending-one") is False
    assert req.token in mailbox.sent[0]["msg"]["Subject"]
    assert tmp_store.pending_approval().resends == 1
    assert approval.describe_pending(tmp_store.pending_approval())[0].startswith(f"approval {req.approval_id}")
    assert approval.describe_pending(None) == ["no decision is pending"]


# ---------------------------------------------------------------- APPROVE REDUCED
def test_approve_reduced_halves_within_the_catalog_bounds(agent, mailbox):
    spec = _spec(failure_type="network_delay",
                 parameters={"duration_s": 45, "delay_ms": 800, "jitter_ms": 200})
    answer(mailbox, "APPROVE REDUCED")
    decision = agent.request_pre_injection(spec)
    assert decision.answer == "approve_reduced"
    assert decision.parameters_before == {"duration_s": 45, "delay_ms": 800, "jitter_ms": 200}
    assert decision.parameters_after == {"duration_s": 22, "delay_ms": 400, "jitter_ms": 100}
    assert any("halved 800 -> 400" in n for n in decision.parameter_notes)


def test_reduction_is_clamped_up_to_the_catalog_minimum_and_recorded(agent, mailbox):
    """Halving may not take a parameter under its floor; the clamp is recorded like any other."""
    answer(mailbox, "APPROVE REDUCED")
    decision = agent.request_pre_injection(_spec(parameters={"duration_s": 15}))
    assert decision.parameters_after["duration_s"] == 10        # container_crash floor
    notes = " | ".join(decision.parameter_notes)
    assert "halved 15 -> 7" in notes and "raised from 7 to minimum 10" in notes


def test_gate_applies_the_reduction_to_the_spec_the_injector_will_run(settings, tmp_store, mailbox):
    approval = pytest.importorskip("faultline.agents.approval")
    spec = _spec()
    answer(mailbox, "APPROVE REDUCED")
    decision = approval.gate(settings, tmp_store, spec, log=lambda m: None)
    assert decision.answer == "approve_reduced"
    assert spec.parameters["duration_s"] == 22                  # mutated in place, halved from 45
    notes = " | ".join(spec.planner_notes)
    assert "halved 45 -> 22" in notes and "admin approved a reduced run" in notes


def test_stop_run_writes_the_existing_stop_file(agent, mailbox, tmp_store):
    answer(mailbox, "STOP RUN")
    decision = agent.request_pre_injection(_spec())
    assert decision.answer == "stop_run"
    assert tmp_store.stop_requested() is True                   # the documented kill switch, reused
    assert tmp_store.stop_file.read_text().startswith("approval:")


# ---------------------------------------------------------------- scenario B
def test_problem_trigger_fires_on_severity_and_on_remediation(make_report, topology):
    approval = pytest.importorskip("faultline.agents.approval")
    risk_agent = pytest.importorskip("faultline.agents.risk")

    quiet = make_report(during_success=1.0, ttr=2.0)
    fire, _ = approval.problem_occurred(quiet, risk_agent.assess(quiet, topology, None))
    assert fire is False

    severe = make_report(during_success=0.0, finding_codes=["health_check_masked_failure"])
    severe_risk = risk_agent.assess(severe, topology, None)
    assert severe_risk.level in ("high", "critical")
    fire, reason = approval.problem_occurred(severe, severe_risk)
    assert fire is True and "Risk agent scored this" in reason

    # a quiet experiment the recovery ladder still had to rescue
    rescued = make_report(during_success=1.0, remediation=["restart things (no recovery after 50% of timeout)"])
    fire, reason = approval.problem_occurred(rescued, risk_agent.assess(rescued, topology, None))
    assert fire is True and "recovery ladder had to intervene" in reason


def test_problem_email_reuses_the_other_agents_output(agent, mailbox, tmp_store, make_report, topology):
    narrator = pytest.importorskip("faultline.agents.narrator")
    risk_agent = pytest.importorskip("faultline.agents.risk")
    fix_agent = pytest.importorskip("faultline.agents.fix")

    report = make_report(during_success=0.0, finding_codes=["health_check_masked_failure"])
    tmp_store.write_experiment(report)
    risk = risk_agent.assess(report, topology, None)
    fix = fix_agent.recommend(report, risk, None)
    narrative = narrator.narrate(report, risk, fix, None, False)

    answer(mailbox, f"FIX {len(fix.recommendations)}")
    decision = agent.request_problem(report, risk, fix, narrative)

    assert decision.answer == "fix" and decision.fix_index == len(fix.recommendations)
    assert decision.fix_title == fix.recommendations[-1].title
    body = mailbox.sent[0]["msg"].get_payload(decode=True).decode()
    assert risk.summary in body and risk.production_implication in body
    assert "advisory only, nothing has been applied" in body
    for i, rec in enumerate(fix.recommendations, 1):
        assert f"{i}. {rec.title}" in body          # numbering is what FIX <n> refers to
    assert "no agent in this system ever applies a fix" in body
    assert "CONTINUE is applied" in body            # scenario B's timeout default


def test_chosen_fix_is_recorded_on_the_report_and_applies_nothing(agent, mailbox, tmp_store, make_report,
                                                                  topology):
    risk_agent = pytest.importorskip("faultline.agents.risk")
    fix_agent = pytest.importorskip("faultline.agents.fix")

    report = make_report(during_success=0.0, finding_codes=["health_check_masked_failure"])
    tmp_store.write_experiment(report)
    risk = risk_agent.assess(report, topology, None)
    fix = fix_agent.recommend(report, risk, None)
    answer(mailbox, "FIX 1")
    agent.request_problem(report, risk, fix, ["something happened"])

    stored = tmp_store.experiment(report.experiment_id)
    assert stored.approval is not None
    assert stored.approval.answer == "fix" and stored.approval.fix_index == 1
    assert stored.approval.fix_title == fix.recommendations[0].title
    assert stored.approval.answered is True
    # a decision log entry only: nothing paused, nothing applied
    assert tmp_store.stop_requested() is False
    assert tmp_store.fix(report.experiment_id) is None or tmp_store.fix(report.experiment_id).advisory_only


def test_wait_pauses_the_run_and_continue_does_not(agent, mailbox, tmp_store, make_report):
    report = make_report(during_success=0.0)
    tmp_store.write_experiment(report)

    answer(mailbox, "WAIT")
    assert agent.request_problem(report, None, None, ["x"]).answer == "wait"
    assert tmp_store.stop_requested() is True
    tmp_store.clear_stop()

    mailbox.sent.clear()
    mailbox.messages.clear()
    answer(mailbox, "CONTINUE")
    assert agent.request_problem(report, None, None, ["x"]).answer == "continue"
    assert tmp_store.stop_requested() is False


def test_scenario_b_times_out_to_continue_never_to_wait(agent, tmp_store, make_report):
    """WAIT must only ever be a deliberate human choice, never a timeout artefact."""
    report = make_report(during_success=0.0)
    tmp_store.write_experiment(report)
    agent.s.approval_timeout_s = 1.0
    decision = agent.request_problem(report, None, None, ["x"])
    assert decision.answer == "continue" and decision.answered is False
    assert tmp_store.stop_requested() is False
    assert tmp_store.experiment(report.experiment_id).approval.answered is False


def test_run_post_experiment_is_silent_when_nothing_is_wrong(settings, tmp_store, make_report, topology,
                                                             mailbox):
    approval = pytest.importorskip("faultline.agents.approval")
    tmp_store.write_topology(topology)
    quiet = make_report(during_success=1.0, ttr=2.0, finding_codes=[])
    tmp_store.write_experiment(quiet)
    assert approval.run_post_experiment(settings, tmp_store, quiet.experiment_id, log=lambda m: None) is None
    assert mailbox.sent == []        # no mail for an experiment that went fine


def test_run_post_experiment_fires_without_the_risk_agent_having_run(settings, tmp_store, make_report,
                                                                     topology, mailbox):
    """The trigger must not silently depend on `--analyze` having been passed."""
    approval = pytest.importorskip("faultline.agents.approval")
    tmp_store.write_topology(topology)
    severe = make_report(during_success=0.0, finding_codes=["health_check_masked_failure"])
    tmp_store.write_experiment(severe)
    assert tmp_store.risk(severe.experiment_id) is None

    answer(mailbox, "CONTINUE")
    decision = approval.run_post_experiment(settings, tmp_store, severe.experiment_id, log=lambda m: None)
    assert decision is not None and decision.answer == "continue" and decision.answered is True
    assert len(mailbox.sent) == 1


# ---------------------------------------------------------------- the Weakness Scout hook
class _ScoutSuggestion:
    target_service = "mongodb"
    failure_type = "container_crash"


class _ScoutWeakness:
    predicted_level = "critical"
    suggested_experiment = _ScoutSuggestion()


class _ScoutReport:
    weaknesses = [_ScoutWeakness()]


class _StoreWithScout:
    """A minimal duck-typed stand-in for a store that has Scout output."""

    def weaknesses(self):
        return _ScoutReport()



def test_scout_hook_is_inert_until_the_scout_runs(tmp_store):
    approval = pytest.importorskip("faultline.agents.approval")
    # No reports/weaknesses.json yet: the hook must simply answer None.
    assert approval.scout_predicted_severity(tmp_store, _spec()) is None

    assert approval.scout_predicted_severity(_StoreWithScout(), _spec()) == "critical"
    assert approval.scout_predicted_severity(_StoreWithScout(), _spec(failure_type="network_delay")) is None
    assert approval.needs_pre_injection_approval(_spec(requires_approval=False)) == (False, "")


def test_scout_hook_reads_the_real_weakness_report(tmp_store):
    """The duck typing must match what the Weakness Scout actually writes, not just the stand-in."""
    from faultline.schemas import PredictedWeakness, SuggestedExperiment, WeaknessReport

    approval = pytest.importorskip("faultline.agents.approval")
    tmp_store.write_weaknesses(WeaknessReport(project=PROJECT, compose_file="docker-compose.yml", weaknesses=[
        PredictedWeakness(
            weakness_id="w1", category="no_redundancy", services=["mongodb"], title="single datastore",
            evidence="one mongodb service, no replica set", predicted_severity=8.6, predicted_level="critical",
            factors={"criticality": 0.95},
            suggested_experiment=SuggestedExperiment(failure_type="container_crash", target_service="mongodb"),
            confirming_finding_codes=["manual_restart_required"], why_it_matters="every service stores state there",
        ),
    ]))
    assert approval.scout_predicted_severity(tmp_store, _spec()) == "critical"
    assert approval.scout_predicted_severity(tmp_store, _spec(failure_type="network_delay")) is None
