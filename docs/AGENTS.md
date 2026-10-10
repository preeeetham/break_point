# Faultline agent inventory

**What this is.** A ground-truth list of every agent that exists in this codebase right now, written
by reading the implementation of each one rather than its docstring, the README, or the build prompt
that asked for it. Where the code disagrees with the README, the code wins and the disagreement is
recorded in [Drift from README](#drift-from-readme) at the end.

**Audience.** A teammate picking the project up, or a judge asking "what does this one actually do."

**As of.** 2026-10-10, working tree (not `HEAD` — see the note below).

> **Read this before trusting git.** The repo has only two commits, and most of the work is
> uncommitted. `faultline/agents/scout.py`, `faultline/agents/reconcile.py`, `faultline/onboarding/`
> and `faultline/dashboard/control.py` are all **untracked**, and `cli.py`, `planner.py`,
> `injector.py`, `README.md` and `dashboard/server.py` have uncommitted modifications. The last
> commit message, "Weakness Scout: schema + blackboard slots for predicted weaknesses", describes a
> much earlier state than what is on disk — the Scout is now fully implemented. Judge the code by
> the working tree, not the log.
>
> **Test status.** `python -m pytest` → **384 passed, 9 failed, 4 xfailed.** All 9 failures are in `tests/test_scout.py`, a 90-test file for the Weakness Scout that landed on disk *while this inventory was being written* (file mtime 23:05). The suite was green at 272 passed before it arrived. See [drift item 15](#c-real-wiring-gaps-found-in-the-code-not-readmes-fault) for what the failures actually say — they are worth reading, because at least two of them look like they have found a real behaviour quirk in the Scout rather than a mistake in the tests.

---

## 1. Summary table

| # | Agent | Lives in | Status | One-line purpose |
|---|---|---|---|---|
| 1 | **Topology / Mapping** | `faultline/topology/` | Fully built and wired | Works out which services exist and what depends on what, including dependencies the compose file never declares, then ranks services by how much breaks if they fail. |
| 2 | **Repo Onboarding** | `faultline/onboarding/` | Fully built and wired | Points Faultline at an unfamiliar Docker Compose repo and writes a reviewed "documented knowledge" file for it. |
| 3 | **Weakness Scout** | `faultline/agents/scout.py` | Fully built and wired — **but never yet run against this repo's `reports/`, and 9 of its brand-new 90 tests are failing** | Reads the config and *predicts* where the system will break, before anything is injected. |
| 4 | **Reconcile** | `faultline/agents/reconcile.py` | Fully built; **CLI-only, absent from the control room** | Scores the Scout's predictions against what the experiments actually found: confirmed, cleared or untested. |
| 5 | **Failure Injection — Planner** | `faultline/planning/` | Fully built and wired | Ranks every (service, failure type) pair in code and lets the LLM pick one from the top five, within code-enforced bounds. |
| 6 | **Failure Injection — Executor** | `faultline/agents/injector.py`, `faultline/chaos/` | Fully built and wired | Runs one experiment end to end: inject, watch, roll back, wait for recovery, write the report. |
| 7 | **Safety monitor + watchdog** | `faultline/safety/` | Fully built and wired | The in-process kill switch: aborts an experiment within ~1 s on error rate, latency, collateral damage or operator STOP. |
| 8 | **Guardian** | `faultline/agents/guardian.py` | Fully built; **separate process, started by hand** | The second, independent kill switch that restores the sandbox even if the injector is dead. |
| 9 | **Risk / Impact** | `faultline/agents/risk.py` | Fully built and wired | Turns one experiment's numbers into a 0–10 severity with a named-factor breakdown. |
| 10 | **Fix Recommendation** | `faultline/agents/fix.py` + `knowledge.py` | Fully built and wired | Matches findings against a library of 18 Ditto-specific remediations. Advisory only — nothing is ever applied. |
| 11 | **Resilience Scorecard** | `faultline/agents/scorecard.py` | Fully built and wired | Scores each service 0–100, grades it A–F and ranks the weakest first. |
| 12 | **Narrator** | `faultline/agents/narrator.py` | Fully built; **CLI-only, writes nothing to the blackboard** | Reads the blackboard and prints a plain-English play-by-play. |
| 13 | **Approval** | — | **Specified but not started: zero code** | Nothing exists. See [§13](#13-approval-agent). |

Deterministic vs. LLM at a glance:

| Agent | LLM involvement |
|---|---|
| Topology, Reconcile, Safety, Guardian, Scorecard | **None at all.** No LLM import, no call path. |
| Injector (executor) | **None.** It never calls a model; findings and hypothesis verdicts are pure code. |
| Planner | LLM **chooses** among the top 5 code-ranked candidates and tunes parameters; code clamps and validates. |
| Scout | LLM writes prose only; it cannot add, remove, re-rank or re-score a weakness. |
| Risk | LLM rewrites two prose fields only; severity is pure code. |
| Fix | Rules propose; LLM may add ≤2 schema-validated, confidence-capped extras. |
| Onboarding | LLM proposes knowledge; code rejects anything it cannot verify. |
| Narrator | LLM writes one optional closing paragraph, opt-in via `--llm`. |

---

## 2. Detailed inventory

### 1. Topology / Mapping agent

- **Lives in** [faultline/topology/](../faultline/topology/) — `compose.py` (parsers), `graph.py` (assembly + criticality), `live.py` (Docker enrichment), `render.py` (Markdown/Mermaid).
- **Status:** fully built and wired in.
- **Trigger:** `faultline topology [--no-live] [--knowledge FILE]` ([cli.py:522](../faultline/cli.py#L522)); the control room's `topology` job (`POST /api/jobs/topology` → `ControlPlane._job_topology`); step 1 of `faultline demo`; and lazily by `Session.__init__` ([cli.py](../faultline/cli.py)) whenever a live command finds no `topology.json` on disk.
- **Inputs:** `deploy/docker-compose.yml`, `deploy/nginx/nginx.conf`, `config/ditto_knowledge.yaml` (overridable with `--knowledge`), and live `docker inspect` of the compose project unless `--no-live`.
- **Outputs:** `reports/topology.json` + `reports/topology.md`.
- **Deterministic vs. LLM:** **100% deterministic.** No LLM import anywhere in `faultline/topology/`.
- **How it actually works:** five edge sources, de-duplicated on `(src, dst, kind)` — compose `depends_on`; an environment-variable scan where the key matches `(_HOSTNAME|_HOST|_URI|_URL|_ADDR|_ADDRESS|_ENDPOINT|_SEED_NODES)$` and the value resolves to another service name or network alias; nginx `proxy_pass` targets; `documented_edges` from the knowledge file; and live container state. Criticality (`graph.criticality`) = transitive dependents + 2 if stateful + 1 if entrypoint + 1 if datastore + 0.5 if no application health endpoint. It also emits structural findings of its own, e.g. "not referenced by any `depends_on`; N services reach it only via environment variables".
- **Safety role:** none directly, but it is load-bearing for safety — `relevant_services()` defines who the safety monitor watches as collateral, and `role` in `("ui", "other")` sets `chaos_relevant=False`, which exempts a service from every experiment.
- **Plain English:** It draws the map of the application by itself, including the connections nobody wrote down, and tells you which service would hurt most if it died.

### 2. Repo Onboarding agent

- **Lives in** [faultline/onboarding/](../faultline/onboarding/) — `source.py` (resolve a reference to files on disk), `agent.py` (extract → one bounded LLM pass → validate → write → compare).
- **Status:** fully built and wired in. 84 tests in `tests/test_onboarding.py`.
- **Trigger:** `faultline onboard <repo> [--out DIR] [--compose PATH] [--ref REF] [--compare-to FILE] [--dry-run] [--force] [--no-llm]` ([cli.py:553](../faultline/cli.py#L553)); the control room's `onboard` job (`ControlPlane._job_onboard`). Never runs as part of the pipeline — it is a one-off setup step.
- **Inputs:** a GitHub URL, `owner/name`, or a local path. Shallow-clones into `.cache/onboarding/`, then locates and reads the compose file, the README (trimmed to 5,000 chars), up to 6 architecture doc files (16,000 chars), any nginx conf, plus `config/ditto_knowledge.yaml` as a worked example in the prompt. One LLM call, up to 3 attempts with shrink-and-retry on provider size errors.
- **Outputs:** `<out>/<slug>_knowledge.yaml` (default `config/`) — **not** a file in `reports/`. This is the one agent that writes outside the blackboard. Also returns an `OnboardingReport` (`--json`) and an F1-style comparison against a hand-authored file (`--compare-to`).
- **Deterministic vs. LLM:** textbook "LLM proposes, code disposes", and the enforcement is unusually strict. The model proposes `documented_edges`, `roles`, `health_port`, `cluster_alias` and `references`. `validate()` then rejects: edges naming a service that does not exist, self-edges, edges duplicating a structural edge with under 12 chars of new reason, and **edges whose evidence borrows vocabulary from the Ditto worked example** (contamination guard, `example_vocabulary`/`own_vocabulary`). `health_port` proposed by the model is **overridden** by one derived from the compose healthchecks when two or more agree. `cluster_alias` must be a real declared network alias. References copied from the example are dropped.
  - The sharpest guard: marking a service `ui`/`other` sets `chaos_relevant=False` and so exempts it from all testing. Code refuses that if the service is **built from this repo's source** and not recognisable as tooling, or if **anything non-proxy depends on it**. The model cannot quietly shrink the system under test.
- **Safety role:** gates what the model is allowed to assert about an unfamiliar repo, and refuses to let the LLM exempt a product service from chaos. It never touches a running stack.
- **Plain English:** Give it any Docker Compose project and it reads the repo's own documentation to write down the dependencies the compose file leaves unsaid — then throws out anything it cannot verify against the actual files.

### 3. Weakness Scout

- **Lives in** [faultline/agents/scout.py](../faultline/agents/scout.py) (866 lines).
- **Status:** **fully built and wired in — with two honest caveats.** The implementation is complete (7 deterministic checks, scoring, suggested experiments, LLM prose pass, Markdown renderer) and it is reachable from the CLI, the control room and the UI. But: (a) **`reports/weaknesses.json` does not exist in either `reports/` or `reports-demo/`** — the Scout has never actually been run against this repo's blackboard, so no downstream consumer has ever seen real output; (b) **`tests/test_scout.py` landed at 23:05, mid-inventory, and 9 of its 90 tests fail** — 112 pass, 4 xfail. Two of the failures appear to have caught a real quirk (the Scout can essentially never report a *clean* deployment, because `is_top_tier()` is relative and so always matches the top-scoring service); the other seven look like harness mismatches against `_llm_pass`'s control flow. Full analysis in [drift item 15](#c-real-wiring-gaps-found-in-the-code-not-readmes-fault). Until that file is green, the Scout's 866 lines of check logic remain the least-verified code in the project.
- **Trigger:** `faultline scout [--json] [--no-llm]` ([cli.py:526](../faultline/cli.py#L526)); the control room's `scout` job (`_job_scout`, [control.py:377](../faultline/dashboard/control.py#L377)); step 2 of `faultline demo`. If no `topology.json` exists it builds one with `--no-live` first, since the Scout is static analysis and works with the stack down.
- **Inputs:** `reports/topology.json`, the compose file, `config/ditto_knowledge.yaml`, `config/thresholds.yaml` (for the protected-service policy), and the nginx config text. Optionally one LLM call.
- **Outputs:** `reports/weaknesses.json` + `reports/weaknesses.md`.
- **Deterministic vs. LLM:** the strictest split in the project. **The LLM never decides whether a weakness exists.** Seven deterministic checks produce the final list:

  | Check | Fires when |
  |---|---|
  | `missing_health_check` | A critical service declares no `healthcheck:`, or `disable: true`. |
  | `shallow_health_check` | A service *with dependencies* has a healthcheck matching only a liveness pattern (`/alive`, `/ping`, `pgrep`, `nc -z`…) and never names a readiness endpoint or a dependency. This is the static predictor for Ditto #1865. |
  | `no_redundancy` | A top-tier service (≥ 40% of the deployment's max criticality) runs as exactly one instance. |
  | `no_resource_limits` | A critical service has no memory and/or no CPU limit. |
  | `weak_restart_policy` | `restart:` unset or `no` on a critical service. *Expected to stay silent on Ditto, which sets `restart: always` everywhere.* |
  | `no_retry_or_backoff` | ≥ 2 services reach the same critical dependency with no retry/backoff/timeout/circuit-breaker token in any env var naming it. |
  | `port_beyond_ingress` | A service publishes a port that is not the declared ingress. |
  | `no_rate_limiting` | The sole ingress shows no `limit_req`/`limit_conn`/throttle setting in its proxy config or env. |

  Severity is deterministic and additive, clamped to 10: `3.5 × (criticality / max criticality) + category weight + 2.0 × (dependents / services) + 0.5 × compounding + 1.0 if stateful + 0.5 if protected`. The LLM gets exactly two jobs: rewrite each `why_it_matters` sentence (≤ 600 chars, falls back to a per-category template), and optionally name up to 2 `systemic_patterns` that must cite ≥ 2 **existing** weakness ids — a pattern citing nothing from the list is discarded as invention. `generated_by` records which path ran.
- **Labelling discipline:** `PREDICTED_LABEL = "[predicted, unconfirmed]"` rides on the report and on every weakness, and the CLI, Markdown and UI all render it verbatim. The code is explicit that a predicted severity must never be confused with the Risk agent's measured one.
- **Each weakness also nominates a real experiment** (`resolve_experiment`): the first preference that is a genuine catalog type, applicable to the target's role, **and allowed by the safety policy**. If everything is blocked it says so in `policy_note` instead of nominating something that cannot run. Note the target is not always the weak service — testing a liveness-only health check on `things` means breaking what `things` depends on.
- **Safety role:** purely advisory. It cannot halt, roll back or gate anything.
- **Plain English:** Before breaking anything, it reads the configuration like a careful reviewer and predicts where this system will fail — and labels every one of those guesses as a guess until an experiment proves it.

### 4. Reconcile agent

- **Lives in** [faultline/agents/reconcile.py](../faultline/agents/reconcile.py).
- **Status:** fully built. **CLI-only** — it is absent from the control room entirely (no `reconcile` job kind, no state in `ControlPlane.state()`, no UI panel; `grep -rn reconcil faultline/dashboard/` returns nothing). 23 tests.
- **Trigger:** `faultline scout --reconcile` only ([cli.py](../faultline/cli.py), `cmd_scout`). Not part of `faultline demo`.
- **Inputs:** `reports/weaknesses.json` and every `reports/experiments/*.json`. Nothing else. Returns `None` with a helpful message if the Scout has not run.
- **Outputs:** `reports/weaknesses_reconcile.json` + `reports/weaknesses_reconcile.md`.
- **Deterministic vs. LLM:** **pure arithmetic, no LLM anywhere in the module.**
- **How it judges:** an experiment "bears on" a weakness if its target, or its observed collateral, is one of the weakness's services. Then:
  - **confirmed** — a bearing experiment raised one of the Scout's nominated finding codes **above `info` severity**. The severity filter is load-bearing: `api_impact` is written on *every* experiment, at `info` when the API was fine, so counting `info` matches would manufacture confirmations out of clean runs.
  - **cleared** — at least one bearing experiment `completed`, none raised a nominated code, and none raised any high/critical finding. An **aborted** run can never clear a prediction, because "nothing bad happened" after the safety monitor cut it short is a statement about the monitor, not the system.
  - **untested** — nothing bore on it, or only skipped/aborted runs did.
  - A `skipped` experiment injected nothing and so proves nothing either way.
- It reports a **predictor hit rate** (`confirmed / (confirmed + cleared)`) and states in the output that this is the static predictor's accuracy, not a severity.
- **Safety role:** none; purely retrospective.
- **Plain English:** It goes back over the Scout's predictions once the experiments have run and says which ones were right, which were wrong, and which nobody tested.

### 5. Failure Injection agent — Planner

- **Lives in** [faultline/planning/planner.py](../faultline/planning/planner.py) (+ `llm.py` as the shared client).
- **Status:** fully built and wired in. 19 tests.
- **Trigger:** `faultline plan` (preview, runs nothing); inside `faultline run` / `rerun` / `demo`; `GET /api/plan` (which the UI polls) and the control room's `run` job.
- **Inputs:** `reports/topology.json`, `config/thresholds.yaml` (the `Policy`), the experiment history from `reports/experiments/`, which helper Docker images are present, and — **in the CLI only** — `reports/weaknesses.json`.
- **Outputs:** an in-memory `ExperimentSpec`. It writes nothing to disk itself; the spec reaches the blackboard via the executor's report.
- **Deterministic vs. LLM:** the canonical "LLM proposes, code disposes", and it holds up in the real implementation.
  - Code scores every `(service, failure_type)` pair: `criticality × (1.5 if untested else 1.0) − 1.5 × times_tested + informativeness − 0.5 if degrade-only − 1.0 if same target as last run`. Pairs are filtered first by role, `chaos_relevant`, the protected-service policy, and missing helper images.
  - The model is shown **only the top 5** and must answer with an index into that list. `_ask_llm` validates everything: an out-of-range index → fall back to deterministic; parameters are clamped to catalog bounds regardless; `expected_affected` is filtered to services that actually exist; error rate is clamped to 0–1 and recovery to 1–600 s; rationale truncated to 300 chars. Any unusable answer and `planned_by` stays `"deterministic"` with a templated rationale.
  - **The LLM cannot raise a safety threshold, pick an unlisted candidate, target a protected service, or exceed a parameter bound.** It can only make an experiment more conservative. `planned_by` is recorded as `llm` / `deterministic` / `operator` on every report.
- **Weakness-aware ranking — asymmetrically wired.** `Planner.__init__` takes `weaknesses: WeaknessReport | None`, and when present adds `1.0 × (predicted_severity / 10)` plus `0.5` when the candidate's failure type is exactly the one the Scout nominated. The weights are deliberately capped so a *prediction* can break near-ties but never outrank a markedly more critical target. **The CLI passes it ([cli.py:256](../faultline/cli.py#L256)); the control room does not ([control.py:442](../faultline/dashboard/control.py#L442)).** So `faultline run` is weakness-aware and the control room's "Run experiment" is not — the same stack planned from the UI will rank candidates differently from the CLI. This is a one-line fix and is listed in the drift section.
- **Safety role:** gating, before the fact. `Policy.allows()` is what refuses `container_stop` on `mongodb` unless `--allow-protected` is passed, and the refusal reason is carried into the spec's notes.
- **Plain English:** It lists every failure worth trying, scores them in plain code, and lets the AI pick one of the best five — then checks the AI's homework and overrules anything out of bounds.

### 6. Failure Injection agent — Executor (Injector)

- **Lives in** [faultline/agents/injector.py](../faultline/agents/injector.py), with the actual fault mechanics in [faultline/chaos/](../faultline/chaos/) (`catalog.py` = 10 failure types with bounded params, `actions.py` = apply/revert handles, `docker_ctl.py` = Docker wrapper).
- **Status:** fully built and wired in.
- **Trigger:** `faultline run` / `rerun` / `demo` via `Injector.run(spec)`; the control room's `run` job.
- **Inputs:** an `ExperimentSpec` from the planner, live Docker state, the two in-process probes (synthetic Things-API at ~4 rps, `/alive` + `/ready` every 3 s), the derived `Thresholds`, and the coordination files `reports/STOP` and `reports/ACTIVE_EXPERIMENT.json`.
- **Outputs:** `reports/experiments/<id>.json`, `reports/experiments/<id>/report.md`, `reports/experiments/<id>/<svc>.log` (last 120 lines per affected service), `reports/live.json` (rewritten roughly every second during a run), and `reports/ACTIVE_EXPERIMENT.json` while it holds the lock.
- **Deterministic vs. LLM:** **the executor never calls an LLM.** Every finding code, the hypothesis verdict, and the recovery decision are plain code.
- **The real sequence:** preflight → take the lock → arm an independent `Watchdog` → inject → 1 Hz observe loop with a safety check every second → roll back in a `finally` → wait for recovery with escalation → data-integrity check → 8 s "after" window → derive findings → write the report.
  - **Preflight refuses to inject** if STOP is present, another experiment is active, the target is not running, *any* relevant service is already down, there are under 5 probe samples, or pre-injection success is below `min_pre_success`. The result is a real report with `status="skipped"` — not a crash. (Small detail: the last two are `if`/`elif`, so with fewer than 5 samples the success-rate check is never evaluated. It still refuses, but the six conditions are not six independent checks.)
  - **Recovery is not naive.** Three consecutive good checks make a *candidate*, which then has to survive a 20 s soak; a degradation during the soak is recorded as a `relapse_after_recovery` finding and the wait continues. At 50% of the recovery timeout it restarts the target; at 80% it restarts the target's direct dependents (the Ditto #1861 pattern). Both are recorded as `remediation_actions`, which is what later makes the Risk agent add its `intervention` penalty.
  - **Findings it can raise:** `api_impact`, `health_check_masked_failure`, `dependency_blind_readiness`, `no_automated_remediation`, `recovery_failed`, `manual_restart_required`, `auto_recovered`, `slow_recovery`, `relapse_after_recovery`, `data_loss_suspected`, `cascade_beyond_hypothesis`, `safety_abort`.
  - **Hypothesis verdict** (`_hypothesis_result`) is a conjunction of three code checks: observed error rate within the predicted bound + 5 pp, no unexpected affected services, and recovery inside 1.5× the predicted time with no remediation.
- **Safety role:** central. It holds the single-experiment lock, enforces the hard wall-clock cap, arms the watchdog that fires even if the main loop blocks, rolls back in a `finally` so an exception still reverts the fault, and force-starts the target if it is not running after rollback.
- **Plain English:** It breaks one thing on purpose, watches what real API users would see second by second, puts it back, and writes down exactly what happened — and it refuses to start if the system is already unwell.

### 7. Safety monitor + watchdog

- **Lives in** [faultline/safety/monitor.py](../faultline/safety/monitor.py) (`SafetyMonitor`, `Watchdog`) and `thresholds.py` (derives `Thresholds` from `config/thresholds.yaml` + the measured baseline p95).
- **Status:** fully built and wired in. 11 tests.
- **Trigger:** constructed by the injector per experiment and `evaluate()`d once per second inside the injection loop. Not separately invocable.
- **Inputs:** the rolling probe window, the health probe, the list of services *outside* the hypothesis, and `reports/STOP` via a callback.
- **Outputs:** a `Verdict` in memory, surfaced on the report as `SafetyOutcome` (`aborted_early`, `abort_reason`, `abort_detail`, `watchdog_fired`, `operator_stop`).
- **Deterministic vs. LLM:** **code only.** The module docstring is explicit: "The model never sees this module's inputs or outputs." Confirmed — no LLM import, no call path.
- **Four abort conditions:** operator STOP; trailing error rate above threshold (default 30%); p95 above `max(3000 ms, 5 × baseline p95)`; or any service outside the hypothesis failing 3 health checks in a row (`blast_radius:<svc>`). The `Watchdog` is a separate `threading.Timer` that fires at `max_experiment_seconds + grace` and reverts the fault even if the main loop is wedged in a blocking call.
- **Safety role:** this is the primary in-process kill switch.
- **Plain English:** A referee that checks four numbers every second and stops the experiment the moment any of them goes past a limit set in a config file — limits the AI is not allowed to change.

### 8. Guardian agent

- **Lives in** [faultline/agents/guardian.py](../faultline/agents/guardian.py).
- **Status:** fully built. **Runs as its own process and must be started by hand** — nothing in `faultline run`, `demo`, or the control room spawns it. The control room and `faultline status` only *report* whether its heartbeat is fresh (< 15 s).
- **Trigger:** `faultline guardian [--interval 5]`. Then an infinite poll loop at 5 s.
- **Inputs:** live Docker state, `reports/topology.json` (to know which services to watch), `reports/ACTIVE_EXPERIMENT.json` (target + deadline), `reports/STOP`. **It shares no memory with the injector — only files.**
- **Outputs:** `reports/guardian.heartbeat.json` every tick, and `reports/logs/guardian.log` for each action taken.
- **Deterministic vs. LLM:** **code only**, by design — the module is the independent check and takes no model input.
- **What it actually does:** starts or unpauses any watched container that has been down longer than a 20 s grace **unless** it is the current experiment's target inside its deadline; clears a stale `ACTIVE_EXPERIMENT.json` 30 s past its deadline; removes leftover chaos helper containers once the deadline passes and clears their `tc` qdiscs; and on STOP, restores everything immediately and reconnects any container detached from the network by a partition experiment.
- **Safety role:** the second, independent kill switch. The explicit design rationale (README and docstring agree): "we do not trust a single agent with the kill switch." It can restore the sandbox even if the injector process has died mid-experiment.
- **Plain English:** A watchdog in a separate process that puts containers back whatever the main tool does — including when the main tool has crashed while something was switched off.

### 9. Risk / Impact agent

- **Lives in** [faultline/agents/risk.py](../faultline/agents/risk.py).
- **Status:** fully built and wired in.
- **Trigger:** `faultline risk [ids...] [--force] [--no-llm]`; `faultline run --analyze`; `cmd_demo`; the control room's `analyze` job and automatically after **every** experiment the control room runs (`_job_run` → `_analyze(only=[id])`). By default it skips experiments that already have output; `--force` redoes them.
- **Inputs:** `reports/experiments/<id>.json` and `reports/topology.json` (for the service count behind the blast-radius fraction). Optionally one LLM call.
- **Outputs:** `reports/risk/<id>.json`.
- **Deterministic vs. LLM:** severity is **entirely deterministic**: `4 × (1 − success during) + 2 × (blast radius / services) + 2 × min(TTR / 120 s, 1) + 1.5 if recovery needed intervention + finding bonuses`, clamped to 10. Levels: < 3 low, < 5.5 medium, < 8 high, else critical — and **any `data_loss_suspected` finding forces `critical` regardless of the arithmetic**. The LLM may rewrite exactly two prose fields (`summary`, `production_implication`), each validated as a non-empty string under 600 chars; otherwise templated prose is used and `generated_by` records `rules` vs `llm+rules`. **The model cannot move the number or the level.**
- A `skipped` experiment short-circuits to severity 0 with an explicit "no conclusion can be drawn" note, rather than being scored as a clean run.
- **Safety role:** none; purely analytical, after the fact.
- **Plain English:** It turns one experiment's measurements into a single 0–10 severity, showing which part of the score came from user impact, spread, recovery time and manual intervention.

### 10. Fix Recommendation agent

- **Lives in** [faultline/agents/fix.py](../faultline/agents/fix.py) with the rule library in [faultline/agents/knowledge.py](../faultline/agents/knowledge.py) (527 lines, 18 rules).
- **Status:** fully built and wired in.
- **Trigger:** `faultline fix [ids...] [--force] [--no-llm]`; `faultline run --analyze`; `cmd_demo`; the control room's `analyze` job and after every control-room experiment.
- **Inputs:** `reports/experiments/<id>.json`, `reports/risk/<id>.json` (and if the risk file is missing it computes an in-memory assessment with **no LLM** rather than failing), `reports/topology.json`. Optionally one LLM call.
- **Outputs:** `reports/fixes/<id>.json` + `reports/fixes/<id>.md`.
- **Deterministic vs. LLM:** rules lead, model is capped. `matching_rules()` ranks Ditto-specific remediations by specificity (finding code > target service > failure type > generic), de-duplicates by title, and takes the top 6. The LLM may add **at most 2** extras, each of which must validate against the `Recommendation` schema, must not duplicate an existing title, must have non-empty `steps`, is **force-prefixed with `[LLM suggestion, unverified]`**, and has its **confidence capped at 0.6**. Anything that fails validation is silently dropped.
- **Safety role:** explicitly none, and that is the point: `advisory_only` is hard-coded `True` at [fix.py:109](../faultline/agents/fix.py#L109), the schema default is `True`, and nothing anywhere ever assigns `False`. The Markdown renderer prints "Advisory only — nothing was applied."
- **Verified claim, stated precisely:** **no agent writes to the target stack's configuration.** Every file write in `faultline/` lands in `reports/`, with exactly one exception — the Onboarding agent's `config/<slug>_knowledge.yaml`, which is Faultline's own knowledge file, not the target's. `compose_file` and `nginx_conf` are only ever *read*; nothing writes to `deploy/`, any `docker-compose*` file, or `nginx.conf`.
  - Worth stating separately, because "nothing is ever applied" is easy to overclaim: three things **do** mutate the *running containers*, as opposed to configuration — the injector during an experiment (and its `_safe_restart`/`_safe_start` recovery escalations), the **Guardian** continuously and outside any experiment (unpause, start, remove helpers, clear qdiscs, reconnect networks — that is its entire job), and `faultline heal`, which is operator-invoked and reachable **both** from the CLI and from the control room's `heal` job (`_job_heal` calls `cli.cmd_heal`). The accurate claim is "no agent rewrites your config or applies a fix", not "nothing touches the stack".
- **Plain English:** For each failure it found, it looks up concrete, Ditto-specific fixes with steps and verification — and never applies any of them.

### 11. Resilience Scorecard agent

- **Lives in** [faultline/agents/scorecard.py](../faultline/agents/scorecard.py).
- **Status:** fully built and wired in.
- **Trigger:** `faultline scorecard`; `faultline run --analyze`; `cmd_demo`; the control room's `analyze` job and after every control-room experiment.
- **Inputs:** every `reports/experiments/*.json`, every `reports/risk/*.json`, `reports/topology.json`.
- **Outputs:** `reports/scorecard.json` + `reports/scorecard.md`.
- **Deterministic vs. LLM:** **pure arithmetic, no LLM.** Per experiment, penalty = `40 × (1 − success during) + 20 × min(TTR / 120 s, 1) + 20 if manual recovery + 10 × min(high/critical findings, 3) + 5 if cascade`. Service score = `100 − mean(penalties)`, floored at 0. Grades A ≥ 90, B ≥ 80, C ≥ 65, D ≥ 50, else F. Only `completed` and `aborted` experiments are scored; untested services are listed last with grade `—` and no score, never as a passing grade.
- **Safety role:** none.
- **Plain English:** A report card giving every service a 0–100 score and an A–F grade, worst first, with a one-line reason the weakest one is weakest.

### 12. Narrator agent

- **Lives in** [faultline/agents/narrator.py](../faultline/agents/narrator.py).
- **Status:** fully built, but the narrowest surface of any agent here: **CLI-only** (not in the control room's `AGENTS` pipeline tuple, no job kind, no UI panel) and **it writes nothing to the blackboard** — output goes to stdout only. It is a pure read-only consumer.
- **Trigger:** `faultline narrate [--watch] [--llm]`; step 5 of `faultline demo`.
- **Inputs:** reads everything — `reports/experiments/`, `reports/risk/`, `reports/fixes/`, `reports/topology.json`, and `reports/live.json` when `--watch`. If risk or fix output is missing it computes both in memory **with the LLM disabled**, so narration never silently invents an LLM-written risk summary.
- **Outputs:** **none on disk.** Console text only.
- **Deterministic vs. LLM:** the whole structured play-by-play (header, timeline, impact, recovery, hypothesis verdict, safety, findings, risk, top fix) is templated code. The LLM adds one optional closing paragraph, **opt-in via `--llm`**, rejected if over 900 chars. Note the inversion worth knowing: the standalone `faultline narrate` defaults the LLM **off** (`--llm` to enable), whereas `risk`/`fix`/`scout`/`plan`/`run`/`onboard` all default it **on** (`--no-llm` to disable). **But `faultline demo` passes `llm=not a.no_llm`, so inside the demo path the narrator's LLM is on by default** — the off-by-default rule applies only to the standalone subcommand.
- **Safety role:** none; purely observational.
- **Plain English:** It reads everything on the board and tells the story of each experiment in ordinary English, optionally following along live.

### 13. Approval agent

- **Status: specified but not started. There is no code of any kind.**
- Searched for `approval`, `approve`, `smtp`, `imap`, `email`, `mailbox` across `faultline/`, `config/`, `tests/`, `scripts/` and `deploy/` (`.py`, `.yaml`, `.yml`, `.js`, `.html`, `.toml`). The only hits are the unrelated `mailhog|mailpit|maildev|smtp4dev` tokens in the onboarding agent's tooling-detection regex.
- There is **no schema** in `faultline/schemas.py`, no `faultline/agents/approval.py`, no CLI subcommand, no control-room job kind, no blackboard file, and no test.
- Worth noting for whoever picks this up: no spec for it exists **in the repo either** — it is not in `README.md`, `HANDOFF_PROMPT.md`, `docs/ARCHITECTURE.md` or `docs/DEMO.md`. If an approval/gating step is still wanted, it needs designing from scratch, not finishing. The nearest existing machinery is `Policy.allows()` (pre-flight gating) and the STOP file (operator interrupt).

---

## 3. Not agents (listed for context)

These are shared machinery. They are on the critical path but nobody would point at them and ask "what does this agent do."

| Thing | Where | What it is |
|---|---|---|
| **Schemas** | `faultline/schemas.py` | The Pydantic contract between all agents. Every `reports/*.json` validates against one of these. |
| **Blackboard store** | `faultline/reporting/store.py` | Atomic read/write over `reports/`, plus the coordination primitives (`STOP`, `ACTIVE_EXPERIMENT.json`, `live.json`, heartbeats). |
| **Failure catalog & actions** | `faultline/chaos/` | The 10 failure types with bounded parameters (`container_crash`, `container_stop`, `container_pause`, `network_delay`, `network_loss`, `network_partition`, `cpu_stress`, `disk_pressure`, `db_flood`, `traffic_spike`), their apply/revert handles, and the Docker wrapper. |
| **Probes & window** | `faultline/observe/` | The synthetic Things-API probe (with a sequence-number canary for the data-integrity check), the health probe, the rolling sample window, and baseline capture. |
| **Remediation knowledge** | `faultline/agents/knowledge.py` | A static library of 18 Ditto-specific fix rules plus the matcher. Data, not an agent — the Fix agent is its only consumer (Risk, Scorecard, Reconcile and Narrator import only its `target_service` helper). |
| **Control room** | `faultline/dashboard/` | `control.py` (long-lived session, single-slot job queue, event log, 1 Hz telemetry), `server.py` (stdlib HTTP), `static/` (the browser UI). It drives the agents; it is not one. |
| **LLM client** | `faultline/planning/llm.py` | One interface over Ollama / any OpenAI-compatible endpoint / Anthropic. **Every method returns `None` on any failure**, which is what makes "the deterministic path always has the final say" true rather than aspirational. |
| **Config** | `faultline/config.py`, `config/*.yaml` | Settings, safety thresholds and policy, Ditto documented knowledge. |

---

## 4. Drift from README

Each line is actionable on its own. `README.md` is currently 50 KB and modified-but-uncommitted, so some of this may be mid-edit.

### A. Agents that exist in code but are missing from README

1. **Weakness Scout is absent from README entirely.** The token "scout" appears **zero** times in README.md. It is missing from the agent list (README.md:83–90), the mermaid architecture diagram (32–79), the CLI command table (166–182 — there is no `scout` row), the `reports/` outputs listing (346–361) and the requirements table (11–24). Two near-misses are not references to it: the word "weaknesses" at README.md:17 is the organisers' requirement wording credited to Risk/Fix/Scorecard, and "predicted" at lines 16/204/212/214 always means the *predicted blast radius* of a running experiment, never a predicted weakness. This is the single largest gap — an 866-line agent with its own UI panel and control-room job is undocumented.
2. **Reconcile agent is absent from README entirely.** Zero case-insensitive matches for "reconcile" in README.md — no mention of `faultline scout --reconcile`, of predicted-vs-confirmed reconciliation, or of `weaknesses_reconcile.json`/`.md`.
3. **Repo Onboarding is half-documented.** It has a good prose section ("Pointing Faultline at a different application", ~lines 397–475) but is **missing from the agent list (README.md:83–90, which runs Topology → Failure Injection → Risk → Fix → Scorecard → Narrator → Guardian → Control room), from the mermaid diagram (whose only nodes are T, I, P, R, F, S, N, G, D and LLM), and from the CLI command table** — there is no `onboard` row despite it being the single most complex CLI command (10 flags). A reader of the architecture section alone would not know it exists. `docs/ARCHITECTURE.md:13` does list it.
4. **Safety monitor / watchdog is not listed as its own item.** It is folded into the one-line "Failure Injection agent (planner, executor and safety monitor)". Defensible as a framing choice, but the independent `Watchdog` thread is a genuinely separate safety mechanism from `SafetyMonitor` and neither is named in the architecture diagram.

### B. README describes things that have drifted

5. **The mermaid diagram's blackboard subgraph is incomplete.** README.md:46–54 lists exactly `topology.json`, `baseline.json`, `experiments/<id>.json`, `risk/<id>.json`, `fixes/<id>.json / .md`, `scorecard.json / .md` and a combined `live.json`/`ACTIVE_EXPERIMENT.json`/`STOP` node. Missing: `weaknesses.json`, `weaknesses_reconcile.json` and `guardian.heartbeat.json` (the last of these *is* in the prose outputs listing at README.md:359, so the diagram is the only place it is absent). The diagram also shows no Scout, Reconcile or Onboarding node.
6. **The `reports/` outputs listing (README.md:346–361) is missing four files:** `weaknesses.json`, `weaknesses.md`, `weaknesses_reconcile.json`, `weaknesses_reconcile.md`. Everything else it lists is accurate, `guardian.heartbeat.json` included.
7. **The agent-pipeline strip is documented as 7 agents; the code has 9.** README line 204 says "Topology → Planner → Injector → Guardian → Risk → Fix → Scorecard". The actual tuple is `AGENTS = ("onboarding", "topology", "scout", "planner", "injector", "guardian", "risk", "fix", "scorecard")` ([control.py:24](../faultline/dashboard/control.py#L24)) — onboarding and scout are both missing from the README text.
8. **The `demo` command's documented steps are wrong.** The CLI table says "topology, baseline, run, risk, fix, scorecard, narrate". `cmd_demo` actually runs **6 labelled steps** and includes a **scout** pass as step 2: topology → scout → baseline → experiments → analysis → narrative.
9. **Requirement row 5 ("Analyse and report: weaknesses, fixes, ranked resilience score") credits only Risk, Fix and Scorecard.** Since the Scout and Reconcile landed, the project's answer to "weaknesses" is materially stronger — predicted-before-injection *and* reconciled-after — and the table undersells it.
10. **README claims "The planner, risk and fix agents talk to one `LLMClient`" (README.md:319). That is now six agents**, not three: the Scout (prose + systemic patterns), the Onboarding agent (its one bounded pass) and the Narrator (optional closing paragraph) all reach the same client through the shared `cli.llm_client()` helper. Of the three, the Narrator's LLM use *is* at least hinted at by the documented `narrate --llm` flag; the Scout's and the Onboarding agent's are entirely undocumented.
11. **`recovery.timeout_seconds` is 300 in `config/thresholds.yaml:18`, while `faultline/safety/thresholds.py:28` carries a code fallback of 240.** The config is what actually runs; the 240 only applies if the key is absent. Any prose quoting a recovery timeout should cite 300, and the two numbers are worth reconciling so the fallback is not mistaken for the effective value.

### C. Real wiring gaps found in the code (not README's fault)

12. **The control room's planner is not weakness-aware, but the CLI's is.** [cli.py:256](../faultline/cli.py#L256) passes `weaknesses=sess.store.weaknesses()`; [control.py:442](../faultline/dashboard/control.py#L442) does not pass the argument at all, so it defaults to `None`. Identical state, two different candidate rankings depending on whether you launched from the terminal or the UI. One-line fix.
13. **Reconcile is unreachable from the control room.** No `reconcile` job kind in `JOB_KINDS`, no `reconciliation` field in `ControlPlane.state()`, no UI panel — even though `ReportStore.reconciliation()` exists and is ready to serve it. A demo driven entirely from the screen cannot show the predicted-vs-confirmed loop, which is arguably the Scout's strongest result.
14. **The Scout has never been run against this repo's blackboard.** Neither `reports/` nor `reports-demo/` contains `weaknesses.json`. Consequence: the weakness panel in the control room has only ever rendered its empty state, and the CLI planner's weakness bonus has never fired on real data. `reports-demo/` in particular would be worth regenerating with a scout pass before a demo.
15. **`tests/test_scout.py` now exists — and 9 of its 90 tests fail.** This file appeared on disk at 23:05, during the writing of this inventory; before it, the Scout genuinely had no dedicated tests and the suite was green at 272. It is now 384 passed / 9 failed / 4 xfailed, with **every failure in this one file**. The failures split into two groups, and the distinction matters because they point in opposite directions:
    - **Two failures look like a real Scout behaviour quirk, not a bad test.** `test_markdown_of_a_clean_deployment_says_so` and `test_llm_pass_returns_cleanly_with_no_weaknesses_to_explain` both build a one-service deployment (`lonely`, with `restart: always`) and assert it yields zero weaknesses. The Scout returns one: `no_redundancy:lonely`. The cause is that `is_top_tier()` is **relative** — `criticality ≥ 0.4 × max_criticality`, and `max_criticality()` coerces to `1.0` when the max is 0 — so the highest-scoring service in *any* deployment is always top tier, and `check_redundancy` therefore always fires on it unless it declares `replicas > 1`. That means **the Scout can essentially never report a clean deployment**, which is the thing `check_restart_policy`'s own docstring warns against ("a check that fires on everything is a broken check"). Worth deciding deliberately: either the relative threshold needs an absolute floor, or single-instance-ness genuinely is always worth flagging and the tests should expect it.
    - **Seven failures are assertions about `_llm_pass`'s notes and fallback wording** (`test_fallback_when_the_answer_is_unusable` × 5 parametrisations, `test_llm_cannot_remove_or_rerank_a_weakness`, `test_llm_sees_the_findings_but_is_told_it_cannot_change_them`). The one I traced expects a note containing `"gave no usable answer"`, and the code produces the `"LLM unavailable"` note instead — i.e. the test's fake client takes a different branch of `_llm_pass` than the test assumes. These read as test-harness mismatches against the real control flow rather than defects in the Scout.
    - **Not fixed here, deliberately.** This is someone's in-flight work from minutes ago; editing either the new tests or `scout.py` under it risked clashing with whatever is still being written. It is flagged rather than patched.
16. **Guardian is never started automatically.** Nothing in `run`, `demo` or the control room spawns it; they only display its heartbeat. A demo that does not separately run `faultline guardian` has the second kill switch documented but not armed. Worth either auto-spawning it or making the UI state "offline" more prominent.

### D. The same gap in `docs/ARCHITECTURE.md`

README is not the only stale doc — the sibling architecture document has the identical blind spot, and it is the more surprising one because that file is otherwise extremely thorough (it documents the Onboarding agent in eight numbered steps).

17. **`docs/ARCHITECTURE.md`'s module map lists every agent module except two.** Rows exist for `injector.py`, `planner.py`, `llm.py`, `thresholds.py`, `monitor.py`, `knowledge.py`, `risk.py`, `fix.py`, `scorecard.py`, `guardian.py` and `narrator.py` ([ARCHITECTURE.md:27-37](ARCHITECTURE.md)). There is **no row for `faultline/agents/scout.py` and no row for `faultline/agents/reconcile.py`** — the two newest agents are the two missing ones.
18. **Its `cli.py` row omits the `scout` subcommand.** [ARCHITECTURE.md:13](ARCHITECTURE.md) enumerates the console script's commands as `setup, topology, baseline, catalog, plan, run, rerun, risk, fix, scorecard, narrate, guardian, onboard, stop, resume, heal, status, dashboard, demo` — accurate except that `scout` is absent.
19. **Its test inventory omits `test_reconcile.py` (23 tests) and `test_control.py` (35 tests)**, while describing `test_onboarding.py` in detail — and its count there has itself gone stale: ARCHITECTURE.md says "82 offline tests", the file now has 84.
20. **No mention of the Scout, predicted weaknesses, or reconciliation anywhere in the file** — not in the module map, the experiment lifecycle, the control-room section, the data contracts or the safety invariants. Since ARCHITECTURE.md's own opening line promises it "lists the modules ... and names the data contracts", `WeaknessReport` / `WeaknessReconciliation` missing from the data-contracts section is a real omission.

### E. Genuinely ambiguous code, called out rather than smoothed over

21. **Two genuinely dead branches in `narrator._live_line`.** The only writer of `reports/live.json` is the injector, and its complete phase vocabulary is exactly six strings: `preflight`, `injecting`, `rollback`, `recovering`, `soaking`, `idle`. Against that:
    - The `"baseline" / "settl"` branch is **dead** — neither substring is ever written.
    - The `"abort"` branch is **dead** as a *phase* test. An abort is recorded as an `abort_reason` **key**, and note it is written in *two* phases, not one: in `injecting` on a safety abort, and again unconditionally in the `rollback` phase from the `finally` block (usually as `None`). So a reader looking for "the abort phase" will not find one.
    - The `"done" / "complete" / "idle"` branch is **live, despite the first two substrings never being written** — they sit in the same `or` chain as `"idle"`, which the injector does write at the end of every run, so the branch fires and prints `✓ idle`.
    - `preflight`, `rollback` and `soaking` all fall through to the generic `· {phase}` line.
    Net effect: two dead branches plus two never-matched substrings carried along by a live one. Whether this is residue from an earlier phase scheme or a contract the injector never implemented is not decidable from the code alone.
22. **Precedence subtlety in `risk._factors` — confusing, but verified *not* a bug.** `len(set(report.affected_services_observed or []) | {target_service(report)} - {""})` relies on `-` binding tighter than `|`, so it parses (AST-confirmed) as `set(...) | ({target} - {""})` rather than the probably-intended `(set(...) | {target}) - {""}`. The two differ only if `affected_services_observed` itself contains `""`, which it cannot: the injector builds it from `spec.target_service` plus health-sample service names, all non-empty. And dropping an empty target before the union is exactly right, since `target_service()` returns `""` when a report's `target` dict has no service/name/container. So there is **no reachable wrong result today** — but it is fragile code that silently depends on operator precedence, and "clarifying" it with the wrong parentheses would introduce a real bug. Worth parenthesising deliberately.
23. **Dead fallback tuple in `scout.resolve_experiment`.** In the ternary `raw.experiment_prefs[0] if raw.experiment_prefs else ("container_stop", raw.services[0])`, the `else` arm is unreachable: all seven checks were traced individually and every `_Raw` they construct carries a non-empty `experiment_prefs`. The non-obvious cases both hold — `check_resource_limits` builds its list conditionally but then unconditionally appends three more entries, and `check_health_checks`' comprehension branch is guarded by an earlier `if not deps: continue`. Note this is only the fallback *tuple* that is dead; the surrounding `policy_note` logic is live and does real work. Harmless defensive code for a case that cannot arise.
24. **Two overlapping "is this service important" predicates in the Scout.** `is_critical()` (absolute floor of 2.0, or stateful, or entrypoint) and `is_top_tier()` (relative: ≥ 40% of this deployment's max criticality) are used by different checks, and the comments explain why each check picked the one it did. On Ditto they will often agree, which makes the distinction easy to miss and easy to get wrong when adding an eighth check. Not a bug — but a reviewer should know both exist.
25. **`reports-demo/` is an undocumented second blackboard.** It is untracked, holds 4 experiments, and is presumably the pre-baked demo state selected via `FAULTLINE_REPORTS_DIR`. Nothing in README explains it, and unlike `reports/` it has no `guardian.heartbeat.json` or `live.json`, so a demo pointed at it will show the Guardian as offline.
