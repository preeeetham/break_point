"""Datastore engines (break_point/topology/datastores.py) and how a service gets one.

The invariant these tests pin down: Break Point never assumes an engine. Every engine on a ServiceNode is
backed by evidence code can quote -- the image, the knowledge file, a dependent's connection URI, or (in
onboarding) the project's own Dockerfile and dependency manifests -- and a datastore with none of those
keeps every generic fault but gets no engine-specific command at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from break_point.chaos.actions import kill_marked_script
from break_point.onboarding import code_evidence
from break_point.topology import datastores as D
from break_point.topology.compose import service_nodes, static_edges
from break_point.topology.graph import _structural_findings


# ---------------------------------------------------------------- the table
@pytest.mark.parametrize("text,engine", [
    ("db postgres:16-alpine", "postgres"),
    ("db docker.io/postgis/postgis:16", "postgres"),
    ("cache bitnami/redis:7.2", "redis"),
    ("cache valkey/valkey:8", "redis"),
    ("store mongo:7.0", "mongo"),
    ("legacy mariadb:11", "mysql"),
    ("events cassandra:4", "cassandra"),
    ("api acme/api:1.0", None),
])
def test_from_image(text, engine):
    found = D.from_image(text)
    assert (found.key if found else None) == engine


@pytest.mark.parametrize("value,engine", [
    ("postgres://app:secret@db:5432/app", "postgres"),
    ("postgresql+asyncpg://db/app", None),          # a driver-qualified scheme names no engine we can trust
    ("mongodb+srv://cluster0.example.net/app", "mongo"),
    ("rediss://cache:6380/0", "redis"),
    ("http://api:8080", None),
    ("db:5432", None),
])
def test_from_uri(value, engine):
    found = D.from_uri(value)
    assert (found.key if found else None) == engine


def test_from_manifest_reads_drivers_from_every_ecosystem():
    assert D.from_manifest('{"dependencies": {"pg": "^8.11", "ioredis": "^5"}}') == {"postgres", "redis"}
    assert D.from_manifest("psycopg[binary]==3.1\nredis==5.0\n") == {"postgres", "redis"}
    assert D.from_manifest("<artifactId>postgresql</artifactId>") == {"postgres"}
    assert D.from_manifest("require go.mongodb.org/mongo-driver v1.15.0") == {"mongo"}
    assert D.from_manifest("gem 'pg'\n") == {"postgres"}
    assert D.from_manifest("requests==2.31\nredis-om==0.3\n") == set()   # a name that merely starts with redis


def test_only_verified_engines_carry_commands():
    verified = {k for k, e in D.ENGINES.items() if e.ping or e.healthcheck or e.flood}
    assert verified == {"mongo", "postgres", "redis"}
    for key in verified:
        e = D.ENGINES[key]
        assert e.ping and e.healthcheck and e.flood and e.flood_cleanup and e.verified, key
    assert not D.ENGINES["mysql"].can_flood and D.ENGINES["mysql"].healthcheck is None


def test_flood_command_fills_only_its_placeholders():
    pg = D.flood_command(D.ENGINES["postgres"], 35, "break_point-abc123")
    assert pg.startswith("timeout 35 sh -c '") and pg.endswith("' break_point-abc123")
    # the shell's own ${...} survive: they are expanded inside the container, not by Python
    assert '"${POSTGRES_USER:-postgres}"' in pg and "{duration}" not in pg
    assert "break_point-abc123" in D.flood_command(D.ENGINES["mongo"], 10, "break_point-abc123")
    assert D.flood_command(D.ENGINES["mysql"], 10, "m") == ""


def test_kill_script_skips_itself_and_collects_before_killing():
    """Each clause is one of the three bugs it was verified against (see kill_marked_script)."""
    script = kill_marked_script("break_point-abc123")
    assert '[ "$i" = "$$" ] && continue' in script                  # its own shell carries the marker too
    assert script.index("c=''") < script.index("kill -9")           # every pid collected before any kill
    assert "${s##*) }" in script                                    # ppid read after the last ')' of stat
    assert "for x in $c $m" in script                               # children before their parents


# ---------------------------------------------------------------- engines on the topology
def test_engine_from_image_from_knowledge_and_from_a_dependents_uri():
    compose = {"services": {
        "pg": {"image": "postgres:16"},
        "store": {"image": "acme/store:1"},
        "cache": {"image": "acme/cache:1"},
        "app": {"image": "app:1", "environment": [
            "DATABASE_URL=postgres://app:hunter2@pg:5432/app",
            "CACHE_URL=redis://cache:6379/0"]},
    }}
    knowledge = {"roles": {"store": "datastore", "cache": "datastore"},
                 "datastores": {"store": {"engine": "mongo", "evidence": "Dockerfile FROM mongo:7"}}}
    nodes = service_nodes(compose, knowledge)
    assert (nodes["pg"].engine, nodes["pg"].engine_evidence) == ("postgres", "image postgres:16")
    assert (nodes["store"].engine, nodes["store"].engine_evidence) == ("mongo", "Dockerfile FROM mongo:7")
    assert nodes["cache"].engine == "redis" and nodes["cache"].engine_evidence == "app env CACHE_URL uses redis://"
    assert "hunter2" not in (nodes["cache"].engine_evidence or "")
    assert nodes["app"].engine is None                                # only datastores get one


def test_unknown_engine_is_a_finding_not_a_guess():
    compose = {"services": {"store": {"image": "acme/store:1"}, "app": {"image": "app:1", "environment": ["DB_HOST=store"]}}}
    nodes = service_nodes(compose, {"roles": {"store": "datastore"}})
    assert nodes["store"].engine is None
    findings = _structural_findings(nodes, static_edges(compose, nodes))
    assert any(f.startswith("store: datastore engine not identified") and "only generic faults" in f for f in findings)


# ---------------------------------------------------------------- onboarding: the project's own code
def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def _detect(root: Path, compose: dict) -> code_evidence.Evidence:
    nodes = service_nodes(compose, None)
    return code_evidence.detect(nodes, static_edges(compose, nodes), compose, root)


def test_dockerfile_from_names_the_engine_and_the_role(tmp_path):
    root = _repo(tmp_path, {"db/Dockerfile": "FROM node:20 AS seed\nRUN true\nFROM postgres:16-alpine\nCOPY init.sql /\n"})
    ev = _detect(root, {"services": {"records": {"build": "./db"},
                                     "api": {"image": "api:1", "environment": ["DB_HOST=records"]}}})
    # the final stage is what runs; the name "records" said nothing, so the role is corrected too
    assert ev.datastores["records"].engine == "postgres"
    assert ev.datastores["records"].evidence == "Dockerfile FROM postgres:16-alpine"
    assert ev.roles == {"records": "datastore"}


def test_drivers_of_every_dependent_decide_when_unambiguous(tmp_path):
    root = _repo(tmp_path, {
        "api/package.json": '{"dependencies": {"pg": "^8", "ioredis": "^5"}}',
        "worker/requirements.txt": "psycopg==3.1\nredis==5\n",
    })
    # `store` is not a datastore by name or image, so the knowledge file (or a human) must say so first
    compose = {"services": {
        "store": {"image": "registry.local/store:2"},
        "cache": {"image": "redis:7"},
        "api": {"build": "./api", "environment": ["STORE_HOST=store", "CACHE_HOST=cache"]},
        "worker": {"build": {"context": "./worker"}, "environment": ["STORE_HOST=store", "CACHE_HOST=cache"]},
    }}
    nodes = service_nodes(compose, {"roles": {"store": "datastore"}})
    ev = code_evidence.detect(nodes, static_edges(compose, nodes), compose, root)
    # redis is already explained by `cache`'s image, so the only driver left over is postgres
    assert ev.datastores["store"].engine == "postgres"
    assert ev.datastores["store"].evidence.startswith("client driver declared by api: ")
    assert "cache" not in ev.datastores                              # the image already said so


def test_ambiguous_drivers_leave_the_engine_unknown_and_say_why(tmp_path):
    root = _repo(tmp_path, {"api/requirements.txt": "psycopg==3.1\npymongo==4.6\n"})
    compose = {"services": {"store": {"image": "registry.local/store:2"},
                            "api": {"build": "./api", "environment": ["STORE_HOST=store"]}}}
    nodes = service_nodes(compose, {"roles": {"store": "datastore"}})
    ev = code_evidence.detect(nodes, static_edges(compose, nodes), compose, root)
    assert ev.datastores == {}
    assert ev.notes == ["store: its dependents declare drivers for mongo, postgres; engine left unidentified "
                        "(generic faults only)"]
