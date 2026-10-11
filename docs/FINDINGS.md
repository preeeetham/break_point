# Faultline findings report: Eclipse Ditto resilience

*Generated from a live run against Docker Compose project `bosch-ditto` (Eclipse Ditto 3.9.8 + MongoDB 7 + nginx), 2026-10-11 04:23:04 to 2026-10-11 04:34:29 UTC. Every number below is copied from the JSON reports the agents wrote to `reports/`; nothing is illustrative.*

## Summary

- **Overall resilience score: 88 / 100** across 6 experiments. 6 scored experiment(s) across 5 of 7 services; overall 87.8/100. Weakest: `things` (F, 45.6/100) — API success fell to 0%.
- **6 experiments, 6 distinct failure types**: `container_crash`, `container_stop`, `cpu_stress`, `network_delay`, `network_loss`, `traffic_spike`.
- **0 rated high or critical** by the Risk agent; the worst is `container_crash` on `things` at **4.7/10 (MEDIUM)**.
- **1 experiment(s) stopped automatically** by a safety threshold and rolled back; every experiment ended with the target restored (see [Safety: the kill switch fired for real](#safety-the-kill-switch-fired-for-real)).
- All recommendations are **advisory only** (`advisory_only: true`); Faultline never changes the system under test.

## What the run shows

1. **`things` is the single point of failure for the API.** Killing its only instance took API success from 100% to 0%: every request in the fault window timed out, and the safety monitor aborted the experiment at +7.2 s. It is the only service graded F. The Fix agent's remedy is horizontal scaling of `things` with idempotent, ETag-guarded client retries.
2. **Readiness lies while a dependency is down.** During the same crash, `gateway`, `things-search` and `connectivity` all kept answering `/ready` = 200 (`dependency_blind_readiness`), so an orchestrator would have kept routing traffic to them. This is the behaviour behind Ditto issue #1865.
3. **Ditto degrades gracefully under most faults.** A 30 s `policies` outage, CPU starvation and 20% packet loss on MongoDB, 800 ms latency on `connectivity` and a 150 req/s burst on `gateway` all kept API success at 100%. The cost was latency: p95 rose up to 6.7x (38 -> 255 ms with `connectivity` delayed; 63 -> 404 ms, 6.4x, with `policies` down), and every target recovered on its own in 6-27 s with no intervention and no lost write (sequence canary matched after every experiment).
4. **Predictions held.** All six hypotheses held; no service outside the predicted blast radius was affected.
5. **Not covered by this run:** `nginx` and `things-search` were not targeted, and MongoDB was only degraded, never crashed (protected-service policy). Each experiment ran once for 15-45 s, so these are single observations, not distributions.

*Who wrote what:* severities, scores, grades and fix matching are computed by code. The prose summaries were written by an open-weights LLM for 5 of 6 risk assessments and the extra fix suggestions for 1 of 6 experiments (`generated_by: llm+rules`); the rest fell back to templates (`generated_by: rules`). The planner used `openai/gpt-oss-120b`; that model hit the account's daily Groq token cap before the analysis step, so risk and fix prose came from `openai/gpt-oss-20b`. One call (the `things` crash) failed Groq's JSON validation on every retry and kept its templated prose; its 4.7/10 severity is unaffected because severity is never model-generated.

## Ranked weaknesses

Every non-informational finding raised across the run, worst first (finding severity, then the experiment's risk score). The fix column is the Fix agent's top recommendation for that experiment; full steps are in the per-experiment sections below.

| # | Severity | Finding | Service / experiment | Evidence | Top recommended fix |
|---|---|---|---|---|---|
| 1 | **HIGH** | `api_impact`: API success dropped to 0% during the fault | `things` container_crash ([1de6fb6a0969](#exp-1de6fb6a0969)) | 7 requests, status counts {'timeout': 7}, p95 None ms (baseline 56.96 ms) | Scale things horizontally and make clients retry with idempotent, ETag-guarded writes |
| 2 | **MEDIUM** | `safety_abort`: Kill switch fired (error_rate) | `things` container_crash ([1de6fb6a0969](#exp-1de6fb6a0969)) | {"trailing_samples": 5, "error_rate": 1.0, "p95_ms": null, "threshold": 0.3} | Scale things horizontally and make clients retry with idempotent, ETag-guarded writes |
| 3 | **LOW** | `dependency_blind_readiness`: Dependents stayed 'ready' while things was down: things-search (100%), connectivity (100%), gateway (100%) | `things` container_crash ([1de6fb6a0969](#exp-1de6fb6a0969)) | readiness only reflects the service's own process, not whether it can serve requests; an orchestrator would keep routing traffic to them | Scale things horizontally and make clients retry with idempotent, ETag-guarded writes |
| 4 | **LOW** | `api_impact`: API stayed available (100%) but p95 latency rose 62.7 -> 404.31 ms | `policies` container_stop ([713eacc680e6](#exp-713eacc680e6)) | 124 requests, status counts {'200': 93, '204': 31}, p95 404.31 ms (baseline 62.7 ms) | Remove the single point of authorization: run ≥2 policies instances and cache decisions |
| 5 | **LOW** | `api_impact`: API stayed available (100%) but p95 latency rose 37.91 -> 254.68 ms | `connectivity` network_delay ([fb5442b902a7](#exp-fb5442b902a7)) | 183 requests, status counts {'204': 46, '200': 137}, p95 254.68 ms (baseline 37.91 ms) | Isolate connectivity failures from the HTTP API and make connections self-healing |
| 6 | **LOW** | `api_impact`: API stayed available (100%) but p95 latency rose 33.5 -> 79.48 ms | `mongodb` cpu_stress ([359e504c9f6c](#exp-359e504c9f6c)) | 181 requests, status counts {'204': 46, '200': 135}, p95 79.48 ms (baseline 33.5 ms) | Add a MongoDB healthcheck and an autoheal sidecar to the compose stack |
| 7 | **LOW** | `api_impact`: API stayed available (100%) but p95 latency rose 39.92 -> 233.18 ms | `mongodb` network_loss ([e85e6fa18520](#exp-e85e6fa18520)) | 61 requests, status counts {'200': 46, '204': 15}, p95 233.18 ms (baseline 39.92 ms) | Run MongoDB as a replica set so the Ditto driver can fail over |

## Resilience scorecard

Computed by the Scorecard agent: score = 100 - mean penalty per service, graded A >= 90, B >= 80, C >= 65, D >= 50, else F. Ranked worst first; untested services carry no grade rather than a flattering default.

| Service | Score | Grade | Experiments | Worst success during | Slowest recovery | Findings | Notes |
|---|---|---|---|---|---|---|---|
| `things` | 46 | **F** | 1 | 0.0% | 26.5 s | 4 | tested: container_crash |
| `policies` | 97 | **A** | 1 | 100.0% | 18.4 s | 2 | tested: container_stop |
| `connectivity` | 98 | **A** | 1 | 100.0% | 8.7 s | 2 | tested: network_delay |
| `mongodb` | 99 | **A** | 2 | 100.0% | 6.4 s | 4 | tested: cpu_stress, network_loss |
| `gateway` | 99 | **A** | 1 | 100.0% | 6.1 s | 2 | tested: traffic_spike |
| `nginx` | n/a | **—** | 0 | n/a | n/a | 0 | not tested |
| `things-search` | n/a | **—** | 0 | n/a | n/a | 0 | not tested |

## Experiments run

Steady-state baseline before any injection (`reports/baseline.json`, 60 s, 240 requests): success **100.0%**, p50 21 ms, p95 **57 ms**. Derived abort thresholds: error rate > 30% over the trailing 10 requests, or p95 > 3000 ms.

| Experiment | Target | Failure type | Planned by | Success before -> during -> after | p95 during | Recovery | Safety | Risk |
|---|---|---|---|---|---|---|---|---|
| [1de6fb6a0969](#exp-1de6fb6a0969) | `things` | `container_crash` {"duration_s": 30, "block_restart": false} | operator | 100.0% -> 0.0% -> 100.0% | n/a | 26.5 s | aborted: `error_rate` | 4.7 medium |
| [fb5442b902a7](#exp-fb5442b902a7) | `connectivity` | `network_delay` {"duration_s": 45, "delay_ms": 800, "jitter_ms": 200} | operator | 100.0% -> 100.0% -> 100.0% | 255 ms | 8.7 s | ran full duration | 0.4 low |
| [713eacc680e6](#exp-713eacc680e6) | `policies` | `container_stop` {"duration_s": 30} | operator | 100.0% -> 100.0% -> 100.0% | 404 ms | 18.4 s | ran full duration | 0.6 low |
| [359e504c9f6c](#exp-359e504c9f6c) | `mongodb` | `cpu_stress` {"duration_s": 45, "workers": 2} | operator | 100.0% -> 100.0% -> 100.0% | 79 ms | 6.4 s | ran full duration | 0.4 low |
| [deeb5a23d7c8](#exp-deeb5a23d7c8) | `gateway` | `traffic_spike` {"duration_s": 30, "rps": 150} | operator | 100.0% -> 100.0% -> 100.0% | 76 ms | 6.1 s | ran full duration | 0.4 low |
| [e85e6fa18520](#exp-e85e6fa18520) | `mongodb` | `network_loss` {"duration_s": 15, "loss_percent": 20} | llm | 100.0% -> 100.0% -> 100.0% | 233 ms | 6.3 s | ran full duration | 0.4 low |

## Findings and fixes, per experiment (worst first)

<a id="exp-1de6fb6a0969"></a>
### MEDIUM 4.7/10: `container_crash` on `things` (1de6fb6a0969)

- **Why this experiment** (operator): operator-selected container crash (sigkill) on things
- **Hypothesis**: taking away things (container crash (sigkill)) for 30s will push the synthetic API error rate to at most 100%, impact at most ['connectivity', 'gateway', 'things-search'], and the API will recover within 90s after rollback without manual intervention
- **Hypothesis result**: held; observed max error rate 100.0%, observed recovery 26.5 s. error rate 100% vs ≤100%; unexpected services none; recovery 26.5s vs ≤135s
- **Errors and slowdown**: success 100.0% before, 0.0% during (7 requests, p95 n/a), 100.0% after (p95 40 ms).
- **Recovery**: recovered in 26.5 s; no intervention needed. Data integrity: stored seq=74, last acknowledged write seq=74.
- **Safety**: aborted early on `error_rate` {"trailing_samples": 5, "error_rate": 1.0, "p95_ms": null, "threshold": 0.3}; watchdog fired: False.
- **Observed blast radius**: `things`

| Severity | Finding | Evidence |
|---|---|---|
| high | `api_impact`: API success dropped to 0% during the fault | 7 requests, status counts {'timeout': 7}, p95 None ms (baseline 56.96 ms) |
| medium | `safety_abort`: Kill switch fired (error_rate) | {"trailing_samples": 5, "error_rate": 1.0, "p95_ms": null, "threshold": 0.3} |
| low | `dependency_blind_readiness`: Dependents stayed 'ready' while things was down: things-search (100%), connectivity (100%), gateway (100%) | readiness only reflects the service's own process, not whether it can serve requests; an orchestrator would keep routing traffic to them |
| info | `auto_recovered`: Recovered on its own in 26s | outage as seen by clients: 32.9s |

**Risk assessment** (rules): Killing `things` (container_crash) moved API success from 100% to 0% across 7 samples; 1 service affected; recovered in 26 s without intervention. The safety monitor aborted early (error_rate).

**Production implication**: In production a container_crash on things would be a visible degradation but not a full outage.

Score factors: impact 4.00, blast 0.29, recovery 0.44, intervention 0.00, findings 0.00

**Recommended fixes** (rules, advisory only):

1. **Scale things horizontally and make clients retry with idempotent, ETag-guarded writes** (architecture, effort M, confidence 0.80). In experiment 1de6fb6a0969 (risk MEDIUM 4.7/10), API success fell to 0% during injection, TTR 26 s, findings: API success dropped to 0% during the fault. Things actors are cluster-sharded, so a second instance lets Pekko re-home shards instead of waiting for the crashed JVM to restart. On the client side, every Thing write in Ditto is a PUT with `If-Match`/ETag support, so retries are safe and cheap.
   - Run `things` with `deploy.replicas: 2` (compose) or `replicas: 2` (k8s); keep the Pekko seed-node list stable.
   - Set a sensible `pekko.cluster.sharding.rebalance-interval` / `passivation` so shards move within a few seconds of a node loss.
   - In client SDKs, wrap PUT/PATCH with 3 retries + exponential backoff on 5xx/connection errors and send `If-Match: <etag>` to make the retry idempotent; treat 412 as 'already applied'.
   - Keep POST-created things (server-generated ids) behind a client-side idempotency key or prefer PUT with a client-chosen thingId.
   - *Verify*: Kill one things instance: API success stays > 90% and the cluster view shows the shard regions rebalanced.; A retried PUT with a stale `If-Match` returns 412 rather than overwriting newer state.
2. **Baseline observability: SLO alert rules and chaos annotations in Grafana** (observability, effort S, confidence 0.65). In experiment 1de6fb6a0969 (risk MEDIUM 4.7/10), API success fell to 0% during injection, TTR 26 s, findings: API success dropped to 0% during the fault. Whatever the specific fault, the first production improvement is knowing within a minute that users are affected. Alert on what the user sees (probe success, error rate, p95), not only on container liveness.
   - Prometheus rules: `probe_success == 0 for 1m` (blackbox on `/api/2/things` through nginx), API error-rate SLO burn (`sum(rate(requests{status=~"5.."}[5m])) / sum(rate(requests[5m])) > 0.02`), and `histogram_quantile(0.95, ...) > 1s for 5m`.
   - Add a Grafana annotation (HTTP API `POST /api/annotations`, tag `chaos`) at the start and end of each Faultline experiment so latency spikes are attributable.
   - Dashboard the Docker health state per container (`container_health_status` via cAdvisor or a tiny exporter) next to the API SLIs.
   - *Verify*: During the next experiment an alert fires within 60 s of the first API error and the Grafana graph shows the chaos window.

Raw: `reports/experiments/1de6fb6a0969.json`, `reports/risk/1de6fb6a0969.json`, `reports/fixes/1de6fb6a0969.md`. Reproduce by hand: `docker kill bosch-ditto-things-1 && sleep 30 && docker update --restart=always bosch-ditto-things-1 && docker start bosch-ditto-things-1`

<a id="exp-713eacc680e6"></a>
### LOW 0.6/10: `container_stop` on `policies` (713eacc680e6)

- **Why this experiment** (operator): operator-selected graceful stop (sigterm) on policies
- **Hypothesis**: taking away policies (graceful stop (sigterm)) for 30s will push the synthetic API error rate to at most 100%, impact at most ['connectivity', 'gateway', 'things', 'things-search'], and the API will recover within 90s after rollback without manual intervention
- **Hypothesis result**: held; observed max error rate 0.0%, observed recovery 18.4 s. error rate 0% vs ≤100%; unexpected services none; recovery 18.4s vs ≤135s
- **Errors and slowdown**: success 100.0% before, 100.0% during (124 requests, p95 404 ms), 100.0% after (p95 45 ms).
- **Recovery**: recovered in 18.4 s; no intervention needed. Data integrity: stored seq=94, last acknowledged write seq=94.
- **Observed blast radius**: `policies`

| Severity | Finding | Evidence |
|---|---|---|
| low | `api_impact`: API stayed available (100%) but p95 latency rose 62.7 -> 404.31 ms | 124 requests, status counts {'200': 93, '204': 31}, p95 404.31 ms (baseline 62.7 ms) |
| info | `auto_recovered`: Recovered on its own in 18s | outage as seen by clients: 49.3s |

**Risk assessment** (llm+rules): The container running the policies service was stopped, causing a spike in p95 latency from 62.7 ms to 404.31 ms while API availability remained at 100%. The system recovered automatically in 18.4 seconds without any remediation actions.

**Production implication**: The incident had a low severity rating of 0.59 and a low impact factor of 0.0, indicating minimal disruption to production. However, the latency increase could affect user experience during the 18-second recovery window.

Score factors: impact 0.00, blast 0.29, recovery 0.31, intervention 0.00, findings 0.00

**Recommended fixes** (rules, advisory only):

1. **Remove the single point of authorization: run ≥2 policies instances and cache decisions** (architecture, effort M, confidence 0.85). In experiment 713eacc680e6 (risk LOW 0.6/10), API success fell to 100% during injection, TTR 18 s. Every Things/Search/Connectivity/Gateway request must enforce a policy, so the policies service is a hard dependency of the whole API. One instance means one crash equals a full outage until Pekko cluster sharding re-homes the policy actors on a replacement node.
   - Run at least two instances: compose `deploy: {replicas: 2}` (or a `policies-2` service) or a Kubernetes Deployment with `replicas: 2` and a PodDisruptionBudget.
   - Keep the Pekko failure detector tuned for fast, stable detection — the compose file already sets it via `JAVA_TOOL_OPTIONS` (`pekko.cluster.failure-detector.*`); verify `acceptable-heartbeat-pause` is ~3–5 s so sharding rebalances quickly without flapping.
   - Enable/raise the policy enforcer cache in the consuming services (Ditto `ditto.*.enforcement.cache` / `policy-enforcer-cache` settings) so recently used decisions survive a short policies outage.
   - Ensure the gateway `pekko.cluster.seed-nodes` list contains both policies instances or uses a discovery mechanism.
   - *Verify*: `curl -u devops:... http://localhost:8558/cluster` (or `/status/cluster`) lists two `policies` members.; Kill one policies container: API success rate during injection stays > 90% and no `cascade_beyond_hypothesis` finding is raised.
2. **Baseline observability: SLO alert rules and chaos annotations in Grafana** (observability, effort S, confidence 0.65). In experiment 713eacc680e6 (risk LOW 0.6/10), API success fell to 100% during injection, TTR 18 s. Whatever the specific fault, the first production improvement is knowing within a minute that users are affected. Alert on what the user sees (probe success, error rate, p95), not only on container liveness.
   - Prometheus rules: `probe_success == 0 for 1m` (blackbox on `/api/2/things` through nginx), API error-rate SLO burn (`sum(rate(requests{status=~"5.."}[5m])) / sum(rate(requests[5m])) > 0.02`), and `histogram_quantile(0.95, ...) > 1s for 5m`.
   - Add a Grafana annotation (HTTP API `POST /api/annotations`, tag `chaos`) at the start and end of each Faultline experiment so latency spikes are attributable.
   - Dashboard the Docker health state per container (`container_health_status` via cAdvisor or a tiny exporter) next to the API SLIs.
   - *Verify*: During the next experiment an alert fires within 60 s of the first API error and the Grafana graph shows the chaos window.

Raw: `reports/experiments/713eacc680e6.json`, `reports/risk/713eacc680e6.json`, `reports/fixes/713eacc680e6.md`. Reproduce by hand: `docker stop bosch-ditto-policies-1 && sleep 30 && docker start bosch-ditto-policies-1`

<a id="exp-fb5442b902a7"></a>
### LOW 0.4/10: `network_delay` on `connectivity` (fb5442b902a7)

- **Why this experiment** (operator): operator-selected network latency (tc netem) on connectivity
- **Hypothesis**: degrading connectivity (network latency (tc netem)) for 45s will push the synthetic API error rate to at most 30%, impact at most ['gateway'], and the API will recover within 10s after rollback without manual intervention
- **Hypothesis result**: held; observed max error rate 0.0%, observed recovery 8.7 s. error rate 0% vs ≤30%; unexpected services none; recovery 8.7s vs ≤15s
- **Errors and slowdown**: success 100.0% before, 100.0% during (183 requests, p95 255 ms), 100.0% after (p95 121 ms).
- **Recovery**: recovered in 8.7 s; no intervention needed. Data integrity: stored seq=98, last acknowledged write seq=98.
- **Observed blast radius**: `connectivity`

| Severity | Finding | Evidence |
|---|---|---|
| low | `api_impact`: API stayed available (100%) but p95 latency rose 37.91 -> 254.68 ms | 183 requests, status counts {'204': 46, '200': 137}, p95 254.68 ms (baseline 37.91 ms) |
| info | `auto_recovered`: Recovered on its own in 9s | outage as seen by clients: 54.4s |

**Risk assessment** (llm+rules): The chaos experiment introduced a network delay to the connectivity service, raising the 95th percentile latency from 37.91 ms to 254.68 ms. The service remained fully available with a 100% success rate throughout the test.

**Production implication**: The increased latency may affect time-sensitive API calls but did not cause any outages. Recovery was automatic, taking 8.7 seconds, with no remediation actions required.

Score factors: impact 0.00, blast 0.29, recovery 0.14, intervention 0.00, findings 0.00

**Recommended fixes** (rules, advisory only):

1. **Isolate connectivity failures from the HTTP API and make connections self-healing** (configuration, effort S, confidence 0.70). In experiment fb5442b902a7 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 9 s. connectivity hosts the MQTT/AMQP/Kafka bridges; its loss should never affect the REST API, but stuck connections do silently drop device data until an operator notices.
   - Confirm connection `failoverEnabled: true` and a bounded reconnect backoff (`ditto.connectivity.connection.client.*` / `pekko` supervision) so connections recover without manual `open`.
   - Alert on `connectivity` connection status != `open` via the devops `/connectivity/connections/<id>/status` endpoint or the Ditto connectivity metrics.
   - Enable `acknowledgements` on inbound mappings so devices/brokers retain messages while connectivity is down.
   - *Verify*: Kill connectivity: the HTTP probe shows no impact and `connections/<id>/status` returns `open` within 30 s of restart.
2. **Set explicit timeouts end to end and add a circuit breaker at the API edge** (configuration, effort S, confidence 0.80). In experiment fb5442b902a7 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 9 s. Latency injected into one hop propagated to the user as long hangs instead of fast failures. Without aligned timeouts each layer waits for the slowest one beneath it, request threads pile up, and the whole API degrades rather than the one slow path.
   - nginx: `proxy_connect_timeout 2s; proxy_send_timeout 10s; proxy_read_timeout 30s;` and `proxy_next_upstream timeout`.
   - Ditto gateway: lower the request timeout (`ditto.gateway.http.request-timeout` / `GATEWAY_HTTP_REQUEST_TIMEOUT`, default 60 s) to ~15 s; have clients send the `timeout` query parameter on commands (e.g. `?timeout=10s`) so Ditto fails fast instead of hanging.
   - Add a circuit breaker in front of the gateway (nginx `max_fails`/`fail_timeout`, or Envoy outlier detection) so a degraded backend sheds load for a few seconds instead of queuing.
   - Align Mongo driver `socketTimeoutMS` below the gateway timeout so the failure surfaces as a clean error with a cause.
   - *Verify*: Replay `network_delay` with the same latency: p95 is capped near the smallest configured timeout and the error budget burn is measurable rather than a hang.; No request in the probe log exceeds the nginx `proxy_read_timeout`.
3. **Baseline observability: SLO alert rules and chaos annotations in Grafana** (observability, effort S, confidence 0.65). In experiment fb5442b902a7 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 9 s. Whatever the specific fault, the first production improvement is knowing within a minute that users are affected. Alert on what the user sees (probe success, error rate, p95), not only on container liveness.
   - Prometheus rules: `probe_success == 0 for 1m` (blackbox on `/api/2/things` through nginx), API error-rate SLO burn (`sum(rate(requests{status=~"5.."}[5m])) / sum(rate(requests[5m])) > 0.02`), and `histogram_quantile(0.95, ...) > 1s for 5m`.
   - Add a Grafana annotation (HTTP API `POST /api/annotations`, tag `chaos`) at the start and end of each Faultline experiment so latency spikes are attributable.
   - Dashboard the Docker health state per container (`container_health_status` via cAdvisor or a tiny exporter) next to the API SLIs.
   - *Verify*: During the next experiment an alert fires within 60 s of the first API error and the Grafana graph shows the chaos window.

Raw: `reports/experiments/fb5442b902a7.json`, `reports/risk/fb5442b902a7.json`, `reports/fixes/fb5442b902a7.md`. Reproduce by hand: `docker run --rm -v /var/run/docker.sock:/var/run/docker.sock gaiaadm/pumba:latest netem --duration 45s --tc-image ghcr.io/alexei-led/pumba-alpine-nettools:latest delay --time 800 --jitter 200 bosch-ditto-connectivity-1`

<a id="exp-359e504c9f6c"></a>
### LOW 0.4/10: `cpu_stress` on `mongodb` (359e504c9f6c)

- **Why this experiment** (operator): operator-selected cpu starvation on mongodb
- **Hypothesis**: degrading mongodb (cpu starvation) for 45s will push the synthetic API error rate to at most 20%, impact at most ['connectivity', 'policies', 'things', 'things-search'], and the API will recover within 10s after rollback without manual intervention
- **Hypothesis result**: held; observed max error rate 0.0%, observed recovery 6.4 s. error rate 0% vs ≤20%; unexpected services none; recovery 6.4s vs ≤15s
- **Errors and slowdown**: success 100.0% before, 100.0% during (181 requests, p95 79 ms), 100.0% after (p95 32 ms).
- **Recovery**: recovered in 6.4 s; no intervention needed. Data integrity: stored seq=93, last acknowledged write seq=93.
- **Observed blast radius**: `mongodb`

| Severity | Finding | Evidence |
|---|---|---|
| low | `api_impact`: API stayed available (100%) but p95 latency rose 33.5 -> 79.48 ms | 181 requests, status counts {'204': 46, '200': 135}, p95 79.48 ms (baseline 33.5 ms) |
| info | `auto_recovered`: Recovered on its own in 6s | outage as seen by clients: 51.5s |

**Risk assessment** (llm+rules): The chaos experiment targeted MongoDB with CPU stress, achieving a severity of 0.39 and low level impact. API availability stayed at 100% while p95 latency rose from 33.5 to 79.48 ms.

**Production implication**: The experiment did not affect service availability but increased latency, potentially impacting user experience. The system recovered automatically in 6.4 seconds, showing resilience.

Score factors: impact 0.00, blast 0.29, recovery 0.11, intervention 0.00, findings 0.00

**Recommended fixes** (rules, advisory only):

1. **Add a MongoDB healthcheck and an autoheal sidecar to the compose stack** (operations, effort S, confidence 0.85). In experiment 359e504c9f6c (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. Docker's `restart: always` only reacts to a process exit; a container that is running but marked `unhealthy` is never restarted. Without a healthcheck on mongodb nothing even notices a wedged database, and dependents that lost their connection just sit there.
   - Add to the mongodb service: `healthcheck: {test: ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"], interval: 10s, timeout: 5s, retries: 3, start_period: 20s}`.
   - Add an `autoheal` service: `image: willfarrell/autoheal`, `environment: [AUTOHEAL_CONTAINER_LABEL=all]`, mount `/var/run/docker.sock:/var/run/docker.sock:ro`, `restart: always`.
   - Label the Ditto services with `autoheal=true` (or keep `AUTOHEAL_CONTAINER_LABEL=all`) so an `unhealthy` state triggers a restart.
   - *Verify*: `docker inspect --format '{{.State.Health.Status}}' mongodb` reports `healthy` at steady state.; Pause mongodb for 60 s: autoheal logs show a restart of the unhealthy dependents within ~30 s of them flipping to unhealthy.
2. **Right-size container resources and rate-limit the edge** (configuration, effort S, confidence 0.80). In experiment 359e504c9f6c (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. In our baseline the Java services already sit at ~80% of their 512 MB limit at idle, so any CPU or query pressure quickly turns into GC thrash and timeouts. Memory headroom, CPU limits and an edge rate limit keep one noisy tenant from degrading the whole API.
   - Raise `mem_limit` for policies/things/things-search/gateway to ≥ 1 GB and set `-XX:MaxRAMPercentage=70` in `JAVA_TOOL_OPTIONS` so the heap fits under the cgroup limit.
   - Set `cpus:` limits (and `deploy.resources.reservations`) so one service cannot starve the others on the same host.
   - nginx: `limit_req_zone $binary_remote_addr zone=api:10m rate=50r/s;` plus `limit_req zone=api burst=100 nodelay;` on `/api/`.
   - MongoDB: review indexes for the things/policies/search collections (`db.collection.getIndexes()`, `explain()` on slow queries from `db.setProfilingLevel(1, 100)`) and cap the Ditto driver `maxPoolSize` so a flood queues at the client instead of exhausting mongod.
   - *Verify*: `docker stats` shows < 60% memory utilisation at idle and no OOMKilled events in `docker inspect`.; Replay `cpu_stress`: p95 stays below the abort threshold and nginx returns 429s for the excess rather than 5xx for everyone.
3. **Baseline observability: SLO alert rules and chaos annotations in Grafana** (observability, effort S, confidence 0.65). In experiment 359e504c9f6c (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. Whatever the specific fault, the first production improvement is knowing within a minute that users are affected. Alert on what the user sees (probe success, error rate, p95), not only on container liveness.
   - Prometheus rules: `probe_success == 0 for 1m` (blackbox on `/api/2/things` through nginx), API error-rate SLO burn (`sum(rate(requests{status=~"5.."}[5m])) / sum(rate(requests[5m])) > 0.02`), and `histogram_quantile(0.95, ...) > 1s for 5m`.
   - Add a Grafana annotation (HTTP API `POST /api/annotations`, tag `chaos`) at the start and end of each Faultline experiment so latency spikes are attributable.
   - Dashboard the Docker health state per container (`container_health_status` via cAdvisor or a tiny exporter) next to the API SLIs.
   - *Verify*: During the next experiment an alert fires within 60 s of the first API error and the Grafana graph shows the chaos window.

Raw: `reports/experiments/359e504c9f6c.json`, `reports/risk/359e504c9f6c.json`, `reports/fixes/359e504c9f6c.md`. Reproduce by hand: `docker exec -d bosch-ditto-mongodb-1 sh -c 'timeout 45 sh -c "while :; do :; done"'  # x2`

<a id="exp-deeb5a23d7c8"></a>
### LOW 0.4/10: `traffic_spike` on `gateway` (deeb5a23d7c8)

- **Why this experiment** (operator): operator-selected traffic spike on gateway
- **Hypothesis**: degrading gateway (traffic spike) for 30s will push the synthetic API error rate to at most 20%, impact at most ['no other services'], and the API will recover within 10s after rollback without manual intervention
- **Hypothesis result**: held; observed max error rate 0.0%, observed recovery 6.1 s. error rate 0% vs ≤20%; unexpected services none; recovery 6.1s vs ≤15s
- **Errors and slowdown**: success 100.0% before, 100.0% during (122 requests, p95 76 ms), 100.0% after (p95 42 ms).
- **Recovery**: recovered in 6.1 s; no intervention needed. Data integrity: stored seq=78, last acknowledged write seq=78.
- **Observed blast radius**: `gateway`

| Severity | Finding | Evidence |
|---|---|---|
| info | `api_impact`: API unaffected (100% success, p95 75.56 ms) | 122 requests, status counts {'200': 91, '204': 31}, p95 75.56 ms (baseline 151.61 ms) |
| info | `auto_recovered`: Recovered on its own in 6s | outage as seen by clients: 36.3s |

**Risk assessment** (llm+rules): The chaos experiment induced a traffic spike on the gateway, resulting in a low severity rating of 0.39 and a low impact level. The experiment achieved 100% success, reduced the p95 latency from 151.61 ms to 75.56 ms, and recovered automatically in 6.1 seconds with no remediation actions required.

**Production implication**: The experiment had no impact on API traffic, maintaining 100% success and a p95 latency of 75.56 ms during the test. Production services remained unaffected, and the system recovered automatically within 6.1 seconds without any remediation actions.

Score factors: impact 0.00, blast 0.29, recovery 0.10, intervention 0.00, findings 0.00

**Recommended fixes** (llm+rules, advisory only):

1. **Run two gateway replicas behind an nginx upstream with retries** (architecture, effort M, confidence 0.85). In experiment deeb5a23d7c8 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. The gateway is the only HTTP entry point. nginx currently proxies to a single host, so a gateway crash is a hard 502 for every client until Docker restarts it and the JVM warms up (~20–40 s).
   - Define a second gateway service in compose (`gateway-2`) or use `deploy.replicas: 2`.
   - In nginx.conf: `upstream ditto_gateway { server gateway:8080 max_fails=2 fail_timeout=10s; server gateway-2:8080 max_fails=2 fail_timeout=10s; }` and `proxy_pass http://ditto_gateway;`.
   - Add `proxy_next_upstream error timeout http_502 http_503 http_504; proxy_next_upstream_tries 2; proxy_connect_timeout 2s; proxy_read_timeout 30s;`.
   - Mark the replicas with a `start_period` in their healthcheck so nginx does not route to a JVM that is still booting.
   - *Verify*: `nginx -T` shows the upstream block; `docker kill gateway` leaves `curl -u ditto:ditto localhost:8080/api/2/things` returning 200 (served by gateway-2).; Replay the experiment: success rate during injection ≥ 95%, TTR < 5 s.
2. **Shed load gracefully: nginx rate limiting, back-pressure 429s and horizontal gateway scaling** (architecture, effort M, confidence 0.80). In experiment deeb5a23d7c8 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. A burst of requests degraded all users equally because nothing between the client and the JVM says no. Returning 429 to the excess preserves the SLO for everyone else and gives clients a clear retry signal.
   - nginx: `limit_req_zone $binary_remote_addr zone=perip:10m rate=20r/s;` and `limit_req_zone $server_name zone=global:10m rate=200r/s;` with `limit_req_status 429;`.
   - Scale the gateway to 2–3 replicas behind an `upstream` block (see the gateway rule) and increase `worker_connections`.
   - Set `limit_conn` per client and `client_body_timeout 10s` to drop slow-loris style load.
   - Clients: honour `Retry-After` on 429 and use jittered exponential backoff.
   - *Verify*: Replay `traffic_spike`: probe success for the normal-rate probe stays ≥ 95% while `nginx_http_requests_total{status="429"}` rises.
3. **Baseline observability: SLO alert rules and chaos annotations in Grafana** (observability, effort S, confidence 0.65). In experiment deeb5a23d7c8 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. Whatever the specific fault, the first production improvement is knowing within a minute that users are affected. Alert on what the user sees (probe success, error rate, p95), not only on container liveness.
   - Prometheus rules: `probe_success == 0 for 1m` (blackbox on `/api/2/things` through nginx), API error-rate SLO burn (`sum(rate(requests{status=~"5.."}[5m])) / sum(rate(requests[5m])) > 0.02`), and `histogram_quantile(0.95, ...) > 1s for 5m`.
   - Add a Grafana annotation (HTTP API `POST /api/annotations`, tag `chaos`) at the start and end of each Faultline experiment so latency spikes are attributable.
   - Dashboard the Docker health state per container (`container_health_status` via cAdvisor or a tiny exporter) next to the API SLIs.
   - *Verify*: During the next experiment an alert fires within 60 s of the first API error and the Grafana graph shows the chaos window.
4. **Add Docker healthcheck and restart policy for gateway container** (operations, effort M, confidence 0.60). [LLM suggestion, unverified] Ensures gateway restarts automatically if it becomes unresponsive, reducing outage duration and manual intervention.
   - Add a healthcheck to the gateway service in docker-compose.yml that pings the /health endpoint or checks process liveness.
   - Set restart: unless-stopped or restart: always to enforce automatic restarts.
   - *Verify*: Simulate a gateway failure (e.g., kill the process) and observe that Docker restarts the container within the healthcheck interval.; Check docker logs to confirm the restart and that the gateway resumes serving traffic without manual intervention.
5. **Configure MongoDB replica set for high availability** (architecture, effort L, confidence 0.60). [LLM suggestion, unverified] Eliminates single point of failure for the database, allowing seamless failover during traffic spikes or node failures.
   - Provision at least two additional MongoDB nodes and configure them as a replica set via mongod.conf or init scripts.
   - Update Ditto configuration to point to the replica set connection string (e.g., mongodb://user:pass@mongo1,mongo2,mongo3:27017/?replicaSet=rs0).
   - Ensure persistence volumes are separate for each node to avoid data loss.
   - *Verify*: Simulate primary node failure and confirm that a secondary node is elected primary via MongoDB logs.; Verify that Ditto continues to write and read data during and after the failover without errors.

Raw: `reports/experiments/deeb5a23d7c8.json`, `reports/risk/deeb5a23d7c8.json`, `reports/fixes/deeb5a23d7c8.md`. Reproduce by hand: `# 150 req/s for 30s against $DITTO_URL/api/2/things/faultline.probe:canary (e.g. hey -z 30s -q 150 -c 16)`

<a id="exp-e85e6fa18520"></a>
### LOW 0.4/10: `network_loss` on `mongodb` (e85e6fa18520)

- **Why this experiment** (llm): Inject moderate packet loss on MongoDB to see how downstream services react under a realistic network fault
- **Hypothesis**: If MongoDB experiences 20% packet loss for 15 s, dependent services will see increased error rates that recover within ~30 s after the loss stops
- **Hypothesis result**: held; observed max error rate 0.0%, observed recovery 6.3 s. error rate 0% vs ≤40%; unexpected services none; recovery 6.3s vs ≤45s
- **Errors and slowdown**: success 100.0% before, 100.0% during (61 requests, p95 233 ms), 100.0% after (p95 41 ms).
- **Recovery**: recovered in 6.3 s; no intervention needed. Data integrity: stored seq=66, last acknowledged write seq=66.
- **Observed blast radius**: `mongodb`

| Severity | Finding | Evidence |
|---|---|---|
| low | `api_impact`: API stayed available (100%) but p95 latency rose 39.92 -> 233.18 ms | 61 requests, status counts {'200': 46, '204': 15}, p95 233.18 ms (baseline 39.92 ms) |
| info | `auto_recovered`: Recovered on its own in 6s | outage as seen by clients: 21.3s |

**Risk assessment** (llm+rules): The chaos experiment induced a network loss to MongoDB, causing p95 latency to rise from 39.92 ms to 233.18 ms while API availability remained at 100%. The system auto‑recovered in 6.3 seconds with no remediation actions required.

**Production implication**: The incident had a low severity impact (0.39) with no measurable service impact or blast effect, as MongoDB remained fully available. Recovery time was 6.3 seconds, and no intervention was needed, indicating minimal production disruption.

Score factors: impact 0.00, blast 0.29, recovery 0.10, intervention 0.00, findings 0.00

**Recommended fixes** (rules, advisory only):

1. **Run MongoDB as a replica set so the Ditto driver can fail over** (architecture, effort M, confidence 0.90). In experiment e85e6fa18520 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. MongoDB is the sole datastore for policies, things, things-search and connectivity. A single standalone mongod turns any Mongo hiccup into a full-platform outage; a replica set lets the Java driver elect a new primary and keep serving within seconds.
   - Start mongod with `--replSet rs0 --bind_ip_all` (compose `command:`) and run `rs.initiate()` once via an init container or `docker exec`.
   - Add two more members (or one member plus an arbiter for the lab) on separate volumes.
   - Point the Ditto services at the set: `MONGO_DB_URI=mongodb://mongodb-0:27017,mongodb-1:27017,mongodb-2:27017/ditto?replicaSet=rs0&w=majority`.
   - Set `retryWrites=true&retryReads=true` on the URI so transient primary step-downs are retried by the driver.
   - *Verify*: `docker exec mongodb mongosh --eval 'rs.status().members.map(m => m.stateStr)'` shows one PRIMARY and ≥1 SECONDARY.; Re-run the same chaos experiment against the primary only; API success rate during injection should stay above the abort threshold and TTR should drop below 15 s.
2. **Add a MongoDB healthcheck and an autoheal sidecar to the compose stack** (operations, effort S, confidence 0.85). In experiment e85e6fa18520 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. Docker's `restart: always` only reacts to a process exit; a container that is running but marked `unhealthy` is never restarted. Without a healthcheck on mongodb nothing even notices a wedged database, and dependents that lost their connection just sit there.
   - Add to the mongodb service: `healthcheck: {test: ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"], interval: 10s, timeout: 5s, retries: 3, start_period: 20s}`.
   - Add an `autoheal` service: `image: willfarrell/autoheal`, `environment: [AUTOHEAL_CONTAINER_LABEL=all]`, mount `/var/run/docker.sock:/var/run/docker.sock:ro`, `restart: always`.
   - Label the Ditto services with `autoheal=true` (or keep `AUTOHEAL_CONTAINER_LABEL=all`) so an `unhealthy` state triggers a restart.
   - *Verify*: `docker inspect --format '{{.State.Health.Status}}' mongodb` reports `healthy` at steady state.; Pause mongodb for 60 s: autoheal logs show a restart of the unhealthy dependents within ~30 s of them flipping to unhealthy.
3. **Tune Ditto's MongoDB client timeouts and connection pool** (configuration, effort S, confidence 0.75). In experiment e85e6fa18520 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. With default driver settings a stalled Mongo socket can hold a request thread for tens of seconds, which inflates p95 and keeps the health endpoints green while users see timeouts. Explicit, short timeouts turn a hang into a fast, retryable error.
   - Set connection/read timeouts on the Ditto services; Ditto reads `MONGO_DB_*` environment variables (e.g. `MONGO_DB_CONNECTION_POOL_SIZE`, `MONGO_DB_READ_PREFERENCE`, `MONGO_DB_QUERY_TIMEOUT`) — verify exact names against `ditto-mongo.conf` in the Ditto source and the operating docs before applying.
   - Alternatively encode them in the URI: `?connectTimeoutMS=3000&socketTimeoutMS=10000&serverSelectionTimeoutMS=5000&maxPoolSize=100`.
   - Keep `serverSelectionTimeoutMS` well below the nginx `proxy_read_timeout` so the edge sees a clean 5xx instead of a hung connection.
   - *Verify*: During a `network_delay` experiment on mongodb the p95 should plateau near the configured timeout instead of growing unbounded.; `docker logs things | grep -i 'MongoTimeout'` shows fast failures rather than minute-long waits.
4. **Set explicit timeouts end to end and add a circuit breaker at the API edge** (configuration, effort S, confidence 0.80). In experiment e85e6fa18520 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. Latency injected into one hop propagated to the user as long hangs instead of fast failures. Without aligned timeouts each layer waits for the slowest one beneath it, request threads pile up, and the whole API degrades rather than the one slow path.
   - nginx: `proxy_connect_timeout 2s; proxy_send_timeout 10s; proxy_read_timeout 30s;` and `proxy_next_upstream timeout`.
   - Ditto gateway: lower the request timeout (`ditto.gateway.http.request-timeout` / `GATEWAY_HTTP_REQUEST_TIMEOUT`, default 60 s) to ~15 s; have clients send the `timeout` query parameter on commands (e.g. `?timeout=10s`) so Ditto fails fast instead of hanging.
   - Add a circuit breaker in front of the gateway (nginx `max_fails`/`fail_timeout`, or Envoy outlier detection) so a degraded backend sheds load for a few seconds instead of queuing.
   - Align Mongo driver `socketTimeoutMS` below the gateway timeout so the failure surfaces as a clean error with a cause.
   - *Verify*: Replay `network_delay` with the same latency: p95 is capped near the smallest configured timeout and the error budget burn is measurable rather than a hang.; No request in the probe log exceeds the nginx `proxy_read_timeout`.
5. **Baseline observability: SLO alert rules and chaos annotations in Grafana** (observability, effort S, confidence 0.65). In experiment e85e6fa18520 (risk LOW 0.4/10), API success fell to 100% during injection, TTR 6 s. Whatever the specific fault, the first production improvement is knowing within a minute that users are affected. Alert on what the user sees (probe success, error rate, p95), not only on container liveness.
   - Prometheus rules: `probe_success == 0 for 1m` (blackbox on `/api/2/things` through nginx), API error-rate SLO burn (`sum(rate(requests{status=~"5.."}[5m])) / sum(rate(requests[5m])) > 0.02`), and `histogram_quantile(0.95, ...) > 1s for 5m`.
   - Add a Grafana annotation (HTTP API `POST /api/annotations`, tag `chaos`) at the start and end of each Faultline experiment so latency spikes are attributable.
   - Dashboard the Docker health state per container (`container_health_status` via cAdvisor or a tiny exporter) next to the API SLIs.
   - *Verify*: During the next experiment an alert fires within 60 s of the first API error and the Grafana graph shows the chaos window.

Raw: `reports/experiments/e85e6fa18520.json`, `reports/risk/e85e6fa18520.json`, `reports/fixes/e85e6fa18520.md`. Reproduce by hand: `docker run --rm -v /var/run/docker.sock:/var/run/docker.sock gaiaadm/pumba:latest netem --duration 15s --tc-image ghcr.io/alexei-led/pumba-alpine-nettools:latest loss --percent 20 bosch-ditto-mongodb-1`

## Safety: the kill switch fired for real

- `1de6fb6a0969` (`container_crash` on `things`): `aborted_early: true`, `abort_reason: error_rate`, detail `{"trailing_samples": 5, "error_rate": 1.0, "p95_ms": null, "threshold": 0.3}`. Rolled back automatically; the target recovered in 26.5 s with remediation actions `[]`, and the after-window shows success 100.0%.
  - +0.8 s `injected` SIGKILL bosch-ditto-things-1
  - +1.9 s `target_condition_observed` docker reports exited/unhealthy
  - +7.2 s `safety_abort` error_rate {"trailing_samples": 5, "error_rate": 1.0, "p95_ms": null, "threshold": 0.3}
  - +7.2 s `rollback_started` 
  - +7.6 s `rollback_done` fault removed
  - +53.8 s `recovered` stable for 20s after rollback, 0 remediation actions
  - +53.8 s `integrity_check` stored seq=74, last acknowledged write seq=74

## How the experiments were chosen

The Topology agent ranks every service by criticality (transitive dependents, state, entry point, datastore, health endpoint). MongoDB ranks first even though no `depends_on` in the compose file mentions it; the environment-variable scan finds it behind four services.

| Rank | Service | Role | Criticality |
|---|---|---|---|
| 1 | `mongodb` | datastore | 9.5 |
| 2 | `policies` | app | 5.0 |
| 3 | `things` | app | 4.0 |
| 4 | `grafana` | other | 3.5 |
| 5 | `prometheus` | other | 2.5 |
| 6 | `connectivity` | app | 2.0 |
| 7 | `things-search` | app | 2.0 |
| 8 | `nginx` | proxy | 1.5 |

- Topology finding: container bosch-ditto-swagger-ui-1 running but not declared in compose
- Topology finding: mongodb: not referenced by any depends_on; 4 services reach it only via environment variables

The planner's ranked shortlist and the model's pick, captured with `faultline plan` before any experiment ran (code ranks; the LLM may only pick one of the top five and write the rationale):

```
 #  priority  service        failure            reasons
------------------------------------------------------------------------------
 1     14.15  mongodb        network_delay      criticality 9.5; untested
 2     14.10  mongodb        db_flood           criticality 9.5; untested
 3     14.05  mongodb        network_loss       criticality 9.5; untested
 4     13.85  mongodb        cpu_stress         criticality 9.5; untested
 5     13.75  mongodb        disk_pressure      criticality 9.5; untested
 6      7.90  policies       container_crash    criticality 5; untested
 7      7.85  policies       network_partition  criticality 5; untested
 8      7.80  policies       container_stop     criticality 5; untested
 9      7.70  policies       container_pause    criticality 5; untested
10      7.40  policies       network_delay      criticality 5; untested
11      7.30  policies       network_loss       criticality 5; untested
12      7.10  policies       cpu_stress         criticality 5; untested

next experiment (llm): network_delay on mongodb {'duration_s': 15, 'delay_ms': 500, 'jitter_ms': 100}
  why: Introduce a brief network delay to MongoDB to observe cascading impact on all dependent services.
  hypothesis: A 15‑second, ~500 ms network delay on MongoDB will cause increased error rates in its dependents and they will recover within half a minute.
  note: LLM (openai:openai/gpt-oss-120b) chose candidate 0
```

## Method and reproducibility

Pipeline: `faultline setup` -> `topology` -> `baseline --duration 60` -> `run` (one experiment at a time, Guardian running) -> `risk` -> `fix` -> `scorecard` -> `narrate`. Severity, scores and fix matching are computed by code; the LLM (open-weights `openai/gpt-oss-120b` via Groq) only picked among code-ranked candidates and rewrote prose fields. See [ARCHITECTURE.md](ARCHITECTURE.md) for the formulas and the safety system.

