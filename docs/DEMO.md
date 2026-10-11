# Faultline: 6-minute live demo runbook

Audience: hackathon judges. Goal: show all five required steps (map, plan, inject safely, observe, report) plus a deliberately tripped kill switch, in six minutes, almost entirely on one screen.

The screen is the **control room** at http://127.0.0.1:8765. Two long-running commands are started before the judges arrive — the dashboard and the Guardian — and during the six minutes you type nothing at all, apart from the one optional terminal segment below. Everything the UI does is a real action against a real Docker Compose stack; there is no scripted mode and no replay.

## Setup

Three terminals, three browser tabs. Tab 1 is the demo; tabs 2 and 3 exist only to prove the application is real and to corroborate the numbers from an independent source; Terminal 3 is used only if you run the optional onboarding segment.

| Window | Command / URL | Started |
|---|---|---|
| Terminal 1 | `cd <repo> && . .venv/bin/activate && faultline dashboard` | before demo |
| Terminal 2 | `cd <repo> && . .venv/bin/activate && faultline guardian` | before demo |
| Terminal 3 | `cd <repo> && . .venv/bin/activate` — left idle, with the onboarding commands in its history; only needed for the optional segment | before demo |
| Browser tab 1 | **Faultline control room** http://127.0.0.1:8765 — full screen, this is the demo | before demo |
| Browser tab 2 | Grafana http://127.0.0.1:3000 (home dashboard "Faultline — Ditto resilience", time range last 15 min, refresh 5 s) | before demo |
| Browser tab 3 | Ditto UI http://127.0.0.1:8080/ui/ (basic auth from `.env`; the bundled stack uses `ditto` / `ditto`) | before demo |

Leave the first two terminals visible but small — the Guardian terminal is a prop you will point at once. If you need a third port (a second browser on a projector, say) start the server explicitly: `python -m faultline.dashboard.server --port 8777`. It binds `127.0.0.1` only, so it is not reachable from another machine.

The control room needs no internet: no CDN, no web fonts, system font stack. Venue wifi being down does not affect it. A hosted LLM does need network — see the fallback below.

## Pre-demo checklist (T-30 min)

```bash
cd <repo> && . .venv/bin/activate

scripts/up.sh                                  # idempotent; waits for /health = UP (cold start ~2 min)
faultline heal --restart                       # reset: clear the lock, drop chaos leftovers and tc qdiscs,
                                               # restart the Ditto services in dependency order, wait for UP
faultline resume                               # make sure no stale reports/STOP is latched
faultline setup                                # Docker ok, stack ok, Pumba images present, LLM reachable
faultline status                               # every container running, guardian offline (not started yet), no active experiment
faultline approvals                            # expect "no decision is pending"; ADMIN_EMAIL is a placeholder, so the gate is unconfigured by design
```

Read the `llm ...` line from `faultline setup` carefully. It is the single most common demo surprise:

- `available` — the planner will use the model. Good.
- `NOT available (LLM_API_KEY not set)` — a hosted provider is configured but has no key. Fill it in or plan to run deterministically; see the fallback section. At the time of writing, `.env` on the demo machine has `LLM_PROVIDER=openai` with the Groq base URL and model but an **empty** `LLM_API_KEY`, so out of the box the planner is deterministic.
- `NOT available (model ... not in ollama list)` — the configured Ollama model is not pulled: `ollama pull phi4-mini`.

Then capture the two slow things up front, because neither is worth 60 seconds of demo time:

```bash
faultline topology                             # ~5 s; you will re-run this live anyway, it is the hook
faultline baseline --duration 60               # expect success 100 %, p95 well under 100 ms, no notes
faultline plan                                 # confirms the planner answers; warms a local model if you use one
```

If you intend to run the optional onboarding segment, warm it now. The first clone needs network and the segment is only worth doing from a warm cache:

```bash
mkdir -p /tmp/faultline-onboard
cp config/ditto_knowledge.yaml /tmp/faultline-onboard/ditto_knowledge.hand.yaml   # insurance

faultline onboard eclipse-ditto/ditto \
  --compare-to config/ditto_knowledge.yaml --out /tmp/faultline-onboard \
  | tee /tmp/faultline-onboard/transcript.txt      # clone + one LLM call; 20-40 s cold, a few seconds warm

export CF=.cache/onboarding/ditto/deployment/docker/docker-compose.yml
FAULTLINE_COMPOSE_FILE=$CF FAULTLINE_REPORTS_DIR=/tmp/faultline-onboard/rep-human \
  faultline topology --no-live --knowledge config/ditto_knowledge.yaml | sed -n '3,13p' \
  > /tmp/faultline-onboard/rank-human.txt
FAULTLINE_COMPOSE_FILE=$CF FAULTLINE_REPORTS_DIR=/tmp/faultline-onboard/rep-generated \
  faultline topology --no-live --knowledge /tmp/faultline-onboard/ditto_knowledge.yaml | sed -n '3,13p' \
  > /tmp/faultline-onboard/rank-generated.txt
diff /tmp/faultline-onboard/rank-human.txt /tmp/faultline-onboard/rank-generated.txt && echo "IDENTICAL RANKING"
```

Three things about that block. **`--out` is not optional**: the slug for `eclipse-ditto/ditto` is `ditto`, so the default `--out config` writes `config/ditto_knowledge.yaml` — the hand-authored file you are comparing against — and the write happens before the comparison, so you would destroy the ground truth and then score the file against itself. **The two `FAULTLINE_*` variables are not optional either**: they point the mapper at the cloned repository's own nine-service compose (which is what the 9/9 result is measured on) and send `topology.json` to a scratch directory, so `reports/`, the blackboard and the control room's map are untouched. And `--no-live` means no Docker is involved, so each of those two runs takes about a second.

Leave Terminal 3 with those commands in its history; during the demo you press Up rather than typing. Keep `transcript.txt` as the fallback if the model is unavailable on the day. (If the console script is not installed in the venv, `python -m faultline ...` is the same program with the same flags.)

Decide what the judges should see in the Experiments table when the page loads. An empty table is honest but the scorecard panel is then empty too, and you will not have time to run three experiments live. Recommended: keep **one** rehearsal experiment on the board and say so out loud ("this one is from a rehearsal ten minutes ago; the next one is live"). To start completely clean instead: `make clean-reports`, then re-run `topology` and `baseline`.

Finally, start the two processes and check the room:

```bash
faultline dashboard            # Terminal 1
faultline guardian             # Terminal 2
```

Open tab 1 and verify, in the header: the model pill (the model name, or `rules only` — hover it for the provider and, if it is unavailable, the reason), a green **Guardian** pill, and a probe pill. Click **Arm probes** in the Operations block at the bottom of the left panel — the probe pill switches from `disarmed` to a sample count that climbs by about four per second, and the telemetry chart starts drawing a flat green line. Leave it armed. You want 2–3 minutes of flat history on the chart before the judges arrive, so the fall is obvious.

Optional but worth doing once per venue, on the actual projector resolution:

```bash
python scripts/verify_ui.py --url http://127.0.0.1:8765 --out /tmp/faultline-ui --expand
```

It loads the page in a real Chrome at five widths, fails on console errors, horizontal overflow or undersized controls, and leaves screenshots in `/tmp/faultline-ui`. It needs `pip install playwright && playwright install chrome`, which is not a project dependency.

Last checks: browser zoom so the feed text is legible from 3 m (110–125 % on a 1440 px laptop); Docker Desktop memory ≥ 4 GB; laptop on mains power; notifications and screen sleep off; terminal font 18+.

## The six minutes

Everything below happens in tab 1 unless it says otherwise.

### 0:00–0:40  What you are looking at

Say: "This is Eclipse Ditto — an open-source IoT digital-twin platform. nginx in front of a gateway, four Java services in one Pekko cluster, one MongoDB behind them. Public repo, EPL-2.0, Docker Compose, running entirely on this laptop with Prometheus and Grafana watching it. Faultline is the agent system that is going to break it on purpose, and this is its control room."

Press **E** (or click **Explain** in the header). Every panel opens a plain-English explanation of what it shows and why it matters.

Say: "Every panel can explain itself, because you should not have to take my word for what you are looking at." Press **E** again to close them.

Point at the header, left to right: the phase ribbon ("the seven steps every experiment goes through"), the model pill, the Guardian pill ("an independent process, that terminal there"), the probe pill ("a synthetic client has been calling the Ditto API four times a second for the last few minutes"), and the red STOP button ("we will use that").

### 0:40–1:20  Step 1: map the application automatically

Click **Rebuild** on the Application map panel header (or **Rebuild map** in Operations).

Say, while the Topology node in the agent pipeline turns and the graph redraws: "Nobody drew this. The mapper reads the Compose file, scans every container's environment variables for hostnames, parses the nginx routes, adds the documented Pekko-cluster edges and then enriches it all from live `docker inspect`."

Point at the MongoDB node — the biggest one. "Compose's `depends_on` never mentions MongoDB. By that file alone it is a leaf with nothing depending on it. The environment scan finds `MONGO_DB_HOSTNAME=mongodb` on four services, so it is ranked the number one single point of failure. Node size is criticality — the number of services that transitively depend on it, plus bonuses for holding state and being the only datastore."

If the findings list shows the undeclared-dependency line, read it out; that is the mapper reporting the gap rather than silently fixing it.

### Optional, 60–90 s: "does this only work for Ditto?"

The one segment that is not on the critical path, and the only one that happens in a terminal rather than in the control room. Run it here if you are ahead of the clock — add a minute to the six if you do — or keep it in your pocket for the questions at the end, where that is the question you are most likely to get. Skip it without regret if the stack needs attention.

The map you just rebuilt leans on one input the Compose file cannot provide: `config/ditto_knowledge.yaml`, the documented dependencies a human read out of Ditto's architecture pages. This segment shows an agent writing that file for a repository it has never seen.

Switch to Terminal 3, full screen, and press Up to the prepared command:

```bash
faultline onboard eclipse-ditto/ditto \
  --compare-to config/ditto_knowledge.yaml --out /tmp/faultline-onboard
```

Say while it runs: "This is the onboarding agent. Give it any Docker Compose repository — a path, `owner/name`, a GitHub URL. It shallow-clones it, finds the compose file, finds the README and the architecture documentation the project ships, runs the same structural parsers you just watched, and then makes exactly one LLM call that is told what is already known and asked only for what Compose cannot express."

On screen, in order: `reusing cached checkout`; `ditto: 9 services, 13 structural edges from docker-compose.yml + nginx.conf`; the architecture documents it chose; `asking openai:openai/gpt-oss-120b to document 9 services ...`; then `model proposed N edges, N survived validation`, the edges themselves with their evidence strings, and `health_port=7626 cluster_alias=ditto-cluster`.

Point at the compose line: "Note which file it picked — `deployment/docker/docker-compose.yml`, not the toy one at the repository root." Point at an evidence string: "Every edge has to say why, and it cites the document it came from." Point at the `survived validation` line: "Same contract as the planner. The model proposes; code decides. Edges naming a service that does not exist are dropped, roles have to be legal, the cluster alias has to be a real network alias in the Compose file, and the health port is derived from the Compose healthchecks in code and overrides whatever the model said. Everything thrown away is printed."

One guard is worth naming out loud: "In Faultline the roles `ui` and `other` mean *excluded from chaos testing*. If a model labels a product's own web front-end a UI, it quietly exempts it from every experiment — so an override to `ui` or `other` is refused for any service this repository builds from source."

Then the comparison block, which is the point of the segment. Read the four numbers off the JSON: `final_graph_coverage` 9/9, `roles.accuracy` 1.0, `health_port.match` true, `cluster_alias.match` true.

Now the headline. Press Up three more times for the ranking commands, or scroll back if you ran them at T-30:

```bash
diff /tmp/faultline-onboard/rank-human.txt /tmp/faultline-onboard/rank-generated.txt && echo "IDENTICAL RANKING"
```

No output, then `IDENTICAL RANKING`.

Say: "Those are the two criticality tables — the mapper run against the human's knowledge file, and against the one the agent just wrote. Not similar: byte-identical, all nine services, same order, same scores. The generated file has fewer edges, because the agent was told not to restate what `depends_on` already declares, and the merged graph comes out the same. That ranking is what the planner breaks things by, so this is the number I care about: the agent reproduced a human's architecture reading well enough that Faultline makes the same decisions."

Close it honestly: "It is a draft for review, not a fact — documented edges change what gets broken first, and the file carries a header saying which repo, which compose file and which model produced it. And it stays honest on repositories nobody tuned it for: run it against `dockersamples/example-voting-app` and it returns zero documented edges, because that Compose file already declares every dependency it has — it does not invent a Pekko cluster out of the Ditto example it was shown. What onboarding does not do is make the remediation rules or the protected-service policy generic; those are still Ditto-shaped."

### 1:20–2:00  Step 2: plan — the LLM proposes, the code disposes

The launcher on the left is already in **AI PLANNER** mode. Click **Re-plan** in the AI planner block.

Say: "The code ranks every service-and-failure pair by how much we would learn: criticality from that map, a bonus for untested pairs, a penalty for anything already tested. That list is deterministic. Only the top five go to the model, and all it may return is an index into that list, parameters that the code clamps into hard-coded bounds anyway, a rationale and a falsifiable hypothesis. It cannot pick a service that is not on the list and it cannot make an experiment longer or harsher. If it is slow, offline or returns nonsense, the top candidate runs with a templated rationale and the report says `planned_by: deterministic`."

Read the pick, the rationale and the hypothesis statement out loud. Then: "Nothing has been injected. This endpoint runs no experiment."

If the pick is a MongoDB fault, add: "MongoDB is protected: the policy allows latency, packet loss, CPU, disk and write floods, but never a full outage of the only datastore. You can see the locked cards in manual mode." Switch to **MANUAL** for two seconds to show the locked MongoDB card and the ten failure types, then continue.

### 2:00–3:30  Steps 3 and 4: inject, and observe what users see

For the first live run take the predictable, fast-recovering one rather than the planner's pick. In **MANUAL** mode: target **things**, failure **Process freeze (SIGSTOP)** (`container_pause`), `duration_s` **20**. Press and hold **LAUNCH** — a stray click cannot start an experiment.

Say while it runs: "Pause, not kill. The process freezes but the TCP connections stay open, so callers hang instead of getting a connection refused — the failure mode that actually hurts in production. Twenty seconds, and the safety limits are armed the whole time."

Point, in this order:

1. **The phase ribbon.** Preflight first (it refuses to inject into an already-unhealthy system), then it settles on Observe with Inject behind it marked done, and the watchdog countdown ticking next to it. The injector reports `injecting`, `recovering` and `soaking`; Inject and Rollback are steps on the ribbon but are not separately reported phases, so do not promise the audience that each of the seven lights up in turn.
2. **The telemetry chart.** The green success area falls and the cyan p95 line climbs. The dashed lines are the abort thresholds derived from the baseline: "if the trailing error rate crosses that, or p95 crosses that, the run rolls itself back without waiting for the twenty seconds."
3. **The application map.** The target goes orange. Services that the hypothesis predicted would be affected are outlined; services that actually degraded go red and the blast radius pulses along the edges. "Predicted versus observed, live. A red node outside the prediction is a cascade we did not expect, and that becomes a finding."
4. **The feed on the right.** "Every agent narrates. Violet lines were written by the model, orange lines are a fault actually being applied to a container, red lines are aborts."

Then watch the ribbon move to Recover and then Soak. Say: "The fault is gone. Soak exists because a system can look recovered and then relapse — twenty more seconds of watching before it is allowed to claim recovery, and a relapse resets the clock."

The findings are computed from what happened, not scripted, so do not promise a specific one. The one to hope for is `health_check_masked_failure`: a service on the request path kept answering `/ready` = 200 while real requests through it were failing. If it appears, say: "That is Eclipse Ditto issue 1865 — readiness does not consider subsystem health. An orchestrator would have kept routing traffic at it. Faultline detects that automatically by comparing what the platform claims with what the synthetic client actually experienced."

### 3:30–4:15  The kill switch

Start something longer and gentler so you have time to stop it. **MANUAL**: target **things**, failure **Network latency (tc netem)** (`network_delay`), `duration_s` **60**, `delay_ms` **1500**. Press and hold LAUNCH.

Wait for the ribbon to reach Inject and the p95 line to climb — about 10 seconds. Then press **STOP** in the header. (The `Esc` key does the same thing. Either way it fires immediately; there is no confirmation dialog, which is the point.)

What the audience sees, in about a second: a red toast, "Kill switch fired — rolling back"; a red line in the feed, "KILL SWITCH: stop requested. The running experiment rolls back within ~1 s and the guardian restores anything still down."; the ribbon leaving Observe for Recover; the p95 line dropping back to the baseline; and the STOP button turning into **RESUME**. The abort reason itself (`operator_stop`) is not in the live feed — it is in the report, which you open in the next segment, under `safety.abort_reason` with `operator_stop: true` and `status: aborted`.

Say: "The stop is a file on disk, `reports/STOP`. Three things watch for it independently: the safety monitor inside the experiment checks it every second and rolls back — here, remove the Pumba sidecar and delete the `tc` qdisc on the target; the report is written with `status: aborted` and `abort_reason: operator_stop`; and the Guardian over there has its own Docker client and would restore the container at the deadline even if the injector process had died. We do not trust a single agent with the kill switch. The same file is written by `faultline stop` from any shell, or by `touch reports/STOP`."

Click **RESUME** before moving on, or the next launch is refused.

### 4:15–5:15  Step 5: analyse and report

The analysis agents already ran as part of each experiment, so the rows at the bottom are complete. (If you need to re-run them: **Run analysis** in Operations, or the **Analyse** button on the Experiments panel header.)

Expand the first row. Walk down it:

- "Predicted versus observed: success before, during and after; p95 before and during; time to recover; client-visible outage; whether an acknowledged write survived."
- "Hypothesis: held or violated. A violated hypothesis is the interesting result, not a bug."
- The findings, each with a plain-English explanation of why it matters.
- The risk score. "Zero to ten, computed by code: user impact, blast radius, recovery time, whether it needed manual intervention, severity of findings. The model only writes the two sentences of explanation underneath — it never touches the number."
- One recommended fix, opened, with its steps and its **Verify** command. "Ditto-specific remediations matched by finding, target and failure type. Every one is advisory. No agent in this system has a code path that changes the stack."
- The **Reproduce by hand** block. "The exact `docker` commands. You can verify any finding without this tool."

Then the **Resilience scorecard** panel: "Zero to a hundred per service, graded, ranked worst first. Untested services are labelled untested rather than given a flattering default."

### 5:15–6:00  Close

Say: "Everything you saw is on disk in `reports/` as JSON that validates against one set of Pydantic schemas — the control room and the CLI are two front ends over the same files, and every button maps to a `faultline` subcommand. Limits we know about: single node, Compose only, and Java cold start dominates crash recovery times, so a killed Ditto service mostly measures JVM boot. Next: a Kubernetes backend, OpenTelemetry traces so cascades are attributed per hop instead of inferred, and a regression mode that re-runs an experiment after a fix and reports the delta."

If a judge asks about a human in the loop: "There is an approval gate, `faultline/agents/approval.py`. An experiment the protected-services policy would otherwise have needed `--allow-protected` for is held before injection while it emails one admin and waits for `APPROVE`, `APPROVE REDUCED`, `SKIP` or `STOP RUN` on the first line of the reply; after a high or critical result it asks again with `FIX <n>`, `WAIT` or `CONTINUE`. The vocabulary is closed, so nothing has to guess what the admin meant, and `APPROVE REDUCED` halves duration and intensity with the catalog's own bounds still clamping the result — no raw parameter ever arrives by email. The wait is 120 s on an independent watchdog, and a timeout, an unreachable mailbox or a missing credential all apply the safe default: skip before an injection, continue after one. A gate that cannot reach the admin never becomes a no-op that approves everything." Say plainly where it does and does not apply: it holds an experiment **you** picked that the policy would otherwise have refused, not one the planner proposed — a protected pair never enters the ranked list in the first place — and it is off unless `ADMIN_EMAIL` and the mail credentials are set, which on this machine they are not. `faultline approvals` shows that there is nothing pending, and nothing on the demo screen depends on it. Do not try to demonstrate it live: it waits up to 120 s for a reply.

Stop. Take questions with the control room on screen. If a judge asks whether any of this is specific to Ditto, that is the optional onboarding segment above: Terminal 3, one command, 60–90 seconds. If a judge asks to pick a target, switch to MANUAL and let them — the locked cards and clamped dials mean you cannot be talked into anything unsafe.

## Fallbacks

**The model is slow, offline, or has no API key.** Turn off the LLM toggle next to the mode switch in the launcher (the UI equivalent of `--no-llm`), or just work in MANUAL mode, which does not consult the planner at all. The header pill reads `rules only` and its tooltip says why. Say it openly: "the model is optional by design — it picks from a list the code produced and the code validates the answer. Without it, the top-ranked candidate runs and the prose comes from templates. Every number in the risk score, the fix matching and the scorecard is computed either way." This is a feature to show, not a failure to hide. On the CLI the same switch is `--no-llm` on `plan`, `run` and `demo`.

**The onboarding segment cannot run live.** It needs network for a first clone and a reachable model for the one LLM call, which is why the checklist pre-warms both: the clone is cached under `.cache/onboarding/`, and `/tmp/faultline-onboard/` holds the generated file, the transcript and the two ranking captures. If the model is down on the day, do not reach for `--no-llm` to rescue this segment — it runs the whole pipeline and writes a valid file, but with zero documented edges, so the comparison collapses to coverage 0.44 and F1 0.0. That proves the plumbing and undercuts the story. Instead `cat /tmp/faultline-onboard/transcript.txt` and `cat /tmp/faultline-onboard/ditto_knowledge.yaml`, then run the `diff` (which needs neither network nor a model), and say plainly that the file was generated half an hour ago by the command on screen. `--dry-run` is the other useful flag here: it prints and compares exactly as above and writes nothing, which is the safe way to demonstrate the command if a judge asks you to point it at a repository of their choosing.

**Recovery is taking too long.** Expected for crash-type faults: Ditto's Java services need 60–120 s to restart and rejoin the Pekko cluster. Recovery waiting is capped by `recovery.timeout_seconds` in `config/thresholds.yaml` (currently 300 s), with automatic escalation at 50 % (150 s, restart the target) and 80 % (240 s, restart its direct dependents), and every such action is recorded against the score. While waiting: press **E** and read a panel explanation, or switch to tab 2 and talk through the Grafana panels. Do not launch a second experiment — the `ACTIVE_EXPERIMENT.json` lock and the single-job rule will refuse it with "already running".

**A job fails.** A red toast names the job and the error, and a red line appears in the feed; the Python traceback is attached to that event's `data` rather than printed on screen, so read it from Terminal 1 or from `/api/state` if you need it. **Cancel job** in Operations cooperatively cancels the current one; for a running experiment it also trips STOP. Then **Heal sandbox** (tick the restart box if containers are in a bad state) and carry on.

**The page looks wrong.** Reload it. All state lives in the server process: the probes keep running, and the first poll after a reload re-fetches the last 300 events, so even the feed comes back. If a toast says a module failed to load, the rest of the room still works — keep going and use the Experiments table instead of the graph or chart.

**The page says it is offline.** The server died. Restart `faultline dashboard` in Terminal 1; note that this disarms the probes and empties the event log, so arm it again and expect an empty chart. Experiments already written to `reports/` are still there.

**The stack is unhealthy before or during the demo.** In order of escalation: `faultline heal` (removes leftovers, unpauses, reconnects, starts anything stopped), `faultline heal --restart` (also restarts the Ditto services in dependency order — 1–2 min), `docker compose -f deploy/docker-compose.yml restart <service>` for a single sick service, `scripts/down.sh && scripts/up.sh` as a last resort at about 2–3 minutes. `faultline status` tells you which.

**Pumba images missing (no network at the venue).** `faultline setup` pulls `gaiaadm/pumba` and `ghcr.io/alexei-led/pumba-alpine-nettools` in advance. Without them the network faults show up greyed out in the launcher with "missing helper image(s)" as the reason, and the planner drops them from the candidate list. Use **Process freeze (SIGSTOP)** on `things` for the kill-switch segment instead — it is just as visible and needs no helper image.

**Docker or the laptop fails entirely.** Keep a rehearsal copy of a full blackboard: after a good dry run, `cp -r reports reports.rehearsal`. If live injection becomes impossible, `cp -r reports.rehearsal/. reports/`, reload the control room and walk through the map, the experiments table, the fixes and the scorecard from the captured reports. Say plainly that it is a rehearsal capture and that nothing is running.

## Reset between demo slots

```bash
faultline resume                                       # clear any latched STOP
faultline heal --restart                               # lock, leftovers, qdiscs, restart in dependency order, wait for UP
make clean-reports                                     # optional: empty the blackboard
faultline topology && faultline baseline --duration 60 # only needed if you cleaned the reports
```

Then, in the control room: reload the page, **Arm probes**, and let the chart build a couple of minutes of flat history before the next slot. Restarting `faultline dashboard` is not required between slots and costs you that history, so do not.
