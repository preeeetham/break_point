"""Datastore engines: which one a service runs, and the few engine-specific commands Break Point may use.

Break Point never assumes an engine. A service counts as a datastore by role (topology.compose), and its
engine is decided by code from evidence, strongest first:

  1. the knowledge file (`datastores:`), which `break_point onboard` fills from the project's own code --
     a Dockerfile `FROM` line, or the client drivers every dependent declares in its dependency manifest;
  2. the image name in compose (`postgres:16`, `bitnami/redis`, ...);
  3. the URI scheme a dependent uses to reach it (`DATABASE_URL=postgres://db:5432/app`).

An engine that is recognised but has no verified commands, or no engine at all, is not an error: the
service still gets every generic fault (crash, stop, pause, network delay / loss / partition, cpu, disk),
only the engine-specific ones (health ping, write flood, healthcheck fix) are withheld, and the topology
says why. Every command below was run against the official image named in `verified` before it went in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

FLOOD_DB = "break_point"   # the database / table / key prefix floods write into, dropped on cleanup


@dataclass(frozen=True)
class Engine:
    key: str
    title: str
    image_re: re.Pattern            # matched against "<service name> <image>", like the role guesser
    uri_schemes: tuple[str, ...]
    driver_re: Optional[re.Pattern]  # client libraries in a dependency manifest (pom.xml, package.json, ...)
    ping: Optional[str] = None       # shell, exit 0 = the engine answers; bounded by its own timeout
    healthcheck: Optional[dict] = None   # compose healthcheck using the same check
    flood: Optional[str] = None      # shell, literal {duration} / {marker} placeholders; one writer, runs until killed
    flood_cleanup: Optional[str] = None  # shell, removes what the flood wrote
    verified: str = ""               # the image the commands were checked against, empty = none to check

    @property
    def can_flood(self) -> bool:
        return bool(self.flood)


def _hc(test: str) -> dict:
    return {"test": ["CMD-SHELL", test], "interval": "15s", "timeout": "5s", "retries": 3, "start_period": "20s"}


# Postgres reads its user and database from the container's own environment, so the commands work on any
# deployment of the official image without Break Point knowing a credential. `$$` is compose's escape for `$`.
_PG_ENV = 'U="${POSTGRES_USER:-postgres}"; D="${POSTGRES_DB:-$U}"; '

ENGINES: dict[str, Engine] = {e.key: e for e in (
    Engine(
        "mongo", "MongoDB", re.compile(r"mongo", re.I), ("mongodb", "mongodb+srv"),
        re.compile(r"mongodb-driver|mongo-java-driver|pymongo|motor\b|mongoose|\"mongodb\"\s*:|go\.mongodb\.org"
                   r"|reactivemongo|persistence-mongo|spring-boot-starter-data-mongodb", re.I),
        ping="timeout 3 mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1",
        # the check Break Point's sandbox compose files ship; mongosh is in the official mongo >= 6 images
        healthcheck={"test": 'mongosh --quiet --eval "db.runCommand({ping:1}).ok" | grep -q 1',
                     "interval": "15s", "timeout": "5s", "retries": 3, "start_period": "20s"},
        flood=("timeout {duration} mongosh --quiet --eval \"var marker='{marker}'; var s='x'.repeat(2048); "
               "while(true){db.getSiblingDB('" + FLOOD_DB + "').flood.insertOne({t:new Date(),x:Math.random(),s:s})}\" "
               ">/dev/null 2>&1"),
        flood_cleanup=f"mongosh --quiet --eval \"db.getSiblingDB('{FLOOD_DB}').flood.drop()\" >/dev/null 2>&1",
        verified="mongo:7.0",
    ),
    Engine(
        "postgres", "PostgreSQL", re.compile(r"postgres|postgis|timescale", re.I), ("postgres", "postgresql"),
        re.compile(r"psycopg|asyncpg|\"pg\"\s*:|gem\s+[\"']pg[\"']|<artifactId>postgresql</artifactId>|org\.postgresql"
                   r"|lib/pq\b|jackc/pgx|npgsql|tokio-postgres|r2dbc-postgresql", re.I),
        ping='timeout 3 sh -c \'pg_isready -q -U "${POSTGRES_USER:-postgres}"\'',
        healthcheck=_hc('pg_isready -q -U "$${POSTGRES_USER:-postgres}"'),
        flood=("timeout {duration} sh -c '" + _PG_ENV +
               f'psql -q -U "$U" -d "$D" -c "CREATE TABLE IF NOT EXISTS {FLOOD_DB}_flood (t timestamptz DEFAULT now(), '
               's text)" >/dev/null 2>&1; while :; do psql -q -U "$U" -d "$D" -c "INSERT INTO '
               f'{FLOOD_DB}_flood (s) SELECT repeat(md5(random()::text), 64) FROM generate_series(1, 200)" '
               ">/dev/null 2>&1; done' {marker}"),
        flood_cleanup=(_PG_ENV + f'psql -q -U "$U" -d "$D" -c "DROP TABLE IF EXISTS {FLOOD_DB}_flood" >/dev/null 2>&1'),
        verified="postgres:16-alpine",
    ),
    Engine(
        "redis", "Redis", re.compile(r"redis|valkey|keydb", re.I), ("redis", "rediss"),
        re.compile(r"ioredis|jedis|lettuce-core|redis/go-redis|go-redis|\"redis\"\s*:|gem\s+[\"']redis[\"']"
                   r"|^\s*redis\s*([=<>~!]|$)|spring-boot-starter-data-redis", re.I | re.M),
        ping="timeout 3 redis-cli ping 2>/dev/null | grep -q PONG",
        healthcheck=_hc("redis-cli ping | grep -q PONG"),
        # Keys rotate over $RANDOM (32768 values), so memory stays bounded at ~32768 x 2 KiB however long it runs.
        flood=("timeout {duration} sh -c 'P=$(head -c 1536 /dev/urandom | base64 | tr -d \"\\n\"); while :; do "
               'redis-cli -r 200 SET "' + FLOOD_DB + ':flood:$RANDOM" "$P" >/dev/null 2>&1; done\' {marker}'),
        flood_cleanup=("redis-cli EVAL \"for _,k in ipairs(redis.call('KEYS','" + FLOOD_DB + ":flood:*')) do "
                       "redis.call('DEL',k) end return 0\" 0 >/dev/null 2>&1"),
        verified="redis:7-alpine",
    ),
    # Recognised so they count as datastores and get every generic fault. No command has been verified
    # against their images yet, so none is offered: add one here only after running it for real.
    Engine("mysql", "MySQL / MariaDB", re.compile(r"mysql|mariadb|percona", re.I), ("mysql", "mariadb"),
           re.compile(r"pymysql|mysqlclient|mysql-connector|\"mysql2?\"\s*:|go-sql-driver/mysql|mariadb-java-client", re.I)),
    Engine("cassandra", "Cassandra", re.compile(r"cassandra|scylla", re.I), ("cassandra",),
           re.compile(r"cassandra-driver|datastax|gocql|scylla-driver", re.I)),
)}

URI_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")


def get(key: Optional[str]) -> Optional[Engine]:
    return ENGINES.get(key or "")


def from_image(text: str) -> Optional[Engine]:
    """The engine an image (or "<name> <image>") names, first match in table order."""
    return next((e for e in ENGINES.values() if e.image_re.search(text or "")), None)


def from_uri(value: str) -> Optional[Engine]:
    m = URI_SCHEME_RE.match((value or "").strip())
    scheme = m.group(1).lower() if m else ""
    return next((e for e in ENGINES.values() if scheme in e.uri_schemes), None) if scheme else None


def from_manifest(text: str) -> set[str]:
    """Engines whose client drivers a dependency manifest declares."""
    return {e.key for e in ENGINES.values() if e.driver_re is not None and e.driver_re.search(text or "")}


def flood_command(engine: Engine, duration_s: int, marker: str) -> str:
    # plain replacement, not str.format: the templates are shell, full of `${VAR:-default}`
    return (engine.flood or "").replace("{duration}", str(int(duration_s))).replace("{marker}", marker)
