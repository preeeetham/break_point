# Faultline architecture

Faultline is a set of agents around a blackboard. The blackboard is the `reports/` directory; the contract is `faultline/schemas.py`; the system under test is one Docker Compose project. This document lists the modules, walks through the lifecycle of one experiment, describes the control room that drives it all, names the data contracts and states the safety invariants.

## Module map

One line per file under `faultline/`.

| File | Purpose |
|---|---|
| `faultline/config.py` | `Settings` dataclass; loads `config/settings.yaml` (optional), then `.env`, then the process environment. `FAULTLINE_*`, `DITTO_*`, `LLM_*` variables |
| `faultline/schemas.py` | Pydantic models for every file on the blackboard (topology, baseline, experiment report, risk, fix, scorecard) |
| `faultline/cli.py` | `faultline` console script: `setup`, `topology`, `baseline`, `catalog`, `plan`, `run`, `rerun`, `risk`, `fix`, `scorecard`, `narrate`, `guardian`, `stop`, `resume`, `heal`, `status`, `dashboard`, `demo`. Also the shared `Session` (docker client, topology, both probes, policy, thresholds) and `build_topology` / `llm_client` helpers, which the dashboard control plane reuses rather than reimplementing |
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
| `faultline/agents/injector.py` | `Injector`: the experiment executor. Preflight, lock, watchdog, inject, observe loop, rollback, recovery and escalation, integrity check, findings, hypothesis validation, report |
| `faultline/planning/planner.py` | `Policy` (protected services, skip roles, run limits), `Planner` (candidate ranking, LLM pick with validation, operator pick, default hypotheses, `ExperimentSpec` assembly) |
| `faultline/planning/llm.py` | `LLMClient` for `ollama` / `openai` / `anthropic`; JSON-mode chat, `<think>` and code-fence stripping; every failure returns `None` |
| `faultline/safety/thresholds.py` | Loads `config/thresholds.yaml`; `thresholds_from(policy, baseline_p95)` turns floors and factors into absolute `Thresholds` |
| `faultline/safety/monitor.py` | `SafetyMonitor.evaluate()` (operator stop, trailing error rate, trailing p95, non-target health failures) and `Watchdog` (independent `threading.Timer`) |
| `faultline/agents/knowledge.py` | Ditto-specific remediation rule library and the matcher (`matching_rules`, specificity: finding code > target > failure type > generic) |
| `faultline/agents/risk.py` | Risk agent: deterministic 0-10 severity and level, templated prose, optional LLM rewording, `run()` over pending reports |
| `faultline/agents/fix.py` | Fix agent: rule recommendations plus up to two LLM extras, Markdown rendering, `advisory_only=True` |
| `faultline/agents/scorecard.py` | Scorecard agent: per-service penalty, score, grade, ranking, Markdown rendering |
| `faultline/agents/guardian.py` | Guardian process: restores overdue containers, cleans helper containers and `tc` qdiscs, obeys `STOP`, writes `guardian.heartbeat.json` |
| `faultline/agents/narrator.py` | Narrator: plain-English play-by-play from the blackboard, optional `--watch` and LLM phrasing |
| `faultline/reporting/store.py` | `ReportStore`: the blackboard. Atomic JSON writes, typed readers, `STOP` file, `ACTIVE_EXPERIMENT.json` lock, `live.json`, heartbeats, per-agent logs |
| `faultline/reporting/markdown.py` | `experiment_markdown()`: renders an `ExperimentReport` as Markdown. Written to `experiments/<id>/report.md` by the injector and returned by `GET /api/experiment/<id>` |
| `faultline/dashboard/control.py` | The control room's control plane. `ControlPlane` owns the long-lived `Session`, the `EventLog`, the `Telemetry` buffer, the single job slot and the per-agent state map; `state()` assembles the one snapshot the UI polls. Talks no HTTP |
| `faultline/dashboard/server.py` | Thin standard-library HTTP adapter over `ControlPlane` (`ThreadingHTTPServer`, HTTP/1.1, `Cache-Control: no-store`, static MIME map, loopback bind). `GET /api/state`, `/api/catalog`, `/api/plan`, `/api/experiment/<id>`; `POST /api/jobs/<kind>`, `/api/jobs/cancel`, `/api/arm`, `/api/disarm`, `/api/stop`, `/api/resume`. Entry points: `faultline dashboard` and `python -m faultline.dashboard.server` |
| `faultline/dashboard/static/index.html` | The shell: header, three columns, bottom row, empty hosts for each panel, `<noscript>` fallback, inline data-URI favicon. One `<script type="module" src="/static/app.js">` and two stylesheets; no other external reference |
| `faultline/dashboard/static/app.js` | Application shell: panel chrome and the explain disclosures, header pills and kill switch, phase ribbon, agent pipeline strip, KPI tiles, event feed, experiments table and detail view, scorecard, toasts, the polling loop, the keyboard map, and defensive loading of the three heavy modules |
| `faultline/dashboard/static/topology.js` | `createTopology(host, {onSelect})`: the live dependency graph. Deterministic layered layout computed once per topology fingerprint (`computeLayout` is exported for testing); every poll only mutates classes, attributes and text, so animations never restart. Renders criticality as node size, edge evidence as edge style, and the predicted vs observed blast radius during a run |
| `faultline/dashboard/static/charts.js` | `createLiveChart` (streaming canvas chart: success-rate area, p95 line, phase bands, abort threshold lines, breach markers, its own pause control), `createSparkline` (KPI micro-chart), `createScoreRing` (SVG resilience gauge). Canvas cannot read CSS custom properties, so design tokens are resolved once and cached |
| `faultline/dashboard/static/launcher.js` | `createLauncher(host, {onLaunch, onPlan, onStop, onJob})`: the injection console. Mode switch, planner block, target and failure cards from `/api/catalog`, clamped parameter dials, hypothesis, safety rails, press-and-hold launch, live-experiment status block, and the Operations buttons. Keeps a `TOUCHED` ledger so `update()` never overwrites a control the operator has touched |
| `faultline/dashboard/static/util.js` | Shared helpers: `el` / `svg` DOM builders, `svgIcon`, formatters (`fmtPct`, `fmtMs`, `fmtDur`, `fmtNum`, `relTime`, `clockTime`, `shortId`), tone mappers (`statusTone`, `severityTone`, `riskTone`, `gradeTone`, `scoreTone`, `roleIcon`), `announce()` into a single polite live region, `reduceMotion`, `hash`, `getJSON` / `postJSON` |
| `faultline/dashboard/static/icons.js` | `ICONS`: name → inline 24x24 stroke-based SVG geometry, `currentColor`. No icon font, no sprite file, no network |
| `faultline/dashboard/static/copy.js` | All explain-mode prose in one place: `EXPLAIN` (per-panel plain-English explanation), `PHASES` and `PHASE_INDEX` (the seven-step ribbon and the mapping from `run.phase`), `FINDING_HELP` (what each finding code means in production terms), `EMPTY` (empty-state text and the action that fills it) |
| `faultline/dashboard/static/tokens.css` | The authoritative design tokens as CSS custom properties on `:root`: surfaces, lines, text, brand, semantic and severity colours, the chaos colour, system font stacks, the dense spacing scale, radii, shadows and motion. Nothing else hardcodes a colour |
| `faultline/dashboard/static/app.css` | Every rule for the shell and the panels the shell owns, consuming only `tokens.css`. Responsive grid: a wider variant ≥1600 px, the contract's three columns at 1280–1599 px, two columns with the launcher as an off-canvas drawer at 768–1279 px (closed by clicking the scrim; the drawer's open control, `.drawer-toggle`, is styled but not yet rendered by `app.js`), single stacked column below 768 px — the only width at which the document itself scrolls. Also focus rings and the `prefers-reduced-motion` overrides |

Configuration and deployment outside the package:

| Path | Purpose |
|---|---|
| `config/thresholds.yaml` | Abort thresholds, recovery criteria, escalation, protected services, run policy |
| `config/ditto_knowledge.yaml` | Health port, cluster alias, role overrides, documented Pekko-cluster edges |
| `deploy/docker-compose.yml` | Ditto 3.9.8 + MongoDB 7 + nginx + ditto-ui + Prometheus + cAdvisor + Blackbox + Grafana; compose project `faultline` |
| `deploy/nginx/` | Reverse proxy config, basic-auth file, CORS snippet |
| `deploy/monitoring/` | Prometheus scrape config (5 s), Blackbox modules, Grafana provisioning and the "Faultline — Ditto resilience" dashboard |
| `scripts/up.sh`, `down.sh`, `status.sh` | Bring the sandbox up (waits for `/health` = UP), down (keeps volumes), show state |
| `scripts/verify_ui.py` | Visual and console verification of the control room. Drives the live page in a real Chrome via Playwright at five breakpoints (1920, 1440, 1024, 820, 390 px), screenshots each, and exits non-zero on console errors or warnings, failed requests, document-level horizontal overflow, an element overflowing its box with `overflow-x: visible`, a visible control shorter than 32 px or narrower than 24 px, or an essentially empty page. Playwright is not a project dependency; a manual check, not a CI gate |
| `docs/UI_CONTRACT.md` | The single source of truth for the control room: design tokens, the HTTP API, the frontend module signatures, the screen layout, explain mode and the accessibility floor. `control.py`, `server.py` and every static module are written against it |
| `tests/` | Offline unit tests; `pytest -q`, no Docker required. There are no tests for the dashboard: `control.py` and the frontend are verified by hand and by `scripts/verify_ui.py` |

## Experiment lifecycle

What happens inside one `faultline run` iteration. Timings come from `config/thresholds.yaml`.

1. **Preflight.** Load `topology.json` and `baseline.json` (refuse if either is missing). Refuse if `reports/STOP` exists or `ACTIVE_EXPERIMENT.json` is present (another run is active). Confirm the target container exists in the compose project and is running. Confirm required images (`pumba`, `tc`) are present for network faults. Measure the pre-experiment window (20 s): if synthetic success is below 95 %, skip with `status: skipped` and a reason. The probes (`ThingsApiProbe`, `HealthProbe`) have been running since the start of the session.
2. **Plan.** `Planner.candidates()` ranks every applicable, policy-allowed (service, failure) pair: `criticality x 1.5 if untested else 1.0 - 1.5 x times tested - 0.5 if degrade-only - 1.0 if same target as last run`. The top five go to the LLM; the answer is validated (index in range, parameters clamped, hypothesis services filtered to known names, rates and seconds bounded). If the LLM is disabled, unreachable or returns nonsense, the top candidate is used with a templated rationale. Operator flags (`--target`, `--failure`, `--param`) bypass ranking but not applicability, policy or clamping. Output: `ExperimentSpec` with `reproduce` commands.
3. **Lock.** Write `ACTIVE_EXPERIMENT.json` with the spec, container name, start time and the hard deadline (`max_experiment_seconds + watchdog_grace_seconds`). The Guardian reads this to know what it may restore and when.
4. **Arm the watchdog.** `Watchdog(90 + 15 s, handle.revert)` on a daemon `threading.Timer`. It fires even if the main loop is blocked in a Docker call.
5. **Inject.** `ACTIONS[failure_type](Context)` performs the fault and returns a `Handle`. Record the `injected` timeline event and `injected_at` for the safety window.
6. **Observe loop.** Once per second until `duration_s` elapses, the handle reports `finished()` (Pumba exited, traffic burst ended) or a verdict says abort: evaluate `SafetyMonitor` (operator stop via `STOP` file, trailing-10 error rate > 30 %, trailing p95 > max(3000 ms, 5 x baseline), any service outside `hypothesis.expected_affected` unhealthy 3 checks in a row); write `live.json` with current stats for the dashboard and narrator; append timeline events for health transitions.
7. **Rollback.** `handle.revert()`: idempotent, guarded by a lock, also called from a `finally` block so an exception anywhere above still rolls back. Container faults restart or unpause; partitions reconnect with the original aliases; Pumba helpers are removed and the `tc` qdisc deleted defensively; marked processes are killed and the flood collection dropped; the fill file is removed; the traffic burst is signalled to stop. Disarm the watchdog.
8. **Recovery wait and escalation.** Poll every 2 s for up to `recovery.timeout_seconds` (300 s). Recovered when success >= 95 % and p95 < max(300 ms, 2 x baseline p95) for 3 consecutive checks, and the target is healthy; a recovery candidate then has to hold through a 20 s soak, and a relapse resets the wait. At 50 % of the timeout (150 s) with no recovery, restart the target; at 80 % (240 s), restart its direct dependents. Each action is appended to `recovery.remediation_actions`. Record `time_to_recover_s`, `outage_duration_s`, `auto_restarted_by_docker`.
9. **Integrity check.** `ThingsApiProbe.integrity_check()`: read the canary's `seq` property back and compare with the last acknowledged write. Lower means `data_loss_suspected`.
10. **Findings and hypothesis validation.** Compute `baseline`, `during`, `after` window stats and per-service `HealthSummary`. Derive findings: `safety_abort`, `health_check_masked_failure` (ready fraction high while API success low for a service on the request path), `no_automated_remediation` (Docker reported `unhealthy` and nothing acted), `manual_restart_required` (escalation acted), `data_loss_suspected`, `cascade_beyond_hypothesis` (affected service not in `expected_affected`), `slow_recovery` (TTR > `expected_recovery_s`), `auto_recovered`. Compare observed max error rate, affected services and recovery time with the hypothesis; set `hypothesis_result.held`.
11. **Report.** Write `experiments/<id>.json` (`ExperimentReport`) atomically; clear `ACTIVE_EXPERIMENT.json`; update `live.json` to idle. Pause 30 s before the next iteration so the next pre-window is clean. Downstream agents (`risk`, `fix`, `scorecard`, `narrate`) run on demand or as part of `demo` and only read the report.

## Control-room architecture

The CLI runs one command and exits. The control room instead keeps state, which is the whole reason it needs a control plane of its own. `faultline/dashboard/control.py` holds all of it; `server.py` is a thin HTTP adapter with no logic beyond routing, JSON and MIME types; the browser renders and nothing more. There is no state in the frontend that matters after a reload, except which experiment rows the operator has expanded.

**The long-lived session.** `ControlPlane.arm()` constructs the same `faultline.cli.Session` the CLI uses — Docker client, topology (loaded from `reports/topology.json` or built if absent), policy, thresholds derived from the baseline, the `ThingsApiProbe` at `probe_rps` (4 req/s) and the `HealthProbe` at `health_interval_s` (3 s) — and keeps it. `disarm()` stops the probes. Jobs that need a live session call `_require_session()`, which arms on demand, so starting an experiment from a cold page works; `GET /api/plan` does the same, which means the planner endpoint has the side effect of starting the probes. An arming failure (no Docker, no Ditto) is caught, recorded in `session.error` and surfaced in the launcher; the page still renders.

**The single-job rule.** Operator actions are `Job`s with an id, a kind (`topology`, `baseline`, `run`, `analyze`, `heal`), a human label, a status and an error. `submit()` takes a lock, refuses with `JobBusy` if one is already running (the server turns that into HTTP 409) and otherwise starts one daemon thread. Only the most recent job is kept. Cancellation is cooperative: `cancel()` sets a `threading.Event` that the run loop checks between experiments and passes to its sleeps, and for a `run` it additionally requests STOP so the in-flight experiment rolls back. This mirrors the injector's own `ACTIVE_EXPERIMENT.json` lock rather than replacing it — both are in force, so a CLI `faultline run` in another terminal still cannot collide with one started from the browser.

**The event log.** `EventLog` is an in-memory ring of 4000 `Event`s, each with a monotonically increasing sequence number, a timestamp, a level (`info`, `success`, `warn`, `error`, `chaos`, `ai`), a source (`system`, `topology`, `planner`, `injector`, `guardian`, `risk`, `fix`, `scorecard`, `probe`) and text. Every agent that the control plane invokes is handed `_logger(source)` as its `log=` callback, so the existing agents narrate into the feed without knowing a UI exists; `infer_level()` guesses the level from the text when the caller does not say. `GET /api/state?since=<seq>` returns only events newer than `<seq>`, at most 300 of them, which makes the feed an append-only delta rather than a list the client re-renders. The log is not persisted: restarting the server empties the narration, while `reports/logs/*.log` and the experiment reports survive.

**The telemetry buffer.** A daemon thread samples once per second (`TELEMETRY_SAMPLE_S`) whenever a session is armed: it asks the probe's `SampleWindow` for stats over the trailing 5 s and appends `{t, success, p95, n}`. The buffer is trimmed to the last 300 s (`TELEMETRY_WINDOW_S`) on read. The same loop maintains the phase bands — `{from, to, phase, experiment_id}` spans, opened when `live.json` reports a non-idle phase and closed when it goes idle again — which is what the chart shades. Resolution is deliberately coarse: the point of the chart is the shape of the fall and the recovery from three metres away, not per-request detail, and 300 one-second points is a payload the browser can re-render every 400 ms without thinking about it.

**The polling contract.** One endpoint, one snapshot, no WebSocket and no server-sent events: `GET /api/state?since=<seq>` returns events, session, job, live run, telemetry, agents, LLM status, topology, baseline, every experiment summary and the scorecard in a single response. The client polls it every 1000 ms when idle and every 400 ms while `run.phase` is not `idle`, skips a poll if one is still in flight, and marks itself offline instead of blanking if a poll fails. Every accessor inside `state()` is wrapped so that one broken section (an unreadable report, a Docker call that hangs) degrades to `null` rather than failing the whole response — the dashboard must never die on a bad request, because it is on a projector.

**Phases.** `_run_state()` derives the live run entirely from `live.json` plus the job in flight: `preflight` is synthesised while a `run` job has a spec but the injector has not written anything yet, `analyzing` while an `analyze` job is running, and otherwise the phase is whatever the injector last wrote — `injecting`, `recovering`, `soaking` or `idle`. `copy.js` maps those onto the seven-step ribbon (`PHASE_INDEX`), with `injecting` lighting the Observe step. Inject and Rollback are ribbon steps rather than reported phases, and `safety.abort_reason` is not written to `live.json`, so an abort reaches the UI with the finished report rather than live.

**Per-agent state.** Separate from the event log, `_agent(name, state, detail)` keeps one row per agent (`idle` / `working` / `done` / `error`, a timestamp and one line of detail) for the pipeline strip. The Guardian's row is not written by the control plane at all: it is derived from the freshness of `guardian.heartbeat.json` (alive if under 15 s old), because the Guardian is a separate process by design.

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

Coordination files without a model: `STOP` (presence is the signal; content is reason and timestamp), `live.json` (free-form dict with `updated_at`, written by the injector once per second and the only place the control plane learns the live phase from), `<agent>.heartbeat.json` (free-form dict with `at`), `logs/<agent>.log`, and `experiments/<id>/` (the directory `ExperimentReport.raw_logs_ref` points at: `report.md` plus the last 120 log lines of the target and of every service the hypothesis expected to be affected).

## Safety invariants

These hold regardless of what the LLM, the operator or a failing experiment does.

1. **Scope.** Every container operation goes through `DockerController`, which lists containers by the label `com.docker.compose.project=<FAULTLINE_COMPOSE_PROJECT>`. Containers outside that project cannot be addressed. Helper containers carry `faultline.helper=1` and are the only things Faultline creates.
2. **Code owns the limits.** Thresholds come from `config/thresholds.yaml` and the baseline. The LLM receives candidate indices and parameter bounds, never the thresholds, and its output is clamped into the catalog bounds. A model can make an experiment shorter or lighter, never longer or heavier.
3. **Bounded parameters.** Every catalog parameter has `lo` and `hi`; `clamp()` is applied to LLM and operator input alike, and every clamp is recorded in `planner_notes`.
4. **Bounded time.** Hard cap of `max_experiment_seconds` (90 s) on the fault, enforced by the observe loop, plus an independent `Watchdog` at 90 + 15 s that calls `revert()` from its own thread. Recovery waiting is capped at `recovery.timeout_seconds` (300 s).
5. **Idempotent rollback.** `Handle.revert()` runs at most once (lock + flag) and is safe from any thread. It is called on normal completion, on abort, from the watchdog, from a `finally` block and, as a last resort, by the Guardian.
6. **One experiment at a time.** `ACTIVE_EXPERIMENT.json` is the lock; a run refuses to start while it exists. The control room adds a second, independent constraint on top: exactly one job runs at a time per server process, and a second request is refused with HTTP 409 rather than queued.
7. **Do not break a sick system.** Injection is refused when the 20 s pre-experiment window shows less than 95 % success, and `baseline` warns when the steady state itself is below 99 %.
8. **Protected services.** `mongodb` accepts only degrade-only faults unless `--allow-protected` is passed; the override is recorded in the report. The `ui` role and the observability containers are never targets.
9. **Blast radius is part of the hypothesis.** Any service outside `expected_affected` that fails 3 consecutive health checks aborts the experiment, and any service affected but not predicted produces a `cascade_beyond_hypothesis` finding.
10. **Kill switch from anywhere.** `reports/STOP` is checked every second by the safety monitor and by the Guardian; it is written by `faultline stop`, by `POST /api/stop` (the control room's STOP button and the `Esc` key), or by hand. `POST /api/stop` is deliberately not a job: it is never queued, never blocked by a running job, and always answers 200.
11. **Independent Guardian.** A separate process with its own Docker client that only restores: it starts containers that are down past the deadline in `ACTIVE_EXPERIMENT.json`, removes stale helpers and `tc` qdiscs, and never injects.
12. **Advisory only.** No agent has a code path that modifies the compose file, container configuration or Ditto settings. `FixRecommendation.advisory_only` is always `true`.
13. **Reproducible.** Every report carries the shell commands equivalent to the injection under `reproduce`, so any finding can be verified without Faultline.
