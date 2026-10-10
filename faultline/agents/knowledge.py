"""Ditto-specific remediation library. Pure data plus one matcher; no LLM involved.

Each rule: {"match": {failure_types, targets, finding_codes}, "recommendation": {...Recommendation fields}}.
A None in a match slot is a wildcard. Specificity order when matching: finding_codes > targets > failure_types > generic.
"""
from __future__ import annotations

from typing import Any, Optional

from ..schemas import ExperimentReport

DITTO_OPERATING = "https://eclipse.dev/ditto/installation-operating.html"
DITTO_ISSUE_1865 = "https://github.com/eclipse-ditto/ditto/issues/1865"
DITTO_PR_1867 = "https://github.com/eclipse-ditto/ditto/pull/1867"
DITTO_DISCUSSION_1861 = "https://github.com/eclipse-ditto/ditto/discussions/1861"
MONGO_REPLSET = "https://www.mongodb.com/docs/manual/tutorial/deploy-replica-set/"
AUTOHEAL = "https://github.com/willfarrell/docker-autoheal"
NGINX_UPSTREAM = "https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_next_upstream"
NGINX_LIMIT_REQ = "https://nginx.org/en/docs/http/ngx_http_limit_req_module.html"
DITTO_HTTP_API = "https://eclipse.dev/ditto/httpapi-concepts.html"
DITTO_SEARCH = "https://eclipse.dev/ditto/basic-search.html"
PEKKO_FD = "https://pekko.apache.org/docs/pekko/current/typed/cluster.html#failure-detector"

MONGO_OUTAGE_TYPES = ["container_crash", "container_stop", "container_pause", "network_partition", "network_delay", "network_loss"]
CRASH_TYPES = ["container_crash", "container_stop", "container_pause"]
NETWORK_TYPES = ["network_delay", "network_loss", "network_partition"]


def _rule(
    *,
    failure_types: Optional[list[str]] = None,
    targets: Optional[list[str]] = None,
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
        "match": {"failure_types": failure_types, "targets": targets, "finding_codes": finding_codes},
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
    # ------------------------------------------------------------------ MongoDB
    _rule(
        failure_types=MONGO_OUTAGE_TYPES,
        targets=["mongodb"],
        title="Run MongoDB as a replica set so the Ditto driver can fail over",
        category="architecture",
        effort="M",
        confidence=0.9,
        rationale=(
            "MongoDB is the sole datastore for policies, things, things-search and connectivity. A single "
            "standalone mongod turns any Mongo hiccup into a full-platform outage; a replica set lets the Java "
            "driver elect a new primary and keep serving within seconds."
        ),
        steps=[
            "Start mongod with `--replSet rs0 --bind_ip_all` (compose `command:`) and run `rs.initiate()` once via an init container or `docker exec`.",
            "Add two more members (or one member plus an arbiter for the lab) on separate volumes.",
            "Point the Ditto services at the set: `MONGO_DB_URI=mongodb://mongodb-0:27017,mongodb-1:27017,mongodb-2:27017/ditto?replicaSet=rs0&w=majority`.",
            "Set `retryWrites=true&retryReads=true` on the URI so transient primary step-downs are retried by the driver.",
        ],
        verify=[
            "`docker exec mongodb mongosh --eval 'rs.status().members.map(m => m.stateStr)'` shows one PRIMARY and ≥1 SECONDARY.",
            "Re-run the same chaos experiment against the primary only; API success rate during injection should stay above the abort threshold and TTR should drop below 15 s.",
        ],
        references=[MONGO_REPLSET, DITTO_OPERATING],
    ),
    _rule(
        failure_types=MONGO_OUTAGE_TYPES + ["db_flood"],
        targets=["mongodb"],
        title="Tune Ditto's MongoDB client timeouts and connection pool",
        category="configuration",
        effort="S",
        confidence=0.75,
        rationale=(
            "With default driver settings a stalled Mongo socket can hold a request thread for tens of seconds, "
            "which inflates p95 and keeps the health endpoints green while users see timeouts. Explicit, short "
            "timeouts turn a hang into a fast, retryable error."
        ),
        steps=[
            "Set connection/read timeouts on the Ditto services; Ditto reads `MONGO_DB_*` environment variables (e.g. `MONGO_DB_CONNECTION_POOL_SIZE`, `MONGO_DB_READ_PREFERENCE`, `MONGO_DB_QUERY_TIMEOUT`) — verify exact names against `ditto-mongo.conf` in the Ditto source and the operating docs before applying.",
            "Alternatively encode them in the URI: `?connectTimeoutMS=3000&socketTimeoutMS=10000&serverSelectionTimeoutMS=5000&maxPoolSize=100`.",
            "Keep `serverSelectionTimeoutMS` well below the nginx `proxy_read_timeout` so the edge sees a clean 5xx instead of a hung connection.",
        ],
        verify=[
            "During a `network_delay` experiment on mongodb the p95 should plateau near the configured timeout instead of growing unbounded.",
            "`docker logs things | grep -i 'MongoTimeout'` shows fast failures rather than minute-long waits.",
        ],
        references=[DITTO_OPERATING],
    ),
    _rule(
        failure_types=None,
        targets=["mongodb"],
        title="Add a MongoDB healthcheck and an autoheal sidecar to the compose stack",
        category="operations",
        effort="S",
        confidence=0.85,
        rationale=(
            "Docker's `restart: always` only reacts to a process exit; a container that is running but "
            "marked `unhealthy` is never restarted. Without a healthcheck on mongodb nothing even notices "
            "a wedged database, and dependents that lost their connection just sit there."
        ),
        steps=[
            "Add to the mongodb service: `healthcheck: {test: [\"CMD\", \"mongosh\", \"--quiet\", \"--eval\", \"db.adminCommand('ping').ok\"], interval: 10s, timeout: 5s, retries: 3, start_period: 20s}`.",
            "Add an `autoheal` service: `image: willfarrell/autoheal`, `environment: [AUTOHEAL_CONTAINER_LABEL=all]`, mount `/var/run/docker.sock:/var/run/docker.sock:ro`, `restart: always`.",
            "Label the Ditto services with `autoheal=true` (or keep `AUTOHEAL_CONTAINER_LABEL=all`) so an `unhealthy` state triggers a restart.",
        ],
        verify=[
            "`docker inspect --format '{{.State.Health.Status}}' mongodb` reports `healthy` at steady state.",
            "Pause mongodb for 60 s: autoheal logs show a restart of the unhealthy dependents within ~30 s of them flipping to unhealthy.",
        ],
        references=[AUTOHEAL],
    ),
    # ------------------------------------------------------------- health masking
    _rule(
        finding_codes=["health_check_masked_failure"],
        title="Make readiness reflect subsystem health, and alert on /status/health",
        category="configuration",
        effort="M",
        confidence=0.9,
        rationale=(
            "The service kept answering `/ready` with 200 while the API was failing, so no orchestrator or "
            "alert could react. Ditto issue #1865 describes exactly this: the Pekko cluster was healthy but the "
            "persistence subsystem was not, and PR #1867 added a `subsystem-health` check under "
            "`management.health-checks.liveness-checks`."
        ),
        steps=[
            "Upgrade to a Ditto release containing PR #1867 (3.6+) and confirm `management.health-checks.liveness-checks.subsystem-health` is enabled.",
            "Verify whether the equivalent subsystem check is wired into the readiness checks for your version; if not, add it (or treat `/status/health` as the readiness probe in compose/k8s instead of `/ready`).",
            "Point the compose `healthcheck` of each Ditto service at `http://localhost:8558/status/health` (requires devops credentials or `DEVOPS_SECURED=false` in the lab) and fail on any `DOWN` component.",
            "Add a Prometheus alert on `probe_success{job=\"ditto-health\"} == 0` and on the API error-rate SLO, not only on `/alive`.",
        ],
        verify=[
            "Stop mongodb: within one healthcheck interval the Ditto services report `unhealthy` in `docker ps` and `/status/health` shows the persistence component `DOWN`.",
            "The `health_check_masked_failure` finding no longer appears when the experiment is replayed.",
        ],
        references=[DITTO_ISSUE_1865, DITTO_PR_1867, DITTO_OPERATING],
    ),
    # ----------------------------------------------------------- recovery failed
    _rule(
        finding_codes=["manual_restart_required", "recovery_failed", "no_automated_remediation"],
        title="Automate recovery after dependency outages: reconnect/backoff, health-keyed restarts, alert rule",
        category="operations",
        effort="M",
        confidence=0.85,
        rationale=(
            "The service did not come back on its own once its dependency returned; the injector had to restart "
            "it. Ditto discussion #1861 reports the same symptom (Things did not reconnect to MongoDB after an "
            "outage). In production this is a page at 03:00 for something a restart policy could do."
        ),
        steps=[
            "Review the Pekko persistence / Mongo driver reconnect settings for the affected service (restart-on-failure backoff in `ditto-*.conf`); make sure the persistence actors are restarted with exponential backoff rather than stopped after N failures.",
            "Add a Docker healthcheck that hits `/status/health` and an `autoheal` sidecar (or Kubernetes liveness probe) so an `unhealthy` service is restarted automatically — `restart: always` ignores health state.",
            "Add a Prometheus alert: `probe_success{instance=~\"things|policies|gateway\"} == 0 for 1m` and `rate(http_requests_total{status=~\"5..\"}[2m]) / rate(http_requests_total[2m]) > 0.05 for 2m`.",
            "Document a runbook entry: 'if X is unhealthy after Mongo returns, `docker compose restart X`' until the automated path is proven.",
        ],
        verify=[
            "Replay the experiment; `recovery.remediation_actions` in the report must be empty and `recovered=true`.",
            "The alert fires in Prometheus/Alertmanager during the outage window.",
        ],
        references=[DITTO_DISCUSSION_1861, AUTOHEAL, DITTO_OPERATING],
    ),
    # -------------------------------------------------------------- policies
    _rule(
        failure_types=CRASH_TYPES + NETWORK_TYPES + ["cpu_stress"],
        targets=["policies"],
        title="Remove the single point of authorization: run ≥2 policies instances and cache decisions",
        category="architecture",
        effort="M",
        confidence=0.85,
        rationale=(
            "Every Things/Search/Connectivity/Gateway request must enforce a policy, so the policies service "
            "is a hard dependency of the whole API. One instance means one crash equals a full outage until "
            "Pekko cluster sharding re-homes the policy actors on a replacement node."
        ),
        steps=[
            "Run at least two instances: compose `deploy: {replicas: 2}` (or a `policies-2` service) or a Kubernetes Deployment with `replicas: 2` and a PodDisruptionBudget.",
            "Keep the Pekko failure detector tuned for fast, stable detection — the compose file already sets it via `JAVA_TOOL_OPTIONS` (`pekko.cluster.failure-detector.*`); verify `acceptable-heartbeat-pause` is ~3–5 s so sharding rebalances quickly without flapping.",
            "Enable/raise the policy enforcer cache in the consuming services (Ditto `ditto.*.enforcement.cache` / `policy-enforcer-cache` settings) so recently used decisions survive a short policies outage.",
            "Ensure the gateway `pekko.cluster.seed-nodes` list contains both policies instances or uses a discovery mechanism.",
        ],
        verify=[
            "`curl -u devops:... http://localhost:8558/cluster` (or `/status/cluster`) lists two `policies` members.",
            "Kill one policies container: API success rate during injection stays > 90% and no `cascade_beyond_hypothesis` finding is raised.",
        ],
        references=[PEKKO_FD, DITTO_OPERATING],
    ),
    # --------------------------------------------------------------- gateway
    _rule(
        failure_types=CRASH_TYPES + ["cpu_stress", "traffic_spike"],
        targets=["gateway"],
        title="Run two gateway replicas behind an nginx upstream with retries",
        category="architecture",
        effort="M",
        confidence=0.85,
        rationale=(
            "The gateway is the only HTTP entry point. nginx currently proxies to a single host, so a gateway "
            "crash is a hard 502 for every client until Docker restarts it and the JVM warms up (~20–40 s)."
        ),
        steps=[
            "Define a second gateway service in compose (`gateway-2`) or use `deploy.replicas: 2`.",
            "In nginx.conf: `upstream ditto_gateway { server gateway:8080 max_fails=2 fail_timeout=10s; server gateway-2:8080 max_fails=2 fail_timeout=10s; }` and `proxy_pass http://ditto_gateway;`.",
            "Add `proxy_next_upstream error timeout http_502 http_503 http_504; proxy_next_upstream_tries 2; proxy_connect_timeout 2s; proxy_read_timeout 30s;`.",
            "Mark the replicas with a `start_period` in their healthcheck so nginx does not route to a JVM that is still booting.",
        ],
        verify=[
            "`nginx -T` shows the upstream block; `docker kill gateway` leaves `curl -u ditto:ditto localhost:8080/api/2/things` returning 200 (served by gateway-2).",
            "Replay the experiment: success rate during injection ≥ 95%, TTR < 5 s.",
        ],
        references=[NGINX_UPSTREAM],
    ),
    # ---------------------------------------------------------------- things
    _rule(
        failure_types=CRASH_TYPES,
        targets=["things"],
        title="Scale things horizontally and make clients retry with idempotent, ETag-guarded writes",
        category="architecture",
        effort="M",
        confidence=0.8,
        rationale=(
            "Things actors are cluster-sharded, so a second instance lets Pekko re-home shards instead of "
            "waiting for the crashed JVM to restart. On the client side, every Thing write in Ditto is a PUT "
            "with `If-Match`/ETag support, so retries are safe and cheap."
        ),
        steps=[
            "Run `things` with `deploy.replicas: 2` (compose) or `replicas: 2` (k8s); keep the Pekko seed-node list stable.",
            "Set a sensible `pekko.cluster.sharding.rebalance-interval` / `passivation` so shards move within a few seconds of a node loss.",
            "In client SDKs, wrap PUT/PATCH with 3 retries + exponential backoff on 5xx/connection errors and send `If-Match: <etag>` to make the retry idempotent; treat 412 as 'already applied'.",
            "Keep POST-created things (server-generated ids) behind a client-side idempotency key or prefer PUT with a client-chosen thingId.",
        ],
        verify=[
            "Kill one things instance: API success stays > 90% and the cluster view shows the shard regions rebalanced.",
            "A retried PUT with a stale `If-Match` returns 412 rather than overwriting newer state.",
        ],
        references=[DITTO_HTTP_API, DITTO_OPERATING],
    ),
    # --------------------------------------------------------- things-search
    _rule(
        failure_types=None,
        targets=["things-search"],
        title="Treat search as eventually consistent: degrade gracefully and expose index lag",
        category="observability",
        effort="S",
        confidence=0.8,
        rationale=(
            "things-search maintains its own Mongo index fed asynchronously from Things/Policies events. When it "
            "is down or slow the authoritative `/api/2/things/<id>` path keeps working, so clients that fall "
            "back to direct reads see no outage — but only if they are built to."
        ),
        steps=[
            "In clients, fall back from `/api/2/search/things` to direct `GET /api/2/things/<id>` (or the Ditto `things?ids=` batch form) when search returns 5xx or times out.",
            "Serve stale search results with a `Warning`/custom header instead of failing when the index is lagging.",
            "Expose the search sync lag (`things-search` background-sync metrics / `search-updater` stream lag) to Prometheus and alert if it exceeds ~30 s.",
            "After an outage, trigger `things-search` background sync (devops `/devops/piggyback/things-search` with `SudoUpdateThings`) rather than waiting for the periodic sync.",
        ],
        verify=[
            "Stop things-search: direct Thing reads keep succeeding and the probe only marks search operations as degraded.",
            "A Grafana panel shows index lag returning to ~0 within a minute of recovery.",
        ],
        references=[DITTO_SEARCH, DITTO_OPERATING],
    ),
    # ----------------------------------------------------------- connectivity
    _rule(
        failure_types=None,
        targets=["connectivity"],
        title="Isolate connectivity failures from the HTTP API and make connections self-healing",
        category="configuration",
        effort="S",
        confidence=0.7,
        rationale=(
            "connectivity hosts the MQTT/AMQP/Kafka bridges; its loss should never affect the REST API, but "
            "stuck connections do silently drop device data until an operator notices."
        ),
        steps=[
            "Confirm connection `failoverEnabled: true` and a bounded reconnect backoff (`ditto.connectivity.connection.client.*` / `pekko` supervision) so connections recover without manual `open`.",
            "Alert on `connectivity` connection status != `open` via the devops `/connectivity/connections/<id>/status` endpoint or the Ditto connectivity metrics.",
            "Enable `acknowledgements` on inbound mappings so devices/brokers retain messages while connectivity is down.",
        ],
        verify=[
            "Kill connectivity: the HTTP probe shows no impact and `connections/<id>/status` returns `open` within 30 s of restart.",
        ],
        references=[DITTO_OPERATING],
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
            "nginx: `proxy_connect_timeout 2s; proxy_send_timeout 10s; proxy_read_timeout 30s;` and `proxy_next_upstream timeout`.",
            "Ditto gateway: lower the request timeout (`ditto.gateway.http.request-timeout` / `GATEWAY_HTTP_REQUEST_TIMEOUT`, default 60 s) to ~15 s; have clients send the `timeout` query parameter on commands (e.g. `?timeout=10s`) so Ditto fails fast instead of hanging.",
            "Add a circuit breaker in front of the gateway (nginx `max_fails`/`fail_timeout`, or Envoy outlier detection) so a degraded backend sheds load for a few seconds instead of queuing.",
            "Align Mongo driver `socketTimeoutMS` below the gateway timeout so the failure surfaces as a clean error with a cause.",
        ],
        verify=[
            "Replay `network_delay` with the same latency: p95 is capped near the smallest configured timeout and the error budget burn is measurable rather than a hang.",
            "No request in the probe log exceeds the nginx `proxy_read_timeout`.",
        ],
        references=[DITTO_HTTP_API, NGINX_UPSTREAM],
    ),
    _rule(
        failure_types=["network_partition"],
        title="Plan for split-brain: Pekko split-brain resolver and partition-tolerant client behaviour",
        category="architecture",
        effort="M",
        confidence=0.7,
        rationale=(
            "A partition between Ditto cluster members can leave two sub-clusters each believing it owns the "
            "shards; without a split-brain resolver the cluster may never heal and writes can diverge."
        ),
        steps=[
            "Enable the Pekko split-brain resolver (`pekko.cluster.downing-provider-class = org.apache.pekko.cluster.sbr.SplitBrainResolverProvider`) with `keep-majority` and a stable `stable-after` of ~10–20 s; Ditto exposes these through `ditto.cluster` / `JAVA_TOOL_OPTIONS`.",
            "Run an odd number of cluster members so a majority exists.",
            "Make clients treat 503/`ClusterUnreachable` as retryable with backoff.",
        ],
        verify=[
            "After a partition heals, `/status/cluster` shows a single cluster with all members `Up` and no `Unreachable` entries within `stable-after` + ~10 s.",
        ],
        references=[PEKKO_FD, DITTO_OPERATING],
    ),
    # ------------------------------------------------------- resource stress
    _rule(
        failure_types=["cpu_stress", "db_flood"],
        title="Right-size container resources and rate-limit the edge",
        category="configuration",
        effort="S",
        confidence=0.8,
        rationale=(
            "In our baseline the Java services already sit at ~80% of their 512 MB limit at idle, so any CPU or "
            "query pressure quickly turns into GC thrash and timeouts. Memory headroom, CPU limits and an edge "
            "rate limit keep one noisy tenant from degrading the whole API."
        ),
        steps=[
            "Raise `mem_limit` for policies/things/things-search/gateway to ≥ 1 GB and set `-XX:MaxRAMPercentage=70` in `JAVA_TOOL_OPTIONS` so the heap fits under the cgroup limit.",
            "Set `cpus:` limits (and `deploy.resources.reservations`) so one service cannot starve the others on the same host.",
            "nginx: `limit_req_zone $binary_remote_addr zone=api:10m rate=50r/s;` plus `limit_req zone=api burst=100 nodelay;` on `/api/`.",
            "MongoDB: review indexes for the things/policies/search collections (`db.collection.getIndexes()`, `explain()` on slow queries from `db.setProfilingLevel(1, 100)`) and cap the Ditto driver `maxPoolSize` so a flood queues at the client instead of exhausting mongod.",
        ],
        verify=[
            "`docker stats` shows < 60% memory utilisation at idle and no OOMKilled events in `docker inspect`.",
            "Replay `cpu_stress`: p95 stays below the abort threshold and nginx returns 429s for the excess rather than 5xx for everyone.",
        ],
        references=[NGINX_LIMIT_REQ, DITTO_OPERATING],
    ),
    # ----------------------------------------------------------- disk pressure
    _rule(
        failure_types=["disk_pressure"],
        title="Protect the data volume: log rotation, dedicated Mongo volume, free-space alerts",
        category="operations",
        effort="S",
        confidence=0.85,
        rationale=(
            "A full disk takes MongoDB (and therefore all of Ditto) down and can corrupt the WiredTiger journal; "
            "on a single host the most common culprit is unbounded container logs sharing the same filesystem."
        ),
        steps=[
            "Add `logging: {driver: json-file, options: {max-size: \"50m\", max-file: \"5\"}}` to every service (or set it in `/etc/docker/daemon.json`).",
            "Put `/data/db` on a dedicated named volume or block device separate from the Docker root and container logs.",
            "Set `--wiredTigerCacheSizeGB` explicitly (≈50% of the container memory limit) so mongod does not assume host RAM.",
            "Alert on `node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.15` for the Mongo volume and on `mongodb_ss_wt_cache_bytes_currently_in_the_cache` nearing the cache size.",
        ],
        verify=[
            "`docker inspect mongodb --format '{{json .HostConfig.LogConfig}}'` shows the size cap; `df -h` on the Mongo volume shows > 20% free after a 24 h soak.",
            "Replay `disk_pressure` against the app container: MongoDB keeps serving because its volume is unaffected.",
        ],
        references=["https://www.mongodb.com/docs/manual/reference/program/mongod/#std-option-mongod.--wiredTigerCacheSizeGB"],
    ),
    # ---------------------------------------------------------- traffic spike
    _rule(
        failure_types=["traffic_spike"],
        title="Shed load gracefully: nginx rate limiting, back-pressure 429s and horizontal gateway scaling",
        category="architecture",
        effort="M",
        confidence=0.8,
        rationale=(
            "A burst of requests degraded all users equally because nothing between the client and the JVM "
            "says no. Returning 429 to the excess preserves the SLO for everyone else and gives clients a clear "
            "retry signal."
        ),
        steps=[
            "nginx: `limit_req_zone $binary_remote_addr zone=perip:10m rate=20r/s;` and `limit_req_zone $server_name zone=global:10m rate=200r/s;` with `limit_req_status 429;`.",
            "Scale the gateway to 2–3 replicas behind an `upstream` block (see the gateway rule) and increase `worker_connections`.",
            "Set `limit_conn` per client and `client_body_timeout 10s` to drop slow-loris style load.",
            "Clients: honour `Retry-After` on 429 and use jittered exponential backoff.",
        ],
        verify=[
            "Replay `traffic_spike`: probe success for the normal-rate probe stays ≥ 95% while `nginx_http_requests_total{status=\"429\"}` rises.",
        ],
        references=[NGINX_LIMIT_REQ, NGINX_UPSTREAM],
    ),
    # ----------------------------------------------------------- cascade
    _rule(
        finding_codes=["cascade_beyond_hypothesis"],
        title="Contain the blast radius: bulkheads and dependency timeouts between Ditto services",
        category="architecture",
        effort="M",
        confidence=0.7,
        rationale=(
            "Services outside the hypothesis were affected, which means a failure crossed a boundary nobody "
            "expected. Explicit timeouts and bulkheads between cluster roles keep one unhealthy role from "
            "exhausting the dispatchers of another."
        ),
        steps=[
            "Review the `cascade_beyond_hypothesis` evidence and add the newly discovered edge to the topology (`expected_affected`) so future hypotheses are honest.",
            "Give cross-service asks a short `ask-timeout` (Ditto `ditto.*.ask-timeout` / command `timeout`) so a slow role fails fast instead of blocking callers.",
            "Use separate Pekko dispatchers for persistence vs. HTTP handling so a Mongo stall cannot starve health endpoints or the gateway.",
        ],
        verify=[
            "Replay the experiment: `hypothesis_result.unexpected_affected` is empty.",
        ],
        references=[DITTO_OPERATING],
    ),
    # ------------------------------------------------------------- data loss
    _rule(
        finding_codes=["data_loss_suspected"],
        title="Investigate suspected data loss: journaling, write concern and post-incident reconciliation",
        category="operations",
        effort="L",
        confidence=0.9,
        rationale=(
            "The post-experiment integrity check did not find what was written before or during injection. "
            "Even one suspected lost write in a digital-twin platform is a stop-ship issue: devices and "
            "applications will act on stale state."
        ),
        steps=[
            "Compare `recovery.data_integrity_detail` with the Mongo journal (`things_journal`) for the probe thing ids to see whether the event was persisted and the snapshot/search index lagged, or never persisted.",
            "Set write concern to `w=majority&journal=true` on the Ditto Mongo URI once a replica set exists; on a standalone, confirm journaling is enabled (default) and `fsync` is not disabled.",
            "Ensure clients require acknowledgements (`requested-acks: twin-persisted`) for writes that must not be lost.",
            "Run `things-search` background sync after recovery to reconcile the search index with the twin store.",
        ],
        verify=[
            "Replay the experiment: `recovery.data_integrity_ok` is true and no `data_loss_suspected` finding is raised.",
        ],
        references=[DITTO_HTTP_API, DITTO_OPERATING],
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
            "Prometheus rules: `probe_success == 0 for 1m` (blackbox on `/api/2/things` through nginx), API error-rate SLO burn (`sum(rate(requests{status=~\"5..\"}[5m])) / sum(rate(requests[5m])) > 0.02`), and `histogram_quantile(0.95, ...) > 1s for 5m`.",
            "Add a Grafana annotation (HTTP API `POST /api/annotations`, tag `chaos`) at the start and end of each Faultline experiment so latency spikes are attributable.",
            "Dashboard the Docker health state per container (`container_health_status` via cAdvisor or a tiny exporter) next to the API SLIs.",
        ],
        verify=[
            "During the next experiment an alert fires within 60 s of the first API error and the Grafana graph shows the chaos window.",
        ],
        references=["https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/"],
    ),
]


def target_service(report: ExperimentReport) -> str:
    t = report.target or {}
    return t.get("service") or t.get("name") or t.get("container") or ""


def _finding_codes(report: ExperimentReport) -> set[str]:
    return {f.code for f in (report.findings or [])}


def _match_level(rule: dict[str, Any], report: ExperimentReport) -> Optional[int]:
    """Return a specificity rank (0 = most specific) or None when the rule does not apply."""
    m = rule["match"]
    codes, target, ftype = _finding_codes(report), target_service(report), report.failure_type
    want_codes, want_targets, want_types = m.get("finding_codes"), m.get("targets"), m.get("failure_types")
    if want_codes is not None and not codes & set(want_codes):
        return None
    if want_targets is not None and target not in want_targets:
        return None
    if want_types is not None and ftype not in want_types:
        return None
    if want_codes is not None:
        return 0
    if want_targets is not None:
        return 1
    if want_types is not None:
        return 2
    return 3


def matching_rules(report: ExperimentReport) -> list[dict[str, Any]]:
    ranked: list[tuple[int, float, int, dict[str, Any]]] = []
    for i, rule in enumerate(RULES):
        level = _match_level(rule, report)
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
