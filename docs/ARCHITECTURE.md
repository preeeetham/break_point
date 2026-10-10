# Faultline architecture

Faultline is a set of agents around a blackboard. The blackboard is the `reports/` directory; the contract is `faultline/schemas.py`; the system under test is one Docker Compose project. This document lists the modules, walks through the lifecycle of one experiment, names the data contracts and states the safety invariants.

## Module map

One line per file under `faultline/`.

| File | Purpose |
|---|---|
| `faultline/config.py` | `Settings` dataclass; loads `config/settings.yaml` (optional), then `.env`, then the process environment. `FAULTLINE_*`, `DITTO_*`, `LLM_*` variables |
| `faultline/schemas.py` | Pydantic models for every file on the blackboard (topology, baseline, experiment report, risk, fix, scorecard) |
| `faultline/cli.py` | `faultline` console script: `setup`, `topology`, `baseline`, `catalog`, `plan`, `run`, `rerun`, `risk`, `fix`, `scorecard`, `narrate`, `guardian`, `stop`, `resume`, `status`, `dashboard`, `demo` (in progress at the time of writing) |
| `faultline/topology/compose.py` | Static topology: compose services to `ServiceNode`s; edges from `depends_on`, environment-variable hostname scan, nginx `proxy_pass`, documented edges from `config/ditto_knowledge.yaml` |
| `faultline/topology/live.py` | Enriches nodes from `docker inspect`: container name, state, health, restart policy, memory limit, network aliases; flags containers missing or undeclared |
| `faultline/topology/graph.py` | Assembles the `Topology`: criticality scoring (transitive dependents + stateful/entrypoint/datastore/no-health bonuses), structural findings, console summary |
| `faultline/topology/render.py` | Topology as Mermaid and Markdown |
| `faultline/observe/window.py` | `Sample`, `SampleWindow` (thread-safe deque) and `WindowStats` computation (success rate, p50/p95/p99, per-operation, status counts) |
| `faultline/observe/probes.py` | `ThingsApiProbe` (synthetic traffic through nginx with a sequence-number canary and `integrity_check()`), `HealthProbe` (`/alive`, `/ready` via `docker exec`, `mongosh ping`, Docker health), `summarize_health()` |
| `faultline/observe/baseline.py` | Captures the steady-state window, derives `Thresholds` from it, renders `baseline.md` |
| `faultline/chaos/catalog.py` | The ten `FailureType`s with parameter bounds, role applicability, protected-service semantics (`degrade_only`), `clamp()` and `reproduce_commands()` |
| `faultline/chaos/actions.py` | `apply_*` functions, one per failure type, each returning an idempotent `Handle` with `revert()` and `finished()` |
| `faultline/chaos/docker_ctl.py` | `DockerController`: thin docker-py wrappers scoped to one compose project label; lifecycle, exec, network connect/disconnect, helper containers (label `faultline.helper=1`) |
| `faultline/chaos/runner.py` | Experiment executor: preflight, lock, watchdog, inject, observe loop, rollback, recovery, integrity, findings, report (in progress at the time of writing; name may differ) |
| `faultline/planning/planner.py` | `Policy` (protected services, skip roles, run limits), `Planner` (candidate ranking, LLM pick with validation, operator pick, default hypotheses, `ExperimentSpec` assembly) |
| `faultline/planning/llm.py` | `LLMClient` for `ollama` / `openai` / `anthropic`; JSON-mode chat, `<think>` and code-fence stripping; every failure returns `None` |
| `faultline/safety/thresholds.py` | Loads `config/thresholds.yaml`; `thresholds_from(policy, baseline_p95)` turns floors and factors into absolute `Thresholds` |
| `faultline/safety/monitor.py` | `SafetyMonitor.evaluate()` (operator stop, trailing error rate, trailing p95, non-target health failures) and `Watchdog` (independent `threading.Timer`) |
| `faultline/agents/knowledge.py` | Ditto-specific remediation rule library and the matcher (`matching_rules`, specificity: finding code > target > failure type > generic) |
| `faultline/agents/risk.py` | Risk agent: deterministic 0-10 severity and level, templated prose, optional LLM rewording, `run()` over pending reports |
| `faultline/agents/fix.py` | Fix agent: rule recommendations plus up to two LLM extras, Markdown rendering, `advisory_only=True` |
| `faultline/agents/scorecard.py` | Scorecard agent: per-service penalty, score, grade, ranking, Markdown rendering |
| `faultline/agents/guardian.py` | Guardian process: restores overdue containers, cleans helper containers and `tc` qdiscs, obeys `STOP`, writes `guardian.heartbeat.json` (in progress at the time of writing) |
| `faultline/agents/narrator.py` | Narrator: plain-English play-by-play from the blackboard, optional `--watch` and LLM phrasing (in progress at the time of writing) |
| `faultline/reporting/store.py` | `ReportStore`: the blackboard. Atomic JSON writes, typed readers, `STOP` file, `ACTIVE_EXPERIMENT.json` lock, `live.json`, heartbeats, per-agent logs |
| `faultline/dashboard/server.py` | Standard-library HTTP server: `GET /`, `GET /api/state`, `GET /api/experiment/<id>`, `POST /api/stop`, `POST /api/resume` |
| `faultline/dashboard/static/index.html` | Single-page dashboard: topology, baseline, live experiment, reports, scorecard, STOP / RESUME button |

Configuration and deployment outside the package:

| Path | Purpose |
|---|---|
| `config/thresholds.yaml` | Abort thresholds, recovery criteria, escalation, protected services, run policy |
| `config/ditto_knowledge.yaml` | Health port, cluster alias, role overrides, documented Pekko-cluster edges |
| `deploy/docker-compose.yml` | Ditto 3.9.8 + MongoDB 7 + nginx + ditto-ui + Prometheus + cAdvisor + Blackbox + Grafana; compose project `faultline` |
| `deploy/nginx/` | Reverse proxy config, basic-auth file, CORS snippet |
| `deploy/monitoring/` | Prometheus scrape config (5 s), Blackbox modules, Grafana provisioning and the "Faultline — Ditto resilience" dashboard |
| `scripts/up.sh`, `down.sh`, `status.sh` | Bring the sandbox up (waits for `/health` = UP), down (keeps volumes), show state |
| `tests/` | Offline unit tests; `pytest -q`, no Docker required |

## Experiment lifecycle

What happens inside one `faultline run` iteration. Timings come from `config/thresholds.yaml`.

1. **Preflight.** Load `topology.json` and `baseline.json` (refuse if either is missing). Refuse if `reports/STOP` exists or `ACTIVE_EXPERIMENT.json` is present (another run is active). Confirm the target container exists in the compose project and is running. Confirm required images (`pumba`, `tc`) are present for network faults. Measure the pre-experiment window (20 s): if synthetic success is below 95 %, skip with `status: skipped` and a reason. The probes (`ThingsApiProbe`, `HealthProbe`) have been running since the start of the session.
2. **Plan.** `Planner.candidates()` ranks every applicable, policy-allowed (service, failure) pair: `criticality x 1.5 if untested else 1.0 - 1.5 x times tested - 0.5 if degrade-only - 1.0 if same target as last run`. The top five go to the LLM; the answer is validated (index in range, parameters clamped, hypothesis services filtered to known names, rates and seconds bounded). If the LLM is disabled, unreachable or returns nonsense, the top candidate is used with a templated rationale. Operator flags (`--target`, `--failure`, `--param`) bypass ranking but not applicability, policy or clamping. Output: `ExperimentSpec` with `reproduce` commands.
3. **Lock.** Write `ACTIVE_EXPERIMENT.json` with the spec, container name, start time and the hard deadline (`max_experiment_seconds + watchdog_grace_seconds`). The Guardian reads this to know what it may restore and when.
4. **Arm the watchdog.** `Watchdog(90 + 15 s, handle.revert)` on a daemon `threading.Timer`. It fires even if the main loop is blocked in a Docker call.
5. **Inject.** `ACTIONS[failure_type](Context)` performs the fault and returns a `Handle`. Record the `injected` timeline event and `injected_at` for the safety window.
6. **Observe loop.** Once per second until `duration_s` elapses, the handle reports `finished()` (Pumba exited, traffic burst ended) or a verdict says abort: evaluate `SafetyMonitor` (operator stop via `STOP` file, trailing-10 error rate > 30 %, trailing p95 > max(3000 ms, 5 x baseline), any service outside `hypothesis.expected_affected` unhealthy 3 checks in a row); write `live.json` with current stats for the dashboard and narrator; append timeline events for health transitions.
7. **Rollback.** `handle.revert()`: idempotent, guarded by a lock, also called from a `finally` block so an exception anywhere above still rolls back. Container faults restart or unpause; partitions reconnect with the original aliases; Pumba helpers are removed and the `tc` qdisc deleted defensively; marked processes are killed and the flood collection dropped; the fill file is removed; the traffic burst is signalled to stop. Disarm the watchdog.
8. **Recovery wait and escalation.** Poll every 2 s for up to 240 s. Recovered when success >= 95 % and p95 < max(300 ms, 2 x baseline p95) for 3 consecutive checks, and the target is healthy. At 50 % of the timeout (120 s) with no recovery, restart the target; at 80 % (192 s), restart its direct dependents. Each action is appended to `recovery.remediation_actions`. Record `time_to_recover_s`, `outage_duration_s`, `auto_restarted_by_docker`.
9. **Integrity check.** `ThingsApiProbe.integrity_check()`: read the canary's `seq` property back and compare with the last acknowledged write. Lower means `data_loss_suspected`.
10. **Findings and hypothesis validation.** Compute `baseline`, `during`, `after` window stats and per-service `HealthSummary`. Derive findings: `safety_abort`, `health_check_masked_failure` (ready fraction high while API success low for a service on the request path), `no_automated_remediation` (Docker reported `unhealthy` and nothing acted), `manual_restart_required` (escalation acted), `data_loss_suspected`, `cascade_beyond_hypothesis` (affected service not in `expected_affected`), `slow_recovery` (TTR > `expected_recovery_s`), `auto_recovered`. Compare observed max error rate, affected services and recovery time with the hypothesis; set `hypothesis_result.held`.
11. **Report.** Write `experiments/<id>.json` (`ExperimentReport`) atomically; clear `ACTIVE_EXPERIMENT.json`; update `live.json` to idle. Pause 30 s before the next iteration so the next pre-window is clean. Downstream agents (`risk`, `fix`, `scorecard`, `narrate`) run on demand or as part of `demo` and only read the report.

## Data contracts

All in `faultline/schemas.py`. Each blackboard file validates against exactly one top-level model.

| Model | File | Purpose |
|---|---|---|
| `ServiceNode` | in `Topology` | one compose service: image, role (`datastore` / `app` / `proxy` / `ui` / `other`), stateful, entrypoint, health port, memory limit, aliases, chaos relevance, live container state |
| `Edge` | in `Topology` | `src` depends on `dst`, with `kind` (`depends_on`, `env`, `proxy`, `documented`, `observed`) and evidence text |
| `Criticality` | in `Topology` | per-service fan-in, transitive dependents, score, reasons |
| `Topology` | `topology.json` | services, edges, criticality ranking, structural findings; helper methods for dependents and dependencies |
| `WindowStats` | in `Baseline`, `ExperimentReport` | sample count, success rate, p50/p95/p99, per-operation breakdown, status counts |
| `Thresholds` | in `Baseline`, `SafetyOutcome` | absolute abort and recovery limits derived from the YAML floors/factors and the baseline p95 |
| `Baseline` | `baseline.json` | steady-state API stats, per-service health availability, derived thresholds, notes |
| `HealthSummary` | in `ExperimentReport` | per-service alive/ready fractions, whether Docker reported unhealthy, whether the container was seen down |
| `Hypothesis` | in `ExperimentSpec`, `ExperimentReport` | falsifiable prediction: statement, expected affected services, expected max error rate, expected recovery seconds |
| `ExperimentSpec` | transient (also in `ACTIVE_EXPERIMENT.json`) | what will be run: target, container, failure type, clamped parameters, rationale, hypothesis, `planned_by` (`llm` / `deterministic` / `operator`), planner notes, reproduce commands |
| `TimelineEvent` | in `ExperimentReport` | timestamped event with `t_plus_s` |
| `Finding` | in `ExperimentReport`, `RiskAssessment` | code, severity (`info` to `critical`), title, evidence |
| `SafetyOutcome` | in `ExperimentReport` | aborted early, reason and detail, watchdog fired, operator stop, thresholds in force |
| `RecoveryOutcome` | in `ExperimentReport` | recovered, time to recover, outage duration, Docker auto-restart, remediation actions, data integrity result |
| `HypothesisResult` | in `ExperimentReport` | held, observed max error rate, observed recovery, unexpected affected services, notes |
| `ExperimentReport` | `experiments/<id>.json` | the complete record of one experiment: spec fields, baseline/during/after stats, health summaries, recovery, safety, findings, timeline, reproduce, status (`completed` / `aborted` / `failed` / `skipped`) |
| `RiskAssessment` | `risk/<id>.json` | severity 0-10, level, blast radius, user impact, recovery seconds, factor breakdown, findings, summary, production implication, `generated_by` |
| `Recommendation` | in `FixRecommendation` | title, category, effort S/M/L, confidence, rationale, steps, verify, references |
| `FixRecommendation` | `fixes/<id>.json` | recommendations for one experiment, `advisory_only` (always true), `generated_by` |
| `ServiceScore` | in `Scorecard` | per-service score, grade, experiment count, worst success rate, slowest recovery, finding count, notes |
| `Scorecard` | `scorecard.json` | overall score, ranking worst first, experiment total, summary |

Coordination files without a model: `STOP` (presence is the signal; content is reason and timestamp), `live.json` (free-form dict with `updated_at`), `<agent>.heartbeat.json` (free-form dict with `at`), `logs/<agent>.log`.

## Safety invariants

These hold regardless of what the LLM, the operator or a failing experiment does.

1. **Scope.** Every container operation goes through `DockerController`, which lists containers by the label `com.docker.compose.project=<FAULTLINE_COMPOSE_PROJECT>`. Containers outside that project cannot be addressed. Helper containers carry `faultline.helper=1` and are the only things Faultline creates.
2. **Code owns the limits.** Thresholds come from `config/thresholds.yaml` and the baseline. The LLM receives candidate indices and parameter bounds, never the thresholds, and its output is clamped into the catalog bounds. A model can make an experiment shorter or lighter, never longer or heavier.
3. **Bounded parameters.** Every catalog parameter has `lo` and `hi`; `clamp()` is applied to LLM and operator input alike, and every clamp is recorded in `planner_notes`.
4. **Bounded time.** Hard cap of `max_experiment_seconds` (90 s) on the fault, enforced by the observe loop, plus an independent `Watchdog` at 90 + 15 s that calls `revert()` from its own thread. Recovery waiting is capped at 240 s.
5. **Idempotent rollback.** `Handle.revert()` runs at most once (lock + flag) and is safe from any thread. It is called on normal completion, on abort, from the watchdog, from a `finally` block and, as a last resort, by the Guardian.
6. **One experiment at a time.** `ACTIVE_EXPERIMENT.json` is the lock; a run refuses to start while it exists.
7. **Do not break a sick system.** Injection is refused when the 20 s pre-experiment window shows less than 95 % success, and `baseline` warns when the steady state itself is below 99 %.
8. **Protected services.** `mongodb` accepts only degrade-only faults unless `--allow-protected` is passed; the override is recorded in the report. The `ui` role and the observability containers are never targets.
9. **Blast radius is part of the hypothesis.** Any service outside `expected_affected` that fails 3 consecutive health checks aborts the experiment, and any service affected but not predicted produces a `cascade_beyond_hypothesis` finding.
10. **Kill switch from anywhere.** `reports/STOP` is checked every second by the safety monitor and by the Guardian; it is written by `faultline stop`, by `POST /api/stop` on the dashboard, or by hand.
11. **Independent Guardian.** A separate process with its own Docker client that only restores: it starts containers that are down past the deadline in `ACTIVE_EXPERIMENT.json`, removes stale helpers and `tc` qdiscs, and never injects.
12. **Advisory only.** No agent has a code path that modifies the compose file, container configuration or Ditto settings. `FixRecommendation.advisory_only` is always `true`.
13. **Reproducible.** Every report carries the shell commands equivalent to the injection under `reproduce`, so any finding can be verified without Faultline.
