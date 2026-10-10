"""Tests for the Weakness Scout (faultline/agents/scout.py).

The module's own docstring names the two invariants these tests exist to pin down:

  1. The checks are 100% deterministic -- the LLM NEVER decides whether a weakness exists. It may
     only rewrite `why_it_matters` and add additive `systemic_patterns`. Every test that involves a
     model uses a stub client (see `FakeLLM`); nothing here touches a network or Docker.
  2. A prediction is labelled as one, everywhere: `PREDICTED_LABEL` rides on the report, on every
     weakness, and on the rendered markdown, which must say in words that the severities were
     predicted and not measured.

Style follows tests/test_topology.py: small synthetic compose dicts fed through the REAL compose
parsers and criticality ranking, so each deterministic check can be driven in isolation and the
evidence string can be asserted against the actual compose field it quotes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from faultline.agents import scout as S
from faultline.chaos.catalog import CATALOG, applicable
from faultline.config import load_settings
from faultline.planning.planner import Policy
from faultline.schemas import PREDICTED_LABEL, Topology, WeaknessReport
from faultline.topology import graph
from faultline.topology.compose import documented_edges, service_nodes, static_edges

ROOT = Path(__file__).resolve().parent.parent
REAL_COMPOSE = ROOT / "deploy" / "docker-compose.yml"
REAL_NGINX = ROOT / "deploy" / "nginx" / "nginx.conf"
REAL_KNOWLEDGE = ROOT / "config" / "ditto_knowledge.yaml"
REAL_THRESHOLDS = ROOT / "config" / "thresholds.yaml"

# The five Ditto application services that share the `x-ditto-healthcheck` anchor in the real file.
DITTO_APPS = ("policies", "things", "things-search", "connectivity", "gateway")


# ---------------------------------------------------------------- stubs
class FakeLLM:
    """Stands in for faultline.planning.llm.LLMClient. Never touches the network.

    Same shape as the stubs in tests/test_planner.py and tests/test_onboarding.py: `available()`,
    `describe()`, `last_error`, `chat_json()`.
    """

    def __init__(self, answer=None, ok: bool = True, error: str = "", name: str = "fake:model-1",
                 raises: BaseException | None = None):
        self.answer = answer
        self._ok = ok
        self.last_error = error
        self._name = name
        self._raises = raises
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._ok

    def describe(self) -> str:
        return self._name

    def chat_json(self, system: str, user: str, max_tokens: int = 700):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self._raises is not None:
            raise self._raises
        return self.answer


class EchoLLM:
    """A well-behaved stub: it answers only about the weakness ids it was actually asked about.

    `_llm_pass` makes several calls -- one `PROSE_SYSTEM` call per chunk of `LLM_CHUNK_SIZE`
    weaknesses, then one `PATTERN_SYSTEM` call over all of them -- so a stub that replays one canned
    answer to every call would apply the same prose in every chunk. This one routes on the system
    prompt and filters its reply to the ids present in the payload, the way a real model would.

    `fail_prose_chunks` answers None for those 0-based chunk indices: exactly how a rate-limited
    provider behaves when one batch out of several is rejected.
    """

    def __init__(self, prose: dict | None = None, patterns: list | None = None,
                 fail_prose_chunks: tuple | set = (), fail_patterns: bool = False,
                 name: str = "echo:model-1", error: str = ""):
        self.prose = prose or {}
        self.patterns = patterns or []
        self.fail_prose_chunks = set(fail_prose_chunks)
        self.fail_patterns = fail_patterns
        self.last_error = error
        self._name = name
        self.calls: list[dict] = []
        self.prose_calls: list[str] = []
        self.pattern_calls: list[str] = []

    def available(self) -> bool:
        return True

    def describe(self) -> str:
        return self._name

    def chat_json(self, system: str, user: str, max_tokens: int = 700):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if system == S.PATTERN_SYSTEM:
            self.pattern_calls.append(user)
            return None if self.fail_patterns else {"systemic_patterns": list(self.patterns)}
        index = len(self.prose_calls)
        self.prose_calls.append(user)
        if index in self.fail_prose_chunks:
            return None
        return {"explanations": {wid: text for wid, text in self.prose.items()
                                 if f'"{wid}"' in user}}


class FakeSettings:
    """The four attributes `scout.build_context` actually reads."""

    def __init__(self, compose_file, nginx_conf="", knowledge_file="", thresholds_file=REAL_THRESHOLDS):
        self.compose_file = str(compose_file)
        self.nginx_conf = str(nginx_conf)
        self.knowledge_file = str(knowledge_file)
        self.thresholds_file = str(thresholds_file)


# ---------------------------------------------------------------- synthetic context helper
def make_ctx(services: dict, *, volumes: dict | None = None, knowledge: dict | None = None,
             nginx_text: str = "", policy: dict | None = None, project: str = "synthetic") -> S.ScoutContext:
    """A ScoutContext over a hand-written compose dict, built with the real parsers.

    The Topology is produced by faultline.topology.{compose,graph} exactly as `faultline topology`
    would produce it, so criticality / aliases / roles are not hand-faked.
    """
    compose = {"services": services, "volumes": volumes or {}}
    nodes = service_nodes(compose, knowledge or {})
    edges = static_edges(compose, nodes, None) + documented_edges(nodes, knowledge)
    topo = Topology(project=project, services=nodes, edges=edges, criticality=graph.criticality(nodes, edges))
    return S.ScoutContext(
        topology=topo,
        compose=compose,
        knowledge=knowledge or {},
        policy=Policy(policy or {}),
        compose_path="synthetic/docker-compose.yml",
        knowledge_path="synthetic/knowledge.yaml" if knowledge else None,
        nginx_text=nginx_text,
    )


def cat(raws: list[S._Raw], category: str) -> list[S._Raw]:
    return [r for r in raws if r.category == category]


def only(raws: list[S._Raw], category: str) -> S._Raw:
    hits = cat(raws, category)
    assert len(hits) == 1, f"expected exactly one {category}, got {[r.services for r in hits]}"
    return hits[0]


def _stateful_db(**extra) -> dict:
    svc = {"image": "mongo:7.0", "volumes": ["dbdata:/data/db"]}
    svc.update(extra)
    return svc


# ================================================================ check_health_checks
def test_missing_health_check_fires_on_critical_service_with_no_block():
    ctx = make_ctx({"db": _stateful_db()}, volumes={"dbdata": {}})
    raw = only(S.check_health_checks(ctx), "missing_health_check")
    assert raw.services == ["db"]
    assert raw.title == "db declares no health check"
    # evidence names precisely what is ABSENT, and why `restart:` is not a substitute
    assert "compose service `db`: no `healthcheck:` block" in raw.evidence
    assert "running but wedged is invisible to the platform" in raw.evidence


def test_missing_health_check_fires_on_explicitly_disabled_block_and_says_so():
    ctx = make_ctx({"db": _stateful_db(healthcheck={"disable": True})}, volumes={"dbdata": {}})
    raw = only(S.check_health_checks(ctx), "missing_health_check")
    # the evidence must distinguish "nobody wrote one" from "somebody switched it off"
    assert "compose service `db`: `healthcheck: {disable: true}`" in raw.evidence
    assert "no `healthcheck:` block" not in raw.evidence


def test_missing_health_check_silent_on_a_non_critical_service():
    # nothing depends on it, no state, no published port -> criticality 0.5, below CRITICAL_SCORE_FLOOR
    ctx = make_ctx({"lonely": {"image": "busybox:1.36"}})
    assert ctx.topology.criticality_of("lonely") < S.CRITICAL_SCORE_FLOOR
    assert ctx.is_critical("lonely") is False
    assert cat(S.check_health_checks(ctx), "missing_health_check") == []


def test_shallow_health_check_fires_on_liveness_only_probe_and_quotes_it():
    ctx = make_ctx({
        "db": _stateful_db(healthcheck={"test": "nc -z localhost 27017"}),
        "app": {"image": "eclipse/ditto-things:3.9.0", "environment": ["MONGO_DB_HOSTNAME=db"],
                "healthcheck": {"test": "curl --fail `hostname`:7626/alive || exit 1"}},
    }, volumes={"dbdata": {}})
    raw = only(S.check_health_checks(ctx), "shallow_health_check")
    assert raw.services == ["app"]
    # the evidence quotes the REAL healthcheck command, names the dependency it cannot see, and cites
    # the empirical precedent the prediction is modelled on
    assert "healthcheck test `curl --fail `hostname`:7626/alive || exit 1`" in raw.evidence
    assert "It never touches db" in raw.evidence
    assert "Ditto issue #1865" in raw.evidence
    assert "health_check_masked_failure" in raw.evidence
    # the suggested experiment breaks the DEPENDENCY, not the weak service
    assert raw.experiment_prefs[0][1] == "db"


@pytest.mark.parametrize("test_cmd", [
    "pgrep -f java",
    "ps -ef | grep -q java",
    "nc -z 127.0.0.1 8080",
    "curl -f localhost:7626/live || exit 1",
    "bash -c 'echo > /dev/tcp/127.0.0.1/8080'",
])
def test_shallow_health_check_recognises_every_liveness_only_shape(test_cmd):
    ctx = make_ctx({
        "db": _stateful_db(),
        "app": {"image": "app:1", "environment": ["DB_HOSTNAME=db"], "healthcheck": {"test": test_cmd}},
    }, volumes={"dbdata": {}})
    raw = only(S.check_health_checks(ctx), "shallow_health_check")
    assert f"healthcheck test `{test_cmd}`" in raw.evidence


@pytest.mark.parametrize("test_cmd", [
    "nc -z db 27017",                  # names the dependency by compose service name
    "nc -z mongo-primary 27017",       # ... or by its network alias
])
def test_shallow_health_check_silent_when_the_probe_names_the_dependency(test_cmd):
    ctx = make_ctx({
        "db": _stateful_db(networks={"default": {"aliases": ["mongo-primary"]}}),
        "app": {"image": "app:1", "environment": ["DB_HOSTNAME=db"], "healthcheck": {"test": test_cmd}},
    }, volumes={"dbdata": {}})
    assert ctx.topology.services["db"].aliases == ["mongo-primary"]
    assert cat(S.check_health_checks(ctx), "shallow_health_check") == []


@pytest.mark.parametrize("test_cmd", [
    "curl -f localhost:7626/alive && curl -f localhost:7626/status/health",  # liveness AND readiness
    "curl -f localhost:8080/ready || exit 1",
    "curl -f localhost:8080/readyz",
])
def test_shallow_health_check_silent_when_the_probe_looks_past_the_process(test_cmd):
    ctx = make_ctx({
        "db": _stateful_db(),
        "app": {"image": "app:1", "environment": ["DB_HOSTNAME=db"], "healthcheck": {"test": test_cmd}},
    }, volumes={"dbdata": {}})
    assert cat(S.check_health_checks(ctx), "shallow_health_check") == []


def test_shallow_health_check_never_fires_on_a_service_with_no_dependencies():
    """The deliberate mongodb carve-out.

    `mongosh ping` / `nc -z localhost` looks liveness-only, but a service with nothing to depend on
    cannot have a dependency-aware health check -- and a mongosh ping genuinely exercises its own
    storage engine. Firing here would be a false positive on the most critical service in the graph.
    """
    ctx = make_ctx({
        "db": _stateful_db(healthcheck={"test": "nc -z localhost 27017"}),
        "app": {"image": "app:1", "environment": ["DB_HOSTNAME=db"]},
    }, volumes={"dbdata": {}})
    assert ctx.topology.dependencies("db") == []          # db depends on nothing...
    assert ctx.topology.transitive_dependents("db") == ["app"]   # ... but app depends on db
    assert S.LIVENESS_ONLY_RE.search("nc -z localhost 27017")    # the probe DOES look liveness-only
    shallow = cat(S.check_health_checks(ctx), "shallow_health_check")
    assert [r.services for r in shallow] == []


def test_healthcheck_text_helper():
    assert S.healthcheck_text({}) is None
    assert S.healthcheck_text({"healthcheck": {"disable": True, "test": "curl x"}}) is None
    assert S.healthcheck_text({"healthcheck": {"test": None}}) is None
    assert S.healthcheck_text({"healthcheck": {"test": "   "}}) is None
    assert S.healthcheck_text({"healthcheck": "nonsense"}) is None
    assert S.healthcheck_text({"healthcheck": {"test": ["CMD", "curl", "-f", "x"]}}) == "CMD curl -f x"
    assert S.healthcheck_text({"healthcheck": {"test": "curl -f x"}}) == "curl -f x"


# ================================================================ check_redundancy
def _redundancy_ctx(**db_extra) -> S.ScoutContext:
    services = {"db": _stateful_db(**db_extra),
                "app": {"image": "app:1", "environment": ["DB_HOSTNAME=db"]},
                "side": {"image": "side:1"}}
    return make_ctx(services, volumes={"dbdata": {}})


def test_redundancy_fires_on_top_tier_single_instance_and_quotes_the_absence():
    ctx = _redundancy_ctx()
    raw = only(S.check_redundancy(ctx), "no_redundancy")
    assert raw.services == ["db"]
    assert raw.title == "db is a single instance with no redundancy"
    assert "compose service `db`: no `deploy.replicas` and no `scale:`" in raw.evidence
    assert "1 service(s) depend on it transitively (app)" in raw.evidence
    assert "the highest in this deployment" in raw.evidence
    assert "It also holds the only copy of its state." in raw.evidence


def test_redundancy_quotes_an_explicit_single_replica():
    raw = only(S.check_redundancy(_redundancy_ctx(deploy={"replicas": 1})), "no_redundancy")
    assert "compose service `db`: `deploy.replicas: 1`" in raw.evidence


@pytest.mark.parametrize("extra", [
    {"deploy": {"replicas": 2}},   # swarm-style
    {"scale": 2},                  # compose shorthand
    {"deploy": {"replicas": 3}},
])
def test_redundancy_silent_when_more_than_one_instance_is_declared(extra):
    assert cat(S.check_redundancy(_redundancy_ctx(**extra)), "no_redundancy") == []


def test_redundancy_silent_below_the_top_tier_fraction():
    ctx = _redundancy_ctx()
    max_crit = ctx.max_criticality()
    for low in ("app", "side"):
        assert ctx.topology.criticality_of(low) < S.TOP_TIER_FRACTION * max_crit
        assert ctx.is_top_tier(low) is False
    assert [r.services for r in cat(S.check_redundancy(ctx), "no_redundancy")] == [["db"]]


def test_replicas_helper_tolerates_junk():
    assert S._replicas({}) is None
    assert S._replicas({"deploy": {"replicas": 2}}) == 2
    assert S._replicas({"scale": "3"}) == 3
    assert S._replicas({"deploy": {"replicas": "two"}}) is None
    assert S._replicas({"deploy": {"replicas": 2}, "scale": 9}) == 2  # deploy wins


# ================================================================ check_resource_limits
def test_resource_limits_fires_when_both_limits_are_absent():
    ctx = make_ctx({"db": _stateful_db()}, volumes={"dbdata": {}})
    raw = only(S.check_resource_limits(ctx), "no_resource_limits")
    assert raw.title == "db has no memory or CPU limit"
    assert ("compose service `db`: no `deploy.resources.limits.memory` and no "
            "`deploy.resources.limits.cpus` and no `mem_limit`/`cpus` shorthand") in raw.evidence
    assert raw.weight_scale == 1.0


def test_resource_limits_half_fires_when_only_one_is_missing_and_halves_the_weight():
    ctx = make_ctx({"db": _stateful_db(mem_limit="512m")}, volumes={"dbdata": {}})
    raw = only(S.check_resource_limits(ctx), "no_resource_limits")
    assert raw.title == "db has no CPU limit"
    assert "no `deploy.resources.limits.cpus` (it does set memory limit `512m`)" in raw.evidence
    assert raw.weight_scale == 0.5


def test_resource_limits_weight_scale_changes_the_category_factor():
    """Half-covered must not score as if nothing were set: the `category` factor is the evidence."""
    both_missing = make_ctx({"db": _stateful_db()}, volumes={"dbdata": {}})
    one_missing = make_ctx({"db": _stateful_db(mem_limit="512m")}, volumes={"dbdata": {}})
    full = next(w for w in S.analyse(both_missing)[0] if w.category == "no_resource_limits")
    half = next(w for w in S.analyse(one_missing)[0] if w.category == "no_resource_limits")
    assert full.factors["category"] == S.CATEGORY_WEIGHT["no_resource_limits"] * 1.0 == 1.0
    assert half.factors["category"] == S.CATEGORY_WEIGHT["no_resource_limits"] * 0.5 == 0.5
    assert half.factors["category"] < full.factors["category"]


@pytest.mark.parametrize("svc_extra", [
    {"deploy": {"resources": {"limits": {"memory": "256m", "cpus": "1.5"}}}},   # long form
    {"mem_limit": "256m", "cpus": 1.5},                                         # shorthand
    {"mem_limit": "256m", "cpu_quota": 150000},                                 # cpu_quota counts too
    {"deploy": {"resources": {"limits": {"memory": "256m"}}}, "cpus": 1.5},     # mixed
])
def test_resource_limits_silent_when_both_are_set_in_either_notation(svc_extra):
    ctx = make_ctx({"db": _stateful_db(**svc_extra)}, volumes={"dbdata": {}})
    assert cat(S.check_resource_limits(ctx), "no_resource_limits") == []


def test_limits_helper_reads_both_notations():
    assert S._limits({}) == (None, None)
    assert S._limits({"mem_limit": "512m", "cpus": 2}) == ("512m", "2")
    assert S._limits({"deploy": {"resources": {"limits": {"memory": "1g", "cpus": "0.5"}}}}) == ("1g", "0.5")
    # the long form wins over the shorthand
    assert S._limits({"deploy": {"resources": {"limits": {"memory": "1g"}}}, "mem_limit": "9g"})[0] == "1g"


def test_resource_limits_silent_on_a_non_critical_service():
    ctx = make_ctx({"lonely": {"image": "busybox:1.36"}})
    assert cat(S.check_resource_limits(ctx), "no_resource_limits") == []


# ================================================================ check_restart_policy
@pytest.mark.parametrize("declared,expected_phrase", [
    ("no", "`restart: no`"),
    ("none", "`restart: none`"),
    (None, "no `restart:` policy at all"),
])
def test_restart_policy_fires_on_no_and_on_absent(declared, expected_phrase):
    extra = {} if declared is None else {"restart": declared}
    ctx = make_ctx({"db": _stateful_db(**extra)}, volumes={"dbdata": {}})
    raw = only(S.check_restart_policy(ctx), "weak_restart_policy")
    assert raw.title == "db will not be restarted after a crash"
    assert f"compose service `db`: {expected_phrase}" in raw.evidence
    assert "Compare `restart: always`" in raw.evidence


@pytest.mark.parametrize("declared", ["always", "unless-stopped", "on-failure", "on-failure:5"])
def test_restart_policy_silent_on_a_real_policy(declared):
    ctx = make_ctx({"db": _stateful_db(restart=declared)}, volumes={"dbdata": {}})
    assert cat(S.check_restart_policy(ctx), "weak_restart_policy") == []


def test_restart_policy_silent_on_a_non_critical_service():
    ctx = make_ctx({"lonely": {"image": "busybox:1.36"}})
    assert cat(S.check_restart_policy(ctx), "weak_restart_policy") == []


# ================================================================ check_retry_backoff
def _retry_ctx(a_env, b_env=("DB_HOSTNAME=db",), extra_services=None) -> S.ScoutContext:
    services = {
        "db": _stateful_db(),
        "a": {"image": "a:1", "environment": list(a_env)},
        "b": {"image": "b:1", "environment": list(b_env)},
    }
    services.update(extra_services or {})
    return make_ctx(services, volumes={"dbdata": {}})


def test_retry_backoff_fires_once_per_dependency_not_per_dependent():
    ctx = _retry_ctx(["DB_HOSTNAME=db"])
    raws = cat(S.check_retry_backoff(ctx), "no_retry_or_backoff")
    # two dependents reach `db`, but the gap belongs to the EDGE target -> one finding, not two
    assert len(raws) == 1
    raw = raws[0]
    assert raw.services == ["a", "b"]
    assert raw.title == "2 services reach db with no declared retry, timeout or backoff"
    assert "a, b depend on `db`" in raw.evidence
    assert "`a`: DB_HOSTNAME=db" in raw.evidence and "`b`: DB_HOSTNAME=db" in raw.evidence
    assert "no retry, backoff, circuit-breaker or timeout setting" in raw.evidence
    assert "nothing in the knowledge file documents one" in raw.evidence
    assert raw.experiment_prefs[0] == ("network_delay", "db")


@pytest.mark.parametrize("token", [
    "DB_URI=mongodb://db:27017/?serverSelectionTimeoutMS=2000",
    "DB_URI=mongodb://db:27017/?retryWrites=true",
    "DB_URI=mongodb://db:27017/?maxPoolSize=20",
    "DB_URI=mongodb://db:27017/?replicaSet=rs0",
])
def test_retry_backoff_silent_when_a_resilience_token_names_the_dependency(token):
    ctx = _retry_ctx(["DB_HOSTNAME=db", token], ["DB_HOSTNAME=db", token])
    assert cat(S.check_retry_backoff(ctx), "no_retry_or_backoff") == []


def test_retry_backoff_silent_with_fewer_than_two_dependents():
    services = {"db": _stateful_db(), "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"]}}
    ctx = make_ctx(services, volumes={"dbdata": {}})
    assert len(ctx.topology.transitive_dependents("db")) == 1
    assert cat(S.check_retry_backoff(ctx), "no_retry_or_backoff") == []


def test_retry_backoff_silent_when_only_one_edge_is_bare():
    # `a` is covered, `b` is not -> one bare edge, which is below the two-dependent bar
    ctx = _retry_ctx(["DB_HOSTNAME=db", "DB_URI=mongodb://db:27017/?retryWrites=true"], ["DB_HOSTNAME=db"])
    assert cat(S.check_retry_backoff(ctx), "no_retry_or_backoff") == []


def test_retry_backoff_notes_resilience_settings_that_are_not_on_this_edge():
    """The "sets X elsewhere, but nothing on this edge" aside.

    A service can show plenty of resilience thinking in a global setting (Ditto tunes the Pekko
    failure detector) and still say nothing about THIS dependency. The check must still fire, and the
    evidence must be fair about what it found. Unexercised on the real Ditto compose file, so this
    test is the only coverage of the branch.
    """
    ctx = _retry_ctx(["DB_HOSTNAME=db", "PEKKO_RETRY_LIMIT=5"])
    raw = only(S.check_retry_backoff(ctx), "no_retry_or_backoff")
    assert "`a`: DB_HOSTNAME=db (sets PEKKO_RETRY_LIMIT elsewhere, but nothing on this edge)" in raw.evidence
    # ... and `b`, which sets nothing of the kind, gets no aside
    assert "`b`: DB_HOSTNAME=db" in raw.evidence
    assert "PEKKO" not in raw.evidence.split("`b`:")[1]


def test_retry_backoff_aside_lists_at_most_two_settings():
    ctx = _retry_ctx(["DB_HOSTNAME=db", "A_RETRY=1", "B_BACKOFF=2", "C_CIRCUIT=3", "D_TIMEOUT=4"])
    raw = only(S.check_retry_backoff(ctx), "no_retry_or_backoff")
    aside = re.search(r"\(sets ([^)]*) elsewhere", raw.evidence)
    assert aside is not None
    assert len(aside.group(1).split(", ")) == 2


def test_retry_backoff_reports_when_nothing_at_all_names_the_dependency():
    services = {
        "db": _stateful_db(),
        "a": {"image": "a:1", "depends_on": ["db"]},
        "b": {"image": "b:1", "depends_on": ["db"]},
    }
    ctx = make_ctx(services, volumes={"dbdata": {}})
    raw = only(S.check_retry_backoff(ctx), "no_retry_or_backoff")
    assert "`a`: nothing naming db" in raw.evidence
    assert "`b`: nothing naming db" in raw.evidence


# ================================================================ check_published_ports
PORTS_SERVICES = {
    "nginx": {"image": "nginx:1.27", "ports": ["127.0.0.1:8080:80"], "depends_on": ["app"]},
    "app": {"image": "app:1"},
    "extra": {"image": "extra:1", "ports": ["9000:9000"]},
    "inner": {"image": "inner:1",
              "ports": [{"target": 9100, "published": 9100, "host_ip": "127.0.0.1"}]},
    "grafana": {"image": "grafana/grafana:11.3.0", "ports": ["127.0.0.1:3000:3000"]},
}
PORTS_KNOWLEDGE = {"roles": {"nginx": "proxy", "grafana": "other"}}


@pytest.fixture
def ports_ctx() -> S.ScoutContext:
    return make_ctx(PORTS_SERVICES, knowledge=PORTS_KNOWLEDGE)


def test_published_ports_identifies_the_proxy_as_the_ingress(ports_ctx):
    assert S._ingress(ports_ctx) == "nginx"


def test_published_ports_silent_for_the_declared_ingress(ports_ctx):
    flagged = {r.services[0] for r in cat(S.check_published_ports(ports_ctx), "port_beyond_ingress")}
    assert "nginx" not in flagged
    assert "app" not in flagged          # publishes nothing
    assert flagged == {"extra", "inner", "grafana"}


def test_published_ports_evidence_distinguishes_loopback_from_the_world(ports_ctx):
    raws = {r.services[0]: r for r in cat(S.check_published_ports(ports_ctx), "port_beyond_ingress")}
    assert "`ports: ['9000:9000']`" in raws["extra"].evidence
    assert "NOT bound to 127.0.0.1: reachable from outside the host" in raws["extra"].evidence
    # the long dict form is normalised back to host:published:target before being quoted
    assert "`ports: ['127.0.0.1:9100:9100']`" in raws["inner"].evidence
    assert "all bound to 127.0.0.1, so the sandbox boundary itself holds" in raws["inner"].evidence
    for r in raws.values():
        assert "while `nginx` is the declared entrypoint" in r.evidence


def test_published_ports_marks_a_target_outside_the_system_under_test(ports_ctx):
    raws = {r.services[0]: r for r in cat(S.check_published_ports(ports_ctx), "port_beyond_ingress")}
    assert ports_ctx.node("grafana").chaos_relevant is False
    assert "tagged outside the system under test" in raws["grafana"].evidence
    # an in-SUT service gets no such excuse
    assert "outside the system under test" not in raws["extra"].evidence

    # ... and its suggested experiment carries the "cannot be confirmed" note rather than a policy
    # complaint, because Faultline will not inject into something outside the SUT at all.
    se = S.resolve_experiment(raws["grafana"], ports_ctx)
    assert "tagged outside the system under test" in se.policy_note
    assert "role other" in se.policy_note
    assert "protected" not in se.policy_note


def test_published_ports_notes_when_there_is_no_single_ingress():
    ctx = make_ctx({"a": {"image": "a:1", "ports": ["8081:80"]},
                    "b": {"image": "b:1", "ports": ["8082:80"]}})
    assert S._ingress(ctx) is None
    flagged = {r.services[0] for r in cat(S.check_published_ports(ctx), "port_beyond_ingress")}
    assert flagged == {"a", "b"}
    assert any("no single declared ingress found" in n for n in ctx.notes)


def test_published_ports_helper_normalises_both_notations():
    assert S._published_ports({}) == []
    assert S._published_ports({"ports": ["8080:80", 9000]}) == ["8080:80", "9000"]
    assert S._published_ports({"ports": [{"target": 80, "published": 8080}]}) == ["8080:80"]
    assert S._published_ports(
        {"ports": [{"target": 80, "published": 8080, "host_ip": "127.0.0.1"}]}) == ["127.0.0.1:8080:80"]


# ================================================================ check_rate_limiting
RATE_SERVICES = {
    "nginx": {"image": "nginx:1.27", "ports": ["127.0.0.1:8080:80"], "depends_on": ["gateway"]},
    "gateway": {"image": "gateway:1"},
}


def test_rate_limiting_fires_when_the_proxy_config_sheds_nothing():
    conf = "server {\n  listen 80;\n  location /api { proxy_pass http://gateway:8080; }\n}\n"
    ctx = make_ctx(RATE_SERVICES, nginx_text=conf)
    raw = only(S.check_rate_limiting(ctx), "no_rate_limiting")
    assert raw.services == ["nginx"]
    assert raw.title == "nginx is the public entrypoint and applies no rate limiting or backpressure"
    assert "neither `limit_req`, `limit_conn` nor any rate-limit/throttle setting appears in " \
           "its mounted config" in raw.evidence
    assert "instead of being shed at the edge with a 429" in raw.evidence
    assert raw.experiment_prefs[0] == ("traffic_spike", "nginx")
    assert ctx.notes == []


@pytest.mark.parametrize("conf", [
    "limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;\nserver { location / { limit_req zone=api burst=20; } }",
    "server { location / { limit_conn addr 10; } }",
    "# throttle inbound traffic\nserver { }",
])
def test_rate_limiting_silent_when_the_proxy_config_does_shed(conf):
    assert S.check_rate_limiting(make_ctx(RATE_SERVICES, nginx_text=conf)) == []


def test_rate_limiting_silent_when_the_ingress_env_declares_a_limit():
    services = dict(RATE_SERVICES)
    services["nginx"] = {**services["nginx"], "environment": {"RATE_LIMIT_RPS": "100"}}
    assert S.check_rate_limiting(make_ctx(services, nginx_text="server { }")) == []


def test_rate_limiting_notes_when_no_proxy_config_was_readable():
    ctx = make_ctx(RATE_SERVICES, nginx_text="")
    raw = only(S.check_rate_limiting(ctx), "no_rate_limiting")
    assert "its environment (and no proxy config was available to read)" in raw.evidence
    assert any("no proxy config text was available for `nginx`" in n for n in ctx.notes)


def test_rate_limiting_silent_without_an_ingress():
    # two non-proxy publishers -> no single ingress -> the check has nothing to judge
    ctx = make_ctx({"a": {"image": "a:1", "ports": ["8081:80"]},
                    "b": {"image": "b:1", "ports": ["8082:80"]}})
    assert S.check_rate_limiting(ctx) == []


# ================================================================ scoring
def test_checks_tuple_is_the_seven_deterministic_checks():
    assert [name for name, _ in S.CHECKS] == [
        "health_checks", "redundancy", "resource_limits", "restart_policy",
        "retry_backoff", "published_ports", "rate_limiting",
    ]
    assert set(S.CATEGORY_WEIGHT) == set(S.CONFIRMING_CODES) == set(S.TEMPLATES)


def test_level_thresholds_sit_exactly_at_3_55_and_8():
    assert S._level(0.0) == "low"
    assert S._level(2.99) == "low"
    assert S._level(3.0) == "medium"
    assert S._level(5.49) == "medium"
    assert S._level(5.5) == "high"
    assert S._level(7.99) == "high"
    assert S._level(8.0) == "critical"
    assert S._level(10.0) == "critical"


def test_score_factor_names_and_bounds():
    ctx = make_ctx({"db": _stateful_db(), "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"]}},
                   volumes={"dbdata": {}})
    raw = only(S.check_redundancy(ctx), "no_redundancy")
    factors = S.score(raw, ctx, {"db": 1})
    assert set(factors) == {"criticality", "category", "blast", "compounding", "stateful", "protected"}
    assert 0.0 <= factors["criticality"] <= 3.5
    assert factors["criticality"] == 3.5           # db is the most critical service here
    assert factors["category"] == S.CATEGORY_WEIGHT["no_redundancy"]
    assert 0.0 <= factors["blast"] <= 2.0
    assert 0.0 <= factors["compounding"] <= 1.5
    assert factors["stateful"] == 1.0
    assert factors["protected"] == 0.0


def test_protected_factor_is_set_from_the_safety_policy(policy_raw):
    ctx = make_ctx({"mongodb": _stateful_db(), "a": {"image": "a:1", "environment": ["MONGO_DB_HOSTNAME=mongodb"]}},
                   volumes={"dbdata": {}}, policy=policy_raw)
    assert ctx.is_protected("mongodb") is True
    raw = only(S.check_redundancy(ctx), "no_redundancy")
    assert S.score(raw, ctx, {"mongodb": 1})["protected"] == 0.5


def test_severity_is_the_clamped_sum_of_the_named_factors():
    ctx = make_ctx({"db": _stateful_db(), "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"]},
                    "b": {"image": "b:1", "environment": ["DB_HOSTNAME=db"]}}, volumes={"dbdata": {}})
    for w in S.analyse(ctx)[0]:
        expected = round(min(max(sum(w.factors.values()), 0.0), 10.0), 2)
        assert w.predicted_severity == expected
        assert 0.0 <= w.predicted_severity <= 10.0
        assert w.predicted_level == S._level(w.predicted_severity)


def test_compounding_rises_when_one_service_fails_several_checks():
    """A service that fails four checks is a worse bet than one that fails only redundancy."""
    many = make_ctx({  # no healthcheck, no restart, no limits, one instance
        "db": _stateful_db(),
        "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"]},
    }, volumes={"dbdata": {}})
    one = make_ctx({   # everything in order except that it is still a single instance
        "db": _stateful_db(restart="always", mem_limit="256m", cpus=1.5,
                           healthcheck={"test": "nc -z localhost 27017"}),
        "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"]},
    }, volumes={"dbdata": {}})

    many_cats = {w.category for w in S.analyse(many)[0] if "db" in w.services}
    one_cats = {w.category for w in S.analyse(one)[0] if "db" in w.services}
    assert many_cats == {"missing_health_check", "no_redundancy", "no_resource_limits", "weak_restart_policy"}
    assert one_cats == {"no_redundancy"}

    many_red = next(w for w in S.analyse(many)[0] if w.weakness_id == "no_redundancy:db")
    one_red = next(w for w in S.analyse(one)[0] if w.weakness_id == "no_redundancy:db")
    assert one_red.factors["compounding"] == 0.0
    assert many_red.factors["compounding"] == 1.5   # 0.5 * min(4 - 1, 3), the cap
    assert many_red.factors["compounding"] > one_red.factors["compounding"]
    assert many_red.predicted_severity > one_red.predicted_severity
    # ... and the only factor that moved is the compounding one
    assert {k: v for k, v in many_red.factors.items() if k != "compounding"} == \
           {k: v for k, v in one_red.factors.items() if k != "compounding"}


def test_analyse_sorts_by_descending_severity_and_builds_stable_ids():
    ctx = make_ctx({"db": _stateful_db(), "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"]},
                    "b": {"image": "b:1", "environment": ["DB_HOSTNAME=db"]}}, volumes={"dbdata": {}})
    weaknesses, checks_run = S.analyse(ctx)
    assert checks_run == [name for name, _ in S.CHECKS]
    sev = [w.predicted_severity for w in weaknesses]
    assert sev == sorted(sev, reverse=True)
    for w in weaknesses:
        assert w.weakness_id == f"{w.category}:{'+'.join(w.services)}"
    assert len({w.weakness_id for w in weaknesses}) == len(weaknesses)


# ================================================================ resolve_experiment
def test_resolve_experiment_always_returns_a_catalog_type_applicable_to_the_target():
    ctx = make_ctx({"db": _stateful_db(), "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"]},
                    "b": {"image": "b:1", "environment": ["DB_HOSTNAME=db"]},
                    "nginx": {"image": "nginx:1.27", "ports": ["127.0.0.1:8080:80"], "depends_on": ["a"]}},
                   volumes={"dbdata": {}})
    weaknesses, _ = S.analyse(ctx)
    assert weaknesses
    for w in weaknesses:
        se = w.suggested_experiment
        assert se.failure_type in CATALOG, se
        if not se.policy_note:
            # a clean resolution must be runnable: real type, right role, allowed by the policy
            assert applicable(CATALOG[se.failure_type], ctx.node(se.target_service)), se
            assert ctx.policy.allows(se.target_service, se.failure_type)[0] is True, se
        assert se.reason


def test_mongodb_no_redundancy_resolves_to_a_degrade_only_experiment(policy_raw):
    """MongoDB is protected and degrade-only in config/thresholds.yaml.

    A `no_redundancy` prediction prefers `container_stop` first -- which the policy forbids. The
    prediction must still nominate something RUNNABLE, which is what makes it actionable.
    """
    ctx = make_ctx({"mongodb": _stateful_db(),
                    "a": {"image": "a:1", "environment": ["MONGO_DB_HOSTNAME=mongodb"]},
                    "b": {"image": "b:1", "environment": ["MONGO_DB_HOSTNAME=mongodb"]}},
                   volumes={"dbdata": {}}, policy=policy_raw)
    raw = only(S.check_redundancy(ctx), "no_redundancy")
    assert raw.experiment_prefs[0] == ("container_stop", "mongodb")   # the preference IS the outage
    se = S.resolve_experiment(raw, ctx)
    assert se.target_service == "mongodb"
    assert se.failure_type != "container_stop"
    assert se.failure_type == "network_delay"
    assert CATALOG[se.failure_type].degrade_only is True
    assert se.failure_type in policy_raw["policy"]["protected"]["mongodb"]["allowed_failure_types"]
    # It resolved to a runnable experiment, but a substitution is not a clean resolution: the note
    # must say that the direct test was refused, or the record would read "take the single instance
    # away" next to a latency injection that deliberately leaves it running.
    assert "container_stop" in se.policy_note and "direct test" in se.policy_note
    assert "--allow-protected" in se.policy_note


def test_resolve_experiment_explains_a_policy_that_blocks_everything(policy_raw):
    ctx = make_ctx({"mongodb": _stateful_db(), "a": {"image": "a:1", "environment": ["MONGO_DB_HOSTNAME=mongodb"]}},
                   volumes={"dbdata": {}}, policy=policy_raw)
    raw = S._Raw(category="no_redundancy", services=["mongodb"], title="t", evidence="e",
                 experiment_prefs=[("container_stop", "mongodb"), ("container_crash", "mongodb")],
                 experiment_reason="take it away")
    se = S.resolve_experiment(raw, ctx)
    assert se.failure_type == "container_stop"     # falls back to the first preference...
    assert "mongodb is protected" in se.policy_note
    assert "--allow-protected" in se.policy_note   # ... but says plainly that it cannot be run


def test_resolve_experiment_explains_when_no_catalog_type_applies():
    ctx = make_ctx({"app": {"image": "app:1"}})
    raw = S._Raw(category="no_resource_limits", services=["app"], title="t", evidence="e",
                 experiment_prefs=[("db_flood", "app")], experiment_reason="load it")
    se = S.resolve_experiment(raw, ctx)
    assert applicable(CATALOG["db_flood"], ctx.node("app")) is False
    assert se.failure_type == "db_flood"
    assert "no catalog failure type applies to this target's role" in se.policy_note


def test_resolve_experiment_skips_unknown_types_and_unknown_targets():
    ctx = make_ctx({"app": {"image": "app:1"}})
    raw = S._Raw(category="no_redundancy", services=["app"], title="t", evidence="e",
                 experiment_prefs=[("teleport_the_rack", "app"), ("container_stop", "ghost"),
                                   ("container_stop", "app")],
                 experiment_reason="r")
    se = S.resolve_experiment(raw, ctx)
    assert (se.failure_type, se.target_service) == ("container_stop", "app")
    assert se.policy_note == ""


# ================================================================ confirming finding codes
def test_every_confirming_code_is_one_the_injector_can_actually_raise():
    """A prediction naming a code nothing can emit could never be confirmed."""
    from faultline.agents import injector

    emitted = set(re.findall(r'code="(\w+)"', Path(injector.__file__).read_text()))
    assert "health_check_masked_failure" in emitted      # sanity: the grep found something real
    declared = {c for codes in S.CONFIRMING_CODES.values() for c in codes}
    assert declared, "CONFIRMING_CODES is empty"
    assert declared <= emitted, f"codes no injector finding can raise: {sorted(declared - emitted)}"


def test_confirming_codes_are_copied_onto_every_prediction():
    ctx = make_ctx({"db": _stateful_db(), "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"]},
                    "b": {"image": "b:1", "environment": ["DB_HOSTNAME=db"]}}, volumes={"dbdata": {}})
    weaknesses, _ = S.analyse(ctx)
    assert weaknesses
    for w in weaknesses:
        assert w.confirming_finding_codes == S.CONFIRMING_CODES[w.category]
        assert w.confirming_finding_codes          # never empty
        # info-severity `auto_recovered` is deliberately not a confirmation signal
        assert "auto_recovered" not in w.confirming_finding_codes
        assert "safety_abort" not in w.confirming_finding_codes


# ================================================================ invariant 1: the LLM cannot decide
@pytest.fixture
def llm_ctx() -> S.ScoutContext:
    """A context rich enough to produce several weaknesses across several categories."""
    return make_ctx({
        "db": _stateful_db(),
        "a": {"image": "a:1", "environment": ["DB_HOSTNAME=db"],
              "healthcheck": {"test": "curl -f localhost:7626/alive"}},
        "b": {"image": "b:1", "environment": ["DB_HOSTNAME=db"]},
        "nginx": {"image": "nginx:1.27", "ports": ["127.0.0.1:8080:80"], "depends_on": ["a"]},
    }, volumes={"dbdata": {}}, nginx_text="server { listen 80; }")


def _shape(report: WeaknessReport) -> list[tuple]:
    """Everything the LLM is forbidden to touch."""
    return [(w.weakness_id, w.category, w.predicted_severity, w.predicted_level,
             tuple(w.services), w.evidence, w.title, tuple(sorted(w.factors.items())),
             w.suggested_experiment.failure_type, w.suggested_experiment.target_service,
             tuple(w.confirming_finding_codes)) for w in report.weaknesses]


def test_llm_cannot_add_a_weakness_however_it_answers(llm_ctx):
    """Unknown weakness ids in `explanations` are silently dropped: the finding list is final."""
    baseline = S.scout(llm_ctx, None)
    real_ids = [w.weakness_id for w in baseline.weaknesses]
    assert len(real_ids) >= 4

    # a canned answer replayed to every prose chunk, carrying ids the checks never produced
    llm = FakeLLM({"explanations": {
        **{wid: f"prose for {wid}" for wid in real_ids},
        "no_redundancy:does-not-exist": "mongodb has no replica set",
        "missing_health_check:ghost-service": "a service that is not in the compose file",
        "totally_made_up_category:db": "a category that is not even in CATEGORY_WEIGHT",
    }})
    with_llm = S.scout(llm_ctx, llm)

    assert [w.weakness_id for w in with_llm.weaknesses] == real_ids
    assert [w.category for w in with_llm.weaknesses] == [w.category for w in baseline.weaknesses]
    assert [w.predicted_severity for w in with_llm.weaknesses] == \
           [w.predicted_severity for w in baseline.weaknesses]
    assert _shape(with_llm) == _shape(baseline)
    # the one thing it IS allowed to change
    assert all(w.why_it_matters == f"prose for {w.weakness_id}" for w in with_llm.weaknesses)
    assert all(w.generated_by == "llm+rules" for w in with_llm.weaknesses)
    assert with_llm.generated_by == "llm+rules"


def test_llm_cannot_remove_or_rerank_a_weakness(llm_ctx):
    baseline = S.scout(llm_ctx, None)
    # a model that answers about exactly one of the findings it was shown
    llm = EchoLLM(prose={baseline.weaknesses[-1].weakness_id: "only this one"})
    with_llm = S.scout(llm_ctx, llm)
    assert _shape(with_llm) == _shape(baseline)
    assert with_llm.weaknesses[-1].why_it_matters == "only this one"
    assert with_llm.weaknesses[0].why_it_matters == baseline.weaknesses[0].why_it_matters
    # and the report says how much of the prose is actually the model's
    assert any(f"wrote prose for 1/{len(baseline.weaknesses)}" in n for n in with_llm.notes)


def test_llm_prose_longer_than_the_cap_is_rejected_and_the_template_survives(llm_ctx):
    baseline = S.scout(llm_ctx, None)
    ids = [w.weakness_id for w in baseline.weaknesses]
    llm = EchoLLM(prose={
        ids[0]: "x" * (S.MAX_PROSE_CHARS + 1),   # one character over the cap -> rejected
        ids[1]: "y" * S.MAX_PROSE_CHARS,         # exactly at the cap -> accepted
        ids[2]: "   ",                           # empty after stripping -> rejected
        ids[3]: 42,                              # not a string -> rejected
    })
    report = S.scout(llm_ctx, llm)
    by_id = {w.weakness_id: w for w in report.weaknesses}
    assert by_id[ids[0]].why_it_matters == baseline.weaknesses[0].why_it_matters
    assert by_id[ids[0]].generated_by == "rules"
    assert by_id[ids[1]].why_it_matters == "y" * S.MAX_PROSE_CHARS
    assert by_id[ids[1]].generated_by == "llm+rules"
    assert by_id[ids[2]].generated_by == "rules"
    assert by_id[ids[3]].generated_by == "rules"
    assert _shape(report) == _shape(baseline)


def test_apply_prose_accepts_only_known_ids_and_well_formed_sentences(llm_ctx):
    weaknesses, _ = S.analyse(llm_ctx)
    known = {w.weakness_id: w for w in weaknesses}
    ids = list(known)
    templated = {wid: w.why_it_matters for wid, w in known.items()}

    assert S._apply_prose({}, known) == 0
    assert S._apply_prose({"explanations": "not a dict"}, known) == 0
    assert S._apply_prose({"explanations": {"ghost:1": "invented"}}, known) == 0
    assert S._apply_prose({"explanations": {ids[0]: "x" * (S.MAX_PROSE_CHARS + 1)}}, known) == 0
    assert S._apply_prose({"explanations": {ids[0]: ""}}, known) == 0
    assert S._apply_prose({"explanations": {ids[0]: None}}, known) == 0
    # all of the above left every record exactly as the templates wrote it
    assert {wid: w.why_it_matters for wid, w in known.items()} == templated
    assert all(w.generated_by == "rules" for w in known.values())

    assert S._apply_prose({"explanations": {ids[0]: "  a real sentence  ", ids[1]: "another"}}, known) == 2
    assert known[ids[0]].why_it_matters == "a real sentence"      # stripped
    assert known[ids[0]].generated_by == "llm+rules"
    assert known[ids[2]].generated_by == "rules"


def test_collect_patterns_only_accepts_patterns_built_from_the_real_findings(llm_ctx):
    weaknesses, _ = S.analyse(llm_ctx)
    known = {w.weakness_id: w for w in weaknesses}
    ids = list(known)

    assert S._collect_patterns({}, known) == []
    assert S._collect_patterns({"systemic_patterns": [{"title": "t", "weakness_ids": ids[:2]}]}, known) == []
    good = S._collect_patterns({"systemic_patterns": [
        {"title": "t", "weakness_ids": [ids[0], ids[1], "ghost:1"], "narrative": "n"}]}, known)
    assert len(good) == 1
    assert good[0].weakness_ids == [ids[0], ids[1]]       # the invented id is filtered out
    assert good[0].narrative == f"{S.LLM_EXTRA_LABEL} n"


def test_templated_prose_comes_from_TEMPLATES_for_every_category(llm_ctx):
    report = S.scout(llm_ctx, None)
    for w in report.weaknesses:
        svc = w.services[0] if len(w.services) == 1 else ", ".join(w.services)
        assert w.why_it_matters == S.TEMPLATES[w.category].format(svc=svc)
        assert svc in w.why_it_matters


@pytest.mark.parametrize("pattern,why", [
    ({"title": "t", "weakness_ids": ["no_such_weakness:x", "another:y"], "narrative": "n"},
     "every cited id is invented"),
    ({"title": "t", "weakness_ids": [], "narrative": "n"}, "no ids at all"),
    ({"title": "t", "weakness_ids": ["__ONE_REAL__"], "narrative": "n"}, "only one valid id"),
    ({"title": "t", "weakness_ids": ["__ONE_REAL__", "ghost:1"], "narrative": "n"},
     "one real plus one invented leaves one valid"),
    ({"title": "", "weakness_ids": ["__ONE_REAL__", "__TWO_REAL__"], "narrative": "n"}, "no title"),
    ({"title": "t", "weakness_ids": ["__ONE_REAL__", "__TWO_REAL__"], "narrative": ""}, "no narrative"),
    ({"title": "t", "weakness_ids": ["__ONE_REAL__", "__TWO_REAL__"], "narrative": "z" * 601},
     "narrative over MAX_PROSE_CHARS"),
    ("not even a dict", "not a mapping"),
])
def test_invalid_systemic_patterns_are_dropped(llm_ctx, pattern, why):
    baseline = S.scout(llm_ctx, None)
    ids = [w.weakness_id for w in baseline.weaknesses]
    if isinstance(pattern, dict):
        pattern = {**pattern, "weakness_ids": [
            ids[0] if i == "__ONE_REAL__" else ids[1] if i == "__TWO_REAL__" else i
            for i in pattern["weakness_ids"]]}
    report = S.scout(llm_ctx, EchoLLM(patterns=[pattern]))
    assert report.systemic_patterns == [], why
    assert _shape(report) == _shape(baseline)


def test_accepted_systemic_patterns_are_capped_and_labelled(llm_ctx):
    baseline = S.scout(llm_ctx, None)
    ids = [w.weakness_id for w in baseline.weaknesses]
    patterns = [{"title": f"pattern {i}", "weakness_ids": [ids[0], ids[1]],
                 "narrative": f"narrative {i}"} for i in range(4)]
    report = S.scout(llm_ctx, EchoLLM(patterns=patterns))

    assert S.MAX_SYSTEMIC_PATTERNS == 2
    assert len(report.systemic_patterns) == S.MAX_SYSTEMIC_PATTERNS
    assert [p.title for p in report.systemic_patterns] == ["pattern 0", "pattern 1"]
    for p in report.systemic_patterns:
        assert p.narrative.startswith(S.LLM_EXTRA_LABEL)
        assert S.LLM_EXTRA_LABEL == "[LLM suggestion, unverified]"
        assert p.generated_by == "llm"
        assert set(p.weakness_ids) <= set(ids)
    # a pattern is commentary, never a finding: the deterministic list is untouched
    assert _shape(report) == _shape(baseline)
    assert report.generated_by == "llm+rules"


def test_long_pattern_titles_are_truncated(llm_ctx):
    ids = [w.weakness_id for w in S.scout(llm_ctx, None).weaknesses]
    report = S.scout(llm_ctx, EchoLLM(patterns=[
        {"title": "T" * 500, "weakness_ids": ids[:2], "narrative": "n"}]))
    assert len(report.systemic_patterns[0].title) == 160


# ---------------------------------------------------------------- the prompts
def test_prose_is_requested_in_small_chunks_and_patterns_in_one_call(llm_ctx):
    weaknesses = S.scout(llm_ctx, None).weaknesses
    ids = [w.weakness_id for w in weaknesses]
    assert len(ids) > S.LLM_CHUNK_SIZE          # otherwise this test proves nothing about chunking

    llm = EchoLLM()
    S.scout(llm_ctx, llm)
    expected_chunks = -(-len(ids) // S.LLM_CHUNK_SIZE)
    assert len(llm.prose_calls) == expected_chunks
    assert len(llm.pattern_calls) == 1
    assert len(llm.calls) == expected_chunks + 1

    prose = [c for c in llm.calls if c["system"] == S.PROSE_SYSTEM]
    assert len(prose) == expected_chunks
    assert all(c["max_tokens"] == S.PROSE_MAX_TOKENS for c in prose)
    pattern = [c for c in llm.calls if c["system"] == S.PATTERN_SYSTEM]
    assert [c["max_tokens"] for c in pattern] == [S.PATTERN_MAX_TOKENS]

    # every finding is described in exactly one prose chunk, none more than LLM_CHUNK_SIZE long
    for wid in ids:
        assert sum(f'"{wid}"' in u for u in llm.prose_calls) == 1
    for u in llm.prose_calls:
        assert sum(f'"{wid}"' in u for wid in ids) <= S.LLM_CHUNK_SIZE
    # ... and the pattern pass sees all of them at once, so it can spot a shared story
    assert all(f'"{wid}"' in llm.pattern_calls[0] for wid in ids)


def test_both_system_prompts_forbid_changing_the_finding_list(llm_ctx):
    llm = EchoLLM()
    S.scout(llm_ctx, llm)
    assert "cannot add to it, remove from it, re-rank it or re-score it" in S.PROSE_SYSTEM
    assert "Nothing has been measured" in S.PROSE_SYSTEM
    assert "nothing has been measured" in S.PATTERN_SYSTEM
    assert "may cite no id that is not in it" in S.PATTERN_SYSTEM
    assert "Do not invent a weakness that is not listed" in S.PATTERN_SYSTEM
    assert "predicted from static configuration; nothing has been injected or measured" in llm.prose_calls[0]


def test_prose_payload_excerpts_the_evidence_without_truncating_the_record(llm_ctx):
    weaknesses, _ = S.analyse(llm_ctx)
    long_evidence = [w for w in weaknesses if len(w.evidence) > S.EVIDENCE_EXCERPT_CHARS]
    assert long_evidence, "expected at least one finding with evidence longer than the excerpt"

    sent = {w["weakness_id"]: w["evidence"]
            for w in json.loads(S._prose_payload(weaknesses, llm_ctx))["weaknesses"]}
    assert set(sent) == {w.weakness_id for w in weaknesses}
    for w in long_evidence:
        # the prompt only gets the gist, with an ellipsis to say so
        assert sent[w.weakness_id] == w.evidence[:S.EVIDENCE_EXCERPT_CHARS - 1] + "…"
        assert len(sent[w.weakness_id]) == S.EVIDENCE_EXCERPT_CHARS
    for w in weaknesses:
        if len(w.evidence) <= S.EVIDENCE_EXCERPT_CHARS:
            assert sent[w.weakness_id] == w.evidence       # short evidence goes through untouched
    # the full evidence survives on the record, LLM or no LLM
    report = S.scout(llm_ctx, EchoLLM(prose={w.weakness_id: "short prose" for w in weaknesses}))
    for w, b in zip(report.weaknesses, weaknesses):
        assert w.evidence == b.evidence
        assert len(w.evidence) == len(b.evidence)


# ---------------------------------------------------------------- the fallback path
def test_fallback_with_no_client_at_all(llm_ctx):
    report = S.scout(llm_ctx, None)
    assert report.weaknesses and report.generated_by == "rules"
    assert all(w.generated_by == "rules" for w in report.weaknesses)
    assert report.systemic_patterns == []
    assert any("LLM unavailable (no client)" in n for n in report.notes)
    assert any(f"templated prose used for all {len(report.weaknesses)} prediction(s)" in n
               for n in report.notes)


def test_fallback_when_the_model_is_unreachable(llm_ctx):
    """Build-order requirement: make the model unreachable and prove every prediction still ships."""
    baseline = S.scout(llm_ctx, None)
    llm = FakeLLM(ok=False, error="connection refused to http://127.0.0.1:11434")
    report = S.scout(llm_ctx, llm)

    assert llm.calls == []                       # unavailable clients are never called
    assert _shape(report) == _shape(baseline)
    assert report.generated_by == "rules"
    assert all(w.generated_by == "rules" for w in report.weaknesses)
    assert all(w.why_it_matters == b.why_it_matters
               for w, b in zip(report.weaknesses, baseline.weaknesses))
    assert any("connection refused" in n for n in report.notes)


def test_fallback_when_a_disabled_client_says_nothing_about_why(llm_ctx):
    report = S.scout(llm_ctx, FakeLLM(ok=False, error=""))
    assert any("LLM unavailable (disabled)" in n for n in report.notes)
    assert all(w.generated_by == "rules" for w in report.weaknesses)


@pytest.mark.parametrize("answer", [None, {}, [], 0, ""])
def test_fallback_when_every_answer_is_unusable(llm_ctx, answer):
    baseline = S.scout(llm_ctx, None)
    report = S.scout(llm_ctx, FakeLLM(answer, error="invalid JSON from model"))
    assert _shape(report) == _shape(baseline)
    assert report.generated_by == "rules"
    assert report.systemic_patterns == []
    assert all(w.generated_by == "rules" for w in report.weaknesses)
    assert all(w.why_it_matters == b.why_it_matters
               for w, b in zip(report.weaknesses, baseline.weaknesses))
    # both passes report their own failure, each naming the provider error
    assert any("prose batch(es) (invalid JSON from model)" in n for n in report.notes)
    assert any("gave no usable systemic-pattern answer" in n for n in report.notes)


def test_fallback_when_the_answer_has_the_wrong_inner_types(llm_ctx):
    baseline = S.scout(llm_ctx, None)
    report = S.scout(llm_ctx, FakeLLM({"explanations": ["not", "a", "dict"],
                                       "systemic_patterns": "not a list"}))
    assert _shape(report) == _shape(baseline)
    assert report.systemic_patterns == []
    assert all(w.generated_by == "rules" for w in report.weaknesses)
    assert any("wrote prose for 0/" in n for n in report.notes)


def test_one_rate_limited_chunk_costs_only_its_own_chunk(real_ctx):
    """What the chunking is for: a 429 on one batch must not cost the other batches' prose."""
    baseline = S.scout(real_ctx, None)
    ids = [w.weakness_id for w in baseline.weaknesses]
    n_chunks = -(-len(ids) // S.LLM_CHUNK_SIZE)
    assert n_chunks >= 3, f"need at least three chunks to prove independence, got {n_chunks}"

    llm = EchoLLM(prose={wid: f"model prose for {wid}" for wid in ids},
                  fail_prose_chunks={1}, error="429 rate limit exceeded")
    report = S.scout(real_ctx, llm)

    failed_ids = set(ids[S.LLM_CHUNK_SIZE:2 * S.LLM_CHUNK_SIZE])
    for w in report.weaknesses:
        if w.weakness_id in failed_ids:
            assert w.generated_by == "rules"
            assert w.why_it_matters == baseline.by_id(w.weakness_id).why_it_matters
        else:
            assert w.generated_by == "llm+rules"
            assert w.why_it_matters == f"model prose for {w.weakness_id}"

    assert any(f"failed on 1 of {n_chunks} prose batch(es) (429 rate limit exceeded)" in n
               for n in report.notes)
    assert report.generated_by == "llm+rules"
    assert _shape(report) == _shape(baseline)


def test_systemic_patterns_survive_a_prose_pass_that_failed_completely(real_ctx):
    baseline = S.scout(real_ctx, None)
    ids = [w.weakness_id for w in baseline.weaknesses]
    n_chunks = -(-len(ids) // S.LLM_CHUNK_SIZE)
    llm = EchoLLM(prose={wid: "never delivered" for wid in ids},
                  patterns=[{"title": "One unlimited-resource story", "weakness_ids": ids[:3],
                             "narrative": "the same gap on three services"}],
                  fail_prose_chunks=set(range(n_chunks)), error="429 rate limit exceeded")
    report = S.scout(real_ctx, llm)

    assert all(w.generated_by == "rules" for w in report.weaknesses)
    assert all(w.why_it_matters == b.why_it_matters
               for w, b in zip(report.weaknesses, baseline.weaknesses))
    assert len(report.systemic_patterns) == 1
    assert report.systemic_patterns[0].narrative.startswith(S.LLM_EXTRA_LABEL)
    assert report.generated_by == "llm+rules"       # the pattern pass did contribute
    assert any(f"failed on {n_chunks} of {n_chunks} prose batch(es)" in n for n in report.notes)
    assert _shape(report) == _shape(baseline)


def test_prose_survives_a_pattern_pass_that_failed(llm_ctx):
    ids = [w.weakness_id for w in S.scout(llm_ctx, None).weaknesses]
    llm = EchoLLM(prose={wid: f"prose {wid}" for wid in ids}, fail_patterns=True,
                  error="429 rate limit exceeded")
    report = S.scout(llm_ctx, llm)
    assert all(w.generated_by == "llm+rules" for w in report.weaknesses)
    assert report.systemic_patterns == []
    assert any("gave no usable systemic-pattern answer (429 rate limit exceeded)" in n
               for n in report.notes)
    assert report.generated_by == "llm+rules"


def test_llm_pass_returns_cleanly_with_no_weaknesses_to_explain():
    ctx = make_ctx({"lonely": {"image": "busybox:1.36", "restart": "always",
                               "deploy": {"replicas": 2}}})
    assert S.analyse(ctx)[0] == []
    llm = EchoLLM(prose={"x": "y"})
    assert S._llm_pass(llm, [], ctx) == (False, [], [])
    assert llm.calls == []


# The two inputs below are the only ways `_llm_pass` could break its own "Never raises" contract.
# Both were real: a truthy non-dict answer reached `_apply_prose` and `data.get` raised
# AttributeError, and `chat_json` was called outside any try/except, so a client that raised took
# all 21 deterministic predictions with it over a cosmetic prose call. Both are now guarded; these
# tests are what keeps them guarded.
@pytest.mark.parametrize("answer", [["explanations"], "some prose", 7.5])
def test_fallback_when_chat_json_returns_a_non_dict(llm_ctx, answer):
    baseline = S.scout(llm_ctx, None)
    report = S.scout(llm_ctx, FakeLLM(answer))
    assert _shape(report) == _shape(baseline)
    assert report.generated_by == "rules"
    assert all(w.generated_by == "rules" for w in report.weaknesses)


def test_fallback_when_chat_json_raises(llm_ctx):
    baseline = S.scout(llm_ctx, None)
    report = S.scout(llm_ctx, FakeLLM(raises=RuntimeError("read timed out")))
    assert _shape(report) == _shape(baseline)
    assert report.generated_by == "rules"
    assert all(w.generated_by == "rules" for w in report.weaknesses)


# ================================================================ invariant 2: always labelled
def test_every_prediction_carries_the_predicted_label(llm_ctx):
    assert PREDICTED_LABEL == "[predicted, unconfirmed]"
    for llm in (None, FakeLLM({"explanations": {}, "systemic_patterns": []})):
        report = S.scout(llm_ctx, llm)
        assert report.label == PREDICTED_LABEL
        assert report.weaknesses
        for w in report.weaknesses:
            assert w.label == PREDICTED_LABEL
            assert w.status == "predicted"


def test_markdown_states_in_words_that_the_severities_are_predicted_not_measured(llm_ctx):
    md = S.render_markdown(S.scout(llm_ctx, None))
    assert PREDICTED_LABEL in md
    assert "every severity below is PREDICTED from configuration" in md
    assert "Nothing here was measured: no fault has been injected" in md
    assert "must not be read as, the Risk agent's post-hoc measured severities" in md
    assert "faultline scout --reconcile" in md
    # ... and it repeats the disclaimer next to every single number, not only once at the top
    n = len(S.scout(llm_ctx, None).weaknesses)
    assert md.count("_(predicted, not measured)_") == n
    assert md.count("predicted severity") == n


def test_markdown_renders_the_whole_finding(llm_ctx):
    report = S.scout(llm_ctx, None)
    md = S.render_markdown(report)
    assert md.startswith("# Predicted weaknesses — static analysis")
    assert f"**Compose:** `{report.compose_file}`" in md
    assert f"{len(report.weaknesses)} prediction(s) from 7 deterministic check(s)" in md
    for i, w in enumerate(report.weaknesses, 1):
        assert f"## {i}. {w.title}" in md
        assert f"`{w.category}` · **{w.predicted_level.upper()}**" in md
        assert w.evidence in md
        assert f"**Why it matters.** {w.why_it_matters}" in md
        assert f"`{w.suggested_experiment.failure_type}` on `{w.suggested_experiment.target_service}`" in md
        for code in w.confirming_finding_codes:
            assert f"`{code}`" in md
    assert "## Notes" in md
    assert md.endswith("\n")


def test_markdown_frames_systemic_patterns_as_additive_commentary(llm_ctx):
    ids = [w.weakness_id for w in S.scout(llm_ctx, None).weaknesses]
    report = S.scout(llm_ctx, FakeLLM({"systemic_patterns": [
        {"title": "One unlimited-resource story", "weakness_ids": ids[:2], "narrative": "the same gap twice"}]}))
    md = S.render_markdown(report)
    assert "## Systemic patterns" in md
    assert "no pattern can introduce a weakness the checks did not find" in md
    assert "Additive commentary, not findings of their own" in md
    assert f"{S.LLM_EXTRA_LABEL} the same gap twice" in md


def test_markdown_of_a_clean_deployment_says_so():
    """A deployment that passes all seven checks still gets a labelled report.

    `deploy.replicas: 2` is what makes this possible: the most critical service in ANY deployment is
    by definition top-tier, so a single-instance one-service compose always has a `no_redundancy`
    prediction. That is the check working, not a bug.
    """
    ctx = make_ctx({"lonely": {"image": "busybox:1.36", "restart": "always",
                               "deploy": {"replicas": 2}}})
    report = S.scout(ctx, None)
    assert report.weaknesses == []
    md = S.render_markdown(report)
    assert PREDICTED_LABEL in md
    assert "0 prediction(s) from 7 deterministic check(s)" in md
    assert "No predicted weaknesses: every deterministic check passed on this deployment." in md


# ================================================================ the real Ditto file (regression)
@pytest.fixture(scope="module")
def real_ctx() -> S.ScoutContext:
    """A ScoutContext over the real deployment: enough weaknesses to need several prose chunks."""
    settings = FakeSettings(REAL_COMPOSE, REAL_NGINX, REAL_KNOWLEDGE, REAL_THRESHOLDS)
    topology = graph.build(str(REAL_COMPOSE), "faultline", str(REAL_NGINX), str(REAL_KNOWLEDGE), live=False)
    return S.build_context(settings, topology)


@pytest.fixture(scope="module")
def real_report() -> WeaknessReport:
    """The scout against the REAL deploy/docker-compose.yml, with no LLM and no Docker.

    `live=False` keeps the topology purely static, so this test needs nothing running. The values
    pinned below were verified by hand against the compose file.
    """
    settings = FakeSettings(REAL_COMPOSE, REAL_NGINX, REAL_KNOWLEDGE, REAL_THRESHOLDS)
    topology = graph.build(str(REAL_COMPOSE), "faultline", str(REAL_NGINX), str(REAL_KNOWLEDGE), live=False)
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


def test_real_ditto_shallow_health_check_quotes_the_alive_probe_and_cites_1865(real_report):
    hits = {w.services[0]: w for w in real_report.weaknesses if w.category == "shallow_health_check"}
    assert set(hits) == set(DITTO_APPS)
    for svc, w in hits.items():
        assert "7626/alive" in w.evidence
        assert "curl --fail `hostname`:7626/alive || exit 1" in w.evidence
        assert "Ditto issue #1865" in w.evidence
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
    """The protected-policy path, on the real config/thresholds.yaml."""
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
    assert real_report.project == "faultline"
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
def test_build_context_reads_compose_knowledge_policy_and_proxy_text(policy_raw):
    settings = FakeSettings(REAL_COMPOSE, REAL_NGINX, REAL_KNOWLEDGE, REAL_THRESHOLDS)
    topology = graph.build(str(REAL_COMPOSE), "faultline", str(REAL_NGINX), str(REAL_KNOWLEDGE), live=False)
    ctx = S.build_context(settings, topology)
    assert ctx.compose_path == str(REAL_COMPOSE)
    assert ctx.knowledge_path == str(REAL_KNOWLEDGE)
    assert ctx.knowledge["health_port"] == 7626
    assert ctx.nginx_text == REAL_NGINX.read_text()
    assert ctx.policy.protected.keys() == policy_raw["policy"]["protected"].keys()
    assert ctx.raw("mongodb")["image"].endswith("mongo:7.0")
    assert "prometheus" not in ctx.candidates()      # tagged `other` in the knowledge file
    assert "mongodb" in ctx.candidates()


def test_build_context_tolerates_missing_knowledge_and_nginx(tmp_path):
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  db:\n    image: mongo:7.0\n")
    settings = FakeSettings(compose, tmp_path / "nope.conf", tmp_path / "nope.yaml", REAL_THRESHOLDS)
    topology = graph.build(str(compose), "p", None, None, live=False)
    ctx = S.build_context(settings, topology)
    assert ctx.knowledge_path is None
    assert ctx.knowledge == {}
    assert ctx.nginx_text == ""
    report = S.scout(ctx, None)
    assert any("no knowledge file was readable" in n for n in report.notes)


# ================================================================ run()
def _real_settings():
    s = load_settings()
    s.compose_file = str(REAL_COMPOSE)
    s.nginx_conf = str(REAL_NGINX)
    s.knowledge_file = str(REAL_KNOWLEDGE)
    s.thresholds_file = str(REAL_THRESHOLDS)
    return s


def _seed_topology(store) -> Topology:
    topo = graph.build(str(REAL_COMPOSE), "faultline", str(REAL_NGINX), str(REAL_KNOWLEDGE), live=False)
    store.write_topology(topo)
    return topo


def test_run_without_a_topology_raises(tmp_store):
    with pytest.raises(FileNotFoundError, match="topology.json"):
        S.run(_real_settings(), tmp_store, None, log=lambda _m: None)


def test_run_with_a_missing_compose_file_raises(tmp_store, tmp_path):
    _seed_topology(tmp_store)
    settings = _real_settings()
    settings.compose_file = str(tmp_path / "not-here.yml")
    with pytest.raises(FileNotFoundError, match="not-here.yml"):
        S.run(settings, tmp_store, None, log=lambda _m: None)
    assert not (tmp_store.root / "weaknesses.json").exists()


def test_run_writes_both_artifacts_and_round_trips_through_the_store(tmp_store):
    _seed_topology(tmp_store)
    report = S.run(_real_settings(), tmp_store, None, log=lambda _m: None)

    assert (tmp_store.root / "weaknesses.json").is_file()
    assert (tmp_store.root / "weaknesses.md").is_file()
    assert tmp_store.weaknesses() == report          # json round-trips back to an equal report
    md = (tmp_store.root / "weaknesses.md").read_text()
    assert md == S.render_markdown(report)
    assert PREDICTED_LABEL in md
    assert "predicted, not measured" in md


def test_run_logs_a_line_per_weakness_and_a_final_tally(tmp_store):
    _seed_topology(tmp_store)
    lines: list[str] = []
    report = S.run(_real_settings(), tmp_store, None, log=lines.append)

    per_weakness = [ln for ln in lines if "would be tested by" in ln]
    assert len(per_weakness) == len(report.weaknesses)
    for ln, w in zip(per_weakness, report.weaknesses):
        assert w.category in ln
        assert "(predicted)" in ln
        assert w.predicted_level.upper() in ln
    assert any(ln.startswith("[scout] note:") for ln in lines)
    tally = lines[-1]
    assert f"{len(report.weaknesses)} predicted weakness(es) from 7 deterministic check(s)" in tally
    assert PREDICTED_LABEL in tally
    assert "weaknesses.json, weaknesses.md" in tally
    assert all(ln.startswith("[scout] ") for ln in lines)


def test_run_logs_systemic_patterns_and_marks_the_report_llm_assisted(tmp_store):
    topo = _seed_topology(tmp_store)
    settings = _real_settings()
    ids = [w.weakness_id for w in S.scout(S.build_context(settings, topo), None).weaknesses]
    llm = FakeLLM({"explanations": {ids[0]: "mongodb is the whole system's single point of failure"},
                   "systemic_patterns": [{"title": "Unbounded CPU everywhere",
                                          "weakness_ids": ids[:3], "narrative": "one story"}]})
    lines: list[str] = []
    report = S.run(settings, tmp_store, llm, log=lines.append)

    assert report.generated_by == "llm+rules"
    assert any("systemic pattern: Unbounded CPU everywhere (3 predictions)" in ln for ln in lines)
    assert report.by_id(ids[0]).why_it_matters == "mongodb is the whole system's single point of failure"
    assert report.by_id(ids[0]).generated_by == "llm+rules"
    assert tmp_store.weaknesses() == report
