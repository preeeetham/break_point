"""The Weakness Scout against the REAL Ditto compose file: the predictions pinned before the core/target
split, unchanged. No LLM, no Docker; `live=False` keeps the topology static."""
from __future__ import annotations

import pytest

from conftest import APPS as DITTO_APPS, COMPOSE as REAL_COMPOSE, KNOWLEDGE as REAL_KNOWLEDGE  # noqa: E402
from conftest import NGINX as REAL_NGINX, THRESHOLDS as REAL_THRESHOLDS  # noqa: E402

from break_point.agents import scout as S
from break_point.chaos.catalog import CATALOG
from break_point.schemas import PREDICTED_LABEL, WeaknessReport
from break_point.topology import graph


class FakeSettings:
    """The four attributes `scout.build_context` actually reads."""

    def __init__(self, compose_file, nginx_conf="", knowledge_file="", thresholds_file=REAL_THRESHOLDS):
        self.compose_file = str(compose_file)
        self.nginx_conf = str(nginx_conf)
        self.knowledge_file = str(knowledge_file)
        self.thresholds_file = str(thresholds_file)


@pytest.fixture(scope="module")
def real_ctx() -> S.ScoutContext:
    """A ScoutContext over the real deployment: enough weaknesses to need several prose chunks."""
    settings = FakeSettings(REAL_COMPOSE, REAL_NGINX, REAL_KNOWLEDGE, REAL_THRESHOLDS)
    topology = graph.build(str(REAL_COMPOSE), "break_point", str(REAL_NGINX), str(REAL_KNOWLEDGE), live=False)
    return S.build_context(settings, topology)


@pytest.fixture(scope="module")
def real_report() -> WeaknessReport:
    """The scout against the REAL deploy/docker-compose.yml, with no LLM and no Docker.

    `live=False` keeps the topology purely static, so this test needs nothing running. The values
    pinned below were verified by hand against the compose file.
    """
    settings = FakeSettings(REAL_COMPOSE, REAL_NGINX, REAL_KNOWLEDGE, REAL_THRESHOLDS)
    topology = graph.build(str(REAL_COMPOSE), "break_point", str(REAL_NGINX), str(REAL_KNOWLEDGE), live=False)
    return S.scout(S.build_context(settings, topology), None)


def test_real_ditto_highest_severity_prediction_is_mongodb_redundancy(real_report):
    """The scout rediscovers "run MongoDB as a replica set" from the compose file alone.

    That is exactly the remediation the Fix agent's knowledge library recommends AFTER an experiment
    proves it; here it is predicted before anything has been broken.
    """
    top = real_report.weaknesses[0]
    assert (top.category, top.services) == ("no_redundancy", ["mongodb"])
    assert top.weakness_id == "no_redundancy:mongodb"
    assert top.predicted_level == "critical"
    # strictly the highest, not merely tied for first
    assert top.predicted_severity > real_report.weaknesses[1].predicted_severity
    sev = [w.predicted_severity for w in real_report.weaknesses]
    assert sev == sorted(sev, reverse=True)
    assert "exactly one container serves it" in top.evidence
    assert "It also holds the only copy of its state." in top.evidence


def test_real_ditto_predicts_a_second_policies_instance(real_report):
    """The library's other post-hoc fix -- "run >= 2 policies instances" -- also falls out statically."""
    pol = real_report.by_id("no_redundancy:policies")
    assert pol is not None
    assert pol.predicted_level in ("high", "critical")
    assert "no `deploy.replicas` and no `scale:`" in pol.evidence
    # policies is the single point of failure for the rest of the Ditto cluster
    assert "things" in pol.evidence and "gateway" in pol.evidence
    mongo = real_report.by_id("no_redundancy:mongodb")
    assert mongo.predicted_severity > pol.predicted_severity


def test_real_ditto_shallow_health_check_quotes_the_alive_probe_and_names_the_finding(real_report):
    hits = {w.services[0]: w for w in real_report.weaknesses if w.category == "shallow_health_check"}
    assert set(hits) == set(DITTO_APPS)
    for svc, w in hits.items():
        assert "7626/alive" in w.evidence
        assert "curl --fail `hostname`:7626/alive || exit 1" in w.evidence
        assert "static shape of `health_check_masked_failure`" in w.evidence
        assert "health_check_masked_failure" in w.confirming_finding_codes
    # the datastore is deliberately NOT flagged: `mongosh ping` is a genuine subsystem check and
    # mongodb depends on nothing, so a dependency-aware probe is not even possible
    assert "mongodb" not in hits


def test_real_ditto_weak_restart_policy_produces_zero_predictions(real_report):
    """The silence IS the point.

    Every service in deploy/docker-compose.yml declares `restart: always`, which is part of why
    Ditto's crash recovery works cleanly. A check that fires on everything is a broken check, so this
    assertion guards against the restart check degenerating into noise.
    """
    assert [w.weakness_id for w in real_report.weaknesses if w.category == "weak_restart_policy"] == []
    compose = S.load_compose(str(REAL_COMPOSE))
    assert all((svc or {}).get("restart") == "always" for svc in compose["services"].values())


def test_real_ditto_mongodb_experiment_is_degrade_only(real_report):
    """The protected-policy path, on the target's own thresholds.yaml."""
    top = real_report.by_id("no_redundancy:mongodb")
    se = top.suggested_experiment
    assert se.target_service == "mongodb"
    assert se.failure_type in CATALOG
    assert CATALOG[se.failure_type].degrade_only is True
    assert se.failure_type not in ("container_stop", "container_crash", "container_pause",
                                   "network_partition")
    # The substitution is disclosed: a degrade-only stand-in tests "there is only one instance" less
    # completely than taking the instance away, and the record says so rather than implying parity.
    assert "container_stop" in se.policy_note and "--allow-protected" in se.policy_note


def test_real_ditto_every_prediction_is_labelled_and_sane(real_report):
    assert real_report.project == "break_point"
    assert real_report.compose_file == str(REAL_COMPOSE)
    assert real_report.knowledge_file == str(REAL_KNOWLEDGE)
    assert real_report.label == PREDICTED_LABEL
    assert real_report.generated_by == "rules"
    assert real_report.checks_run == [name for name, _ in S.CHECKS]
    assert len(real_report.weaknesses) >= 15
    for w in real_report.weaknesses:
        assert w.label == PREDICTED_LABEL
        assert w.status == "predicted"
        assert w.generated_by == "rules"
        assert 0 < w.predicted_severity <= 10
        assert w.evidence.startswith(("compose service", "`")) or " depend on `" in w.evidence
        assert w.services


def test_real_ditto_other_categories_land_where_expected(real_report):
    by_cat: dict[str, set[str]] = {}
    for w in real_report.weaknesses:
        by_cat.setdefault(w.category, set()).update(w.services)

    # nginx is the one service in the file without a healthcheck block
    assert by_cat["missing_health_check"] == {"nginx"}
    # mongodb sets a memory limit but no cpus; no service in the file sets a CPU limit
    assert "mongodb" in by_cat["no_resource_limits"]
    mongo_limits = real_report.by_id("no_resource_limits:mongodb")
    assert "no `deploy.resources.limits.cpus` (it does set memory limit `256m`)" in mongo_limits.evidence
    assert mongo_limits.factors["category"] == 0.5     # half-covered, half-weighted
    # nginx is the only ingress and the real nginx.conf has no limit_req/limit_conn
    assert by_cat["no_rate_limiting"] == {"nginx"}
    assert "limit_req" not in REAL_NGINX.read_text()
    # the observability stack publishes its own loopback ports, outside the system under test
    assert by_cat["port_beyond_ingress"] == {"prometheus", "grafana"}
    for svc in ("prometheus", "grafana"):
        w = real_report.by_id(f"port_beyond_ingress:{svc}")
        assert "all bound to 127.0.0.1" in w.evidence
        assert "tagged outside the system under test" in w.evidence
        assert "cannot be confirmed by an experiment" in w.suggested_experiment.policy_note
    # one retry/backoff finding per dependency edge target, never one per caller
    retry_targets = [w for w in real_report.weaknesses if w.category == "no_retry_or_backoff"]
    assert any("depend on `mongodb`" in w.evidence for w in retry_targets)
    assert all(len(w.services) >= 2 for w in retry_targets)


def test_real_ditto_markdown_is_self_describing(real_report):
    md = S.render_markdown(real_report)
    assert PREDICTED_LABEL in md
    assert "predicted, not measured" in md
    assert "no_redundancy" in md and "mongodb" in md
    assert "weak_restart_policy" not in md     # nothing fired, so nothing is rendered


# ================================================================ build_context
