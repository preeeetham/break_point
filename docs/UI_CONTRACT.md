# Faultline Control Room — UI contract

Single source of truth for the dashboard rewrite. Backend (`faultline/dashboard/control.py`,
`server.py`) and every frontend module are written against this file. Do not deviate; if something
is missing, add it here first.

## 0. Product

An operator control room for AI-driven chaos engineering against Eclipse Ditto. Shown on a
projector to hackathon judges. It must, at a glance, answer: what is the system, what is the AI
about to break, what is happening right now, did it survive, and what should we fix.

Non-negotiables: dark theme, no external network at runtime (no CDN, no Google Fonts — system font
stack only), vanilla ES modules, no build step, keyboard accessible, `prefers-reduced-motion`
honoured, readable from 3 m away.

## 1. Design tokens (authoritative)

Defined once in `static/tokens.css` as CSS custom properties on `:root`. Everything else consumes
them. Never hardcode a hex outside `tokens.css`.

```
Surfaces      --bg #060910   --surface #0E1424   --surface-2 #141C30   --surface-3 #1B2540
Lines         --border #1E2A44   --border-strong #2C3C5E   --grid #121A2E
Text          --text #E8EDF7   --text-dim #A8B6D1   --text-mute #6F7F9F
Brand         --blue #4D8DFF   --blue-dim #1E3A6B   --cyan #22D3EE   --violet #A78BFA
Semantic      --ok #2DD36F   --warn #F5A524   --danger #FF4D4D   --critical #F43F5E
Chaos         --chaos #FF7A1A  (injection in progress)
Severity      --sev-info var(--text-dim) --sev-low var(--cyan) --sev-medium var(--warn)
              --sev-high var(--danger) --sev-critical var(--critical)
Fonts         --font-sans: ui-sans-serif,-apple-system,"SF Pro Text","Segoe UI",Inter,system-ui,sans-serif
              --font-mono: ui-monospace,"SF Mono",SFMono-Regular,Menlo,"JetBrains Mono",Consolas,monospace
Space         --s1 4px --s2 8px --s3 12px --s4 16px --s5 24px --s6 32px (dense dashboard scale)
Radius        --r1 6px --r2 10px --r3 14px   Shadow --shadow-1, --shadow-2
Motion        --t-fast 140ms --t 220ms --t-slow 420ms --ease cubic-bezier(.2,.7,.3,1)
```

Status→colour mapping used everywhere (single rule, no exceptions):
`healthy/UP/recovered/held → --ok`, `degraded/warn/soaking → --warn`,
`failing/down/aborted/violated → --danger`, `critical/data loss → --critical`,
`injecting/chaos → --chaos`, `AI/LLM-authored → --violet`, `live telemetry → --cyan`.

## 2. HTTP API

All JSON, `Cache-Control: no-store`. Errors: `{"error": "..."}` with a 4xx/5xx status.

### GET `/api/state?since=<seq>`
The single polled snapshot. Poll 1000 ms idle, 400 ms while `run.phase` is not `idle`.

```jsonc
{
  "now": 1760000000.0,
  "seq": 412,                       // latest event sequence number
  "events": [ {"seq":410,"ts":...,"level":"info|success|warn|error|chaos|ai",
               "source":"system|topology|scout|planner|injector|guardian|risk|fix|scorecard|probe",
               "text":"...", "data":{} } ],   // only events with seq > `since`, max 300
  "session": { "armed": false, "error": null, "probe": {"running":false,"rps":4.0,
               "canary":"faultline.probe:canary","samples":0} },
  "job": { "id":"j7", "kind":"run|topology|scout|baseline|analyze|heal|plan", "status":"running|done|failed|cancelled",
           "label":"Crash things", "started_at":..., "ended_at":null, "error":null } ,  // or null
  "run": {                          // live experiment state; phase "idle" when nothing is running
    "phase":"idle|preflight|injecting|rollback|recovering|soaking|analyzing",
    "experiment_id":"ab12...", "target_service":"things", "failure_type":"container_crash",
    "parameters":{...}, "rationale":"...", "hypothesis":"...", "planned_by":"llm|deterministic|operator",
    "elapsed_s":12.4, "deadline_in_s":17.6, "max_s":30,
    "success_rate":0.12, "p95_ms":1800, "sample_count":20,
    "health": {"things":{"alive":null,"ready":null,"status":"exited"}, ...},
    "timeline":[{"t_plus_s":0.7,"event":"injected","detail":"SIGKILL ..."}],
    "safety":{"abort_reason":null,"detail":{}},
    "blast":{"expected":["gateway"],"observed":["gateway","things-search"]}
  },
  "telemetry": { "points":[{"t":1760000000.0,"success":1.0,"p95":42.0,"n":8}],  // 1 Hz, last 300 s
                 "phases":[{"from":...,"to":...,"phase":"injecting","experiment_id":"ab12"}] },
  "stop_requested": false,
  "guardian": {"at":..., "stop":false, "actions":[...], "down":{}} | null,
  "agents": { "topology":{"state":"idle|working|done|error","at":...,"detail":"12 services"},
              "scout":{...}, "planner":{...}, "injector":{...}, "guardian":{...},
              "risk":{...}, "fix":{...}, "scorecard":{...} },   // pipeline order, see §4
  "llm": {"provider":"ollama","model":"phi4-mini","available":true,"enabled":true,"error":""},
  "topology": {
    "services": {"things":{"role":"app","stateful":false,"entrypoint":false,"container":"...",
                           "state":"running","health":"healthy","chaos_relevant":true,
                           "criticality":4.0,"mem_limit_mb":512,"image":"..."}},
    "edges":[{"src":"things","dst":"mongodb","kind":"env"}],
    "criticality":[{"service":"mongodb","score":9.5,"fan_in":4,
                    "transitive_dependents":[...],"reasons":[...]}],
    "findings":["..."], "generated_at":"..."
  } | null,
  "baseline": {"captured_at":"...","api":{"success_rate":1.0,"p50_ms":22,"p95_ms":44,"p99_ms":128,
               "sample_count":453},"thresholds":{...},"notes":[...]} | null,
  "experiments": [ <ExperimentSummary>, ... ],   // newest first, see §2.1
  "scorecard": {"overall_score":61.7,"summary":"...","ranking":[{"service":"things","score":24.9,
                "grade":"F","experiments":1,"worst_success_rate":0.0,"slowest_recovery_s":30.5,
                "findings":6,"notes":[]}]} | null,
  // PREDICTED, never measured. Read straight off reports/weaknesses.json (the Weakness Scout's
  // static analysis of compose + declared health checks + documented knowledge), so it is present
  // before any experiment has run. Every record carries `label` == "[predicted, unconfirmed]" and
  // the UI must render that label (or an equally explicit text marker) next to every predicted
  // severity: a prediction may never read like a measured Finding or a measured RiskAssessment.
  "weaknesses": {
    "generated_at":"ISO", "project":"ditto", "compose_file":"deploy/ditto/docker-compose.yml",
    "knowledge_file":"config/knowledge.ditto.yaml" | null,
    "label":"[predicted, unconfirmed]",
    "checks_run":["shallow_health_check", ...], "notes":[...],
    "generated_by":"rules|llm+rules",
    "weaknesses":[ <PredictedWeakness>, ... ],          // ranked, worst predicted_severity first
    "systemic_patterns":[{"title":"...","weakness_ids":["w1","w3"],"narrative":"...",
                          "generated_by":"llm"}]        // additive LLM narrative, never a finding
  } | null
}
```

### 2.1 `ExperimentSummary`
```jsonc
{"experiment_id":"...","status":"completed|aborted|failed|skipped","started_at":"ISO","ended_at":"ISO",
 "target_service":"things","failure_type":"container_crash","parameters":{...},"rationale":"...",
 "planned_by":"llm|deterministic|operator","criticality_score":4.0,
 "hypothesis":{"statement":"...","expected_affected":[...],"expected_max_error_rate":1.0,
               "expected_recovery_s":90},
 "hypothesis_held":true|false|null,
 "baseline_success_rate":1.0,"during_success_rate":0.0,"during_p95_ms":null,"after_success_rate":1.0,
 "baseline_p95_ms":46.0,
 "time_to_recover_s":30.5,"outage_duration_s":37.0,"recovered":true,
 "auto_restarted_by_docker":true,"remediation_actions":[],
 "data_integrity_ok":true,"data_integrity_detail":"...",
 "aborted_early":true,"abort_reason":"error_rate","watchdog_fired":false,"operator_stop":false,
 "affected_services_observed":["things"],
 "findings":[{"code":"health_check_masked_failure","severity":"high","title":"...","evidence":"..."}],
 "risk":{"severity":5.8,"level":"high","summary":"...","production_implication":"...",
         "blast_radius":1,"factors":{"impact":4.0,"blast":0.3,...},"generated_by":"rules"} | null,
 "fix":{"generated_by":"rules","recommendations":[{"title":"...","category":"configuration",
        "effort":"M","confidence":0.8,"rationale":"...","steps":[...],"verify":[...],
        "references":[...]}]} | null,
 "timeline":[{"t_plus_s":0.7,"event":"injected","detail":"..."}],
 "reproduce":["docker kill ..."] }
```

### 2.2 `PredictedWeakness`
Mirrors `faultline.schemas.PredictedWeakness`. `predicted_level` shares `RiskAssessment.level`'s
domain (`low|medium|high|critical`), so the UI tones it with `riskTone()` — but only ever inside a
row whose provenance channel is `data-tone="violet"` (= AI-authored / not measured, §1).
```jsonc
{"weakness_id":"w1","category":"shallow_health_check|missing_health_check|no_redundancy|
   no_resource_limits|weak_restart_policy|no_retry_or_backoff|port_beyond_ingress|no_rate_limiting",
 "services":["things"], "title":"...",
 "evidence":"healthcheck: curl -f http://localhost:8080/alive",   // quotes a real compose field, mono
 "predicted_severity":7.4, "predicted_level":"high",              // PREDICTED, 0-10, not measured
 "factors":{"criticality":0.8,"blast":0.4},
 "suggested_experiment":{"failure_type":"container_crash","target_service":"mongodb",
                         "reason":"...","policy_note":"mongodb is protected; ..."},
 "confirming_finding_codes":["health_check_masked_failure"],      // which measured Finding would confirm it
 "why_it_matters":"...",
 "status":"predicted|confirmed|cleared",
 "label":"[predicted, unconfirmed]", "generated_by":"rules|llm+rules"}
```

### GET `/api/catalog`
```jsonc
{"failures":[{"key":"container_crash","title":"Container crash (SIGKILL)",
  "slide_category":"Service or container crash","description":"...","degrade_only":false,
  "expected_recovery_s":90,"requires_images":["pumba"],"available":true,"unavailable_reason":null,
  "params":[{"name":"duration_s","default":30,"lo":10,"hi":90,"unit":"s","desc":"...","type":"number"},
            {"name":"block_restart","default":false,"type":"bool","desc":"..."}],
  "applies_to":["things","policies","gateway","nginx","mongodb"],
  "policy_blocked_on":["mongodb"]}],
 "protected":{"mongodb":{"reason":"...","allowed_failure_types":[...]}},
 "thresholds":{...}}
```

### GET `/api/plan?llm=0|1`
Ranked candidates plus the next pick. Never runs anything.
```jsonc
{"candidates":[{"service":"mongodb","failure_type":"network_delay","priority":13.75,
                "reasons":["criticality 9.5","untested"],"criticality":9.5}],
 "pick":{"target_service":"...","failure_type":"...","parameters":{...},"rationale":"...",
         "hypothesis":{"statement":"...","expected_affected":[...],"expected_max_error_rate":0.3,
                       "expected_recovery_s":10},
         "planned_by":"llm|deterministic","planner_notes":[...],"reproduce":[...]} | null,
 "llm":{"used":false,"provider":"ollama","model":"phi4-mini","available":true,"error":""}}
```

### POST job endpoints
All return `202 {"ok":true,"job":{...}}`, or `409 {"error":"a job is already running", "job":{...}}`.
Exactly one job runs at a time.

| Endpoint | Body | Notes |
|---|---|---|
| `/api/jobs/topology` | `{}` | rebuild the map from compose + env + nginx + live docker |
| `/api/jobs/scout` | `{"llm":true}` | static-analysis pass: writes `reports/weaknesses.json` (predicted weaknesses). Runs with nothing armed and breaks nothing |
| `/api/jobs/baseline` | `{"duration":60}` | 20–300 s |
| `/api/jobs/run` | `{"mode":"auto"\|"manual","target":"things","failure":"container_crash","params":{"duration_s":30},"experiments":1,"llm":true,"allow_protected":false,"analyze":true}` | `auto` = planner picks |
| `/api/jobs/analyze` | `{"llm":true}` | risk → fix → scorecard over all experiments |
| `/api/jobs/heal` | `{"restart":false}` | remove chaos leftovers, optionally restart cluster |
| `/api/stop` | `{}` | **kill switch**, always 200, never queued, never blocked |
| `/api/resume` | `{}` | clears STOP |
| `/api/jobs/cancel` | `{}` | cooperative cancel of the current job (also trips STOP for `run`) |
| `/api/arm` | `{}` | start the probes + health monitor |
| `/api/disarm` | `{}` | stop them |

### GET `/api/experiment/<id>` → `{"experiment":{...full report...},"risk":{...},"fix":{...},"markdown":"..."}`

### Static
`/` → `static/index.html`; `/static/<file>` → css/js/svg with correct MIME. Unknown → 404 JSON.

## 3. Frontend module contract

ES modules in `faultline/dashboard/static/`, loaded by `index.html` via
`<script type="module" src="/static/app.js">`. Every module is side-effect free on import and
exports a factory that returns an object with at least `update(state)` and `destroy()`.

```js
// topology.js
export function createTopology(hostEl, {onSelect}) -> {update(state), select(name), destroy()}
// charts.js
export function createLiveChart(hostEl, opts) -> {update(telemetry, run), destroy()}
export function createSparkline(hostEl, opts) -> {update(values), destroy()}
export function createScoreRing(hostEl, opts) -> {update(score, grade), destroy()}
// launcher.js
export function createLauncher(hostEl, {onLaunch, onPlan, onStop, onJob}) -> {update(state, catalog, plan), destroy()}
```
`hostEl` is an empty `<div>`; the module owns its subtree. `update()` is called on every poll and
must be cheap and idempotent (diff, don't rebuild). No module writes outside its host element.

Shared helpers live in `util.js`: `el(tag, props, ...children)`, `fmtPct`, `fmtMs`, `fmtDur`,
`relTime`, `cls`, `svgIcon(name, size)`, `announce(text)` (polite live region).
`icons.js` exports `ICONS` — a map of name → inline SVG path markup (24×24, stroke-based, 1.75
stroke width, `currentColor`). Required names: `play, pause, stop, zap, crosshair, activity,
shield, brain, graph, database, server, globe, search, link, clock, check, x, alert, info,
chevron, refresh, wrench, gauge, list, terminal, flask, skull, wifi, cpu, hdd, trend`.

## 4. Screen layout

Single page, CSS grid, no scrolling of the shell (panels scroll internally). ≥1280 px: three
columns. 768–1279 px: two columns (launcher collapses to a drawer). <768 px: single column stack.

```
┌ HEADER: brand · phase ribbon · LLM pill · guardian pill · STOP ──────────────┐
├──────────────┬────────────────────────────────────────┬─────────────────────┤
│ LAUNCHER     │ STAGE                                  │ FEED                │
│ (left, 320)  │  agent pipeline strip                  │ (right, 360)        │
│  target      │  topology graph (main stage)           │  live event log     │
│  failure     │  live telemetry chart                  │  agent narration    │
│  params      │  KPI row                               │                     │
│  hypothesis  │                                        │                     │
│  LAUNCH      │                                        │                     │
├──────────────┴────────────────────────────────────────┴─────────────────────┤
│ PREDICTED WEAKNESSES: static-analysis predictions, before any experiment    │
├─────────────────────────────────────────────────────────────────────────────┤
│ EXPERIMENTS: cards/table, expandable detail with findings, risk, fixes      │
└─────────────────────────────────────────────────────────────────────────────┘
```

The agent pipeline strip carries one node per agent in execution order (`PIPELINE` in `app.js` is
the list, `AGENTS` in `control.py` is its backend half). The Scout runs early — between Topology
and Planner — because it predicts from static configuration and needs no experiment.

The PREDICTED WEAKNESSES panel (`[data-panel="weaknesses"]`, host `#weaknesses-host`, rendered by
`renderWeaknesses()` in `app.js`) is full-width, sits above EXPERIMENTS and is the one panel that
has something to say before anything has been broken. It reuses the `.finding` row markup, but the
provenance channel is inverted: the row is `data-tone="violet"` (AI-authored / not measured) and
every predicted severity is accompanied by the record's literal `label`
("[predicted, unconfirmed]") as visible text, so a prediction can never be read as a measured
Finding or as the Risk agent's measured severity. `systemic_patterns` render under their own
sub-heading, explicitly marked as LLM narrative over the deterministic predictions.

Phase ribbon (always visible): `PREFLIGHT → INJECT → OBSERVE → ROLLBACK → RECOVER → SOAK → REPORT`
with the active phase lit, a countdown, and the abort reason if one fired.

## 5. Explain mode

Every panel header carries an `info` affordance (button, `aria-expanded`, 44×44 hit area) that
reveals 1–3 sentences of plain English: what this panel shows, why it matters for resilience, and
what the judge should look at. Copy lives in `copy.js` as `EXPLAIN = {panelId: {title, body}}`.
A global "Explain" toggle in the header opens all of them at once.

## 6. Accessibility floor

Contrast ≥4.5:1 for text on its surface (verify `--text-mute` on `--surface`; it is borderline —
use it only for ≥14 px non-essential text). Focus ring: `outline: 2px solid var(--cyan);
outline-offset: 2px` — never removed. All controls reachable by keyboard; the launcher is a real
`<form>`. Status changes announce via one polite live region (`announce()`), not per-widget.
Never colour-only: pair every status colour with a glyph or text label. Honour
`prefers-reduced-motion: reduce` by disabling pulses/transitions and rendering final states.
