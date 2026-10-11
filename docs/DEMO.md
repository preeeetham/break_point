# Faultline: 6-minute live demo runbook

Audience: hackathon judges. Goal: show all five required steps (map, plan, inject safely, observe, report) plus a deliberately tripped kill switch, in six minutes, with the LLM in the loop but never on the critical path.

The full six-minute sequence below does not fit a 3-minute video: the speaking budget alone is six minutes, and the pinned `policies` stop in Step 3 needs about 2-3 minutes of wall-clock time by itself (20 s probe warm-up, the fault, 60-120 s JVM cold start, a 20 s soak and an 8 s after-window before the report is written). Use the cut below for the video.

## 3-minute cut (demo video)

A subset of the steps further down, in the same order. Nothing here is new; every command is the one described in the full runbook.

Before recording: finish the pre-demo checklist, start the dashboard, Guardian and narrator, and, if you want a dramatic `policies` result in the scorecard, run Step 3's pinned `policies` stop off camera and let it write its report. Do not run it on camera; its recovery alone takes most of the budget.

| Time | Step (section below) | On screen | Budget |
|---|---|---|---|
| 0:00-0:20 | What you are looking at (shortened) | Faultline dashboard; one sentence on Ditto (nginx, gateway, four Java services in one Pekko cluster, one MongoDB) and Faultline | 20 s |
| 0:20-0:50 | Step 1: `faultline topology` | Terminal 1: criticality table and the `mongodb: not referenced by any depends_on` finding | 30 s |
| 0:50-1:20 | Step 2: `faultline plan` | Terminal 1: ranked candidates, the pick, rationale and hypothesis; "LLM proposes, code disposes" | 30 s |
| 1:20-2:20 | Steps 3-4 + kill switch: `faultline run --target things --failure network_delay --param duration_s=60 --param delay_ms=1500` | Narrator shows `▶ injecting`; about 10 s later press STOP on the dashboard; narrator and report show `status: aborted`, `abort_reason: operator_stop` | 60 s on screen (about 70-80 s wall clock: trim the 20 s probe warm-up and the 20 s soak in the edit, or talk over them) |
| 2:20-2:50 | Step 5: `faultline risk && faultline fix && faultline scorecard` | Terminal 1 output, then expand the experiment on the dashboard: risk level, findings, first fix recommendation (advisory). With a slow local model, add `--no-llm` to `risk` and `fix` to stay inside the budget | 30 s |
| 2:50-3:00 | Close (one sentence) | Dashboard | 10 s |

After recording: `faultline resume` to clear the STOP file.

**Alternative for the 1:20-2:20 slot, no button press: an automatic threshold abort.** `faultline run --target things --failure container_crash --duration 30` shows the *automatic* stop the brief asks for ("stop if impact exceeds defined thresholds"). In the Day-2 verification run (experiment `1de6fb6a0969`, see [FINDINGS.md](FINDINGS.md#safety-the-kill-switch-fired-for-real)) the narrator showed `injected` at +0.8 s, `safety_abort — error_rate` (100% errors > 30% threshold) and `rollback_done` at +7.2-7.6 s, then `recovered` at +53.8 s after Docker restarted `things` and the 20 s soak passed. The abort itself is on screen within 10 s of launch; cut or talk over the recovery wait. `faultline resume` is not needed afterwards because no STOP file is written.

## Setup

Two terminals and three browser tabs, arranged before the judges arrive.

| Window | Purpose | Started |
|---|---|---|
| Terminal 1 (Driver) | every command you type live | before demo |
| Terminal 2 (Narrator) | `faultline narrate --watch`, plain-English play-by-play | before demo |
| Background tab or `tmux` pane A | `faultline dashboard` | before demo |
| Background tab or `tmux` pane B | `faultline guardian` | before demo |
| Browser tab 1 | Faultline dashboard http://127.0.0.1:8765 | before demo |
| Browser tab 2 | Grafana http://127.0.0.1:3000 (home dashboard "Faultline — Ditto resilience", set time range to last 15 min, refresh 5 s) | before demo |
| Browser tab 3 | Ditto UI http://127.0.0.1:8080/ui/ (credentials `ditto` / `ditto`) | before demo |

Font size 18+ in both terminals. Terminal 1 on the left half of the screen, Terminal 2 on the right; the browser on the second screen or behind.

Topology and baseline are captured before the demo (about 1.5 minutes together: a 10 s probe warm-up plus the 60 s baseline window). You will re-run `topology` live because it is fast and the MongoDB insight is the hook; you will not re-run `baseline`.

## Pre-demo checklist (T-30 min)

```bash
cd /path/to/bosch-hackathon-2 && . .venv/bin/activate
scripts/up.sh                                  # bundled stack only (skip when attaching via FAULTLINE_COMPOSE_PROJECT); idempotent; waits for /health = UP
faultline status                               # every container of FAULTLINE_COMPOSE_PROJECT running, Ditto services healthy
curl -s -u ditto:ditto http://127.0.0.1:8080/api/2/things | head -c 200; echo
ollama list | grep -E 'phi4-mini|qwen3.5|gemma4'                        # only if LLM_PROVIDER=ollama
curl -s http://127.0.0.1:11434/api/tags >/dev/null && echo "ollama up"  # only if LLM_PROVIDER=ollama
faultline setup                                # Docker ok, stack ok, Pumba images present, prints "llm <provider>:<model>: available"
make clean-reports                             # start from an empty blackboard (keep a rehearsal copy first, see fallbacks)
faultline resume                               # make sure no stale STOP file
faultline topology
faultline baseline --duration 60               # expect success >= 99 % and no notes; note the p95: the latency abort is max(3000 ms, 5 x p95)
faultline plan                                 # confirm the LLM answers in < 30 s; otherwise plan to use --no-llm
```

Then start the background processes and leave them running:

```bash
faultline dashboard            # pane A
faultline guardian             # pane B
faultline narrate --watch      # Terminal 2
```

Final checks: dashboard shows topology + baseline and the Guardian heartbeat; Grafana panels are populated; Docker Desktop memory >= 4 GB; laptop on mains power; notifications off; model warm (`faultline plan` was just run, so a local Ollama model is loaded).

## The six minutes

### 0:00-0:45  What you are looking at

Say: "This is Eclipse Ditto, an open-source IoT digital-twin platform: nginx in front of a gateway, four Java services in one Pekko cluster, one MongoDB behind them. Public repo, EPL-2.0, Docker Compose, running entirely on this laptop. Grafana and Prometheus watch it. Faultline is the agent system that is going to break it on purpose."

Show tab 3 (Ditto UI) for two seconds, then tab 2 (Grafana) and point at the flat "Gateway HTTP requests/s by status" panel and the green values in the "Service ready (/ready)" panel.

### 0:45-1:30  Step 1: map the dependencies

Terminal 1:

```bash
faultline topology
```

Say, while the table prints: "Compose `depends_on` says every service depends on `policies` and nothing depends on MongoDB. The agent also scans environment variables and finds `MONGO_DB_HOSTNAME=mongodb` on four services, parses the nginx config and adds the documented cluster edges. Result: MongoDB is ranked the number one single point of failure, which the compose file alone would never tell you."

Point at the criticality ranking and the finding line `mongodb: not referenced by any depends_on; 4 services reach it only via environment variables`. The dashboard (tab 1) shows the same ranking as service cards ordered by criticality; the Mermaid graph is in `reports/topology.md`.

### 1:30-2:15  Step 2: plan, LLM proposes, code disposes

Terminal 1:

```bash
faultline plan
```

Say: "Code ranks every service-failure pair by criticality and whether it has been tested. The model (by default `phi4-mini` running locally in Ollama; whatever `faultline setup` reported) picks one of the top five and writes a falsifiable hypothesis. It cannot pick anything outside the list, cannot raise a parameter beyond its bound, and if it is slow or wrong the code runs the top candidate anyway. Nothing has been injected yet."

Read the rationale and the hypothesis statement out loud. If the pick is a degrade-only fault on MongoDB, add: "MongoDB is protected on Day 1: the policy allows latency, loss, CPU, disk and write floods, but never a full outage."

### 2:15-3:45  Step 3 and 4: inject and observe

Terminal 1, one experiment, letting the planner choose:

```bash
faultline run --experiments 1
```

If you need a predictable, visibly dramatic result instead (recommended for the first live run), pin it:

```bash
faultline run --target policies --failure container_stop --param duration_s=30
```

While it runs, read Terminal 2 (the Narrator) rather than Terminal 1. Say: "Four requests per second are going through nginx as a real client would. Health checks on every service every three seconds. The moment the trailing error rate crosses 30 percent, the safety monitor aborts and rolls back; it does not wait for the 30 seconds to finish."

Point at tab 2 (Grafana): gateway requests shift to error statuses, `/ready` for `policies` goes red, and, this is the interesting part, `/ready` for `things` may stay green while its API calls fail. Say: "Readiness only reflects the service's own process, the pattern behind Ditto issue 1865. Faultline records it automatically: as `dependency_blind_readiness` when the failed service is down, as in this run, and as `health_check_masked_failure` when the faulted service itself stays up and keeps answering `/ready`."

Wait for the Narrator to print the report. Expect 60-120 s for the JVM to cold start and rejoin the cluster, then a 20 s soak and an 8 s after-window; fill the time with the hypothesis Terminal 1 printed before injection. The Narrator prints `held` / `violated` against it once the report is written.

### 3:45-4:30  The kill switch

Start a longer, gentler experiment and stop it on purpose:

Terminal 1:

```bash
faultline run --target things --failure network_delay --param duration_s=60 --param delay_ms=1500
```

The run first warms the probes for about 20 s. About 10 s after the Narrator shows `▶ injecting network_delay into things`, press **STOP** on the dashboard (tab 1) and confirm. Alternatively, from any shell: `faultline stop`.

Say: "Operator stop. The injector sees the STOP file within a second, removes the Pumba sidecar, clears the `tc` qdisc on the target, and, once the API has recovered and soaked, writes the report with `status: aborted`, `abort_reason: operator_stop`. There is also a Guardian process over there (pane B) with its own Docker client: on STOP it removes chaos helpers and restores anything down straight away, and if the injector itself died it would clean up once the deadline in `ACTIVE_EXPERIMENT.json` passed. We do not trust a single agent with the kill switch."

If the latency threshold (max(3000 ms, 5 x baseline p95)) fires before you press STOP, the report reads `abort_reason: latency` instead: that is the automatic kill switch, so say so rather than pressing STOP.

Then clear the STOP file (no new experiment starts while it exists; the dashboard button now reads RESUME and does the same):

```bash
faultline resume
```

### 4:30-5:30  Step 5: analyse and report

Terminal 1:

```bash
faultline risk && faultline fix && faultline scorecard
```

Say: "Risk is a deterministic 0-10 from user impact, blast radius, recovery time and findings; the model only writes the two sentences of explanation. The fix agent matches Ditto-specific remediation rules: for a `policies` outage it proposes two `policies` instances with decision caching; when a run raises `health_check_masked_failure` it points at the readiness fix from Ditto PR 1867, and when recovery needed a restart it proposes health-keyed restarts with an autoheal sidecar. Everything is advisory, nothing was changed in the stack."

Click the experiment row on the dashboard and read the first recommendation's title and steps; the `verify` checks are in `reports/fixes/<id>.md`. Then show the scorecard table: ranked worst first, letter grades, untested services listed without a grade.

### 5:30-6:00  Close

Say: "Everything you saw is in `reports/` as JSON that validates against one set of Pydantic schemas; the dashboard and the narrator are just viewers. Limits we know about: single node, Compose only, and Java cold start dominates crash recovery times. Next: Kubernetes backend, OpenTelemetry traces so cascades are attributed per hop, and a regression mode that re-runs an experiment after a fix and reports the delta."

Stop. Take questions with the dashboard on screen.

## Fallback plans

**LLM slow or down.** Add `--no-llm` to `plan` and `run`. The output is identical except `planned_by: deterministic` and templated prose. Say so openly: "the model is optional by design". If Ollama is up but the model is cold, `faultline plan` once in the pre-demo checklist loads it.

**Experiment takes too long to recover.** Recovery is capped at 300 s (`recovery.timeout_seconds`), with automatic escalation at 150 s (restart target) and 240 s (restart dependents). While waiting, switch to tab 2 (Grafana) and talk through the panels. Do not start the kill-switch `run` until the first one has written its report: while the fault is applied the `ACTIVE_EXPERIMENT.json` lock refuses it, and during the recovery wait the preflight will most likely skip it because the pre-experiment window is below 95 % success.

**Stack unhealthy before the demo.** `faultline heal --restart` removes chaos leftovers, restarts the Ditto services in dependency order and waits for `/health` = UP. For the bundled stack, `scripts/down.sh && scripts/up.sh` also works and takes about 2-3 minutes. If only one Ditto service is unhealthy, `docker compose -f deploy/docker-compose.yml restart <service>` (bundled stack).

**Docker or the laptop misbehaves entirely.** Keep a rehearsal copy of a full blackboard: `cp -r reports reports.rehearsal` after a successful dry run, and before the demo `make clean-reports`. If live injection is impossible, `cp -r reports.rehearsal/. reports/` and walk through the dashboard, `faultline status`, the fixes Markdown and the scorecard. Say it is a rehearsal capture.

**Pumba images missing (no network at the venue).** `faultline setup` pulls `gaiaadm/pumba` and `ghcr.io/alexei-led/pumba-alpine-nettools` in advance. Without them, the planner silently drops `network_delay` / `network_loss` from the candidate list, and a pinned `network_delay` run fails; use `container_pause` on `things` for the kill-switch segment instead (the error-rate threshold may fire before you press STOP; the report then shows `abort_reason: error_rate`).

**Judges ask to pick the target.** `faultline catalog` lists each failure type with the roles it applies to and its parameter bounds; `faultline plan` shows the (service, failure) pairs the policy currently allows. Anything on MongoDB except the five degrade-only faults needs `--allow-protected`; say that out loud if you use it.

## Reset between demo slots

```bash
faultline resume
make clean-reports
faultline heal                                  # removes leftover Pumba helpers and qdiscs, waits for /health = UP
faultline topology && faultline baseline --duration 60
```
