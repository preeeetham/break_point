# Faultline: 6-minute live demo runbook

Audience: hackathon judges. Goal: show all five required steps (map, plan, inject safely, observe, report) plus a deliberately tripped kill switch, in six minutes, with the LLM in the loop but never on the critical path.

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

Topology and baseline are captured before the demo (they take 2 minutes together). You will re-run `topology` live because it is fast and the MongoDB insight is the hook; you will not re-run `baseline`.

## Pre-demo checklist (T-30 min)

```bash
cd /path/to/bosch-hackathon-2 && . .venv/bin/activate
scripts/up.sh                                  # idempotent; waits for /health = UP
docker compose -f deploy/docker-compose.yml ps # every container Up, Ditto services (healthy)
curl -s -u ditto:ditto http://127.0.0.1:8080/api/2/things | head -c 200; echo
ollama list | grep -E 'phi4-mini|qwen3.5|gemma4'
curl -s http://127.0.0.1:11434/api/tags >/dev/null && echo "ollama up"
faultline setup                                # Docker ok, stack ok, Pumba images present, LLM reachable
make clean-reports                             # start from an empty blackboard (keep a rehearsal copy first, see fallbacks)
faultline resume                               # make sure no stale STOP file
faultline topology
faultline baseline --duration 60               # expect success 100 %, p95 < 100 ms, no notes
faultline plan                                 # confirm the LLM answers in < 30 s; otherwise plan to use --no-llm
```

Then start the background processes and leave them running:

```bash
faultline dashboard            # pane A
faultline guardian             # pane B
faultline narrate --watch      # Terminal 2
```

Final checks: dashboard shows topology + baseline and the Guardian heartbeat; Grafana panels are populated; Docker Desktop memory >= 4 GB; laptop on mains power; notifications off; Ollama warm (`faultline plan` was just run, so the model is loaded).

## The six minutes

### 0:00-0:45  What you are looking at

Say: "This is Eclipse Ditto, an open-source IoT digital-twin platform: nginx in front of a gateway, four Java services in one Pekko cluster, one MongoDB behind them. Public repo, EPL-2.0, Docker Compose, running entirely on this laptop. Grafana and Prometheus watch it. Faultline is the agent system that is going to break it on purpose."

Show tab 3 (Ditto UI) for two seconds, then tab 2 (Grafana) and point at the flat API success and the five green `/ready` panels.

### 0:45-1:30  Step 1: map the dependencies

Terminal 1:

```bash
faultline topology
```

Say, while the table prints: "Compose `depends_on` says every service depends on `policies` and nothing depends on MongoDB. The agent also scans environment variables and finds `MONGO_DB_HOSTNAME=mongodb` on four services, parses the nginx config and adds the documented cluster edges. Result: MongoDB is ranked the number one single point of failure, which the compose file alone would never tell you."

Point at the criticality ranking and the finding line `mongodb: not referenced by any depends_on; 4 services reach it only via environment variables`. The dashboard (tab 1) shows the same graph.

### 1:30-2:15  Step 2: plan, LLM proposes, code disposes

Terminal 1:

```bash
faultline plan
```

Say: "Code ranks every service-failure pair by criticality and whether it has been tested. The model, `phi4-mini` running locally in Ollama, picks one of the top five and writes a falsifiable hypothesis. It cannot pick anything outside the list, cannot raise a parameter beyond its bound, and if it is slow or wrong the code runs the top candidate anyway. Nothing has been injected yet."

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

Point at tab 2 (Grafana): API success drops, `/ready` for `policies` goes red, and, this is the interesting part, `/ready` for `things` stays green while its API calls fail. Say: "That is Ditto issue 1865: readiness does not consider subsystem health. Faultline detects it automatically as `health_check_masked_failure`."

Wait for the Narrator to announce recovery. Expect 60-120 s because the JVM has to cold start and rejoin the cluster; fill the time with the hypothesis-versus-observed line when it prints.

### 3:45-4:30  The kill switch

Start a longer, gentler experiment and stop it on purpose:

Terminal 1:

```bash
faultline run --target things --failure network_delay --param duration_s=60 --param delay_ms=1500
```

About 10 s in, press **STOP** on the dashboard (tab 1) and confirm. Alternatively, from any shell: `faultline stop`.

Say: "Operator stop. The injector sees the STOP file within a second, removes the Pumba sidecar, clears the `tc` qdisc on the target, and writes the report with `status: aborted`, `abort_reason: operator_stop`. There is also a Guardian process over there (pane B) with its own Docker client: if the injector itself died, the Guardian would restore the container at the deadline. We do not trust a single agent with the kill switch."

Then:

```bash
faultline resume
```

### 4:30-5:30  Step 5: analyse and report

Terminal 1:

```bash
faultline risk && faultline fix && faultline scorecard
```

Say: "Risk is a deterministic 0-10 from user impact, blast radius, recovery time and findings; the model only writes the two sentences of explanation. The fix agent matches Ditto-specific remediation rules: for a `policies` outage it proposes two `policies` instances with decision caching; for the masked health check it points at the readiness fix in Ditto PR 1867 and an autoheal sidecar. Everything is advisory, nothing was changed in the stack."

Open `reports/fixes/<id>.md` on the dashboard (click the experiment) and read the first recommendation title plus its `verify` step. Then show the scorecard table: ranked worst first, letter grades, untested services listed without a grade.

### 5:30-6:00  Close

Say: "Everything you saw is in `reports/` as JSON that validates against one set of Pydantic schemas; the dashboard and the narrator are just viewers. Limits we know about: single node, Compose only, and Java cold start dominates crash recovery times. Next: Kubernetes backend, OpenTelemetry traces so cascades are attributed per hop, and a regression mode that re-runs an experiment after a fix and reports the delta."

Stop. Take questions with the dashboard on screen.

## Fallback plans

**LLM slow or down.** Add `--no-llm` to `plan` and `run`. The output is identical except `planned_by: deterministic` and templated prose. Say so openly: "the model is optional by design". If Ollama is up but the model is cold, `faultline plan` once in the pre-demo checklist loads it.

**Experiment takes too long to recover.** Recovery is capped at 240 s, with automatic escalation at 120 s (restart target) and 192 s (restart dependents). While waiting, switch to tab 2 (Grafana) and talk through the panels; or move on to the kill-switch segment in a second shell and come back. Do not start a second `run` while the first is still active: the `ACTIVE_EXPERIMENT.json` lock will refuse it.

**Stack unhealthy before the demo.** `scripts/down.sh && scripts/up.sh` takes about 2-3 minutes. If only one Ditto service is unhealthy, `docker compose -f deploy/docker-compose.yml restart <service>`.

**Docker or the laptop misbehaves entirely.** Keep a rehearsal copy of a full blackboard: `cp -r reports reports.rehearsal` after a successful dry run, and before the demo `make clean-reports`. If live injection is impossible, `cp -r reports.rehearsal/. reports/` and walk through the dashboard, `faultline status`, the fixes Markdown and the scorecard. Say it is a rehearsal capture.

**Pumba images missing (no network at the venue).** `faultline setup` pulls `gaiaadm/pumba` and `ghcr.io/alexei-led/pumba-alpine-nettools` in advance. Without them, the planner silently drops `network_delay` / `network_loss` from the candidate list; use `container_pause` on `things` for the kill-switch segment instead.

**Judges ask to pick the target.** `faultline catalog` lists what applies to which service. Anything on MongoDB except the five degrade-only faults needs `--allow-protected`; say that out loud if you use it.

## Reset between demo slots

```bash
faultline resume
make clean-reports
docker ps --filter label=faultline.helper=1 -q | xargs -r docker rm -f   # leftover Pumba helpers, normally none
faultline topology && faultline baseline --duration 60
```
