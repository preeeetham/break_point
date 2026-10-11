"""End to end through `faultline run`, both scenarios, with real mail.

What is real here: the whole CLI run path (`cli.cmd_run`), the planner and its policy check, the
approval agent, and the `smtplib` / `imaplib` clients talking to the loopback servers in
`tests/fakemail.py`. What is faked: the Docker client, the probes and the injector, exactly as
`tests/test_control.py` fakes them, because this machine has no Docker daemon and no Ditto stack.

The one thing these tests cannot cover is the container mechanics themselves, which is also the one
thing the approval agent never touches.
"""
from __future__ import annotations

import argparse
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from faultline import cli
from faultline.config import Settings
from faultline.planning.planner import Policy
from faultline.safety.thresholds import thresholds_from

from .conftest import PROJECT, ROOT
from .fakemail import FakeIMAPServer, FakeMailbox, FakeSMTPServer

ADMIN = "admin@example.invalid"


# ---------------------------------------------------------------- the faked layer
class FakeDocker:
    def __init__(self) -> None:
        self.network_name = f"{PROJECT}_default"

    def has_image(self, image: str) -> bool:
        return True

    def is_running(self, svc: str) -> bool:
        return True

    def state(self, svc: str) -> dict[str, Any]:
        return {"status": "running", "health": "healthy", "restart_count": 0}

    def containers(self) -> dict:
        return {}


class RecordingInjector:
    """Records what it was asked to run. Nothing is injected; that layer has no daemon here."""

    runs: list = []
    report_for = None          # a class attribute so a test can monkeypatch it

    def __init__(self, settings, store, topology, docker, window, api, health, thresholds, policy, log=None):
        self.store = store

    def run(self, spec):
        RecordingInjector.runs.append(spec)
        report = self.report_for(experiment_id=spec.experiment_id, service=spec.target_service,
                                 failure_type=spec.failure_type)
        report.parameters = dict(spec.parameters)
        self.store.write_experiment(report)
        return report


@pytest.fixture
def mailbox() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
def mail(mailbox):
    with FakeSMTPServer(mailbox) as smtp, FakeIMAPServer(mailbox) as imap:
        yield smtp, imap


@pytest.fixture
def run_env(monkeypatch, tmp_path, topology, policy_raw, mail, make_report):
    """A `faultline run` that reaches real mail and a recording injector, and nothing else."""
    smtp, imap = mail
    settings = Settings(
        compose_project=PROJECT, reports_dir=str(tmp_path / "reports"),
        thresholds_file=str(ROOT / "config" / "thresholds.yaml"), llm_enabled=False,
        admin_email=ADMIN, smtp_host=smtp.host, smtp_port=smtp.port, smtp_user="faultline",
        smtp_password="secret", smtp_from="faultline@example.invalid", smtp_starttls=False,
        imap_host=imap.host, imap_port=imap.port, imap_user="faultline", imap_password="secret",
        imap_ssl=False, approval_timeout_s=6.0, approval_poll_s=0.05,
    )

    class FakeSession:
        def __init__(self, s, store, need_probes=True, rebuild_topology=False):
            self.s, self.store = s, store
            self.docker = FakeDocker()
            self.topology = topology
            self.policy_raw = policy_raw
            self.policy = Policy(policy_raw)
            self.policy.pause_between_s = 0.0
            self.thresholds = thresholds_from(policy_raw, None)
            self.window = SimpleNamespace(all=lambda: [], stats_since=lambda *a: None)
            self.api = SimpleNamespace(stop=lambda: None)
            self.health = SimpleNamespace(stop=lambda: None)

        def warmup(self, seconds: float) -> None:
            pass

        def close(self) -> None:
            pass

    import faultline.agents.injector as injector_mod

    monkeypatch.setattr(cli, "Session", FakeSession)
    monkeypatch.setattr(injector_mod, "Injector", RecordingInjector)
    monkeypatch.setattr(RecordingInjector, "runs", [])
    monkeypatch.setattr(RecordingInjector, "report_for", staticmethod(make_report))
    monkeypatch.setattr(cli, "build_topology", lambda *a, **kw: topology)

    store = cli.ReportStore(settings.reports)
    store.write_topology(topology)
    return SimpleNamespace(settings=settings, store=store, said=_capture(monkeypatch))


def _capture(monkeypatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(cli, "say", lambda m: lines.append(str(m)))
    return lines


def _args(**kw: Any) -> argparse.Namespace:
    base = dict(experiments=1, target=None, failure=None, param=[], duration=None, no_llm=True,
                allow_protected=False, analyze=False, resume_stop=True, no_approvals=False)
    base.update(kw)
    return argparse.Namespace(**base)


def answer(mailbox: FakeMailbox, body: str, *, index: int = 0, after: float = 0.0) -> None:
    """Reply to the Nth email the agent sent, from a thread, once it exists."""

    def _reply() -> None:
        deadline = time.time() + 8
        while len(mailbox.sent) <= index and time.time() < deadline:
            time.sleep(0.01)
        if after:
            time.sleep(after)
        if len(mailbox.sent) > index:
            mailbox.reply_to(index, body, from_addr=ADMIN)

    threading.Thread(target=_reply, daemon=True).start()


# ---------------------------------------------------------------- scenario A end to end
def test_protected_experiment_runs_only_after_the_admin_approves(run_env, mailbox):
    answer(mailbox, "APPROVE")
    rc = cli.cmd_run(run_env.settings, run_env.store, _args(target="mongodb", failure="container_crash",
                                                            param=["duration_s=40"]))
    assert rc == 0
    # the gate let it through, and the injector saw the full-strength parameters
    assert len(RecordingInjector.runs) == 1
    spec = RecordingInjector.runs[0]
    assert spec.target_service == "mongodb" and spec.requires_approval is True
    assert spec.parameters["duration_s"] == 40
    said = "\n".join(run_env.said)
    assert "approval gate on" in said and "cleared by the admin: APPROVE" in said
    # the decision is on the blackboard and nothing is left pending
    assert run_env.store.pending_approval() is None
    record = run_env.store.approvals()[0]
    assert record.scenario == "pre_injection" and record.decision.answered is True


def test_skip_means_nothing_is_injected(run_env, mailbox):
    answer(mailbox, "SKIP")
    cli.cmd_run(run_env.settings, run_env.store, _args(target="mongodb", failure="container_crash"))
    assert RecordingInjector.runs == []
    assert "SKIPPED by the admin" in "\n".join(run_env.said)
    assert run_env.store.approvals()[0].decision.answer == "skip"


def test_no_reply_at_all_means_nothing_is_injected(run_env):
    run_env.settings.approval_timeout_s = 1.0
    cli.cmd_run(run_env.settings, run_env.store, _args(target="mongodb", failure="container_crash"))
    assert RecordingInjector.runs == []        # the safe default is not to do the risky thing
    decision = run_env.store.approvals()[0].decision
    assert decision.answer == "skip" and decision.answered is False
    assert "no valid reply" in decision.reason


def test_approve_reduced_injects_the_halved_parameters(run_env, mailbox):
    answer(mailbox, "APPROVE REDUCED")
    cli.cmd_run(run_env.settings, run_env.store, _args(target="mongodb", failure="container_crash",
                                                       param=["duration_s=60"]))
    assert len(RecordingInjector.runs) == 1
    spec = RecordingInjector.runs[0]
    assert spec.parameters["duration_s"] == 30
    # and the report says what the admin changed, not just that they approved
    report = run_env.store.experiment(spec.experiment_id)
    assert report.parameters["duration_s"] == 30
    notes = " | ".join(spec.planner_notes)
    assert "halved 60 -> 30" in notes and "admin approved a reduced run" in notes


def test_stop_run_aborts_the_whole_run_through_the_existing_stop_file(run_env, mailbox):
    answer(mailbox, "STOP RUN")
    cli.cmd_run(run_env.settings, run_env.store,
                _args(target="mongodb", failure="container_crash", experiments=3))
    assert RecordingInjector.runs == []
    assert run_env.store.stop_requested() is True          # the documented kill switch, reused
    assert "STOP RUN by the admin" in "\n".join(run_env.said)


def test_a_malformed_reply_then_a_real_one(run_env, mailbox):
    answer(mailbox, "looks fine to me")
    answer(mailbox, "APPROVE", after=0.8)
    cli.cmd_run(run_env.settings, run_env.store, _args(target="mongodb", failure="container_crash"))
    assert len(RecordingInjector.runs) == 1
    decision = run_env.store.approvals()[0].decision
    assert decision.answer == "approve" and decision.clarifications_sent == 1
    # three mails: the request, the clarification, nothing else
    assert len(mailbox.sent) == 2
    assert "not one of the options" in mailbox.sent[1]["msg"].get_payload(decode=True).decode()


def test_allow_protected_still_bypasses_the_gate_entirely(run_env, mailbox):
    """The explicit operator override is untouched: no mail, no wait."""
    cli.cmd_run(run_env.settings, run_env.store,
                _args(target="mongodb", failure="container_crash", allow_protected=True))
    assert len(RecordingInjector.runs) == 1
    assert RecordingInjector.runs[0].requires_approval is False
    assert mailbox.sent == [] and run_env.store.approvals() == []


def test_no_approvals_flag_restores_the_old_refusal(run_env, mailbox):
    with pytest.raises(ValueError, match="protected"):
        cli.cmd_run(run_env.settings, run_env.store,
                    _args(target="mongodb", failure="container_crash", no_approvals=True))
    assert mailbox.sent == []


# ---------------------------------------------------------------- scenario B end to end
def test_a_severe_experiment_emails_and_records_the_chosen_fix(run_env, mailbox, make_report):
    """A permitted experiment runs without a gate, then its result triggers scenario B."""
    def severe(**kw):
        return make_report(during_success=0.0, finding_codes=["health_check_masked_failure"], **kw)

    RecordingInjector.report_for = staticmethod(severe)  # restored by the fixture's monkeypatch
    answer(mailbox, "FIX 1")
    cli.cmd_run(run_env.settings, run_env.store, _args(target="things", failure="container_crash"))

    assert len(RecordingInjector.runs) == 1
    eid = RecordingInjector.runs[0].experiment_id
    # one mail, and it is the post-incident one (no gate: things is not protected)
    assert len(mailbox.sent) == 1
    body = mailbox.sent[0]["msg"].get_payload(decode=True).decode()
    assert "Risk assessment (Risk agent)" in body and "advisory only" in body
    # the choice is recorded against the experiment and nothing else happened
    report = run_env.store.experiment(eid)
    assert report.approval.answer == "fix" and report.approval.fix_index == 1
    assert report.approval.fix_title
    assert run_env.store.stop_requested() is False


def test_wait_pauses_the_remaining_experiments(run_env, mailbox, make_report):
    def severe(**kw):
        return make_report(during_success=0.0, finding_codes=["health_check_masked_failure"], **kw)

    RecordingInjector.report_for = staticmethod(severe)  # restored by the fixture's monkeypatch
    answer(mailbox, "WAIT")
    cli.cmd_run(run_env.settings, run_env.store, _args(experiments=3))

    # the first experiment ran, the admin said WAIT, and the loop stopped there
    assert len(RecordingInjector.runs) == 1
    assert run_env.store.stop_requested() is True
    assert "STOP requested; not starting another experiment" in "\n".join(run_env.said)


def test_a_quiet_experiment_sends_nothing(run_env, mailbox, make_report):
    RecordingInjector.report_for = staticmethod(
        lambda **kw: make_report(during_success=1.0, ttr=2.0, finding_codes=[], **kw))
    cli.cmd_run(run_env.settings, run_env.store, _args(experiments=2))
    assert len(RecordingInjector.runs) == 2
    assert mailbox.sent == []                  # no mail when nothing went wrong
    assert run_env.store.approvals() == []


def test_both_scenarios_in_one_run(run_env, mailbox, make_report):
    """The protected experiment is gated, runs reduced, comes back severe and is then reported."""
    def severe(**kw):
        return make_report(during_success=0.0, finding_codes=["health_check_masked_failure"], **kw)

    RecordingInjector.report_for = staticmethod(severe)  # restored by the fixture's monkeypatch
    answer(mailbox, "APPROVE REDUCED", index=0)
    answer(mailbox, "FIX 2", index=1)
    cli.cmd_run(run_env.settings, run_env.store, _args(target="mongodb", failure="container_crash",
                                                       param=["duration_s=60"]))

    assert len(mailbox.sent) == 2
    first, second = (str(m["msg"]["Subject"]) for m in mailbox.sent)
    assert "approve container_crash on mongodb?" in first
    assert "decide" in second and ("HIGH" in second or "CRITICAL" in second)

    spec = RecordingInjector.runs[0]
    assert spec.parameters["duration_s"] == 30                       # scenario A reduced it
    report = run_env.store.experiment(spec.experiment_id)
    assert report.approval.answer == "fix" and report.approval.fix_index == 2   # scenario B logged it

    # two decisions on the blackboard, one per scenario, nothing pending
    scenarios = [r.scenario for r in run_env.store.approvals()]
    assert scenarios == ["pre_injection", "problem_occurred"]
    assert run_env.store.pending_approval() is None
    assert run_env.store.stop_requested() is False
