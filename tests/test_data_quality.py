"""Ingestion validation and cleaning (faultline/topology/quality.py): the six checks, no LLM."""
import yaml

from faultline.config import load_settings
from faultline.reporting.store import ReportStore
from faultline.topology import quality
from faultline.topology.graph import build

DITTO = ("deploy/docker-compose.yml", "ditto", "deploy/nginx/nginx.conf", "config/ditto_knowledge.yaml")


def _build(tmp_path, compose: dict):
    p = tmp_path / "docker-compose.yml"
    p.write_text(yaml.safe_dump(compose))
    audit = quality.Audit()
    topo = build(str(p), "x", None, None, live=False, audit=audit)
    return topo, audit.issues


def _has(issues, check, action, record=None):
    return any(i.check == check and i.action == action and (record is None or i.record == record) for i in issues)


def test_ditto_compose_is_clean_and_graph_unchanged():
    audit = quality.Audit()
    with_pass = build(*DITTO, live=False, audit=audit)
    without = build(*DITTO, live=False)
    assert audit.issues == []
    assert {(e.src, e.dst, e.kind) for e in with_pass.edges} == {(e.src, e.dst, e.kind) for e in without.edges}
    assert set(with_pass.services) == set(without.services)


def test_null_empty_service_and_missing_image(tmp_path):
    topo, issues = _build(tmp_path, {"services": {"a": None, "b": {"image": "x"}}})
    assert "a" in topo.services
    assert _has(issues, "null", "defaulted", "a") and _has(issues, "null", "flagged", "a")


def test_conformity_bad_name_and_types(tmp_path):
    topo, issues = _build(tmp_path, {"services": {"bad name!": {"image": "x"}, "b": {"image": "x", "environment": 5, "depends_on": "a"}}})
    assert "bad name!" not in topo.services
    assert _has(issues, "conformity", "excluded", "bad name!")
    assert sum(i.check == "conformity" and i.record == "b" for i in issues) == 2


def test_range_port_memory_and_healthcheck(tmp_path):
    svc = {"image": "x", "ports": ["70000:80", "8080:80"], "mem_limit": "0m",
           "healthcheck": {"test": ["CMD", "true"], "interval": "0s", "timeout": "5h", "retries": -1}}
    topo, issues = _build(tmp_path, {"services": {"a": svc}})
    assert _has(issues, "range", "excluded", "a")  # port 70000 dropped
    flags = [i.field for i in issues if i.check == "range" and i.action == "flagged"]
    assert {"mem_limit", "healthcheck.interval", "healthcheck.timeout", "healthcheck.retries"} <= set(flags)
    assert topo.services["a"].entrypoint  # the valid port survives


def test_missing_healthcheck_interval_is_defaulted_and_recorded(tmp_path):
    _, issues = _build(tmp_path, {"services": {"a": {"image": "x", "healthcheck": {"test": "true"}}}})
    assert _has(issues, "null", "defaulted", "a")


def test_consistency_dangling_env_and_depends_on_never_invent_edges(tmp_path):
    compose = {"services": {
        "app": {"image": "x", "depends_on": ["db", "ghost"], "environment": ["DB_HOST=db", "CACHE_HOST=redis", "API_URL=https://api.example.com"]},
        "db": {"image": "mongo"}}}
    topo, issues = _build(tmp_path, compose)
    assert _has(issues, "consistency", "excluded", "app")            # depends_on ghost
    dangling = [i for i in issues if i.check == "consistency" and i.action == "flagged"]
    assert [i.field for i in dangling] == ["CACHE_HOST"]              # redis flagged, external FQDN not
    assert all(e.dst in topo.services and e.src in topo.services for e in topo.edges)
    assert {(e.src, e.dst) for e in topo.edges} == {("app", "db")}


def test_cleaning_trims_names_and_dedupes_env_and_depends_on(tmp_path):
    compose = {"services": {" app ": {"image": "x", "depends_on": ["db", "db"], "environment": ["A=1", "A=2"]}, "db": {"image": "y"}}}
    topo, issues = _build(tmp_path, compose)
    assert "app" in topo.services
    assert _has(issues, "cleaning", "normalized", "app")
    assert sum(i.check == "cleaning" and i.action == "deduplicated" for i in issues) == 2


def test_edge_check_excludes_unknown_endpoints_and_duplicates():
    from faultline.schemas import Edge, ServiceNode
    nodes = {"a": ServiceNode(name="a"), "b": ServiceNode(name="b")}
    audit = quality.Audit()
    kept = quality.check_edges([Edge(src="a", dst="b", kind="env"), Edge(src="a", dst="b", kind="env"),
                                Edge(src="a", dst="zz", kind="documented")], nodes, audit)
    assert [(e.src, e.dst) for e in kept] == [("a", "b")]
    assert {i.action for i in audit.issues} == {"deduplicated", "excluded"}


def test_time_lenience_never_manufactures_order():
    assert quality.same_window(10.0, 11.2, 1.5) is True
    assert quality.order(10.0, 11.2, 1.5) == "same_window"
    assert quality.order(10.0, 13.0, 1.5) == "before"
    assert quality.order(None, 13.0, 1.5) == "unknown"


def test_time_lenience_is_a_setting(monkeypatch):
    monkeypatch.setenv("TIME_LENIENCE_SECONDS", "2.5")
    assert load_settings().time_lenience_s == 2.5


def test_report_written_and_schema_valid(tmp_path):
    audit = quality.Audit()
    audit.add("range", "clamped", "a", "x", "ports")
    store = ReportStore(tmp_path)
    store.write_data_quality(quality.report("topology:test", audit, 1.5, 1, 0))
    d = store.data_quality()
    assert d["counts"] == {"range": 1} and d["rules_only"] is True and d["issues"][0]["record"] == "a"
