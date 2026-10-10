import os

from faultline.reporting.store import ReportStore
from faultline.schemas import (
    Baseline,
    FixRecommendation,
    Recommendation,
    RiskAssessment,
    Scorecard,
    ServiceScore,
    WindowStats,
    utcnow,
)


def test_store_creates_layout(tmp_path):
    store = ReportStore(tmp_path / "r")
    for sub in ("experiments", "risk", "fixes", "logs"):
        assert (store.root / sub).is_dir()
    assert store.topology() is None and store.baseline() is None and store.scorecard() is None
    assert store.experiments() == [] and store.active() is None and store.live() is None


def test_topology_and_baseline_round_trip(tmp_store, topology, thresholds):
    tmp_store.write_topology(topology)
    back = tmp_store.topology()
    assert back is not None
    assert set(back.services) == set(topology.services)
    assert len(back.edges) == len(topology.edges)
    assert back.criticality_of("mongodb") == topology.criticality_of("mongodb")

    b = Baseline(captured_at=utcnow(), duration_s=30, ditto_url="http://x", api=WindowStats(sample_count=3, success_rate=1.0),
                 health_availability={"things": 1.0, "mongodb": None}, thresholds=thresholds, notes=["n"])
    tmp_store.write_baseline(b)
    back_b = tmp_store.baseline()
    assert back_b.thresholds == thresholds and back_b.health_availability == {"things": 1.0, "mongodb": None}


def test_experiment_round_trip_and_ordering(tmp_store, make_report):
    r1, r2, r3 = make_report(experiment_id="b"), make_report(experiment_id="a"), make_report(experiment_id="c")
    paths = [tmp_store.write_experiment(r) for r in (r1, r2, r3)]
    # ordering is by mtime, not by name: make it deterministic
    for i, p in enumerate(paths):
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    assert tmp_store.experiment_ids() == ["b", "a", "c"]
    assert [r.experiment_id for r in tmp_store.experiments()] == ["b", "a", "c"]
    back = tmp_store.experiment("a")
    assert back == r2
    assert tmp_store.experiment("missing") is None
    assert not list(tmp_store.root.glob("experiments/*.tmp"))
    d = tmp_store.experiment_dir("a")
    assert d.is_dir() and d.name == "a"


def test_corrupt_json_reads_as_none(tmp_store):
    (tmp_store.root / "experiments" / "bad.json").write_text("{not json")
    assert tmp_store.experiment("bad") is None
    assert tmp_store.experiments() == []


def test_risk_fix_scorecard_round_trip(tmp_store, make_report):
    report = make_report(experiment_id="e1")
    risk = RiskAssessment(experiment_id="e1", target_service="policies", failure_type="container_crash", severity=4.4,
                          level="medium", blast_radius=2, user_impact=0.9, recovery_s=14.0, factors={"impact": 3.6},
                          findings=[], summary="s", production_implication="p", generated_by="rules")
    fix = FixRecommendation(experiment_id="e1", target_service="policies", failure_type="container_crash", risk_level="medium",
                            recommendations=[Recommendation(title="t", category="operations", effort="S", confidence=0.5,
                                                            rationale="r", steps=["a"], verify=["b"])],
                            generated_by="rules")
    sc = Scorecard(project="p", overall_score=61.7, experiments_total=1, summary="s",
                   ranking=[ServiceScore(service="policies", score=61.7, grade="D", experiments=1)])
    tmp_store.write_experiment(report)
    tmp_store.write_risk(risk)
    tmp_store.write_fix(fix)
    tmp_store.write_scorecard(sc)
    assert tmp_store.risk("e1") == risk
    assert tmp_store.fix("e1") == fix
    assert tmp_store.scorecard() == sc
    assert tmp_store.risk("nope") is None and tmp_store.fix("nope") is None


def test_stop_file_request_and_clear(tmp_store):
    assert tmp_store.stop_requested() is False
    tmp_store.request_stop("operator")
    assert tmp_store.stop_requested() is True
    assert tmp_store.stop_file.read_text().startswith("operator ")
    tmp_store.clear_stop()
    assert tmp_store.stop_requested() is False
    tmp_store.clear_stop()  # idempotent


def test_active_lock_set_read_clear(tmp_store):
    assert tmp_store.active() is None
    tmp_store.set_active({"experiment_id": "e1", "pid": 42})
    assert tmp_store.active() == {"experiment_id": "e1", "pid": 42}
    assert tmp_store.lock_file.name == "ACTIVE_EXPERIMENT.json"
    tmp_store.clear_active()
    assert tmp_store.active() is None
    tmp_store.clear_active()


def test_live_heartbeat_and_log(tmp_store):
    tmp_store.write_live({"phase": "inject"})
    live = tmp_store.live()
    assert live["phase"] == "inject" and isinstance(live["updated_at"], float)
    tmp_store.write_heartbeat("guardian", {"ok": True})
    hb = tmp_store.heartbeat("guardian")
    assert hb["ok"] is True and "at" in hb
    assert tmp_store.heartbeat("nobody") is None
    tmp_store.append_log("run", "line one\n")
    tmp_store.append_log("run", "line two")
    assert (tmp_store.root / "logs" / "run.log").read_text() == "line one\nline two\n"
