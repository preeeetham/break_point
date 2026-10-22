"""What a target directory supplies and how core consumes it: the probe spec, extra fix rules, and the
facts core derives from the topology instead of hardcoding them (restart order, the prompt's tier line,
outage error rates). Everything runs against the invented shop in tests/fixtures/shop; no Docker."""
from __future__ import annotations

import argparse

import pytest
import requests

from break_point import cli
from break_point.agents import knowledge as K
from break_point.config import Settings
from break_point.observe import probes as P
from break_point.planning.planner import Planner, Policy, application_line
from break_point.topology.graph import build, start_order, system_summary

from .conftest import SHOP


@pytest.fixture(scope="module")
def shop():
    return build(str(SHOP / "docker-compose.yml"), "shop", str(SHOP / "nginx" / "nginx.conf"),
                 str(SHOP / "shop_knowledge.yaml"), live=False)


# ---------------------------------------------------------------- probe spec
def test_probe_spec_loads_operations_routes_and_integrity(shop):
    spec = P.load_spec(SHOP / "probe.yaml", shop)
    assert spec.health_path == "/health" and spec.health_expect == {"status": "UP"}
    assert spec.mix[:4] == ("get_order", "put_property", "get_order", "search")
    assert spec.routes["search"] == ["nginx", "gateway", "orders-search"]
    assert spec.operations["put_property"].write and not spec.operations["get_order"].write
    assert (spec.integrity, spec.spike) == ("get_property", "get_order")
    assert [op.name for op in spec.setup] == ["create_canary"]


def test_no_spec_probes_the_health_url_through_the_entry_points(shop, tmp_path):
    spec = P.load_spec(tmp_path / "absent.yaml", shop)
    assert list(spec.operations) == ["health"] and spec.mix == ("health",) and spec.spike == "health"
    # published proxy and apps only; prometheus and grafana publish ports too, but are not the product
    assert spec.routes == {"health": ["nginx"]}
    assert spec.integrity == "" and P.load_spec(None).routes == {"health": []}


def test_probe_spec_rejects_unknown_operation_names(tmp_path):
    bad = tmp_path / "probe.yaml"
    bad.write_text("operations:\n  read: {path: /r}\nmix: [read, wrte]\nintegrity: read_back\n")
    with pytest.raises(ValueError, match=r"unknown operation\(s\) \['read_back', 'wrte'\]"):
        P.load_spec(bad)


def test_placeholders_fill_namespace_everywhere_and_seq_only_where_it_is_the_whole_value():
    body = {"id": "{namespace}:canary", "seq": "{seq}", "note": "seq is {seq}", "tags": ["{namespace}"]}
    assert P._fill(body, "ns", 7) == {"id": "ns:canary", "seq": 7, "note": "seq is {seq}", "tags": ["ns"]}
    assert P._fill("{seq}", "ns") == "{seq}"                 # no sequence number: left alone


def _response(status: int, body: str) -> requests.Response:
    r = requests.Response()
    r.status_code, r._content = status, body.encode()
    return r


def test_health_ok_checks_status_and_the_expected_json():
    spec = P.ProbeSpec(health_expect={"status": "UP"})
    assert P.health_ok(spec, _response(200, '{"status": "UP", "children": []}'))
    assert not P.health_ok(spec, _response(200, '{"status": "DOWN"}'))
    assert not P.health_ok(spec, _response(200, "OK"))
    assert not P.health_ok(spec, _response(503, '{"status": "UP"}'))
    assert P.health_ok(P.ProbeSpec(), _response(204, ""))


def test_api_probe_sends_what_the_spec_says(shop):
    spec = P.load_spec(SHOP / "probe.yaml", shop)
    probe = P.HttpApiProbe("http://shop.test/", spec, window=None, user="probe", password="pw")
    sent = []

    class Session:
        def request(self, method, url, **kw):
            sent.append((method, url, kw))
            return _response(204, "")

    probe.send(spec.operations["put_property"], seq=42, session=Session())
    method, url, kw = sent[0]
    assert (method, url) == ("PUT", "http://shop.test/api/orders/break_point.probe:canary/seq")
    assert kw["json"] == 42 and kw["auth"] == ("probe", "pw")
    assert probe.spike_request()[:2] == ("GET", "http://shop.test/api/orders/break_point.probe:canary")
    assert P.HttpApiProbe("http://x", spec, window=None).auth is None   # no user, no credentials sent


# ---------------------------------------------------------------- target rules
def _rules(tmp_path, text: str):
    p = tmp_path / "rules.yaml"
    p.write_text(text)
    return K.load_rules(p)


RULE = """
  - match: {targets: [orders], failure_types: [container_crash]}
    title: Run two orders instances
    category: architecture
    effort: M
    confidence: 0.9
    rationale: orders is single-instance.
    steps: [scale it]
    verify: [kill one]
"""


def test_target_rules_extend_by_default_and_replace_on_request(tmp_path):
    extended = _rules(tmp_path, "rules:" + RULE)
    assert extended[:-1] == K.RULES and extended[-1]["recommendation"]["title"] == "Run two orders instances"
    assert extended[-1]["match"] == {"failure_types": ["container_crash"], "targets": ["orders"],
                                     "roles": None, "finding_codes": None}
    assert [r["recommendation"]["title"] for r in _rules(tmp_path, "mode: replace\nrules:" + RULE)] == [
        "Run two orders instances"]
    assert K.load_rules(None) == K.RULES


@pytest.mark.parametrize("text,match", [
    ("mode: merge\nrules: []\n", "mode must be one of"),
    ("rules:\n  - {match: {service: [a]}, title: t}\n", "unknown match slot"),
    ("rules:\n  - {title: no category}\n", "category"),
])
def test_bad_target_rules_fail_at_load(tmp_path, text, match):
    with pytest.raises(Exception, match=match):
        _rules(tmp_path, text)


def test_role_rules_need_the_topology_and_rank_between_targets_and_failure_types(make_report, topology):
    report = make_report(service="postgres", failure_type="container_crash")
    titles = [r["recommendation"]["title"] for r in K.matching_rules(report, topology=topology)]
    assert titles[0].startswith("Remove the single datastore")        # role rule beats failure-type rules
    no_topo = [r["recommendation"]["title"] for r in K.matching_rules(report)]
    assert not any(t.startswith("Remove the single datastore") for t in no_topo)
    own = K.load_rules(None) + [K._from_yaml(
        {"match": {"targets": ["postgres"]}, "title": "Target rule", "category": "operations", "effort": "S",
         "confidence": 0.1, "rationale": "r", "steps": ["s"], "verify": ["v"]}, "test")]
    assert K.matching_rules(report, own, topology)[0]["recommendation"]["title"] == "Target rule"


def test_generic_rules_name_no_service():
    names = {"postgres", "orders", "gateway", "auth", "nginx"}
    for rule in K.RULES:
        assert rule["match"]["targets"] is None, rule["recommendation"]["title"]
        text = " ".join([rule["recommendation"]["title"], rule["recommendation"]["rationale"]]).lower()
        assert not any(f" {n} " in f" {text} " for n in names - {"nginx"}), rule["recommendation"]["title"]


# ---------------------------------------------------------------- derived from the topology
def test_start_order_puts_every_service_after_what_it_needs(shop):
    order = start_order(shop)
    assert order == ["postgres", "auth", "orders", "notifier", "orders-search", "gateway"]
    for e in shop.edges:
        if e.src in order and e.dst in order:
            assert order.index(e.dst) < order.index(e.src), e


def test_start_order_survives_a_dependency_cycle(topology):
    from break_point.schemas import Edge

    cyclic = topology.model_copy(update={"edges": topology.edges + [Edge(src="auth", dst="orders", kind="documented")]})
    assert sorted(start_order(cyclic)) == sorted(start_order(topology))


def test_system_summary_names_the_tiers_from_data(shop, topology):
    assert system_summary(shop) == "nginx -> gateway -> auth/notifier/orders/orders-search -> postgres (postgres)"
    assert system_summary(None) == "a docker-compose deployment"


def test_planner_is_told_the_application_and_reads_outage_rates_from_knowledge(shop, policy_raw):
    s = Settings(app_name="The Shop", knowledge_file=str(SHOP / "shop_knowledge.yaml"))
    assert application_line(s, shop).startswith("Application: The Shop. Tiers: nginx -> gateway")
    p = Planner(shop, Policy(policy_raw), [], None, s, {"pumba", "tc"})
    from break_point.chaos.catalog import CATALOG

    full = CATALOG["container_stop"]
    assert p.default_hypothesis("orders-search", full, {}).expected_max_error_rate == 0.3
    assert p.default_hypothesis("orders", full, {}).expected_max_error_rate == 1.0
    assert p.default_hypothesis("postgres", full, {}).expected_max_error_rate == 1.0
    # degrade-only faults never list the proxy as affected
    assert "nginx" not in p.default_hypothesis("gateway", CATALOG["network_delay"], {}).expected_affected


# ---------------------------------------------------------------- the CLI without and with a target
def test_cli_without_a_target_says_how_to_choose_one(capsys):
    s = Settings()
    assert cli.cmd_target(s, None, argparse.Namespace(shell=False, wait=0)) == 1
    assert "no target selected" in capsys.readouterr().out
    with pytest.raises(cli.NoTarget, match="BREAK_POINT_TARGET"):
        cli.build_topology(s, None, live=False)
    missing = Settings(target="ghost", target_error="BREAK_POINT_TARGET=ghost but targets/ghost/ does not exist")
    assert cli.no_target_message(missing) == "error: BREAK_POINT_TARGET=ghost but targets/ghost/ does not exist"


def test_cli_target_shell_output_is_sourceable(capsys):
    s = Settings(compose_file=str(SHOP / "docker-compose.yml"), compose_project="shop", probe_file=str(SHOP / "probe.yaml"),
                 target_url="http://127.0.0.1:8181")
    assert cli.cmd_target(s, None, argparse.Namespace(shell=True, wait=0)) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["COMPOSE_PROJECT"] == "shop" and lines["HEALTH_URL"] == "http://127.0.0.1:8181/health"
    assert lines["COMPOSE_FILE"] == str(SHOP / "docker-compose.yml")
