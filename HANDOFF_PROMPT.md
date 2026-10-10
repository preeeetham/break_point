# Faultline — continuation prompt for a fresh Claude session

Paste this whole file as your first message in a new Claude Code session, with the working
directory set to `/Users/dakshtyagi/Documents/bosch-hackathon-2`.

---

## Who you are and what this is

You are continuing work on **Faultline**, a hackathon project for the **Bosch hackathon**, problem
statement *"AI-Driven Chaos Engineering for Cloud-Native Resilience"*. It is due imminently and the
user (Daksh) demos it to judges. Move fast, verify with real tools, and do not break what works.

Faultline deliberately breaks a real open-source cloud-native app — **Eclipse Ditto 3.9.8** (IoT
digital-twin platform, EPL-2.0, https://github.com/eclipse-ditto/ditto) — measures the damage and
recovery, and reports weaknesses with recommended fixes. It is driven from a web "Control Room".

**Everything below is already built and verified working unless explicitly marked otherwise.**

---

## 1. Read these first, in this order

1. `README.md` — the product overview and the judges'-requirements mapping.
2. `docs/UI_CONTRACT.md` — **authoritative** spec for the dashboard API + design tokens + module
   contracts. If backend and frontend disagree, this file wins.
3. `docs/ARCHITECTURE.md` — module map and the experiment lifecycle.
4. `docs/DEMO.md` — the 6-minute demo runbook.
5. `faultline/schemas.py` — every data contract (pydantic v2). Nothing crosses a boundary that
   isn't in here.

---

## 2. How to run it right now

```bash
cd /Users/dakshtyagi/Documents/bosch-hackathon-2
source .venv/bin/activate            # Python 3.12 venv, deps already installed

# The Ditto stack is ALREADY RUNNING on this machine as compose project `bosch-ditto`
docker compose ls                     # should show bosch-ditto with 13 containers

# The control room (this is the demo)
.venv/bin/python -m faultline dashboard --port 8777
# -> http://127.0.0.1:8777

# Independent watchdog, in a second terminal (judges like seeing this)
.venv/bin/python -m faultline guardian

# Health check before demoing
.venv/bin/python scripts/demo_doctor.py          # may still be being written; see §6
.venv/bin/python scripts/verify_ui.py --url http://127.0.0.1:8777 --out /tmp/flui --settle 4
```

**Important:** `.env` is gitignored and already configured. It points at the **existing**
`bosch-ditto` compose project (not the bundled `deploy/` one) with user `hackathon`, and at
**Groq** for the LLM:

```
FAULTLINE_COMPOSE_PROJECT=bosch-ditto
DITTO_URL=http://127.0.0.1:8080
DITTO_USER=hackathon
DITTO_PASSWORD=<set>
PROMETHEUS_URL=http://127.0.0.1:9090
LLM_PROVIDER=openai
LLM_MODEL=openai/gpt-oss-120b          # Groq; llama-3.3-70b-versatile is NOT on this account
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=<set, working>
```

The server reads settings **once at startup** — restart it after editing `.env`.

---

## 3. Architecture in one page

**Blackboard pattern.** Eight agents communicate only through JSON files in `reports/`. No message
bus. You can watch them work independently.

```
reports/
  topology.json / .md        the application map + criticality ranking
  baseline.json / .md        steady state + derived safety thresholds
  experiments/<id>.json      one full ExperimentReport each (+ <id>/ dir with logs, report.md)
  risk/<id>.json             RiskAssessment (severity 0-10)
  fixes/<id>.json / .md      FixRecommendation (advisory only)
  scorecard.json / .md       0-100 per service, graded A-F
  live.json                  what the injector is doing right now
  ACTIVE_EXPERIMENT.json     the single-experiment lock
  STOP                       kill-switch flag; its presence aborts everything
  PENDING_APPROVAL.json      an admin is being waited on for a decision (absent = none)
  approvals/<id>.json        ApprovalRequest + its decision, one file per decision
```

**The eight agents**

| Agent | File | Job |
|---|---|---|
| Topology | `faultline/topology/` | Maps services from compose `depends_on` + **env-var scan** + nginx `proxy_pass` + live `docker inspect`. Computes criticality. |
| Planner | `faultline/planning/planner.py` | Ranks (service, failure) pairs; the LLM picks from the top 5 and writes a falsifiable hypothesis. |
| Injector | `faultline/agents/injector.py` | Runs one experiment end to end: preflight → inject → observe → rollback → recover → soak → report. |
| Guardian | `faultline/agents/guardian.py` | Separate process. Restores the sandbox even if the injector dies. |
| Risk | `faultline/agents/risk.py` | Deterministic 0-10 severity. LLM only writes prose. |
| Fix | `faultline/agents/fix.py` | 18 Ditto-specific rules in `knowledge.py` + ≤2 LLM extras. **Never applies anything.** |
| Scorecard | `faultline/agents/scorecard.py` | 0-100 per service, worst first, untested shown as untested. |
| Approval | `faultline/agents/approval.py` | Human in the loop, by email. Blocks an experiment that would have needed `--allow-protected`; asks again after a high/critical result. Closed reply vocabulary, 120 s deadline, safe default on timeout. **No LLM. Applies nothing.** |

**The governing principle: "LLM proposes, code disposes."** Every number (severity, score, ranking,
thresholds) is computed by code. The model may make an experiment *safer* (shorter, lighter) but can
never raise a safety limit. Everything works with `--no-llm` / `FAULTLINE_LLM=false`.

**The project's headline insight:** Compose's `depends_on` never mentions MongoDB, yet every Ditto
service needs it via `MONGO_DB_HOSTNAME`. Only the env-var scan reveals it is the #1 single point of
failure (criticality 9.5 vs 5.0 for the next). Say this to judges.

**Safety (code only, `config/thresholds.yaml`)** — abort if trailing-10 error rate > 30%, or p95 >
max(3000 ms, 5× baseline), or any service outside the predicted blast radius fails 3 health checks;
hard cap 90 s per experiment plus an independent watchdog timer (+15 s grace) that force-restores
even if the main loop hangs; one experiment at a time; MongoDB is **degrade-only** on Day 1
(`--allow-protected` overrides explicitly); refuse to inject if pre-experiment success < 95%.
Recovery requires 3 consecutive good checks **plus a 20 s soak** (a relapse resets it); escalation
restarts the target at 50% of the 300 s timeout and its dependents at 80%.

**Failure catalog (10)** in `faultline/chaos/catalog.py`: `container_crash`, `container_stop`,
`container_pause`, `network_delay`, `network_loss` (Pumba + tc netem sidecar), `network_partition`,
`cpu_stress`, `disk_pressure`, `db_flood`, `traffic_spike`.

---

## 4. The dashboard (the thing being actively worked on)

- `faultline/dashboard/control.py` (~690 lines) — the control plane. Owns a **long-lived session**
  (synthetic Ditto API probe ~4 req/s + health monitor every 3 s via `docker exec` on `/alive` and
  `/ready` port 7626, plus `mongosh ping`), samples telemetry at **1 Hz into a 300 s buffer**, and
  runs operator actions as **one-at-a-time jobs**. Has an event log with monotonic sequence numbers.
- `faultline/dashboard/server.py` (161 lines) — thin stdlib HTTP adapter. No framework.
- `faultline/dashboard/static/` — vanilla ES modules, **no build step, no CDN, no web fonts**
  (venue wifi may not exist):

| File | Lines | Owner/purpose |
|---|---|---|
| `index.html` | 95 | shell markup |
| `tokens.css` | 215 | design tokens — the ONLY place raw hex is allowed |
| `app.css` | ~2400 | the entire visual system |
| `app.js` | ~670 | shell, polling, header, ribbon, pipeline, KPIs, feed, experiments, scorecard |
| `topology.js` | 723 | animated dependency graph (the main stage) |
| `charts.js` | 699 | streaming canvas telemetry chart, sparklines, score ring |
| `launcher.js` | ~1080 | the injection console (choose target/failure/params and launch) |
| `util.js` / `icons.js` / `copy.js` | 212/60/85 | DOM+format helpers / SVG icons / explain-mode copy |

**API** (full detail in `docs/UI_CONTRACT.md` §2): `GET /api/state?since=<seq>` (one snapshot —
events, session, job, live run, telemetry, agents, topology, baseline, experiments, scorecard),
`GET /api/catalog`, `GET /api/plan?llm=0|1`, `GET /api/experiment/<id>`,
`POST /api/jobs/{topology,baseline,run,analyze,heal}`, `POST /api/{stop,resume,arm,disarm}`,
`POST /api/jobs/cancel`. Polling is 1000 ms idle, 400 ms during a run.

**Screen layout:** header (brand, 7-step phase ribbon, LLM/guardian/probe pills, STOP) · left
injection console · centre stage (agent pipeline strip → dependency graph → telemetry chart → KPI
tiles) · right agent activity feed · bottom experiments table + resilience scorecard.
Keyboard: `Esc` = kill switch, `E` = explain all panels, `Space` = pause updates.

**Verified working end to end:** I launched real experiments through the HTTP API and watched
`preflight → injecting → rollback → recovering → soaking → analyzing → idle`, with risk, fixes and
the scorecard all updating. The AI planner on Groq chose `network_loss` on `mongodb`, wrote its own
rationale and hypothesis, the hypothesis held, and 7 fix recommendations were generated.

---

## 5. How to verify anything you change

```bash
# Python unit tests — offline, no Docker needed. 84 passing, 1 xfail.
.venv/bin/python -m pytest -q tests

# JS syntax (node --check treats .js as CJS and WILL pass broken ES modules — copy to .mjs first)
cd faultline/dashboard/static && for f in *.js; do cp "$f" "/tmp/x_$f.mjs" && node --check "/tmp/x_$f.mjs" || echo "FAIL $f"; done

# Real browser: screenshots 5 breakpoints, fails on console errors, overflow, tiny touch targets
.venv/bin/python scripts/verify_ui.py --url http://127.0.0.1:8777 --out /tmp/flui --settle 4 --expand
# THEN ACTUALLY LOOK at /tmp/flui/desktop.png and phone.png with the Read tool. The numbers alone
# will not tell you whether it looks right — this is how the two worst bugs were caught.
```

Playwright uses the installed Chrome via `channel="chrome"` (no browser download needed).

---

## 6. State of play — what is done, what is not

### Done and verified
- All backend agents, the CLI, the control plane, the HTTP API, the Groq LLM path.
- Topology mapping (12 services, 21 edges on the live stack; mongodb ranked #1).
- Experiments run end to end from the dashboard with risk/fix/scorecard auto-analysis.
- `tokens.css`, `app.css`, `app.js`, `index.html`, `topology.js`, `charts.js`, `util.js`,
  `icons.js`, `copy.js`, `scripts/verify_ui.py`.
- Docs: `README.md`, `docs/DEMO.md`, `docs/ARCHITECTURE.md`, `docs/UI_CONTRACT.md`.

### Approval agent — landed and wired on the CLI path
- `faultline/agents/approval.py`: two scenarios (`pre_injection`, `problem_occurred`), one mechanism
  — pause, email `ADMIN_EMAIL`, wait for one of a closed set of replies or for the
  `APPROVAL_TIMEOUT_SECONDS` (120 s) deadline on an independent `Watchdog`, record what happened and
  why. Safe default on timeout, on an unreachable mailbox and on any unset credential: `SKIP` before
  an injection, `CONTINUE` after one. `WAIT` / `STOP RUN` reuse `reports/STOP`; `FIX <n>` is a
  decision log entry on `ExperimentReport.approval` and nothing else. Schemas, the twelve
  `SMTP_*` / `IMAP_*` settings, `ReportStore.pending_approval` / `write_approval` and
  `tests/fakemail.py` (in-process SMTP and IMAP doubles — a test must never touch a real inbox) are
  all in. Documented in `README.md`, `docs/PROJECT_GUIDE.md` 5.8 and 8.4, `docs/ARCHITECTURE.md`,
  `.env.example`.
- **Wired:** `cmd_run` enables the gate via `approvals_enabled()` unless `--no-approvals`, calls
  `gate()` before a held experiment and `run_post_experiment()` after each non-skipped one;
  `Planner.plan_operator` sets `requires_approval` when the policy refuses the pair and
  `approval_gate=True`; `faultline approvals [--resend ID] [--limit 5]` and `GET /api/approvals` are
  read-only views.
- **Not wired, check these:** `Planner.candidates` deliberately never marks a protected pair for
  approval, so an AI-planned run never reaches the gate — only an operator-chosen one does.
  `ControlPlane._job_run` has the hold (`_gate`) but `ControlPlane._planner` does not pass
  `approval_gate`, so nothing sets the flag from the browser and that branch is unreachable today.
  `scout_predicted_severity()` is a duck-typed hook for a Weakness Scout that has not landed; nothing
  calls it. See PROJECT_GUIDE section 17, item 22.
- **Before merging:** this agent was built on `feature/approval-agent` in a separate worktree,
  off `main`, while the Weakness Scout was being built on `feature/weakness-scout`. Both branches
  add a section to the end of `faultline/schemas.py` and `faultline/reporting/store.py`, add a
  subcommand to `faultline/cli.py`, and change `Planner.__init__`, so a merge conflicts in exactly
  those four files. Every conflict is of the "both sides appended" kind and resolves by keeping both
  sides; `git merge-tree --write-tree --name-only feature/approval-agent feature/weakness-scout`
  lists them. Nothing else overlaps.
- **The Scout trigger is now a small change, not a guess.** The Scout branch already feeds predicted
  weaknesses into the planner's ranking, so the second scenario-A trigger this agent documents
  (`approval.scout_predicted_severity`, duck-typed and inert until `reports/weaknesses.json` exists)
  can be called from `Planner.candidates` next to the policy check once the two branches are merged.
- **`ADMIN_EMAIL` is a placeholder.** No default, deliberately not hardcoded anywhere; the project
  owner has not supplied the real address. Leave it empty until they do — the gate then reports
  itself unconfigured and applies the safe default rather than approving anything. Use
  `admin@example.invalid` in examples.
- There is deliberately no way to answer a decision from the CLI or over HTTP. The answer comes back
  by email on purpose; a local bypass would defeat the gate, and `faultline stop` / `resume` already
  cover an operator at the laptop.

### In flight when the session ended — CHECK THESE FIRST
1. **`launcher.js` restyle** — the panel still uses its own `fl-launch-*` class names and only
   injects its fallback `<style>` when `app.css` is absent, so in the real browser it renders raw
   radio buttons and run-on text. **This is the worst-looking thing on the screen.** It must be
   remapped onto the classes `app.css` already defines: `.panel/.panel__head/.panel__body`,
   `.fieldset/.fieldset__legend`, `.optgrid/.card/.card__input/.card__icon/.card__name/.card__sub/
   .card__badge/.card__lock`, `.params/.param/.param__label/.param__range/.param__out/.param__desc`,
   `.btn --primary/--ghost/--danger/--icon/--sm/--block/--launch`, `.chip/.pill/.stat/.hypothesis`,
   and `data-tone="ok|warn|danger|critical|chaos|cyan|violet|neutral"`. Inject its scoped style
   unconditionally. Do not change its behaviour, exports, dirty-state handling, policy gating or
   payload shape — presentation only. Verify with `verify_ui.py` and by looking at the screenshot.
2. **`app.css` layout fixes** — an agent was mid-edit. Confirm: (a) `.app__main` has **four**
   children (pipeline, graph, chart, kpis) so `grid-template-rows` must be
   `auto minmax(220px,1fr) auto auto` or the **application-map panel collapses to zero height**;
   (b) the scorecard ring no longer overlaps the ranked rows; (c) `span.chip` and
   `span.ribbon__glyph` stop overflowing; (d) `.btn--sm` ≥32px and a `@media (pointer: coarse)`
   raising controls to 44px; (e) no horizontal scroll at 390px.
3. **`scripts/demo_doctor.py` and `scripts/seed_demo.py`** — an agent was writing these. The doctor
   is a pre-demo readiness check; the seeder populates a fake-but-schema-valid `reports/` so the UI
   can be shown without Docker. If they are missing or broken, they are nice-to-have, not critical.

### Known issues, not yet fixed
- The 7-step ribbon synthesises `preflight` and `analyzing`; `injecting` maps to the *Observe* step,
  so *Inject* only ever shows as already-done. Acceptable, but could be made literal.
- `app.css` styles a `.drawer-toggle`; a button with that class was added to `index.html` and wired
  in `app.js` — verify it actually appears and works between 768–1279 px.
- No automated tests cover `control.py` or any frontend module.
- `GET /api/plan` was made read-only (`arm=False`) — confirm it no longer starts probes as a side
  effect on a cold server.

---

## 7. House rules for this codebase

- **Match the existing style.** Dense, precise, few comments — and only comments that explain *why*,
  never *what*. No emojis anywhere. No marketing language in docs.
- **Never put a raw hex colour in `app.css`** — only in `tokens.css`.
  Check: `grep -nE "#[0-9a-fA-F]{3,8}" faultline/dashboard/static/app.css` must return nothing.
- **Never let the model override a safety limit.** New thresholds go in `config/thresholds.yaml`
  and are enforced in `faultline/safety/`.
- **Advisory only** — the Fix agent must never mutate the target system.
- Frontend modules must be side-effect free on import and export a factory returning
  `{update(...), destroy()}`; `update()` is called every poll and must be cheap and idempotent
  (diff, don't rebuild — rebuilding kills the animations).
- Accessibility floor: visible focus rings (never removed), contrast ≥4.5:1, never colour alone
  (always pair with a glyph or label), honour `prefers-reduced-motion`.
- Don't commit `.env`, `reports/*`, or anything under `ditto-local/secrets/`.
- Commit messages end with the attribution line the system prompt gives you. Ask before pushing.

---

## 8. If you only have time for three things

1. **Fix the launcher panel styling** (§6.1). It is the one part of the UI that looks unfinished.
2. **Confirm the application-map panel renders at a usable size** (§6.2a) — it is the centrepiece
   and the best visual on the screen when it works.
3. **Run `scripts/verify_ui.py`, look at the screenshots, and run one live experiment** from the
   dashboard to prove the demo path still works after your changes.

## 9. Demo-day cheat sheet

```bash
faultline heal --restart     # reset the sandbox between runs
faultline dashboard          # the demo
faultline guardian           # second terminal
faultline run --no-llm ...   # fallback if Groq is slow or offline
```

Fallback if the live stack misbehaves mid-demo: serve the seeded blackboard instead —
`.venv/bin/python -m faultline.dashboard.server --reports reports-demo --port 8790`.

The single best moment to show a judge: kill `things`, and point out that the platform kept
reporting itself **healthy** (`/ready` = 200) while the API returned 0% success — a real, documented
Eclipse Ditto bug ([issue #1865](https://github.com/eclipse-ditto/ditto/issues/1865)) that the
system discovered on its own from evidence.
