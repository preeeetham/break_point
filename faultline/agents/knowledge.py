"""Remediation library. Pure data plus one matcher; no LLM involved.

Each rule: {"match": {failure_types, targets, roles, finding_codes}, "recommendation": {...Recommendation fields}}.
A None in a match slot is a wildcard. Specificity order when matching:
finding_codes > targets > roles > failure_types > generic.

The rules here are generic: they name failure types, finding codes and service roles, never a service or
a product. A target adds its own in targets/<name>/rules.yaml (see `load_rules`), either extending these or,
with `mode: replace`, standing in for them when it carries a complete hand-written library of its own.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from ..schemas import ExperimentReport, Recommendation, Topology

AUTOHEAL = "https://github.com/willfarrell/docker-autoheal"
NGINX_UPSTREAM = "https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_next_upstream"
NGINX_LIMIT_REQ = "https://nginx.org/en/docs/http/ngx_http_limit_req_module.html"
COMPOSE_HEALTHCHECK = "https://docs.docker.com/reference/compose-file/services/#healthcheck"
COMPOSE_LOGGING = "https://docs.docker.com/engine/logging/drivers/json-file/"
PROM_ALERTING = "https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/"

OUTAGE_TYPES = ["container_crash", "container_stop", "container_pause", "network_partition", "network_delay", "network_loss"]
CRASH_TYPES = ["container_crash", "container_stop", "container_pause"]
NETWORK_TYPES = ["network_delay", "network_loss", "network_partition"]
MATCH_SLOTS = ("failure_types", "targets", "roles", "finding_codes")
RULE_MODES = ("extend", "replace")


def _rule(
    *,
    failure_types: Optional[list[str]] = None,
    targets: Optional[list[str]] = None,
    roles: Optional[list[str]] = None,
    finding_codes: Optional[list[str]] = None,
    title: str,
    category: str,
    effort: str,
    confidence: float,
    rationale: str,
    steps: list[str],
    verify: list[str],
    references: Optional[list[str]] = None,
) -> dict[str, Any]:
    return {
        "match": {"failure_types": failure_types, "targets": targets, "roles": roles, "finding_codes": finding_codes},
        "recommendation": {
            "title": title,
            "category": category,
            "effort": effort,
            "confidence": confidence,
            "rationale": rationale,
            "steps": steps,
            "verify": verify,
            "references": references or [],
        },
    }


RULES: list[dict[str, Any]] = [
    # ------------------------------------------------------------------ datastores
    _rule(
        failure_types=OUTAGE_TYPES,
        roles=["datastore"],
        title="Remove the single datastore as a single point of failure: replicate it and let clients fail over",
        category="architecture",
        effort="M",
        confidence=0.85,
        rationale=(
            "One datastore instance behind every service turns any hiccup in it into a full outage of everything "
            "that reads through it. A replicated datastore with client-side failover keeps serving while one "
            "member is down or unreachable."
        ),
        steps=[
            "Run the datastore in its engine's replicated mode (replica set, primary/standby streaming replication, "
            "or a sentinel/cluster setup) with members on separate volumes.",
            "Point every client at the replicated endpoint or the full member list, not a single hostname, and enable "
            "the driver's retry-on-failover options.",
            "Keep the clients' connect and server-selection timeouts well below the proxy's read timeout so a "
            "failover surfaces as a fast, retryable error instead of a hung request.",
        ],
        verify=[
            "The engine's own status command lists one primary and at least one healthy secondary.",
            "Re-run the same experiment against the primary only: API success during injection stays above the "
            "abort threshold and time to recover drops.",
        ],
    ),
    _rule(
        roles=["datastore"],
        title="Give the datastore a healthcheck and restart unhealthy containers automatically",
        category="operations",
        effort="S",
        confidence=0.8,
        rationale=(
            "Docker's restart policy only reacts to a process exit; a container that is running but unhealthy is "
            "never restarted. Without a healthcheck on the datastore nothing even notices a wedged database, and "
            "dependents that lost their connection just sit there."
        ),
        steps=[
            "Add a compose `healthcheck` to the datastore that runs the engine's own ping (`faultline propose` offers "
            "the verified one for known engines).",
            "Add an `autoheal` service (`willfarrell/autoheal`, `AUTOHEAL_CONTAINER_LABEL=all`, docker socket mounted "
            "read-only) so an `unhealthy` state triggers a restart.",
            "Make dependents wait for it on start-up: `depends_on: {<datastore>: {condition: service_healthy}}`.",
        ],
        verify=[
            "`docker inspect --format '{{.State.Health.Status}}' <datastore>` reports `healthy` at steady state.",
            "Pause the datastore for 60 s: autoheal restarts the dependents that turned unhealthy within ~30 s.",
        ],
        references=[AUTOHEAL, COMPOSE_HEALTHCHECK],
    ),
    # ------------------------------------------------------------- health masking
    _rule(
        finding_codes=["health_check_masked_failure"],
        title="Make readiness reflect dependency health, and alert on what users see",
        category="configuration",
        effort="M",
        confidence=0.85,
        rationale=(
            "The service kept answering its readiness check with 200 while the API through it was failing, so no "
            "orchestrator or alert could react. A readiness check that only proves the process is up hides every "
            "failure of the things the process needs."
        ),
        steps=[
            "Extend the readiness endpoint so it fails when a dependency the service cannot work without (its "
            "datastore, its upstream services) is unavailable, with a short timeout per check.",
            "Point the compose `healthcheck` (or the orchestrator's readiness probe) at that endpoint instead of a "
            "liveness-only one.",
            "Alert on the API error-rate SLO and on a synthetic probe through the entry point, not only on liveness.",
        ],
        verify=[
            "Stop the dependency: within one healthcheck interval the service reports `unhealthy` in `docker ps`.",
            "The `health_check_masked_failure` finding no longer appears when the experiment is replayed.",
        ],
        references=[COMPOSE_HEALTHCHECK],
    ),
    # ----------------------------------------------------------- recovery failed
    _rule(
        finding_codes=["manual_restart_required", "recovery_failed", "no_automated_remediation"],
        title="Automate recovery after dependency outages: reconnect/backoff, health-keyed restarts, alert rule",
        category="operations",
        effort="M",
        confidence=0.8,
        rationale=(
            "The service did not come back on its own once its dependency returned; the injector had to restart "
            "it. In production this is a page at 03:00 for something a reconnect policy or a health-keyed restart "
            "could do."
        ),
        steps=[
            "Make the service reconnect to its dependencies with exponential backoff instead of giving up after N "
            "failures (driver reconnect settings, connection-pool validation, supervision/backoff of workers).",
            "Add a healthcheck that reflects the dependency and an `autoheal` sidecar (or an orchestrator liveness "
            "probe) so an `unhealthy` service is restarted automatically -- `restart: always` ignores health state.",
            "Alert on the probe through the entry point failing for one minute, and on a 5xx error-rate burn.",
            "Until the automated path is proven, document the runbook entry: which service to restart, and when.",
        ],
        verify=[
            "Replay the experiment; `recovery.remediation_actions` in the report must be empty and `recovered=true`.",
            "The alert fires during the outage window.",
        ],
        references=[AUTOHEAL],
    ),
    # ------------------------------------------------------- network latency
    _rule(
        failure_types=["network_delay", "network_loss"],
        title="Set explicit timeouts end to end and add a circuit breaker at the API edge",
        category="configuration",
        effort="S",
        confidence=0.8,
        rationale=(
            "Latency injected into one hop propagated to the user as long hangs instead of fast failures. "
            "Without aligned timeouts each layer waits for the slowest one beneath it, request threads pile "
            "up, and the whole API degrades rather than the one slow path."
        ),
        steps=[
            "Proxy: `proxy_connect_timeout 2s; proxy_send_timeout 10s; proxy_read_timeout 30s;` and `proxy_next_upstream timeout`.",
            "Each service: a request timeout shorter than the proxy's, and client timeouts on every outbound call "
            "shorter than that, so the innermost failure surfaces first and with a cause.",
            "Add a circuit breaker in front of the slow path (nginx `max_fails`/`fail_timeout`, Envoy outlier "
            "detection, or a client-side breaker) so a degraded backend sheds load instead of queuing.",
        ],
        verify=[
            "Replay the experiment with the same latency: p95 is capped near the smallest configured timeout and "
            "the error budget burn is measurable rather than a hang.",
            "No request in the probe log exceeds the proxy's `proxy_read_timeout`.",
        ],
        references=[NGINX_UPSTREAM],
    ),
    _rule(
        failure_types=["network_partition"],
        title="Plan for partitions: quorum-based membership and partition-tolerant client behaviour",
        category="architecture",
        effort="M",
        confidence=0.7,
        rationale=(
            "A partition between members of a clustered service can leave two halves each believing they are in "
            "charge; without a resolver the cluster may never heal and writes can diverge. Clients that treat "
            "unreachable as fatal turn a short partition into an outage."
        ),
        steps=[
            "If the service clusters, enable its split-brain / downing strategy (keep-majority or a quorum) and run "
            "an odd number of members so a majority exists.",
            "Make clients treat connection errors and 503 as retryable with jittered backoff and an overall deadline.",
            "Give every cross-service call a timeout so callers fail fast while the partition lasts.",
        ],
        verify=[
            "After the partition heals, the cluster reports a single membership view with every member up within "
            "a few seconds of its stability window.",
        ],
    ),
    # ------------------------------------------------------- resource stress
    _rule(
        failure_types=["cpu_stress", "db_flood"],
        title="Right-size container resources and rate-limit the edge",
        category="configuration",
        effort="S",
        confidence=0.75,
        rationale=(
            "CPU or query pressure on one container degraded the API for everyone. Resource limits isolate noisy "
            "neighbours, memory headroom avoids GC thrash and OOM kills, and an edge rate limit keeps one client "
            "from degrading all of them."
        ),
        steps=[
            "Set `deploy.resources.limits` (cpus, memory) and reservations per service so one cannot starve the "
            "others on the same host; give runtimes with a managed heap a heap size below the memory limit.",
            "Proxy: `limit_req_zone $binary_remote_addr zone=api:10m rate=50r/s;` plus `limit_req zone=api burst=100 nodelay;`.",
            "Datastore: review indexes for the hot queries and cap the clients' connection pools so a flood queues "
            "at the client instead of exhausting the server.",
        ],
        verify=[
            "`docker stats` shows comfortable headroom at idle and no OOMKilled events in `docker inspect`.",
            "Replay the experiment: p95 stays below the abort threshold and the proxy returns 429s for the excess "
            "rather than 5xx for everyone.",
        ],
        references=[NGINX_LIMIT_REQ],
    ),
    # ----------------------------------------------------------- disk pressure
    _rule(
        failure_types=["disk_pressure"],
        title="Protect the data volume: log rotation, a dedicated datastore volume, free-space alerts",
        category="operations",
        effort="S",
        confidence=0.8,
        rationale=(
            "A full disk takes the datastore (and therefore everything behind it) down and can corrupt its journal; "
            "on a single host the most common culprit is unbounded container logs sharing the same filesystem."
        ),
        steps=[
            "Add `logging: {driver: json-file, options: {max-size: \"50m\", max-file: \"5\"}}` to every service "
            "(or set it in `/etc/docker/daemon.json`).",
            "Put the datastore's data directory on a dedicated named volume or block device, separate from the "
            "Docker root and container logs.",
            "Alert when free space on the data volume drops below 15%.",
        ],
        verify=[
            "`docker inspect <service> --format '{{json .HostConfig.LogConfig}}'` shows the size cap on every service.",
            "Replay `disk_pressure` against an application container: the datastore keeps serving because its "
            "volume is unaffected.",
        ],
        references=[COMPOSE_LOGGING],
    ),
    # ---------------------------------------------------------- traffic spike
    _rule(
        failure_types=["traffic_spike"],
        title="Shed load gracefully: rate limiting, back-pressure 429s and horizontal scaling of the entry tier",
        category="architecture",
        effort="M",
        confidence=0.8,
        rationale=(
            "A burst of requests degraded all users equally because nothing between the client and the service "
            "says no. Returning 429 to the excess preserves the SLO for everyone else and gives clients a clear "
            "retry signal."
        ),
        steps=[
            "Proxy: `limit_req_zone $binary_remote_addr zone=perip:10m rate=20r/s;` and a global zone, with "
            "`limit_req_status 429;`.",
            "Scale the entry service to 2-3 replicas behind an `upstream` block and raise `worker_connections`.",
            "Set `limit_conn` per client and `client_body_timeout 10s` to drop slow-loris style load.",
            "Clients: honour `Retry-After` on 429 and use jittered exponential backoff.",
        ],
        verify=[
            "Replay `traffic_spike`: the normal-rate probe's success stays >= 95% while the proxy counts 429s.",
        ],
        references=[NGINX_LIMIT_REQ, NGINX_UPSTREAM],
    ),
    # ----------------------------------------------------------- cascade
    _rule(
        finding_codes=["cascade_beyond_hypothesis"],
        title="Contain the blast radius: bulkheads and dependency timeouts between services",
        category="architecture",
        effort="M",
        confidence=0.7,
        rationale=(
            "Services outside the hypothesis were affected, which means a failure crossed a boundary nobody "
            "expected. Explicit timeouts and bulkheads between services keep one unhealthy service from "
            "exhausting the threads, connections or workers of another."
        ),
        steps=[
            "Review the `cascade_beyond_hypothesis` evidence and add the newly discovered edge to the target's "
            "knowledge file so future hypotheses are honest.",
            "Give cross-service calls a short timeout so a slow service fails fast instead of blocking callers.",
            "Use separate pools (threads, connections, workers) for datastore access and request handling, so a "
            "datastore stall cannot starve health endpoints or the entry tier.",
        ],
        verify=[
            "Replay the experiment: `hypothesis_result.unexpected_affected` is empty.",
        ],
    ),
    # ------------------------------------------------------------- data loss
    _rule(
        finding_codes=["data_loss_suspected"],
        title="Investigate suspected data loss: durability settings, write acknowledgements and reconciliation",
        category="operations",
        effort="L",
        confidence=0.85,
        rationale=(
            "The post-experiment integrity check did not find a write the system had acknowledged. Even one "
            "suspected lost write is a stop-ship issue: users and downstream systems will act on stale state."
        ),
        steps=[
            "Compare `recovery.data_integrity_detail` with the datastore's own journal or log to see whether the "
            "write was persisted and a read path lagged, or never persisted.",
            "Require durable acknowledgements for writes that must not be lost (journaled / majority / synchronous "
            "commit, depending on the engine) and confirm durability is not disabled for speed.",
            "Make sure the service only acknowledges a write after the datastore has, and that any derived index or "
            "cache is reconciled after an outage.",
        ],
        verify=[
            "Replay the experiment: `recovery.data_integrity_ok` is true and no `data_loss_suspected` finding is raised.",
        ],
    ),
    # ----------------------------------------------------------- observability
    _rule(
        title="Baseline observability: SLO alert rules and chaos annotations in Grafana",
        category="observability",
        effort="S",
        confidence=0.65,
        rationale=(
            "Whatever the specific fault, the first production improvement is knowing within a minute that "
            "users are affected. Alert on what the user sees (probe success, error rate, p95), not only on "
            "container liveness."
        ),
        steps=[
            "Prometheus rules: `probe_success == 0 for 1m` (a blackbox probe through the entry point), API "
            "error-rate SLO burn (`sum(rate(requests{status=~\"5..\"}[5m])) / sum(rate(requests[5m])) > 0.02`), "
            "and `histogram_quantile(0.95, ...) > 1s for 5m`.",
            "Add a Grafana annotation (HTTP API `POST /api/annotations`, tag `chaos`) at the start and end of each "
            "Faultline experiment so latency spikes are attributable.",
            "Dashboard the Docker health state per container (`container_health_status` via cAdvisor or a tiny "
            "exporter) next to the API SLIs.",
        ],
        verify=[
            "During the next experiment an alert fires within 60 s of the first API error and the Grafana graph "
            "shows the chaos window.",
        ],
        references=[PROM_ALERTING],
    ),
]


# ---------------------------------------------------------------- target rules
def _from_yaml(raw: dict[str, Any], where: str) -> dict[str, Any]:
    """A YAML rule is a flat mapping: `match` plus the Recommendation fields. Validated here, at load."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: a rule must be a mapping, got {type(raw).__name__}")
    match = raw.get("match") or {}
    unknown = set(match) - set(MATCH_SLOTS)
    if unknown:
        raise ValueError(f"{where}: unknown match slot(s) {sorted(unknown)}; allowed: {list(MATCH_SLOTS)}")
    body = {k: v for k, v in raw.items() if k != "match"}
    Recommendation.model_validate({"references": [], **body})
    return {"match": {slot: match.get(slot) for slot in MATCH_SLOTS},
            "recommendation": {"references": [], **body}}


def load_rules(path: str | Path | None) -> list[dict[str, Any]]:
    """The generic RULES, extended or replaced by a target's rules.yaml. No file = the generic rules."""
    if not path or not Path(path).is_file():
        return list(RULES)
    data = yaml.safe_load(Path(path).read_text()) or {}
    mode = data.get("mode", "extend")
    if mode not in RULE_MODES:
        raise ValueError(f"{path}: mode must be one of {RULE_MODES}, got {mode!r}")
    own = [_from_yaml(r, f"{path} rule {i + 1}") for i, r in enumerate(data.get("rules") or [])]
    return own if mode == "replace" else list(RULES) + own


def rules_for(settings) -> list[dict[str, Any]]:
    return load_rules(getattr(settings, "rules_file", "") or None)


# ---------------------------------------------------------------- matching
def target_service(report: ExperimentReport) -> str:
    t = report.target or {}
    return t.get("service") or t.get("name") or t.get("container") or ""


def _finding_codes(report: ExperimentReport) -> set[str]:
    return {f.code for f in (report.findings or [])}


def _match_level(rule: dict[str, Any], report: ExperimentReport, role: Optional[str] = None) -> Optional[int]:
    """Return a specificity rank (0 = most specific) or None when the rule does not apply."""
    m = rule["match"]
    codes, target, ftype = _finding_codes(report), target_service(report), report.failure_type
    want_codes, want_targets, want_types = m.get("finding_codes"), m.get("targets"), m.get("failure_types")
    want_roles = m.get("roles")
    if want_codes is not None and not codes & set(want_codes):
        return None
    if want_targets is not None and target not in want_targets:
        return None
    if want_roles is not None and role not in want_roles:
        return None
    if want_types is not None and ftype not in want_types:
        return None
    if want_codes is not None:
        return 0
    if want_targets is not None:
        return 1
    if want_roles is not None:
        return 2
    if want_types is not None:
        return 3
    return 4


def matching_rules(report: ExperimentReport, rules: Optional[list[dict[str, Any]]] = None,
                   topology: Optional[Topology] = None) -> list[dict[str, Any]]:
    """Rules that apply, most specific first. `roles` slots need the topology to know the target's role."""
    node = topology.services.get(target_service(report)) if topology is not None else None
    role = node.role if node is not None else None
    ranked: list[tuple[int, float, int, dict[str, Any]]] = []
    for i, rule in enumerate(RULES if rules is None else rules):
        level = _match_level(rule, report, role)
        if level is not None:
            ranked.append((level, -float(rule["recommendation"].get("confidence", 0.0)), i, rule))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for level, _, _, rule in ranked:
        title = rule["recommendation"]["title"]
        if title in seen:
            continue
        seen.add(title)
        out.append({**rule, "specificity": level})
    return out
