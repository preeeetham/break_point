"""The Ditto target directory reproduces what Faultline hardcoded for Ditto before the core/target split.

Each test names the pre-split behaviour it pins: the Things-API probe (OPS, OP_ROUTES, canary, integrity),
the 18-rule fix library and its ranking, the `mongodb` protection, the Pekko cluster in topology.md, the
outage error rates of the default hypothesis, and the restart order of `faultline heal --restart`."""
from __future__ import annotations

import pytest
import yaml

from conftest import APPS, COMPOSE, KNOWLEDGE, NGINX, PROBE, RULES, TARGET, THRESHOLDS  # noqa: E402

from faultline.agents import fix
from faultline.agents import knowledge as K
from faultline.chaos.catalog import CATALOG, applicable, entry_tier
from faultline.config import load_settings
from faultline.observe import probes as P
from faultline.planning.planner import Planner, Policy
from faultline.remediation.catalog import protected_services
from faultline.safety.thresholds import load_policy
from faultline.schemas import RepoKnowledge
from faultline.topology import quality
from faultline.topology.graph import build, start_order, system_summary
from faultline.topology.render import markdown

# What the pre-split ThingsApiProbe sent, verbatim.
OLD_OPS = ("get_thing", "put_property", "get_thing", "search", "get_property", "put_property", "get_thing", "search")
OLD_ROUTES = {"get_thing": ["nginx", "gateway", "things"], "put_property": ["nginx", "gateway", "things"],
              "get_property": ["nginx", "gateway", "things"], "search": ["nginx", "gateway", "things-search"]}
OLD_CANARY_BODY = {"attributes": {"kind": "faultline-canary"},
                   "features": {"telemetry": {"properties": {"seq": 0, "temperature": 20.0}}}}


# ---------------------------------------------------------------- the target resolves
def test_faultline_target_ditto_resolves_every_file(monkeypatch):
    for key in ("FAULTLINE_COMPOSE_FILE", "FAULTLINE_KNOWLEDGE_FILE", "FAULTLINE_PROBE_FILE", "FAULTLINE_RULES_FILE",
                "FAULTLINE_NGINX_CONF", "FAULTLINE_COMPOSE_PROJECT", "TARGET_URL", "TARGET_USER", "TARGET_PASSWORD"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("FAULTLINE_TARGET", "ditto")
    s = load_settings()
    assert s.target_error == "" and s.target_dir == TARGET
    assert (s.compose_file, s.nginx_conf, s.knowledge_file, s.probe_file, s.rules_file, s.thresholds_file) == tuple(
        str(p) for p in (COMPOSE, NGINX, KNOWLEDGE, PROBE, RULES, THRESHOLDS))
    assert (s.compose_project, s.target_url, s.target_user, s.target_password) == (
        "faultline", "http://127.0.0.1:8080", "ditto", "ditto")
    assert s.remediation_override_file == str(TARGET / "deploy" / "docker-compose.override.yml")


def test_knowledge_file_validates_and_keeps_its_hand_written_facts():
    k = RepoKnowledge.model_validate(yaml.safe_load(KNOWLEDGE.read_text()))
    assert k.health_port == 7626 and k.cluster_alias == "ditto-cluster"
    assert (k.health_paths.alive, k.health_paths.ready) == ("/alive", "/ready")
    assert k.roles["mongodb"] == "datastore" and k.roles["nginx"] == "proxy"
    assert len(k.documented_edges) == 9 and all(e.evidence for e in k.documented_edges)
    assert k.outage_error_rates == {"things-search": 0.3, "connectivity": 0.05}


# ---------------------------------------------------------------- the probe is the old Things-API probe
def test_probe_spec_is_the_old_things_api_probe(ditto_topology):
    spec = P.load_spec(PROBE, ditto_topology)
    assert spec.mix == OLD_OPS and spec.routes == OLD_ROUTES
    assert spec.health_path == "/health" and spec.health_expect == {"status": "UP"}
    assert spec.integrity == "get_property" and spec.spike == "get_thing"
    (setup,) = spec.setup
    assert (setup.method, setup.path, setup.json) == ("PUT", "/api/2/things/{namespace}:canary", OLD_CANARY_BODY)


def test_probe_sends_the_old_requests(ditto_topology):
    spec = P.load_spec(PROBE, ditto_topology)
    probe = P.HttpApiProbe("http://127.0.0.1:8080", spec, window=None, user="ditto", password="ditto")
    sent = []

    class Session:
        def request(self, method, url, **kw):
            sent.append((method, url, kw.get("params"), kw.get("json")))

    for name in ("get_thing", "get_property", "search"):
        probe.send(spec.operations[name], session=Session())
    probe.send(spec.operations["put_property"], seq=7, session=Session())
    base = "http://127.0.0.1:8080/api/2"
    assert sent == [
        ("GET", f"{base}/things/faultline.probe:canary", None, None),
        ("GET", f"{base}/things/faultline.probe:canary/features/telemetry/properties/seq", None, None),
        ("GET", f"{base}/search/things", {"filter": 'eq(attributes/kind,"faultline-canary")', "option": "size(1)"}, None),
        ("PUT", f"{base}/things/faultline.probe:canary/features/telemetry/properties/seq", None, 7),
    ]
    assert probe.auth == ("ditto", "ditto")


# ---------------------------------------------------------------- the fix library is the old one
def test_rules_replace_the_generic_library_with_the_18_ditto_rules():
    rules = K.load_rules(RULES)
    assert len(rules) == 18 and not any(r in K.RULES for r in rules)
    assert rules[0]["recommendation"]["title"] == "Run MongoDB as a replica set so the Ditto driver can fail over"
    assert all(r["match"]["roles"] is None for r in rules)


@pytest.mark.parametrize("service,failure,codes,first", [
    ("mongodb", "container_crash", [], "Run MongoDB as a replica set so the Ditto driver can fail over"),
    ("gateway", "container_crash", [], "Run two gateway replicas behind an nginx upstream with retries"),
    ("things", "container_pause", ["health_check_masked_failure"],
     "Make readiness reflect subsystem health, and alert on /status/health"),
    ("connectivity", "network_partition", [],
     "Isolate connectivity failures from the HTTP API and make connections self-healing"),
])
def test_ditto_rules_rank_as_before(make_report, ditto_topology, service, failure, codes, first):
    report = make_report(service=service, failure_type=failure, finding_codes=codes)
    assert K.matching_rules(report, K.load_rules(RULES), ditto_topology)[0]["recommendation"]["title"] == first


def test_masked_failure_fix_still_cites_issue_1865(make_report, ditto_topology):
    report = make_report(service="things", finding_codes=["health_check_masked_failure"])
    out = fix.recommend(report, None, None, K.load_rules(RULES), ditto_topology, "Eclipse Ditto")
    assert any("1865" in " ".join([r.rationale, *r.references]) for r in out.recommendations)


# ---------------------------------------------------------------- topology, protection, planning
def test_real_compose_maps_as_before(ditto_topology):
    audit = quality.Audit()
    audited = build(str(COMPOSE), "faultline", str(NGINX), str(KNOWLEDGE), live=False, audit=audit)
    assert audit.issues == [] and (len(audited.services), len(audited.edges)) == (12, 21)
    db = ditto_topology.services["mongodb"]
    assert (db.engine, db.engine_evidence) == ("mongo", "image docker.io/mongo:7.0")
    for svc in APPS:
        node = ditto_topology.services[svc]
        assert (node.health_port, node.health_path, node.ready_path) == (7626, "/alive", "/ready")
    assert ditto_topology.criticality[0].service == "mongodb"
    assert 'subgraph cluster["ditto-cluster (cluster)"]' in markdown(ditto_topology, "ditto-cluster")
    assert system_summary(ditto_topology) == "nginx -> gateway -> connectivity/policies/things/things-search -> mongodb (mongo)"


def test_mongodb_is_protected_and_degrade_only(ditto_topology):
    policy = Policy(load_policy(str(THRESHOLDS)))
    assert protected_services(thresholds_file=str(THRESHOLDS)) == {"mongodb"}
    assert policy.allows("mongodb", "network_delay")[0] and not policy.allows("mongodb", "container_stop")[0]


def test_planner_and_catalog_keep_the_old_ditto_behaviour(ditto_topology, monkeypatch):
    monkeypatch.setenv("FAULTLINE_TARGET", "ditto")
    s = load_settings()
    p = Planner(ditto_topology, Policy(load_policy(str(THRESHOLDS))), [], None, s, {"pumba", "tc"})
    rate = lambda svc: p.default_hypothesis(svc, CATALOG["container_stop"], {}).expected_max_error_rate  # noqa: E731
    assert (rate("things-search"), rate("connectivity"), rate("things"), rate("mongodb")) == (0.3, 0.05, 1.0, 1.0)
    # traffic_spike was hardcoded to ("nginx", "gateway"); the entry tier derives the same pair
    assert entry_tier(ditto_topology) == {"nginx", "gateway"}
    assert applicable(CATALOG["db_flood"], ditto_topology.services["mongodb"], ditto_topology)
    # heal --restart used the fixed order mongodb, policies, things, things-search, connectivity, gateway
    order = start_order(ditto_topology)
    assert order[:3] == ["mongodb", "policies", "things"] and order[-1] == "gateway"
    assert sorted(order) == sorted(["mongodb", *APPS])
