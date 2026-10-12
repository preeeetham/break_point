"""The apply engine (faultline/remediation/apply.py), end to end with fakes: no Docker, no network, no sleeping.

Real pieces: the catalog, the override file handling, propose.build_proposals (so the preview/hash is the one a
human would really approve), proposals.approve, the operator Planner (clamping + policy) and the risk/scorecard
agents. Fakes: the Docker controller, the `docker compose` runner, the Injector, the probe window and the clock.
"""
from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.agents import risk, scorecard
from faultline.config import Settings
from faultline.planning.planner import Planner, Policy
from faultline.remediation import apply as A
from faultline.remediation import override as ov
from faultline.remediation import proposals as P
from faultline.remediation import propose
from faultline.schemas import Finding, WindowStats

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "faultline-apply-test"     # no such compose project exists: nothing here can match a real container

BASE_COMPOSE = """\
services:
  mongodb:
    image: mongo:7.0
  policies:
    image: eclipse/ditto-policies:3.9.0
    restart: always
    healthcheck:
      test: ["CMD-SHELL", "curl --fail http://localhost:7626/alive || exit 1"]
      interval: 10s
      retries: 6
  things:
    image: eclipse/ditto-things:3.9.0
    restart: always
    healthcheck:
      test: ["CMD-SHELL", "curl --fail http://localhost:7626/alive || exit 1"]
      interval: 10s
      timeout: 6s
      retries: 6
      start_period: 60s
  gateway:
    image: eclipse/ditto-gateway:3.9.0
    restart: always
    healthcheck:
      test: ["CMD-SHELL", "curl --fail http://localhost:7626/alive || exit 1"]
"""

# Deliberately odd bytes (comment, quoting, trailing spaces) so "restored" must mean byte-for-byte.
HAND_OVERRIDE = "# pre-existing override, hand formatted   \nservices:\n  gateway:\n    environment:\n      FOO: 'bar'   # keep me\n"


# ---------------------------------------------------------------------- fakes
class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps += 1
        self.t += s


class FakeDocker:
    """state() per service. `health_fn(svc, override_text)` decides health from what is in the override now."""

    def __init__(self, settings, health_fn=None, down=()):
        self.s = settings
        self.health_fn = health_fn or (lambda svc, text: "healthy")
        self.down = set(down)
        self.restarts: list[str] = []
        self.raise_on_state = None

    def state(self, svc):
        if self.raise_on_state:
            raise self.raise_on_state
        if svc in self.down:
            return {"status": "exited", "health": None, "paused": False}
        text = ov.read_text(self.s.remediation_override_file)
        return {"status": "running", "health": self.health_fn(svc, text), "paused": False}

    def restart(self, svc, timeout=10):
        self.restarts.append(svc)


class FakeRunner:
    """Stands in for subprocess.run of `docker compose ... up`. Records the override text and lock at each call."""

    def __init__(self, store, fail_calls=()):
        self.store = store
        self.fail_calls = set(fail_calls)
        self.calls: list[dict] = []

    def __call__(self, cmd, **kw):
        n = len(self.calls) + 1
        override_file = next((cmd[i + 1] for i, c in enumerate(cmd) if c == "-f" and "override" in cmd[i + 1]), None)
        self.calls.append({"cmd": list(cmd), "override": ov.read_text(override_file) if override_file else None,
                           "lock": (self.store.active() or {}).get("experiment_id")})
        rc = 1 if n in self.fail_calls else 0
        return SimpleNamespace(returncode=rc, stdout="" if rc == 0 else "", stderr="boom: no such image" if rc else "")


class FakeWindow:
    def __init__(self, rate=1.0, n=40):
        self.rate, self.n = rate, n

    def stats_since(self, t0, t1=None):
        return WindowStats(sample_count=self.n, success_rate=self.rate, p95_ms=50.0)


class FakeInjector:
    """Writes a report the way the real Injector does; `report_kw` shapes the re-run's numbers."""

    def __init__(self, store, make_report, **report_kw):
        self.store, self.make_report, self.kw = store, make_report, report_kw
        self.specs = []
        self.lock_seen = "unset"
        self.raise_exc = None

    def __call__(self, spec):
        self.lock_seen = self.store.active()
        self.specs.append(spec)
        if self.raise_exc:
            raise self.raise_exc
        kw = dict(self.kw)
        r = self.make_report(experiment_id=spec.experiment_id, service=spec.target_service,
                             failure_type=spec.failure_type, **kw)
        r.parameters = dict(spec.parameters)
        self.store.write_experiment(r)
        return r


# ---------------------------------------------------------------------- fixtures
@pytest.fixture
def env(tmp_path, tmp_store, topology, make_report, policy_raw):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    compose = deploy / "docker-compose.yml"
    compose.write_text(BASE_COMPOSE)
    s = Settings(compose_project=PROJECT, compose_file=str(compose), nginx_conf=str(deploy / "none.conf"),
                 reports_dir=str(tmp_store.root), thresholds_file=str(ROOT / "config" / "thresholds.yaml"),
                 knowledge_file=str(deploy / "none.yaml"),
                 remediation_override_file=str(deploy / "docker-compose.override.yml"), fix_health_timeout_s=30)
    tmp_store.write_topology(topology)
    clock = Clock()
    runner = FakeRunner(tmp_store)
    docker = FakeDocker(s)
    ns = SimpleNamespace(s=s, store=tmp_store, topology=topology, make_report=make_report, policy_raw=policy_raw,
                         clock=clock, runner=runner, docker=docker, window=FakeWindow(), logs=[], tmp=tmp_path)
    ns.injector = FakeInjector(tmp_store, make_report, ttr=12.0, during_success=0.6, finding_codes=[])
    return ns


def make_deps(env, allow_protected=False, **over):
    planner = Planner(env.topology, Policy(env.policy_raw), env.store.experiments(), None, env.s, {"pumba", "tc"},
                      allow_protected)

    def analyze():
        risk.run(env.store, None, env.topology, log=lambda m: None)
        scorecard.run(env.store, env.topology, env.s.compose_project, log=lambda m: None)

    kw = dict(docker=env.docker, window=env.window, planner=planner, inject=env.injector, analyze=analyze,
              runner=env.runner, clock=env.clock, sleep=env.clock.sleep, topology=env.topology,
              policy=Policy(env.policy_raw), health_interval_s=3.0, healthy_checks=2)
    kw.update(over)
    return A.Deps(**kw)


def original_experiment(env, code="health_check_masked_failure", **kw):
    """The experiment that found the problem, plus a scorecard so the proposal records a `before` score."""
    kw.setdefault("ttr", 20.0)
    kw.setdefault("during_success", 0.4)
    r = env.make_report(experiment_id="orig0001", service="things", failure_type="container_crash",
                        finding_codes=[code], **kw)
    env.store.write_experiment(r)
    scorecard.run(env.store, env.topology, env.s.compose_project, log=lambda m: None)
    return r


def approved(env, fix_id="add_or_tighten_healthcheck", include_test=False):
    base = ov.load(env.s.compose_file)
    ps = propose.build_proposals(env.s, env.store, "orig0001", llm=None, include_test=include_test,
                                 log=lambda m: None, base=base)
    p = next(p for p in ps if p.catalog_fix_id == fix_id)
    return P.approve(env.store, p.proposal_id, p.change_hash, "test", "go")


def run(env, p, deps=None, **kw):
    return A.apply_and_verify(env.s, env.store, p.proposal_id, log=env.logs.append,
                              deps=deps or make_deps(env, allow_protected=kw.get("allow_protected", False)), **kw)


def statuses(p):
    return [a.to_status for a in p.audit if a.from_status and a.from_status != a.to_status]


def assert_unlocked(env):
    assert not env.store.fix_lock_file.exists()
    assert env.store.active() is None


# ---------------------------------------------------------------------- preflight refusals
def test_stop_present_refuses_and_writes_nothing(env):
    original_experiment(env)
    p = approved(env)
    env.store.request_stop("test")
    got = run(env, p)
    assert got.status == "approved"
    assert "apply refused" in got.audit[-1].note and "STOP" in got.audit[-1].note
    assert got.audit[-1].from_status == got.audit[-1].to_status == "approved"
    assert not Path(env.s.remediation_override_file).exists()
    assert env.runner.calls == [] and env.injector.specs == []
    assert env.store.proposal(p.proposal_id).status == "approved"       # persisted, unchanged
    assert_unlocked(env)


def test_active_experiment_lock_refuses_and_is_left_alone(env):
    original_experiment(env)
    p = approved(env)
    env.store.set_active({"experiment_id": "someone-else", "target_service": "things"})
    got = run(env, p)
    assert got.status == "approved" and "someone-else" in got.audit[-1].note
    assert env.store.active()["experiment_id"] == "someone-else"           # never clears another owner's lock
    assert env.runner.calls == []


def test_not_approved_is_refused(env):
    original_experiment(env)
    ps = propose.build_proposals(env.s, env.store, "orig0001", llm=None, log=lambda m: None,
                                 base=ov.load(env.s.compose_file))
    got = run(env, ps[0])
    assert got.status == "pending" and "not approved" in got.audit[-1].note
    assert env.runner.calls == []


def test_tampered_preview_is_refused(env):
    original_experiment(env)
    p = approved(env)
    p.change_preview += "+    restart: always\n"
    env.store.write_proposal(p)
    got = run(env, p)
    assert got.status == "approved" and "no longer matches its own hash" in got.audit[-1].note
    assert not Path(env.s.remediation_override_file).exists()


def test_change_that_differs_from_the_approved_one_is_refused(env):
    original_experiment(env)
    p = approved(env)
    Path(env.s.remediation_override_file).write_text(HAND_OVERRIDE)     # the override moved under the approval
    got = run(env, p)
    assert got.status == "approved" and "differs from the one approved" in got.audit[-1].note
    assert Path(env.s.remediation_override_file).read_text() == HAND_OVERRIDE
    assert env.runner.calls == []


def test_expired_is_refused_without_transition(env):
    original_experiment(env)
    p = approved(env)
    p.expires_at = P.utcnow() - timedelta(seconds=1)
    env.store.write_proposal(p)
    got = run(env, p)
    assert got.status == "approved" and "expired" in got.audit[-1].note
    assert env.runner.calls == []


def test_protected_needs_allow_protected(env):
    original_experiment(env)
    p = approved(env)
    p.requires_allow_protected = True
    env.store.write_proposal(p)
    got = run(env, p)
    assert got.status == "approved" and "--allow-protected" in got.audit[-1].note
    assert env.runner.calls == []
    # the same proposal goes through when the operator passes it on purpose
    assert run(env, got, allow_protected=True).status == "verified"


def test_protected_target_refused_even_if_the_flag_was_not_stored(env):
    original_experiment(env)
    p = approved(env)
    p.target_services = ["mongodb"]
    env.store.write_proposal(p)
    assert "protected service(s) mongodb" in run(env, p).audit[-1].note


def test_second_fix_in_flight_is_refused_but_a_stale_lock_is_not(env):
    original_experiment(env)
    p = approved(env)
    env.store.fix_lock_file.write_text(json.dumps({"proposal_id": "fx-other", "started_at": time.time(),
                                                   "pid": os.getppid()}))
    got = run(env, p)
    assert got.status == "approved" and "fx-other" in got.audit[-1].note
    assert env.store.fix_lock_file.exists()                                  # the other fix keeps its lock
    # a lock whose process is gone does not block forever
    env.store.fix_lock_file.write_text(json.dumps({"proposal_id": "fx-dead", "started_at": 1.0, "pid": 999_999_999}))
    assert run(env, got).status == "verified"
    assert_unlocked(env)


def test_test_only_fix_needs_include_test(env, monkeypatch):
    monkeypatch.delenv("FAULTLINE_TEST_CATALOG", raising=False)
    original_experiment(env)
    p = approved(env, "break_healthcheck_for_test", include_test=True)
    got = run(env, p)
    assert got.status == "approved" and "test-only" in got.audit[-1].note


def test_unknown_catalog_fix_is_refused(env):
    original_experiment(env)
    p = approved(env)
    p.catalog_fix_id = "rm_rf_everything"
    env.store.write_proposal(p)
    assert "is unknown" in run(env, p).audit[-1].note


def test_sick_stack_fails_the_95_percent_gate(env):
    original_experiment(env)
    p = approved(env)
    env.window.rate = 0.80
    got = run(env, p)
    assert got.status == "approved" and "95%" in got.audit[-1].note
    env.window.rate, env.docker.down = 1.0, {"policies"}
    got = run(env, got)
    assert got.status == "approved" and "policies is exited" in got.audit[-1].note
    assert env.runner.calls == []


def test_preflight_lists_every_reason(env):
    original_experiment(env)
    p = approved(env)
    env.store.request_stop()
    env.store.set_active({"experiment_id": "x"})
    reasons = A.preflight(env.s, env.store, p, False, docker=env.docker, window=FakeWindow(rate=0.5), client=env.docker)
    assert any("STOP" in r for r in reasons) and any("active" in r for r in reasons)
    assert any("95%" in r for r in reasons)


# ---------------------------------------------------------------------- happy path
def test_happy_path_applies_reruns_and_verifies(env):
    orig = original_experiment(env)
    p = approved(env)
    got = run(env, p)

    assert got.status == "verified", got.audit[-1].note
    assert statuses(got) == ["approved", "applying", "applied", "verified"]
    assert got.applied_at is not None
    # the override now carries the change and the backup records "there was no override before"
    text = Path(env.s.remediation_override_file).read_text()
    assert "/ready" in text and "add_or_tighten_healthcheck" in text
    assert Path(got.override_backup).is_file() and ov.is_absent_marker(got.override_backup)
    assert Path(got.override_backup).parent == env.store.root / "remediation" / "backups"
    # one recreate, of exactly the changed service, with the override layered last, under our lock
    assert len(env.runner.calls) == 1
    call = env.runner.calls[0]
    assert call["cmd"][-1] == "things" and "--force-recreate" in call["cmd"] and "--no-deps" in call["cmd"]
    assert "/ready" in call["override"] and call["lock"] == f"fix:{got.proposal_id}"
    # the SAME experiment was re-run, through plan_operator, after our experiment lock was released
    spec = env.injector.specs[0]
    assert (spec.target_service, spec.failure_type, spec.parameters["duration_s"]) == ("things", "container_crash", 30)
    assert spec.planned_by == "operator" and env.injector.lock_seen is None
    # before/after
    v = got.verification
    assert v.outcome == "verified" and v.rerun_experiment_id == spec.experiment_id
    by = {c.metric: c for c in v.comparisons}
    assert by["time_to_recover_s"].before == orig.recovery.time_to_recover_s == 20.0
    assert by["time_to_recover_s"].after == 12.0 and by["time_to_recover_s"].verdict == "better"
    assert by["error_rate"].before == pytest.approx(0.6) and by["error_rate"].after == pytest.approx(0.4)
    assert by["error_rate"].verdict == "better" and v.targeted_metric == "error_rate"
    assert by["service_score"].before is not None and by["service_score"].after is not None
    assert by["finding:health_check_masked_failure"].after == 0 and by["finding:health_check_masked_failure"].verdict == "better"
    assert v.new_findings == []
    assert env.store.risk(spec.experiment_id) is not None                 # analysed like any experiment
    assert env.store.proposal(got.proposal_id).status == "verified"        # persisted
    log = (env.store.root / "logs" / "remediation.log").read_text()
    assert got.proposal_id in log and "verified" in log
    assert_unlocked(env)


def test_health_wait_tolerates_services_without_a_healthcheck(env):
    original_experiment(env)
    p = approved(env)
    env.docker.health_fn = lambda svc, text: None        # e.g. the live MongoDB: no healthcheck at all
    assert run(env, p).status == "verified"


# ---------------------------------------------------------------------- automatic rollback
def test_unhealthy_after_apply_restores_exact_previous_bytes(env):
    Path(env.s.remediation_override_file).write_text(HAND_OVERRIDE)
    original = Path(env.s.remediation_override_file).read_bytes()
    original_experiment(env)
    p = approved(env, "break_healthcheck_for_test", include_test=True)
    env.docker.health_fn = lambda svc, text: "unhealthy" if "exit 1" in text else "healthy"
    got = run(env, p, include_test=True)

    assert got.status == "rolled_back"
    assert statuses(got) == ["approved", "applying", "rolled_back"]
    assert "unhealthy" in got.audit[-1].note and "override restored" in got.audit[-1].note
    assert Path(env.s.remediation_override_file).read_bytes() == original
    assert len(env.runner.calls) == 2                       # apply, then recreate the same services on rollback
    assert env.runner.calls[0]["cmd"][-1] == env.runner.calls[1]["cmd"][-1] == "things"
    assert "exit 1" in env.runner.calls[0]["override"] and env.runner.calls[1]["override"] == HAND_OVERRIDE
    assert env.injector.specs == []                          # never re-ran on an unhealthy stack
    assert env.clock.sleeps > 0                              # it waited, bounded by fix_health_timeout_s
    assert_unlocked(env)


def test_unhealthy_rollback_when_there_was_no_override_before(env):
    original_experiment(env)
    p = approved(env, "break_healthcheck_for_test", include_test=True)
    env.docker.health_fn = lambda svc, text: "unhealthy" if "exit 1" in text else "healthy"
    got = run(env, p, include_test=True)
    assert got.status == "rolled_back"
    assert not Path(env.s.remediation_override_file).exists()      # restored to "absent"
    assert "-f" in env.runner.calls[1]["cmd"] and env.runner.calls[1]["override"] is None


def test_worse_rerun_rolls_back_with_reason(env):
    Path(env.s.remediation_override_file).write_text(HAND_OVERRIDE)
    original = Path(env.s.remediation_override_file).read_bytes()
    original_experiment(env, code="slow_recovery")
    p = approved(env, "set_resource_limits")
    env.injector.kw.update(ttr=60.0)
    got = run(env, p)

    assert got.status == "rolled_back"
    assert statuses(got) == ["approved", "applying", "applied", "verification_failed", "rolled_back"]
    v = got.verification
    assert v.outcome == "verification_failed" and v.targeted_metric == "time_to_recover_s"
    assert "time to recover went from 20 s to 60 s" in v.reason
    assert "time to recover went from 20 s to 60 s" in got.audit[-1].note
    assert Path(env.s.remediation_override_file).read_bytes() == original
    assert len(env.runner.calls) == 2
    assert_unlocked(env)


def test_new_finding_fails_verification_and_rolls_back(env):
    original_experiment(env)
    p = approved(env)
    env.injector.kw.update(finding_codes=["cascade_beyond_hypothesis"])
    got = run(env, p)
    assert got.status == "rolled_back"
    assert "verification_failed" in statuses(got)
    assert got.verification.new_findings == ["cascade_beyond_hypothesis"]
    assert "new finding" in got.verification.reason
    assert not Path(env.s.remediation_override_file).exists()


def test_targeted_finding_getting_worse_fails_verification(env):
    original_experiment(env)
    p = approved(env)

    def worse(spec):
        r = FakeInjector.__call__(env.injector, spec)
        r.findings = [Finding(code="health_check_masked_failure", severity="critical", title="things masked",
                              evidence="x")]
        env.store.write_experiment(r)
        return r

    got = run(env, p, deps=make_deps(env, inject=worse))
    assert got.status == "rolled_back" and "got worse" in got.verification.reason


def test_rerun_aborted_by_safety_counts_as_failed_verification(env):
    original_experiment(env)
    p = approved(env)
    env.injector.kw.update(status="aborted")
    got = run(env, p)
    assert got.status == "rolled_back" and "aborted by safety" in got.verification.reason
    assert got.verification.outcome == "verification_failed"


def test_rerun_skipped_by_injector_preflight_rolls_back(env):
    original_experiment(env)
    p = approved(env)
    env.injector.kw.update(status="skipped", error="system is not steady")
    got = run(env, p)
    assert got.status == "rolled_back" and "skipped" in got.verification.reason


def test_recreate_failure_rolls_back(env):
    Path(env.s.remediation_override_file).write_text(HAND_OVERRIDE)
    original_experiment(env)
    p = approved(env)
    env.runner.fail_calls = {1}
    got = run(env, p)
    assert got.status == "rolled_back"
    assert statuses(got) == ["approved", "applying", "rolled_back"]
    assert "applying failed" in got.audit[-1].note and "boom" in got.audit[-1].note
    assert Path(env.s.remediation_override_file).read_text() == HAND_OVERRIDE
    assert len(env.runner.calls) == 2 and env.injector.specs == []
    assert_unlocked(env)


def test_exception_during_rerun_restores_and_releases_locks(env):
    Path(env.s.remediation_override_file).write_text(HAND_OVERRIDE)
    original_experiment(env)
    p = approved(env)
    env.injector.raise_exc = RuntimeError("injector exploded")
    got = run(env, p)
    assert got.status == "rolled_back" and "injector exploded" in got.audit[-1].note
    assert Path(env.s.remediation_override_file).read_text() == HAND_OVERRIDE
    assert_unlocked(env)


def test_exception_during_health_wait_restores_and_releases_locks(env):
    original_experiment(env)
    p = approved(env)

    class Boom(FakeDocker):
        def state(self, svc):
            if "/ready" in ov.read_text(self.s.remediation_override_file):   # only once the change is on disk
                raise ConnectionError("docker socket went away")
            return super().state(svc)

    got = run(env, p, deps=make_deps(env, docker=Boom(env.s)))
    assert got.status == "rolled_back" and "ConnectionError" in got.audit[-1].note
    assert not Path(env.s.remediation_override_file).exists()
    assert len(env.runner.calls) == 2
    assert_unlocked(env)


class RecordingDocker(FakeDocker):
    """FakeDocker that also records `exec` calls (e.g. the proxy reload)."""

    def __init__(self, settings, exec_code=0, **kw):
        super().__init__(settings, **kw)
        self.execs: list[tuple[str, list]] = []
        self.exec_code = exec_code

    def exec(self, svc, cmd, detach=False, user=None):
        self.execs.append((svc, list(cmd)))
        return self.exec_code, "" if self.exec_code == 0 else "nginx: [emerg] boom"


def _approved_on(env, services):
    """An approved add_or_tighten_healthcheck on exactly `services`, built the way propose does it."""
    from faultline.remediation import catalog
    from faultline.schemas import FixProposal, ProposalExplanation
    base, label = ov.load(env.s.compose_file), propose._path_label(env.s)
    change = catalog.get("add_or_tighten_healthcheck").build(base, {}, list(services), env.topology)
    preview = catalog.preview(change, "", label)
    p = FixProposal(proposal_id=P.new_proposal_id(), experiment_id="orig0001",
                    finding_code="health_check_masked_failure", catalog_fix_id="add_or_tighten_healthcheck",
                    target_services=list(change.services), change_preview=preview, change_hash=P.change_hash(preview),
                    explanation=ProposalExplanation(problem="p", change="c", expected_improvement="e", risks="r",
                                                    rollback="rb"),
                    expires_at=P.expires_at(600))
    env.store.write_proposal(p)
    return P.approve(env.store, p.proposal_id, p.change_hash, "test", "go")


def test_recreating_a_proxied_service_reloads_the_proxy_in_front_of_it(env):
    """Found live: nginx resolves `proxy_pass http://gateway:8080` once, so a recreated gateway (new IP) left the
    API at 0% until nginx was reloaded. After every recreate, apply and rollback, the proxy is reloaded."""
    original_experiment(env)
    p = _approved_on(env, ["gateway"])
    docker = RecordingDocker(env.s)
    got = run(env, p, deps=make_deps(env, docker=docker))
    assert got.status == "verified", got.audit[-1].note
    assert docker.execs == [("nginx", ["nginx", "-s", "reload"])]
    assert any("reloaded nginx" in m for m in env.logs)


def test_rollback_recreate_also_reloads_the_proxy(env):
    original_experiment(env)
    p = _approved_on(env, ["gateway"])
    docker = RecordingDocker(env.s, health_fn=lambda svc, text: "unhealthy" if "/ready" in text else "healthy")
    got = run(env, p, deps=make_deps(env, docker=docker))
    assert got.status == "rolled_back"
    assert docker.execs == [("nginx", ["nginx", "-s", "reload"])] * 2      # after apply and after rollback


def test_no_proxy_reload_for_services_no_proxy_fronts(env):
    original_experiment(env)
    p = approved(env)                                     # things: nginx only proxies to gateway and ditto-ui
    docker = RecordingDocker(env.s)
    assert run(env, p, deps=make_deps(env, docker=docker)).status == "verified"
    assert docker.execs == []


def test_failed_proxy_reload_is_reported_not_raised(env):
    original_experiment(env)
    p = _approved_on(env, ["gateway"])
    docker = RecordingDocker(env.s, exec_code=1)
    assert run(env, p, deps=make_deps(env, docker=docker)).status == "verified"
    assert any("reloading nginx failed" in m for m in env.logs)


def test_container_replaced_mid_lookup_is_not_healthy_yet_rather_than_an_error(env):
    """docker.errors.NotFound while compose swaps a container is transient: keep waiting, do not roll back."""
    original_experiment(env)
    p = approved(env)

    class NotFound(Exception):
        pass

    class Flaky(FakeDocker):
        n = 0

        def state(self, svc):
            if Path(self.s.remediation_override_file).exists():     # only during the health wait after the change
                Flaky.n += 1
                if Flaky.n <= 2:
                    raise NotFound("No such container: bb464c44")
            return super().state(svc)

    got = run(env, p, deps=make_deps(env, docker=Flaky(env.s)))
    assert got.status == "verified", got.audit[-1].note
    assert Flaky.n >= 3                                   # it really hit NotFound twice and kept waiting


class RecoveringWindow:
    """The API probe after a recreate: failing until `recovered_at` (fake clock), then 100%. A window that still
    covers the outage reports the outage's success rate, as the real SampleWindow does."""

    def __init__(self, recovered_at):
        self.recovered_at = recovered_at

    def stats_since(self, t0, t1=None):
        return WindowStats(sample_count=40, success_rate=1.0 if t0 >= self.recovered_at else 0.69, p95_ms=50.0)


def test_health_wait_uses_the_injectors_steady_state_window_so_the_rerun_is_not_refused(env):
    """Found live: health was judged over the last 10 s, the Injector's preflight over pre_window_s (20 s), so the
    re-run started at once and was skipped ("system is not steady: success rate 69%"). They must agree."""
    original_experiment(env)
    p = approved(env)
    env.s.fix_health_timeout_s = 120
    pol = Policy(env.policy_raw)
    window = RecoveringWindow(recovered_at=env.clock() + 5)
    seen = []

    def inject(spec):          # what the real Injector's preflight measures at the moment the re-run starts
        seen.append(window.stats_since(env.clock() - pol.pre_window_s).success_rate)
        return env.injector(spec)

    got = run(env, p, deps=make_deps(env, window=window, inject=inject))
    assert seen and seen[0] >= pol.min_pre_success, seen
    assert got.status == "verified", got.audit[-1].note


def test_skipped_rerun_without_a_report_is_not_linked(env):
    original_experiment(env)
    p = approved(env)

    def skipped(spec):         # the real Injector returns a skipped report without writing it
        r = env.make_report(experiment_id=spec.experiment_id, service=spec.target_service,
                            failure_type=spec.failure_type, status="skipped")
        r.error = "system is not steady: success rate 69% before injection"
        return r

    got = run(env, p, deps=make_deps(env, inject=skipped))
    assert got.status == "rolled_back" and "not steady" in got.verification.reason
    assert got.verification.rerun_experiment_id is None


def test_locks_are_held_during_apply_and_released_after_refusal_inside_the_lock(env, monkeypatch):
    original_experiment(env)
    p = approved(env)
    seen = {}
    real = A.rebuild

    def rebuild_then_refuse(*a, **kw):
        if env.store.fix_lock_file.exists():          # second call: under the lock
            seen["fix_lock"] = json.loads(env.store.fix_lock_file.read_text())
            raise A.Refused("changed under our feet")
        return real(*a, **kw)

    monkeypatch.setattr(A, "rebuild", rebuild_then_refuse)
    got = run(env, p)
    assert seen["fix_lock"]["proposal_id"] == p.proposal_id and seen["fix_lock"]["pid"] == os.getpid()
    assert "started_at" in seen["fix_lock"]
    assert got.status == "approved" and "changed under our feet" in got.audit[-1].note
    assert_unlocked(env)


# ---------------------------------------------------------------------- runtime-only
def test_runtime_only_fix_restarts_and_never_touches_the_override(env):
    original_experiment(env, code="manual_restart_required")
    p = approved(env, "restart_service_now")
    got = run(env, p)
    assert sorted(env.docker.restarts) == sorted(got.target_services) and "things" in env.docker.restarts
    assert env.runner.calls == []
    assert got.override_backup is None and not Path(env.s.remediation_override_file).exists()
    assert got.status == "verified" and got.verification.targeted_metric == "time_to_recover_s"


def test_runtime_only_failed_verification_stays_verification_failed(env):
    original_experiment(env, code="manual_restart_required")
    p = approved(env, "restart_service_now")
    env.injector.kw.update(ttr=90.0)
    got = run(env, p)
    assert got.status == "verification_failed"
    assert any("nothing to roll back" in a.note for a in got.audit)
    assert env.runner.calls == []
    assert_unlocked(env)


# ---------------------------------------------------------------------- comparison rules
@pytest.mark.parametrize("metric,before,after,verdict", [
    ("time_to_recover_s", 20.0, 21.0, "same"),          # inside max(2 s, 10%)
    ("time_to_recover_s", 20.0, 30.0, "worse"),
    ("time_to_recover_s", 20.0, 10.0, "better"),
    ("error_rate", 0.50, 0.51, "same"),
    ("error_rate", 0.50, 0.70, "worse"),
    ("service_score", 70.0, 80.0, "better"),
    ("service_score", 70.0, 60.0, "worse"),
    ("p95_ms", 800.0, None, "unknown"),
])
def test_verdict_rules(metric, before, after, verdict):
    assert A._verdict(metric, before, after) == verdict


def test_never_recovering_is_worse_than_any_recovery():
    assert A._verdict("time_to_recover_s", 20.0, None, none_is_worst=True) == "worse"
    assert A._verdict("time_to_recover_s", None, 50.0, none_is_worst=True) == "better"


def _abort(rep, reason="error_rate"):
    rep.status = "aborted"
    rep.safety.aborted_early, rep.safety.abort_reason = True, reason
    return rep


def test_rerun_aborted_for_the_same_reason_as_the_original_is_still_compared(env):
    r = _abort(original_experiment(env))
    env.store.write_experiment(r)
    p = approved(env)

    def inject(spec):
        rep = _abort(env.injector(spec))
        env.store.write_experiment(rep)
        return rep

    got = run(env, p, deps=make_deps(env, inject=inject))
    assert "aborted by safety" not in (got.verification.reason or "")
    assert got.status == "verified" and got.verification.comparisons


def test_rerun_aborted_for_a_different_reason_still_fails(env):
    r = _abort(original_experiment(env), "latency")
    env.store.write_experiment(r)
    p = approved(env)

    def inject(spec):
        rep = _abort(env.injector(spec), "error_rate")
        env.store.write_experiment(rep)
        return rep

    got = run(env, p, deps=make_deps(env, inject=inject))
    assert got.status == "rolled_back" and "aborted by safety" in got.verification.reason


def test_service_score_compares_the_same_service_and_scorecard_shows_the_final_state(env):
    original_experiment(env)
    p = approved(env)
    got = run(env, p)
    assert got.status == "verified"
    sc = env.store.scorecard()
    things = next(r for r in sc.ranking if r.service == "things")
    score = next(c for c in got.verification.comparisons if c.metric == "service_score")
    assert score.after == things.score                       # the experiment target, not the fix's other services
    assert any(f.proposal_id == p.proposal_id and f.state == "verified" for f in things.fixes)
    md = (env.store.root / "proposals" / f"{p.proposal_id}.md").read_text()
    assert "**Status:** verified" in md


def test_cli_apply_only_applies_an_approved_proposal(env, monkeypatch):
    from faultline import cli
    original_experiment(env)
    p = approved(env)
    P.transition(p, "applying", "test")
    env.store.write_proposal(p)

    class A:
        proposal_id, allow_protected = p.proposal_id, False

    assert cli.cmd_apply(env.s, env.store, A()) == 1
    A.proposal_id = "fx-missing"
    assert cli.cmd_apply(env.s, env.store, A()) == 1
