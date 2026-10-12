"""Control plane behind the dashboard. Offline: the cli Session (docker, probes), the injector, heal and
topology rebuilds are all replaced at the boundary, and the LLM is disabled so nothing touches the network."""
from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

import faultline.agents.approval as approval_mod
import faultline.agents.scout as scout_mod
import faultline.cli as cli
import faultline.dashboard.control as control_mod
from faultline.config import Settings
from faultline.dashboard.control import (
    TELEMETRY_SAMPLE_S,
    TELEMETRY_WINDOW_S,
    ControlPlane,
    EventLog,
    JobBusy,
    Telemetry,
    infer_level,
)
from faultline.observe.window import SampleWindow
from faultline.planning.planner import Policy
from faultline.schemas import (
    PREDICTED_LABEL,
    CascadeAttribution,
    CascadeEdge,
    CascadeHop,
    PredictedWeakness,
    SuggestedExperiment,
    SystemicPattern,
    WeaknessReport,
)
from faultline.safety.thresholds import thresholds_from
from faultline.schemas import PRE_INJECTION_REPLIES, ApprovalDecision, ApprovalRequest, utcnow

from .conftest import PROJECT, ROOT

STATE_KEYS = {"now", "seq", "events", "session", "job", "run", "telemetry", "stop_requested",
              "pending_approval", "guardian", "agents", "llm", "topology", "baseline", "experiments",
              "scorecard", "weaknesses", "reconciliation", "target", "proposals"}
PROPOSAL_SUMMARY_KEYS = {"id", "experiment_id", "finding_code", "catalog_fix_id", "target_services", "status",
                         "created_at", "expires_at", "seconds_left", "requires_allow_protected", "explanation",
                         "change_preview", "change_hash", "verification", "decided_by", "decided_at", "reason"}
PREVIEW = ("--- deploy/docker-compose.override.yml\n+++ deploy/docker-compose.override.yml\n@@ services @@\n"
           " services:\n   things:\n-    healthcheck: {test: [CMD, curl, -f, /alive]}\n"
           "+    healthcheck: {test: [CMD, curl, -f, /ready]}\n")


def make_proposal(proposal_id: str = "fx-0123456789", experiment_id: str = "exp-fix", preview: str = PREVIEW,
                  expires_in: float = 600.0, created_ago: float = 0.0, **kw):
    """A real FixProposal, hashed with proposals.change_hash, as the remediation engine would write it."""
    from faultline.remediation import proposals as P
    from faultline.schemas import FixProposal, ProposalExplanation

    fields = dict(
        proposal_id=proposal_id, experiment_id=experiment_id, finding_code="health_check_masked_failure",
        catalog_fix_id="add_or_tighten_healthcheck", target_services=["things"], change_preview=preview,
        change_hash=P.change_hash(preview),
        explanation=ProposalExplanation(
            problem="things answered /ready 200 while 92% of real requests through it failed (evidence: probe).",
            change="Point the things health check at /ready instead of /alive.",
            expected_improvement="Docker marks things unhealthy while its dependency is down.",
            risks="A slow dependency now marks things unhealthy too.",
            rollback="The previous override is restored byte-for-byte and things is recreated."),
        created_at=utcnow() - timedelta(seconds=created_ago),
        expires_at=utcnow() + timedelta(seconds=expires_in),
    )
    fields.update(kw)
    return FixProposal(**fields)


def wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def wait_job(control: ControlPlane, timeout: float = 5.0) -> dict:
    assert wait_for(lambda: control.job is not None and control.job.status != "running"
                    and control.job.ended_at is not None, timeout), "job did not finish"
    return control.job.as_dict()


# ---------------------------------------------------------------- fakes at the boundary
class FakeApi:
    def __init__(self):
        self.alive = True
        self.canary_calls = 0
        self.stopped = False

    def is_alive(self) -> bool:
        return self.alive and not self.stopped

    def ensure_canary(self) -> None:
        self.canary_calls += 1

    def stop(self) -> None:
        self.stopped = True


class FakeDocker:
    def has_image(self, image: str) -> bool:
        return True


def make_session_cls(topology, policy_raw, built: list, delay: float = 0.0):
    class FakeSession:
        """Stands in for faultline.cli.Session: no docker client, no probe threads."""

        def __init__(self, s, store, need_probes=True, rebuild_topology=False):
            if delay:
                time.sleep(delay)
            self.s, self.store = s, store
            self.docker = FakeDocker()
            self.topology = topology
            self.policy_raw = policy_raw
            self.policy = Policy(policy_raw)
            self.thresholds = thresholds_from(policy_raw, None)
            self.window = SampleWindow()
            self.api = FakeApi()
            self.health = SimpleNamespace(stop=lambda: None)
            self.closed = False
            built.append(self)

        def close(self) -> None:
            self.closed = True
            self.api.stop()

    return FakeSession


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(compose_project=PROJECT, reports_dir=str(tmp_path / "reports"),
                    thresholds_file=str(ROOT / "config" / "thresholds.yaml"), llm_enabled=False)


@pytest.fixture
def sessions(monkeypatch, topology, policy_raw) -> list:
    built: list = []
    monkeypatch.setattr(cli, "Session", make_session_cls(topology, policy_raw, built))

    def no_docker(*a, **kw):
        raise AssertionError("test tried to reach docker")

    monkeypatch.setattr(cli, "build_topology", no_docker)
    monkeypatch.setattr(cli, "cmd_heal", no_docker)
    return built


@pytest.fixture
def control(settings, tmp_store, sessions):
    c = ControlPlane(settings, tmp_store)
    yield c
    c.shutdown()


def block_handler(control: ControlPlane, name: str, monkeypatch) -> threading.Event:
    """Swap one job handler for one that runs until the returned event is set."""
    release = threading.Event()

    def _blocking(body):
        release.wait(5)
        return {"blocked": True}

    monkeypatch.setattr(control, name, _blocking)
    return release


# ---------------------------------------------------------------- event log
def test_event_log_sequence_is_monotonic_and_since_filters():
    log = EventLog()
    assert log.seq == 0 and log.since(0) == []
    for i in range(5):
        ev = log.add("info", "system", f"e{i}")
        assert ev.seq == i + 1
    assert log.seq == 5
    assert [e["seq"] for e in log.since(0)] == [1, 2, 3, 4, 5]
    assert [e["text"] for e in log.since(3)] == ["e3", "e4"]
    assert log.since(5) == [] and log.since(99) == []
    assert set(log.since(4)[0]) == {"seq", "ts", "level", "source", "text", "data"}


def test_event_log_since_caps_at_300_newest_and_eviction_keeps_seq():
    log = EventLog()
    for i in range(405):
        log.add("info", "system", str(i))
    out = log.since(0)
    # a client that fell far behind gets the newest 300, not the oldest
    assert len(out) == 300 and out[0]["seq"] == 106 and out[-1]["seq"] == 405
    small = EventLog(maxlen=10)
    for i in range(25):
        small.add("info", "system", str(i))
    assert small.seq == 25
    assert [e["seq"] for e in small.since(0)] == list(range(16, 26))
    assert [e["seq"] for e in small.since(20)] == [21, 22, 23, 24, 25]


def test_event_text_truncated_and_data_defaults():
    ev = EventLog().add("info", "system", "x" * 5000)
    assert len(ev.text) == 2000 and ev.data == {}


@pytest.mark.parametrize("text,source,level", [
    ("Job failed: boom", "system", "error"),
    ("Safety abort: p95 over threshold", "injector", "warn"),
    ("Service recovered after 12s", "injector", "success"),
    ("injecting container_crash", "injector", "chaos"),
    ("injecting container_crash", "risk", "info"),
    ("nothing notable", "planner", "info"),
])
def test_infer_level(text, source, level):
    assert infer_level(text, source) == level


# ---------------------------------------------------------------- state snapshot
def test_cold_state_matches_contract_and_does_not_arm(control, sessions):
    st = control.state(0)
    assert STATE_KEYS <= set(st)
    assert st["seq"] >= 1 and st["events"][0]["seq"] == 1
    assert st["session"]["armed"] is False and st["session"]["probe"]["running"] is False
    assert st["job"] is None and st["run"]["phase"] == "idle"
    assert st["stop_requested"] is False
    assert st["pending_approval"] is None
    assert st["telemetry"] == {"points": [], "phases": []}
    assert set(st["agents"]) == set(control_mod.AGENTS)
    assert st["llm"]["available"] is False  # LLM disabled: no availability probe over the network
    assert st["topology"] is None and st["baseline"] is None and st["experiments"] == [] and st["scorecard"] is None
    assert st["weaknesses"] is None  # predictions only exist once the Scout has run
    assert sessions == [] and control.session is None


def test_state_since_returns_only_newer_events(control):
    seq0 = control.state(0)["seq"]
    control.log("system", "first")
    control.log("planner", "second", "ai", {"k": 1})
    st = control.state(seq0)
    assert st["seq"] == seq0 + 2
    assert [e["text"] for e in st["events"]] == ["first", "second"]
    assert all(e["seq"] > seq0 for e in st["events"])
    assert st["events"][1]["level"] == "ai" and st["events"][1]["data"] == {"k": 1}
    assert control.state(st["seq"])["events"] == []


def test_run_state_reads_live_file_and_derives_blast(control, tmp_store):
    tmp_store.write_live({"phase": "injecting", "experiment_id": "abc", "target_service": "things",
                          "failure_type": "container_crash", "elapsed_s": 3.0, "success_rate": 0.2,
                          "health": {"things": {"alive": None, "ready": None, "status": "exited"},
                                     "gateway": {"alive": 200, "ready": 200, "status": "running"},
                                     "policies": {"alive": 503, "ready": 503, "status": "running"}}})
    run = control.state(0)["run"]
    assert run["phase"] == "injecting" and run["experiment_id"] == "abc" and run["target_service"] == "things"
    assert sorted(run["blast"]["observed"]) == ["policies", "things"]
    assert run["safety"] == {"abort_reason": None, "detail": {}} and run["timeline"] == []


def test_run_state_carries_timeline_and_safety_detail_when_the_injector_writes_them(control, tmp_store):
    # the monitor's verdict detail is spread flat into live.json by Injector._live
    tmp_store.write_live({"phase": "rollback", "experiment_id": "abc", "abort_reason": "error_rate",
                          "trailing_samples": 20, "error_rate": 0.9, "threshold": 0.5, "p95_ms": 900,
                          "timeline": [{"t_plus_s": 0.7, "event": "injected", "detail": "SIGKILL"}]})
    run = control.state(0)["run"]
    assert run["safety"] == {"abort_reason": "error_rate",
                             "detail": {"trailing_samples": 20, "error_rate": 0.9, "threshold": 0.5}}
    assert run["timeline"] == [{"t_plus_s": 0.7, "event": "injected", "detail": "SIGKILL"}]


def test_analyze_job_shows_as_analyzing_phase(control, monkeypatch):
    release = block_handler(control, "_job_analyze", monkeypatch)
    control.submit("analyze", {})
    try:
        assert control.state(0)["run"]["phase"] == "analyzing"
    finally:
        release.set()
    wait_job(control)
    assert control.state(0)["run"]["phase"] == "idle"


def test_experiments_newest_first_and_detail(control, tmp_store, make_report):
    old, new = make_report(experiment_id="old"), make_report(experiment_id="new")
    tmp_store.write_experiment(old)
    tmp_store.write_experiment(new)
    ids = [e["experiment_id"] for e in control.state(0)["experiments"]]
    assert ids == ["new", "old"]
    detail = control.experiment_detail("old")
    assert set(detail) == {"experiment", "risk", "fix", "markdown"}
    assert detail["experiment"]["experiment_id"] == "old" and detail["risk"] is None
    assert control.experiment_detail("missing") is None


# ---------------------------------------------------------------- cascade attribution
LIVE_EDGES = [{"src": "gateway", "dst": "things", "mechanism": "dependency_timeout", "confidence": "ambiguous",
               "lag_s": 2.9},
              {"src": "nginx", "dst": "gateway", "mechanism": "dependency_error", "confidence": "ordered",
               "lag_s": 4.0}]
LIVE_HOPS = [{"service": "things", "mechanism": "injected_fault", "path_confidence": "origin",
              "masked_by_health_check": False, "beyond_hypothesis": False},
             {"service": "gateway", "mechanism": "dependency_timeout", "path_confidence": "ambiguous",
              "masked_by_health_check": True, "beyond_hypothesis": False}]


def test_run_state_passes_live_cascade_through_to_blast(control, tmp_store):
    tmp_store.write_live({"phase": "injecting", "experiment_id": "abc", "target_service": "things",
                          "health": {"things": {"alive": None, "ready": None, "status": "exited"}},
                          "cascade_edges": LIVE_EDGES + ["junk", None], "cascade_hops": LIVE_HOPS})
    blast = control.state(0)["run"]["blast"]
    assert blast["observed"] == ["things"]
    assert blast["cascade_edges"] == LIVE_EDGES       # malformed entries dropped, the rest verbatim
    assert blast["cascade_hops"] == LIVE_HOPS


@pytest.mark.parametrize("extra", [{}, {"cascade_edges": None, "cascade_hops": "nope"}])
def test_run_state_without_cascade_degrades_to_empty_lists(control, tmp_store, extra):
    tmp_store.write_live({"phase": "injecting", "experiment_id": "abc", "target_service": "things",
                          "health": {"things": {"alive": None, "ready": None, "status": "exited"}}, **extra})
    blast = control.state(0)["run"]["blast"]
    assert blast["cascade_edges"] == [] and blast["cascade_hops"] == []
    assert set(blast) == {"expected", "observed", "cascade_edges", "cascade_hops"}


def cascade_attribution() -> CascadeAttribution:
    hops = [
        CascadeHop(service="things", onset_earliest_s=0.0, onset_latest_s=0.0, onset_source="injection",
                   mechanism="injected_fault", mechanism_evidence="docker kill", path=["things"],
                   path_confidence="origin", hops_from_origin=0),
        CascadeHop(service="gateway", onset_earliest_s=0.0, onset_latest_s=3.1, onset_source="api_probe",
                   mechanism="dependency_timeout", mechanism_evidence="12/14 requests timed out",
                   masked_by_health_check=True, upstream="things", edge_kinds=["env"], lag_s=3.1,
                   path=["gateway", "things"],
                   candidate_paths=[["gateway", "things"], ["gateway", "things-search", "things"]],
                   path_confidence="ambiguous", hops_from_origin=1),
        CascadeHop(service="connectivity", onset_earliest_s=6.0, onset_latest_s=6.0, onset_source="health_probe",
                   mechanism="readiness_failed", mechanism_evidence="alive=200 ready=503",
                   path_confidence="no_graph_path", beyond_hypothesis=True),
    ]
    return CascadeAttribution(
        origin="things", health_interval_s=3.0, window_start_s=0.0, window_end_s=30.0, hops=hops,
        edges=[CascadeEdge(src="gateway", dst="things", mechanism="dependency_timeout", confidence="ambiguous",
                           lag_s=3.1)],
        hops_beyond_hypothesis=1, beyond_hypothesis=["connectivity"], unattributed=["connectivity"],
        summary="things was killed; gateway timed out waiting on it.", notes=["health onsets are +/- 3.0 s"])


def test_experiment_summary_and_detail_carry_the_cascade(control, tmp_store, make_report):
    tmp_store.write_experiment(make_report(experiment_id="casc", service="things", cascade=cascade_attribution()))
    summary = control.state(0)["experiments"][0]
    assert summary["cascade_hops_beyond_hypothesis"] == 1 and summary["cascade_beyond"] == ["connectivity"]
    assert "cascade" not in summary          # the full attribution stays out of the 1 Hz poll
    cascade = control.experiment_detail("casc")["experiment"]["cascade"]
    assert cascade["origin"] == "things" and cascade["summary"].startswith("things was killed")
    assert [h["service"] for h in cascade["hops"]] == ["things", "gateway", "connectivity"]
    assert cascade["hops"][1]["masked_by_health_check"] is True
    assert cascade["edges"] == [{"src": "gateway", "dst": "things", "mechanism": "dependency_timeout",
                                 "confidence": "ambiguous", "lag_s": 3.1}]


def test_experiment_without_cascade_degrades_to_todays_payload(control, tmp_store, make_report):
    tmp_store.write_experiment(make_report(experiment_id="old"))
    summary = control.state(0)["experiments"][0]
    assert summary["cascade_hops_beyond_hypothesis"] is None and summary["cascade_beyond"] == []
    assert control.experiment_detail("old")["experiment"]["cascade"] is None


# ---------------------------------------------------------------- predicted weaknesses
def weakness_report(**overrides) -> WeaknessReport:
    """A minimal but complete WeaknessReport, built straight from the schema."""
    w = PredictedWeakness(
        weakness_id="w1", category="shallow_health_check", services=["things"],
        title="things reports ready while MongoDB is unreachable",
        evidence="healthcheck: curl -f http://localhost:7626/alive",
        predicted_severity=7.4, predicted_level="high",
        factors={"criticality": 0.9, "blast": 0.4},
        suggested_experiment=SuggestedExperiment(
            failure_type="container_crash", target_service="mongodb",
            reason="break what things depends on and watch /ready",
            policy_note="mongodb is protected; requires allow_protected"),
        confirming_finding_codes=["health_check_masked_failure"],
        why_it_matters="An orchestrator would keep routing traffic to a service that cannot serve it.")
    base = {"project": PROJECT, "compose_file": "deploy/ditto/docker-compose.yml",
            "knowledge_file": "config/knowledge.ditto.yaml", "weaknesses": [w],
            "systemic_patterns": [SystemicPattern(title="Liveness-only health checks everywhere",
                                                  weakness_ids=["w1"], narrative="Every service ...")],
            "checks_run": ["shallow_health_check", "no_redundancy"], "notes": ["compose read from disk"]}
    base.update(overrides)
    return WeaknessReport(**base)


def test_weaknesses_state_is_none_until_the_scout_writes_a_report(control, tmp_store):
    assert control._weaknesses_state() is None
    tmp_store.write_weaknesses(weakness_report())
    assert control._weaknesses_state() is not None


def test_weaknesses_state_matches_the_ui_contract(control, tmp_store):
    report = weakness_report()
    tmp_store.write_weaknesses(report)
    st = control.state(0)["weaknesses"]
    assert set(st) == {"generated_at", "project", "compose_file", "knowledge_file", "label",
                       "checks_run", "notes", "generated_by", "weaknesses", "systemic_patterns"}
    # the label is what stops the UI confusing a prediction with a measured finding: carried verbatim
    assert st["label"] == PREDICTED_LABEL == "[predicted, unconfirmed]"
    assert st["generated_at"] == report.generated_at.isoformat()  # ISO string, not a datetime
    assert st["project"] == PROJECT and st["generated_by"] == "rules"
    assert st["checks_run"] == ["shallow_health_check", "no_redundancy"]

    (w,) = st["weaknesses"]
    assert w["label"] == PREDICTED_LABEL and w["status"] == "predicted"
    assert w["predicted_level"] == "high" and w["predicted_severity"] == 7.4
    assert w["category"] == "shallow_health_check" and w["services"] == ["things"]
    assert w["suggested_experiment"]["failure_type"] == "container_crash"
    assert w["suggested_experiment"]["target_service"] == "mongodb"
    assert w["suggested_experiment"]["policy_note"]
    assert w["confirming_finding_codes"] == ["health_check_masked_failure"]
    assert w["evidence"] and w["why_it_matters"]
    # nested models are JSON-ready: the whole snapshot must survive json.dumps as-is
    json.dumps(st)

    (sp,) = st["systemic_patterns"]
    assert sp["generated_by"] == "llm" and sp["weakness_ids"] == ["w1"]


def test_scout_job_is_accepted_and_narrates_predictions(control, monkeypatch, tmp_store):
    seen = {}

    def fake_run(settings, store, llm, log=print):
        seen["store"] = store
        seen["llm_enabled"] = getattr(llm, "enabled", None)
        report = weakness_report()
        store.write_weaknesses(report)
        log("read 8 services from the compose file")
        return report

    monkeypatch.setattr(scout_mod, "run", fake_run)
    job = control.submit("scout", {"llm": True})
    assert job.kind == "scout" and job.label == "Scout for predicted weaknesses"
    done = wait_job(control)
    assert done["status"] == "done"
    assert done["result"] == {"weaknesses": 1, "patterns": 1, "generated_by": "rules",
                              "label": PREDICTED_LABEL}
    assert seen["store"] is tmp_store and seen["llm_enabled"] is False  # llm_enabled=False in Settings

    st = control.state(0)
    assert st["agents"]["scout"]["state"] == "done"
    assert st["agents"]["scout"]["detail"] == "1 predicted weaknesses"
    assert st["weaknesses"]["weaknesses"][0]["weakness_id"] == "w1"
    # every scout line says "predicted" and carries the label, so the feed cannot read as measured
    lines = [e for e in st["events"] if e["source"] == "scout"]
    assert lines and all(PREDICTED_LABEL in e["text"] for e in lines
                         if "Predicted" in e["text"] or "Systemic" in e["text"])
    assert any("Predicted high (7.4/10)" in e["text"] and e["level"] == "ai" for e in lines)
    assert not any(e["level"] in ("error", "chaos") for e in lines)


def test_scout_job_failure_is_reported_like_any_other_job(control, monkeypatch):
    def boom(settings, store, llm, log=print):
        raise FileNotFoundError("no topology.json and no compose file")

    monkeypatch.setattr(scout_mod, "run", boom)
    control.submit("scout", {})
    job = wait_job(control)
    assert job["status"] == "failed" and "FileNotFoundError" in job["error"]
    assert control.state(0)["weaknesses"] is None


# ---------------------------------------------------------------- reconcile
def test_reconcile_job_closes_the_loop_and_exposes_state(control, tmp_store, make_report):
    assert "reconcile" in control_mod.JOB_KINDS and control_mod.AGENTS[-1] == "reconcile"
    assert control.state(0)["reconciliation"] is None
    tmp_store.write_weaknesses(weakness_report())
    tmp_store.write_experiment(make_report(service="things", finding_codes=["health_check_masked_failure"]))
    job = control.submit("reconcile", {})
    assert job.label == "Reconcile predictions against experiments"
    done = wait_job(control)
    assert done["status"] == "done"
    assert done["result"] == {"predicted": 1, "confirmed": 1, "cleared": 0, "untested": 0,
                              "experiments_considered": 1}
    st = control.state(0)
    assert st["agents"]["reconcile"] == {**st["agents"]["reconcile"], "state": "done", "detail": "1/1 confirmed"}
    rec = st["reconciliation"]
    assert {"generated_at", "project", "predicted_total", "confirmed", "cleared", "untested",
            "experiments_considered", "outcomes", "summary"} <= set(rec)
    assert rec["confirmed"] == 1 and rec["outcomes"][0]["state"] == "confirmed"
    assert rec["outcomes"][0]["weakness_id"] == "w1" and isinstance(rec["generated_at"], str)
    json.dumps(st)
    lines = [e for e in st["events"] if e["source"] == "reconcile"]
    assert any("1 confirmed" in e["text"] and e["level"] == "success" for e in lines)


def test_reconcile_job_without_a_scout_report_fails_clearly(control):
    control.submit("reconcile", {})
    job = wait_job(control)
    assert job["status"] == "failed" and "scout" in job["error"]
    assert control.state(0)["agents"]["reconcile"]["state"] == "error"
    assert control.state(0)["reconciliation"] is None


def test_analyze_reconciles_after_every_run_and_survives_a_reconcile_error(control, tmp_store, make_report,
                                                                            topology, monkeypatch):
    import faultline.agents.reconcile as reconcile_mod

    tmp_store.write_topology(topology)
    tmp_store.write_experiment(make_report(service="things", finding_codes=["health_check_masked_failure"]))
    control.submit("analyze", {"llm": False})
    assert wait_job(control)["status"] == "done"
    assert control.state(0)["reconciliation"] is None  # no predictions: nothing to reconcile
    tmp_store.write_weaknesses(weakness_report())
    control.submit("analyze", {"llm": False})
    assert wait_job(control)["status"] == "done"
    assert control.state(0)["reconciliation"]["confirmed"] == 1

    def boom(store, project, log=print):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(reconcile_mod, "run", boom)
    control.submit("analyze", {"llm": False})
    job = wait_job(control)
    assert job["status"] == "done" and job["result"]["risks"] == 0  # already scored by the second pass
    assert control.state(0)["agents"]["reconcile"]["state"] == "error"
    assert any("Reconcile skipped" in e["text"] and e["level"] == "warn" for e in control.state(0)["events"])


# ---------------------------------------------------------------- planner + baseline agent entries
def test_control_room_planner_is_weakness_aware(control, tmp_store, topology):
    tmp_store.write_topology(topology)
    assert control._planner(False, False, arm=False).weaknesses is None
    tmp_store.write_weaknesses(weakness_report())
    planner = control._planner(False, False, arm=False)
    assert planner.weaknesses is not None and planner.weaknesses.weaknesses[0].weakness_id == "w1"


def test_baseline_job_drives_its_own_agent_entry(control, monkeypatch, make_report, thresholds):
    import faultline.observe.baseline as baseline_mod
    from faultline.schemas import Baseline, WindowStats, utcnow

    assert "baseline" in control_mod.AGENTS and control.state(0)["agents"]["baseline"]["state"] == "idle"

    def fake_capture(api, health, window, duration, url, policy, log=print):
        assert duration == 20
        return Baseline(captured_at=utcnow(), duration_s=duration, ditto_url=url,
                        api=WindowStats(sample_count=80, success_rate=1.0, p50_ms=20, p95_ms=44, p99_ms=90),
                        health_availability={}, thresholds=thresholds)

    monkeypatch.setattr(baseline_mod, "capture", fake_capture)
    control.submit("baseline", {"duration": 20})
    assert wait_job(control)["status"] == "done"
    agent = control.state(0)["agents"]["baseline"]
    assert agent["state"] == "done" and agent["detail"] == "success 100.00%, p95 44.0 ms"

    def boom(*a, **kw):
        raise ConnectionError("probe lost")

    monkeypatch.setattr(baseline_mod, "capture", boom)
    control.submit("baseline", {"duration": 20})
    assert wait_job(control)["status"] == "failed"
    assert control.state(0)["agents"]["baseline"]["state"] == "error"


# ---------------------------------------------------------------- narrator
def test_narrative_fills_missing_risk_and_fix_in_memory(control, tmp_store, make_report):
    tmp_store.write_experiment(make_report(experiment_id="abc", finding_codes=["health_check_masked_failure"]))
    story = control.narrative("abc")
    assert set(story) == {"header", "timeline", "impact", "cascade", "recovery", "hypothesis", "safety",
                          "findings", "risk", "fix", "error", "llm"}
    assert story["cascade"] == []  # no attribution on this report
    assert story["header"].startswith("[") and "Experiment abc" in story["header"]
    assert story["timeline"] == ["[+0.0s] inject — container_crash"]
    assert story["impact"].startswith("Impact: success 100% → 10%")
    assert story["recovery"].startswith("Recovery: 14 s")
    assert story["hypothesis"].startswith("Hypothesis: held")
    assert story["findings"] == ["Finding [HIGH] policies health check masked failure (health_check_masked_failure)"]
    assert story["risk"].startswith("Risk: ") and "not yet assessed" not in story["risk"]
    assert story["fix"].startswith("Top fix: ") and "not yet available" not in story["fix"]
    assert story["safety"] is None and story["llm"] is None and story["error"] is None
    assert tmp_store.risk("abc") is None and tmp_store.fix("abc") is None  # computed, never written
    assert control.narrative("missing") is None
    json.dumps(story)


def test_run_job_pushes_the_play_by_play_into_the_feed(control, injector):
    injector()
    control.submit("run", {"mode": "auto", "analyze": False, "llm": False})
    assert wait_job(control)["status"] == "done"
    lines = [e for e in control.state(0)["events"] if e["source"] == "narrator"]
    texts = [e["text"] for e in lines]
    assert texts[0].startswith("[") and "Experiment" in texts[0]
    assert any(t.startswith("Impact:") for t in texts) and any(t.startswith("Hypothesis: held") for t in texts)
    assert not any(t.startswith(" ") for t in texts)
    assert all(e["level"] == infer_level(e["text"], "narrator") for e in lines)


# ---------------------------------------------------------------- target
def test_adopt_target_accepts_repo_relative_paths_and_disarms(control, sessions):
    control.arm()
    seq = control.events.seq
    out = control.adopt_target({"compose": "deploy/docker-compose.yml", "knowledge": None})
    assert out == {"compose_file": str(ROOT / "deploy" / "docker-compose.yml"),
                   "knowledge_file": control.s.knowledge_file, "project": PROJECT}
    assert control.state(0)["target"] == out
    assert sessions[0].closed and control.session is None
    assert control.state(0)["agents"]["topology"]["state"] == "idle"
    assert any(e["source"] == "system" and "Target adopted" in e["text"] and e["seq"] > seq
               for e in control.state(0)["events"])
    out = control.adopt_target({"knowledge": "config/ditto_knowledge.yaml"})
    assert out["knowledge_file"] == str(ROOT / "config" / "ditto_knowledge.yaml")


def test_adopt_target_rejects_outside_paths_and_missing_files(control, tmp_path):
    before = control.state(0)["target"]
    secret = tmp_path / "docker-compose.yml"
    secret.write_text("services: {}")
    with pytest.raises(ValueError, match="must live under"):
        control.adopt_target({"compose": str(secret)})
    with pytest.raises(ValueError, match="not found"):
        control.adopt_target({"compose": "deploy/nope.yml"})
    with pytest.raises(ValueError, match="must live under"):
        control.adopt_target({"knowledge": "../" * 6 + "etc/hosts"})
    # inside the repo is not enough: a YAML error on a file like .env would echo its contents
    with pytest.raises(ValueError, match="must live under"):
        control.adopt_target({"compose": "README.md"})
    with pytest.raises(ValueError, match="nothing to adopt"):
        control.adopt_target({})
    assert control.state(0)["target"] == before


def test_topology_job_honours_compose_knowledge_and_live(control, monkeypatch, topology):
    seen = []

    def fake_build(s, store, live, knowledge=None, compose=None):
        seen.append({"live": live, "knowledge": knowledge, "compose": compose})
        return topology

    monkeypatch.setattr(cli, "build_topology", fake_build)
    control.submit("topology", {})
    assert wait_job(control)["status"] == "done"
    assert seen[-1] == {"live": True, "knowledge": None, "compose": None}
    control.s.compose_file = str(ROOT / "deploy" / "configured-elsewhere.yml")
    control.submit("topology", {"compose": "deploy/docker-compose.yml", "knowledge": "config/ditto_knowledge.yaml"})
    assert wait_job(control)["status"] == "done"
    # a compose file other than the configured one defaults to a static read: no docker to inspect
    assert seen[-1] == {"live": False, "compose": str(ROOT / "deploy" / "docker-compose.yml"),
                        "knowledge": str(ROOT / "config" / "ditto_knowledge.yaml")}
    control.submit("topology", {"compose": "deploy/docker-compose.yml", "live": True})
    assert wait_job(control)["status"] == "done" and seen[-1]["live"] is True
    control.submit("topology", {"compose": "/etc/hosts"})
    assert wait_job(control)["status"] == "failed" and len(seen) == 3


# ---------------------------------------------------------------- jobs
def test_second_job_while_one_runs_is_refused(control, monkeypatch):
    release = block_handler(control, "_job_topology", monkeypatch)
    first = control.submit("topology", {})
    assert first.id == "j1" and first.status == "running" and first.label == "Rebuild application map"
    try:
        for kind in ("baseline", "run", "analyze", "heal", "topology"):
            with pytest.raises(JobBusy, match="already running"):
                control.submit(kind, {})
        assert control.job is first
    finally:
        release.set()
    assert wait_job(control)["status"] == "done"
    assert control.job.result == {"blocked": True}
    monkeypatch.setattr(control, "_job_heal", lambda body: {"rc": 0})
    second = control.submit("heal", {})
    assert second.id == "j2"
    assert wait_job(control)["status"] == "done"


def test_failed_job_records_error_and_frees_the_slot(control, monkeypatch):
    def boom(body):
        raise RuntimeError("compose file missing")

    monkeypatch.setattr(control, "_job_topology", boom)
    control.submit("topology", {})
    job = wait_job(control)
    assert job["status"] == "failed" and job["error"] == "RuntimeError: compose file missing"
    errors = [e for e in control.state(0)["events"] if e["level"] == "error"]
    assert errors and "traceback" in errors[-1]["data"]
    monkeypatch.setattr(control, "_job_topology", lambda body: {})
    control.submit("topology", {})
    assert wait_job(control)["status"] == "done"


def test_cancel_non_run_job_marks_it_cancelled(control, monkeypatch, tmp_store):
    assert control.cancel() == {"ok": True, "cancelled": False}
    release = block_handler(control, "_job_baseline", monkeypatch)
    control.submit("baseline", {})
    assert control.cancel() == {"ok": True, "cancelled": True}
    release.set()
    assert wait_job(control)["status"] == "cancelled"
    assert not tmp_store.stop_requested()  # only cancelling a run trips the kill switch


def test_cancel_run_job_trips_stop(control, monkeypatch, tmp_store):
    release = block_handler(control, "_job_run", monkeypatch)
    control.submit("run", {"mode": "auto"})
    try:
        assert control.cancel()["cancelled"] is True
        assert tmp_store.stop_requested()
    finally:
        release.set()
    wait_job(control)


@pytest.mark.parametrize("body,label", [
    ({"mode": "manual", "target": "things", "failure": "network_delay"}, "network_delay on things"),
    ({"mode": "auto"}, "AI-planned experiment"),
    ({"mode": "auto", "experiments": 3}, "AI-planned experiments (x3)"),
    ({"mode": "manual"}, "AI-planned experiment"),  # manual without a target falls back to the planner
])
def test_run_job_labels(control, body, label):
    assert control._label("run", body) == label


def test_heal_job_passes_restart_and_reseeds_canary(control, monkeypatch, sessions):
    seen = {}

    def fake_heal(s, store, a):
        seen["restart"] = a.restart
        return 0

    monkeypatch.setattr(cli, "cmd_heal", fake_heal)
    control.arm()
    control.submit("heal", {"restart": True})
    job = wait_job(control)
    assert job["status"] == "done" and job["result"] == {"rc": 0}
    assert seen == {"restart": True} and sessions[0].api.canary_calls == 1


# ---------------------------------------------------------------- kill switch
def test_stop_and_resume_write_and_clear_stop_file(control, tmp_store):
    assert not tmp_store.stop_file.exists()
    assert control.stop() == {"ok": True, "stop_requested": True}
    assert tmp_store.stop_file.exists() and tmp_store.stop_file.read_text().startswith("dashboard ")
    st = control.state(0)
    assert st["stop_requested"] is True
    assert st["events"][-1]["level"] == "error" and "KILL SWITCH" in st["events"][-1]["text"]
    assert control.resume() == {"ok": True, "stop_requested": False}
    assert not tmp_store.stop_file.exists() and control.state(0)["stop_requested"] is False
    control.resume()  # clearing an absent flag is a no-op, not an error


def test_stop_is_never_blocked_by_a_running_job(control, monkeypatch, tmp_store):
    release = block_handler(control, "_job_baseline", monkeypatch)
    control.submit("baseline", {})
    try:
        assert control.stop()["stop_requested"] is True
        assert tmp_store.stop_requested() and control.job.status == "running"
    finally:
        release.set()
    wait_job(control)


class FakeInjector:
    """Stands in for agents.injector.Injector: records each spec and returns a synthetic report."""
    instances: list = []
    on_run = None
    report_factory = None

    def __init__(self, settings, store, topology, docker, window, api, health, thresholds, policy, log=None):
        self.calls: list = []
        FakeInjector.instances.append(self)

    def run(self, spec):
        self.calls.append(spec)
        if FakeInjector.on_run:
            FakeInjector.on_run()
        return FakeInjector.report_factory(experiment_id=spec.experiment_id, service=spec.target_service,
                                           failure_type=spec.failure_type)


@pytest.fixture
def injector(monkeypatch, make_report):
    import faultline.agents.injector as injector_mod

    monkeypatch.setattr(injector_mod, "Injector", FakeInjector)
    monkeypatch.setattr(FakeInjector, "instances", [])
    monkeypatch.setattr(FakeInjector, "report_factory", make_report)

    def configure(on_run=None):
        monkeypatch.setattr(FakeInjector, "on_run", on_run)

    return configure


def test_run_job_honours_latched_stop(control, injector, tmp_store):
    injector()
    control.stop()
    control.submit("run", {"mode": "auto", "experiments": 3, "resume_stop": False, "analyze": False, "llm": False})
    job = wait_job(control)
    assert job["status"] == "done" and job["result"] == {"experiments": []}
    assert FakeInjector.instances and FakeInjector.instances[0].calls == []
    assert tmp_store.stop_requested()
    assert any("STOP is latched" in e["text"] for e in control.state(0)["events"])


def test_stop_mid_run_prevents_the_next_experiment(control, injector, tmp_store):
    injector(on_run=control.stop)  # operator hits STOP while the first experiment is in flight
    control.submit("run", {"mode": "auto", "experiments": 3, "analyze": False, "llm": False})
    job = wait_job(control)
    calls = FakeInjector.instances[0].calls
    assert len(calls) == 1 and job["result"] == {"experiments": [calls[0].experiment_id]}
    assert calls[0].planned_by == "deterministic"
    assert tmp_store.stop_requested()


def test_manual_run_uses_operator_plan_and_clears_stale_stop(control, injector, tmp_store, sessions):
    injector()
    tmp_store.request_stop("old")
    control.submit("run", {"mode": "manual", "target": "things", "failure": "container_crash",
                           "params": {"duration_s": 15}, "analyze": False, "llm": False})
    job = wait_job(control)
    spec = FakeInjector.instances[0].calls[0]
    assert job["status"] == "done" and spec.planned_by == "operator"
    assert spec.target_service == "things" and spec.parameters["duration_s"] == 15
    # resume_stop defaults to true: launching a run is treated as an explicit resume
    assert not tmp_store.stop_requested()
    assert len(sessions) == 1 and control.state(0)["session"]["armed"] is True


# ---------------------------------------------------------------- approval gate (scenario A)
def make_approval_request(**kw) -> ApprovalRequest:
    fields = dict(approval_id="7f3c1a9b4d02", scenario="pre_injection", experiment_id="exp0001",
                  token="FL-1a2b3c4d", to="admin@example.test", target_service="mongodb",
                  failure_type="container_crash", created_at=utcnow(),
                  deadline=utcnow() + timedelta(seconds=90), timeout_s=90.0, default_on_timeout="skip",
                  options=list(PRE_INJECTION_REPLIES), sent=True)
    fields.update(kw)
    return ApprovalRequest(**fields)


class HeldPlanner:
    """Wraps the real planner so every spec it hands back needs an admin decision.

    The planner does not set `requires_approval` from the policy yet; the run loop's gate is what
    these tests exercise, so the flag is set here rather than waited on.
    """

    def __init__(self, inner):
        self._inner = inner
        self.history = inner.history

    @staticmethod
    def _held(spec):
        if spec is not None:
            spec.requires_approval = True
            spec.approval_reason = "mongodb is protected and the policy requires an explicit decision"
        return spec

    def plan_next(self):
        return self._held(self._inner.plan_next())

    def plan_operator(self, *a, **kw):
        return self._held(self._inner.plan_operator(*a, **kw))


class FakeApprovalAgent:
    """Stands in for agents.approval.ApprovalAgent: the closed reply vocabulary, no SMTP, no IMAP.

    Only the mail transport is faked — `approval.gate` itself still runs, so the spec mutation on
    APPROVE REDUCED and the STOP file on STOP RUN are the production code paths.
    """
    reply = "approve"
    asked: list = []

    def __init__(self, settings, store, log=print, mailer=None, mailbox=None):
        assert mailer is None and mailbox is None, "the gate must not build a live transport in tests"
        self.s, self.store, self.log = settings, store, log

    def request_pre_injection(self, spec, reason: str = "") -> ApprovalDecision:
        FakeApprovalAgent.asked.append(spec)
        decision = ApprovalDecision(answer=self.reply, answered=True, raw_first_line=self.reply.upper(),
                                    reason=f"the admin replied {self.reply.replace('_', ' ').upper()!r}")
        if self.reply == "approve_reduced":
            decision.parameters_before = dict(spec.parameters)
            decision.parameters_after, decision.parameter_notes = approval_mod.halve(spec.failure_type,
                                                                                     spec.parameters)
        elif self.reply == "stop_run":
            self.store.request_stop(f"approval:{spec.experiment_id}")
        return decision


@pytest.fixture
def approvals(control, monkeypatch):
    """Route every planned spec through the real gate and answer it without touching a mailbox."""
    plan = control._planner
    monkeypatch.setattr(control, "_planner", lambda *a, **kw: HeldPlanner(plan(*a, **kw)))
    monkeypatch.setattr(approval_mod, "ApprovalAgent", FakeApprovalAgent)
    monkeypatch.setattr(FakeApprovalAgent, "asked", [])

    def configure(reply: str) -> list:
        monkeypatch.setattr(FakeApprovalAgent, "reply", reply)
        return FakeApprovalAgent.asked

    return configure


def test_pending_approval_surfaces_in_state_and_degrades_to_none(control, tmp_store):
    tmp_store.set_pending_approval(make_approval_request())
    pending = control.state(0)["pending_approval"]
    assert pending["approval_id"] == "7f3c1a9b4d02" and pending["scenario"] == "pre_injection"
    assert pending["token"] == "FL-1a2b3c4d" and pending["status"] == "pending"
    assert pending["default_on_timeout"] == "skip" and pending["decision"] is None
    assert 0 < pending["seconds_left"] <= 90
    tmp_store.pending_approval_file.write_text("{ not json")  # a half-written file must not break /api/state
    assert control.state(0)["pending_approval"] is None
    tmp_store.clear_pending_approval()
    assert control.state(0)["pending_approval"] is None


def test_gate_skip_never_reaches_the_injector(control, injector, approvals, tmp_store):
    injector()
    asked = approvals("skip")
    control.submit("run", {"mode": "auto", "experiments": 1, "analyze": False, "llm": False})
    job = wait_job(control)
    assert job["status"] == "done" and job["result"] == {"experiments": []}
    assert len(asked) == 1 and asked[0].requires_approval is True
    assert FakeInjector.instances[0].calls == []
    assert not tmp_store.stop_requested()  # SKIP rejects one candidate, it does not end the run
    st = control.state(0)
    assert st["run"]["phase"] == "idle"  # a spec that was never injected must not linger as preflight
    assert any(e["source"] == "approval" and "Not injecting" in e["text"] for e in st["events"])


def test_gate_approve_lets_the_experiment_run(control, injector, approvals):
    injector()
    asked = approvals("approve")
    control.submit("run", {"mode": "auto", "experiments": 1, "analyze": False, "llm": False})
    job = wait_job(control)
    calls = FakeInjector.instances[0].calls
    assert len(asked) == 1 and len(calls) == 1 and calls[0] is asked[0]
    assert job["result"] == {"experiments": [calls[0].experiment_id]}
    assert any("admin approved this experiment" in n for n in calls[0].planner_notes)


def test_gate_stop_run_ends_the_loop_without_injecting(control, injector, approvals, tmp_store):
    injector()
    asked = approvals("stop_run")
    control.submit("run", {"mode": "auto", "experiments": 3, "analyze": False, "llm": False})
    job = wait_job(control)
    assert len(asked) == 1  # asked once; the loop broke instead of planning a second experiment
    assert FakeInjector.instances[0].calls == [] and job["result"] == {"experiments": []}
    assert tmp_store.stop_requested()
    st = control.state(0)
    assert st["run"]["phase"] == "idle"
    assert any(e["level"] == "error" and "Run aborted" in e["text"] for e in st["events"])


def test_gate_approve_reduced_injects_the_halved_spec(control, injector, approvals):
    live: dict = {}
    injector(on_run=lambda: live.update(control.state(0)["run"]))
    approvals("approve_reduced")
    control.submit("run", {"mode": "manual", "target": "things", "failure": "container_crash",
                           "params": {"duration_s": 40}, "analyze": False, "llm": False})
    assert wait_job(control)["status"] == "done"
    spec = FakeInjector.instances[0].calls[0]
    assert spec.parameters["duration_s"] == 20
    # the launcher was showing 40 s when the gate opened; it must show what the admin approved
    assert live["parameters"]["duration_s"] == 20 and live["max_s"] == 20
    assert any("halved 40 -> 20" in n for n in spec.planner_notes)


def test_the_kill_switch_is_served_while_the_gate_holds_the_job_slot(control, injector, approvals,
                                                                     monkeypatch, tmp_store):
    """An open decision blocks the single job slot. STOP, cancel and /api/state must not queue behind it."""

    class BlockingAgent(FakeApprovalAgent):
        def request_pre_injection(self, spec, reason: str = "") -> ApprovalDecision:
            FakeApprovalAgent.asked.append(spec)
            assert wait_for(self.store.stop_requested, 5.0), "STOP never reached the waiting gate"
            # what approval._wait returns when the STOP file appears: the safe default, not consent
            return ApprovalDecision(answer="skip", answered=False,
                                    reason="STOP was latched while waiting for the decision")

    injector()
    approvals("approve")  # never consulted: BlockingAgent answers instead
    monkeypatch.setattr(approval_mod, "ApprovalAgent", BlockingAgent)
    control.submit("run", {"mode": "auto", "experiments": 2, "analyze": False, "llm": False})
    assert wait_for(lambda: bool(FakeApprovalAgent.asked), 5.0), "the gate never opened"
    assert control.state(0)["job"]["status"] == "running"  # the slot is held by the open decision
    assert control.stop() == {"ok": True, "stop_requested": True}
    job = wait_job(control)
    assert job["result"] == {"experiments": []} and FakeInjector.instances[0].calls == []
    assert len(FakeApprovalAgent.asked) == 1  # the latched STOP stopped a second request being opened


# ---------------------------------------------------------------- read-only previews
def test_plan_is_read_only_on_a_cold_controller(control, tmp_store, topology, sessions):
    tmp_store.write_topology(topology)
    out = control.plan(use_llm=True, allow_protected=False)  # llm requested but disabled in settings
    assert out["candidates"] and out["pick"] is not None
    assert out["pick"]["planned_by"] == "deterministic" and out["llm"]["used"] is False
    assert all(c["service"] != "mongodb" or c["failure_type"] in
               ("network_delay", "network_loss", "cpu_stress", "disk_pressure", "db_flood") for c in out["candidates"])
    assert sessions == [] and control.session is None
    assert control.state(0)["session"]["armed"] is False
    assert not tmp_store.stop_requested() and tmp_store.live() is None


def test_plan_without_a_map_raises_and_still_does_not_arm(control, sessions):
    with pytest.raises(RuntimeError, match="no application map"):
        control.plan()
    assert sessions == [] and control.session is None


def test_plan_narrates_only_when_the_pick_changes(control, tmp_store, topology):
    tmp_store.write_topology(topology)
    control.plan()
    seq = control.events.seq
    control.plan()
    control.plan()
    assert control.events.seq == seq


def test_catalog_is_read_only_on_a_cold_controller(control, tmp_store, topology, sessions):
    tmp_store.write_topology(topology)
    cat = control.catalog()
    assert len(cat["failures"]) == 10 and "mongodb" in cat["protected"]
    crash = next(f for f in cat["failures"] if f["key"] == "container_crash")
    assert "mongodb" in crash["policy_blocked_on"] and "things" in crash["applies_to"]
    assert sessions == [] and control.session is None


# ---------------------------------------------------------------- session
def test_arm_is_idempotent_and_disarm_closes(control, sessions):
    assert control.arm() == {"armed": True}
    assert control.arm() == {"armed": True}
    assert len(sessions) == 1
    st = control.state(0)
    assert st["session"]["armed"] is True and st["session"]["probe"]["running"] is True
    assert control.disarm() == {"armed": False}
    assert sessions[0].closed and control.session is None
    assert control.disarm() == {"armed": False}


def test_arm_failure_is_reported_not_swallowed(control, monkeypatch):
    class Broken:
        def __init__(self, *a, **kw):
            raise ConnectionError("docker socket not found")

    monkeypatch.setattr(cli, "Session", Broken)
    with pytest.raises(ConnectionError):
        control.arm()
    st = control.state(0)
    assert st["session"]["armed"] is False
    assert st["session"]["error"] == "ConnectionError: docker socket not found"
    assert st["agents"]["topology"]["state"] == "error"


def test_concurrent_arm_builds_one_session(settings, tmp_store, monkeypatch, topology, policy_raw):
    built: list = []
    monkeypatch.setattr(cli, "Session", make_session_cls(topology, policy_raw, built, delay=0.3))
    c = ControlPlane(settings, tmp_store)
    try:
        threads = [threading.Thread(target=c.arm) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert len(built) == 1
    finally:
        c.shutdown()


# ---------------------------------------------------------------- telemetry
@pytest.fixture
def clock(monkeypatch):
    now = [1_760_000_000.0]
    monkeypatch.setattr(control_mod, "time", SimpleNamespace(time=lambda: now[0]))
    return now


def test_telemetry_buffer_is_bounded_and_serves_only_the_window(clock):
    tel, window = Telemetry(), SampleWindow()
    for _ in range(1000):  # 1000 s at 1 Hz
        tel.sample(window, "idle", None)
        clock[0] += TELEMETRY_SAMPLE_S
    clock[0] -= TELEMETRY_SAMPLE_S
    assert len(tel.points) == tel.points.maxlen
    served = tel.as_dict()["points"]
    assert len(served) <= TELEMETRY_WINDOW_S / TELEMETRY_SAMPLE_S + 1
    assert served[-1]["t"] == round(clock[0], 2)
    assert all(clock[0] - p["t"] <= TELEMETRY_WINDOW_S for p in served)
    assert set(served[0]) == {"t", "success", "p95", "n"}


def test_telemetry_burst_cannot_grow_the_buffer_past_its_cap(clock):
    tel = Telemetry()
    for _ in range(5000):
        tel.sample(SampleWindow(), "idle", None)
    assert len(tel.points) == tel.points.maxlen == int(TELEMETRY_WINDOW_S / TELEMETRY_SAMPLE_S) + 60


def test_telemetry_phase_bands_open_and_close(clock):
    tel, window = Telemetry(), SampleWindow()
    for phase in ("idle", "injecting", "injecting", "rollback", "idle", "idle"):
        tel.sample(window, phase, None if phase == "idle" else "exp1")
        clock[0] += 1
    phases = tel.as_dict()["phases"]
    assert [p["phase"] for p in phases] == ["injecting", "rollback"]
    assert phases[0]["to"] == phases[1]["from"] and phases[1]["to"] is not None
    assert all(p["experiment_id"] == "exp1" for p in phases)


def test_sampler_only_samples_once_armed(settings, tmp_store, sessions, monkeypatch):
    monkeypatch.setattr(control_mod, "TELEMETRY_SAMPLE_S", 0.02)
    c = ControlPlane(settings, tmp_store)
    try:
        time.sleep(0.15)
        assert c.state(0)["telemetry"]["points"] == []
        c.arm()
        assert wait_for(lambda: len(c.state(0)["telemetry"]["points"]) >= 3, 2.0)
    finally:
        c.shutdown()


# ---------------------------------------------------------------- fix proposals ("Can I fix this?")
@pytest.fixture
def fake_apply(monkeypatch):
    """Stands in for remediation.apply.apply_and_verify: approved -> applying -> applied -> verified."""
    import faultline.remediation.apply as apply_mod
    from faultline.remediation import proposals as P
    from faultline.schemas import FixVerification, MetricComparison

    calls: list[dict] = []
    # the room's Deps builder arms the session and may warm the probes up; that is its own test below
    monkeypatch.setattr(control_mod.ControlPlane, "_fix_deps",
                        lambda self, allow_protected, use_llm: ("deps", allow_protected, use_llm))

    def _apply(settings, store, proposal_id, allow_protected=False, log=print, deps=None, **kw):
        assert deps is not None and deps[0] == "deps"     # never live_deps: no second Session
        calls.append({"id": proposal_id, "allow_protected": allow_protected})
        p = store.proposal(proposal_id)
        if p.status != "approved":
            P.note(p, "faultline", f"apply refused: proposal is {p.status}, not approved")
            store.write_proposal(p)
            return p
        for to in ("applying", "applied", "verified"):
            P.transition(p, to, "faultline")
        p.verification = FixVerification(rerun_experiment_id="exp-rerun", targeted_metric="time_to_recover_s",
                                         outcome="verified", reason="recovered faster",
                                         comparisons=[MetricComparison(metric="time_to_recover_s", before=40.0,
                                                                       after=12.0, better_is="lower",
                                                                       verdict="better")])
        store.write_proposal(p)
        log("applied and verified")
        return p

    monkeypatch.setattr(apply_mod, "apply_and_verify", _apply)
    return calls


def test_state_carries_proposals_summary_and_degrades(control, tmp_store):
    assert control.state(0)["proposals"] == {"open": 0, "pending": 0, "total": 0, "latest": []}
    tmp_store.write_proposal(make_proposal("fx-old", created_ago=60, status="rejected", decided_by="dashboard"))
    tmp_store.write_proposal(make_proposal("fx-new"))
    tmp_store.write_proposal(make_proposal("fx-overdue", expires_in=-5, created_ago=30))
    st = control.state(0)["proposals"]
    assert st["open"] == 1 and st["pending"] == 1 and st["total"] == 3   # overdue is not "open"
    assert [p["id"] for p in st["latest"]] == ["fx-new", "fx-overdue", "fx-old"]
    assert set(st["latest"][0]) == PROPOSAL_SUMMARY_KEYS
    assert st["latest"][0]["change_preview"] == PREVIEW and st["latest"][0]["seconds_left"] > 500
    assert st["latest"][1]["seconds_left"] < 0
    # the poll path never writes: the overdue record is still pending on disk
    assert tmp_store.proposal("fx-overdue").status == "pending"
    (tmp_store.root / "proposals" / "fx-bad.json").write_text('{"proposal_id": "fx-bad"}')
    assert control.state(0)["proposals"]["latest"] == []          # unreadable record: degrades, never 500s


def test_fix_job_is_not_a_generic_job_and_needs_an_approved_proposal(control, tmp_store, fake_apply):
    from faultline.remediation import proposals as P

    assert "fix" in control_mod.JOB_KINDS and "fix" not in control_mod.GENERIC_JOB_KINDS
    tmp_store.write_proposal(make_proposal())
    with pytest.raises(P.IllegalTransition, match="only an approved proposal"):
        control.submit_fix("fx-0123456789")
    with pytest.raises(KeyError):
        control.submit_fix("fx-nope")
    assert control.job is None and fake_apply == []
    P.approve(tmp_store, "fx-0123456789", P.change_hash(PREVIEW), "test")
    control.submit_fix("fx-0123456789", allow_protected=True)
    job = wait_job(control)
    assert job["kind"] == "fix" and job["status"] == "done" and "fx-0123456789" in job["label"]
    assert job["result"]["status"] == "verified" and job["result"]["verification"]["outcome"] == "verified"
    assert fake_apply == [{"id": "fx-0123456789", "allow_protected": True}]
    assert tmp_store.proposal("fx-0123456789").status == "verified"
    assert control.state(0)["agents"]["fix"]["state"] == "done"


def test_fix_job_surfaces_an_apply_refusal_as_a_failed_job(control, tmp_store, monkeypatch):
    import faultline.remediation.apply as apply_mod
    from faultline.remediation import proposals as P

    def refuse(settings, store, proposal_id, allow_protected=False, log=print, **kw):
        p = store.proposal(proposal_id)
        P.note(p, "faultline", "apply refused: this fix touches protected service(s) mongodb")
        store.write_proposal(p)
        return p

    monkeypatch.setattr(apply_mod, "apply_and_verify", refuse)
    monkeypatch.setattr(control, "_fix_deps", lambda allow_protected, use_llm: None)
    tmp_store.write_proposal(make_proposal(requires_allow_protected=True))
    P.approve(tmp_store, "fx-0123456789", P.change_hash(PREVIEW), "test")
    control.submit_fix("fx-0123456789")
    job = wait_job(control)
    assert job["status"] == "failed" and "protected service(s) mongodb" in job["error"]
    assert tmp_store.proposal("fx-0123456789").status == "approved"   # a refusal never changes status


def test_fix_job_refuses_while_stop_is_latched(control, tmp_store, fake_apply):
    from faultline.remediation import proposals as P

    tmp_store.write_proposal(make_proposal())
    P.approve(tmp_store, "fx-0123456789", P.change_hash(PREVIEW), "test")
    tmp_store.request_stop("test")
    control.submit_fix("fx-0123456789")
    job = wait_job(control)
    assert job["status"] == "failed" and "STOP" in job["error"] and fake_apply == []


def test_approve_starts_the_apply_or_says_the_slot_is_busy(control, tmp_store, fake_apply, monkeypatch):
    from faultline.remediation import proposals as P

    tmp_store.write_proposal(make_proposal("fx-a"))
    tmp_store.write_proposal(make_proposal("fx-b"))
    release = block_handler(control, "_job_topology", monkeypatch)
    control.submit("topology", {})
    try:
        out = control.approve_proposal("fx-a", P.change_hash(PREVIEW), "looks right")
        assert out["proposal"]["status"] == "approved" and out["proposal"]["decided_by"] == "dashboard"
        assert out["apply"]["started"] is False and "already running" in out["apply"]["reason"]
        assert "/api/proposals/fx-a/apply" in out["apply"]["reason"]
    finally:
        release.set()
    wait_job(control)
    assert tmp_store.proposal("fx-a").status == "approved" and fake_apply == []
    out = control.approve_proposal("fx-b", P.change_hash(PREVIEW))
    assert out["apply"]["started"] is True and out["apply"]["job"]["kind"] == "fix"
    assert wait_job(control)["status"] == "done" and tmp_store.proposal("fx-b").status == "verified"
    with pytest.raises(P.IllegalTransition):
        control.reject_proposal("fx-b")
    assert any("approved by dashboard" in e["text"] for e in control.state(0)["events"])


def test_analyze_builds_proposals_for_findings_above_info_and_never_fails(control, tmp_store, make_report,
                                                                          topology, monkeypatch):
    import faultline.remediation.propose as propose_mod
    from faultline.schemas import Finding

    tmp_store.write_topology(topology)
    tmp_store.write_experiment(make_report(experiment_id="exp-high", service="things",
                                           finding_codes=["health_check_masked_failure"]))
    info_only = make_report(experiment_id="exp-info", service="things")
    info_only.findings = [Finding(code="auto_recovered", severity="info", title="ok", evidence="-")]
    tmp_store.write_experiment(info_only)
    tmp_store.write_experiment(make_report(experiment_id="exp-done", service="things",
                                           finding_codes=["slow_recovery"]))
    tmp_store.write_proposal(make_proposal("fx-existing", experiment_id="exp-done"))
    calls: list[tuple] = []

    def build(settings, store, experiment_id, llm=None, **kw):
        calls.append((experiment_id, llm))
        p = make_proposal(f"fx-{experiment_id}", experiment_id=experiment_id)
        store.write_proposal(p)
        return [p]

    monkeypatch.setattr(propose_mod, "build_proposals", build)
    control.submit("analyze", {"llm": False})
    job = wait_job(control)
    assert job["status"] == "done" and job["result"]["proposals"] == 1
    assert calls == [("exp-high", None)]         # info-only and already-proposed experiments skipped
    assert tmp_store.proposal("fx-exp-high").status == "pending"   # proposed, never approved

    def boom(*a, **kw):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(propose_mod, "build_proposals", boom)
    tmp_store.write_experiment(make_report(experiment_id="exp-new", service="things",
                                           finding_codes=["slow_recovery"]))
    control.submit("analyze", {"llm": False})
    assert wait_job(control)["status"] == "done"
    assert any("Fix proposals skipped for exp-new" in e["text"] and e["level"] == "warn"
               for e in control.state(0)["events"])


def test_analyze_passes_the_llm_only_when_asked(control, tmp_store, make_report, topology, monkeypatch):
    import faultline.remediation.propose as propose_mod

    tmp_store.write_topology(topology)
    tmp_store.write_experiment(make_report(experiment_id="exp-llm", service="things",
                                           finding_codes=["health_check_masked_failure"]))
    seen: list = []
    monkeypatch.setattr(propose_mod, "build_proposals",
                        lambda settings, store, eid, llm=None, **kw: seen.append(llm) or [])
    control._analyze(True, only=["exp-llm"])
    assert len(seen) == 1 and seen[0] is not None
    control._analyze(False, only=["exp-llm"])
    assert seen[1] is None


def test_fix_deps_reuse_the_rooms_armed_session(control, sessions, monkeypatch, tmp_store, make_report, topology):
    import faultline.remediation.apply as apply_mod

    waits: list = []
    monkeypatch.setattr(control._cancel, "wait", lambda t=None: waits.append(t) or False)
    deps = control._fix_deps(allow_protected=True, use_llm=False)
    assert isinstance(deps, apply_mod.Deps)
    assert len(sessions) == 1 and control.session is sessions[0]          # armed once, like _job_run
    s = sessions[0]
    assert deps.docker is s.docker and deps.window is s.window and deps.topology is s.topology
    assert deps.policy is s.policy and callable(deps.inject) and hasattr(deps.planner, "plan_operator")
    assert deps.planner.allow_protected is True
    assert waits and waits[0] >= 15                                         # empty window: probes warmed up
    deps.close()
    assert s.closed is False                                                # the session is the room's
    assert control._fix_deps(False, False) and len(sessions) == 1           # still one session
    tmp_store.write_topology(topology)
    tmp_store.write_experiment(make_report(experiment_id="exp-rerun", service="things"))
    deps.analyze()                                                          # risk + scorecard, no LLM
    assert tmp_store.risk("exp-rerun") is not None and tmp_store.scorecard() is not None
