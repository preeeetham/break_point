# Incident Zero — What is built and how

This is the living explanation of the package: the architecture as it stands now, the decisions behind it, and a
log entry per checkpoint. Requirements are in REQUIREMENTS.md (IDs like F3.3 refer there); progress is in
CHECKPOINTS.md.

---

## Architecture (current)

```
                 Prometheus   Jaeger   OpenSearch / docker logs   Docker (read-only)
                      \          |            |                     /
                       +---- telemetry tools (redact feature_flag.*) ----+
                                          |
                                   evidence ledger (E1, E2, ...)
                                          |
 Sentinel --alert--> Investigator <--> Diagnostician --> Evidence Auditor --(fail: back)--> Investigator
                                                              | pass
                                                     Remediation Planner --(GET /actions, dry_run preview)--> Action API
                                                              |
                                                       Approval Gate  <-- human (Control Room / CLI), email
                                                              | approved
                                                          Executor --(POST /actions/execute)--> Action API
                                                              |
                                                          Verifier --(not recovered: POST /actions/rollback)--> Action API
                                                              |
                                                          Reporter --> report.json (jury schema) + report.md

 Guardian/kill switch: STOP file + POST /api/stop -> freeze agents, block Action API calls, roll back recent actions
```

### Package layout

| Path | What it is |
|---|---|
| `config.py` | Settings: `incident_zero.yaml` + `IZ_*` environment variables; endpoints discovered from the Compose project when unset |
| `models.py` | Pydantic models: Alert, Evidence, Diagnosis, ProposedAction, Approval, Execution, Verification, Incident |
| `report_schema.py` | The jury's incident report, modelled strictly (`extra="forbid"`), plus the JSON Schema it validates against |
| `store.py` | Per-incident durable state: `incidents/<id>/incident.json`, `evidence.jsonl`, `events.jsonl` |
| `events.py` | In-process event bus; every agent step is an event, persisted and streamed to the Control Room |
| `evidence.py` | Evidence ledger and the citation audit used by the Evidence Auditor |
| `redact.py` | Removes feature-flag data from every telemetry result (R2) |
| `telemetry/` | Backends (Prometheus, Jaeger, OpenSearch, Docker) and the tool registry the Investigator uses |
| `actions/` | Action API client, the mock Action API server, the action catalog |
| `agents/` | The nine agents |
| `orchestrator.py` | The incident lifecycle: one worker per incident, phase by phase, persisted after each phase |
| `server/` | JSON API + server-sent events + static Control Room |
| `ui/` | The Control Room (React + React Router + Vite) |
| `practice/` | Simulator scenarios, hidden ground truth, Judge |
| `cli.py` | `incident_zero up | stop | resume | status | report | demo | practice | action-api` |

### The agents

| # | Agent | Uses an LLM | Does |
|---|---|---|---|
| 1 | Sentinel | no | Evaluates alert rules on Prometheus and Docker every few seconds against a learned baseline; correlates alerts into one incident |
| 2 | Investigator | yes (tool loop; deterministic playbook as fallback) | Picks the next telemetry tool from the findings so far, within a budget; every result becomes evidence |
| 3 | Diagnostician | yes (rule-based fallback) | Root cause(s), component(s), confidence, claims quoting evidence |
| 4 | Evidence Auditor | no | Cited IDs exist, quotes appear verbatim, every symptom explained; else back to the Investigator |
| 5 | Remediation Planner | yes for the choice (rule-based fallback) | Picks an allowed action and parameters with a reason; code validates; dry-run preview from the Action API |
| 6 | Approval Gate | no (a human decides) | Pending approval bound to a plan hash; approve / reject / send back; timeout → escalate; notifies |
| 7 | Executor | no | Calls the Action API for the approved action only; checks the kill switch first |
| 8 | Verifier | no | Re-evaluates the triggering signals until they clear or the window ends; rollback + escalate on failure |
| 9 | Reporter | wording only | Builds the report from recorded facts; the model writes only the summary and prevention text |

---

## Decisions

| Decision | Why |
|---|---|
| Sibling package, not a change to Break Point's core | Break Point is built around knowing the injected fault; an on-call agent must not. Separation keeps both intact and makes R3 provable |
| Durable file-backed lifecycle + LangGraph only inside the Investigator | Approvals can wait minutes and must survive restarts; the tool loop is where a graph framework pays off. Reuses Break Point's Gemini-safe LangChain model |
| Standard-library HTTP server with server-sent events | Same idiom as Break Point's dashboard, no new server dependency |
| Action API as a separate HTTP process (the mock) | "Action API only" is then structural: the agent process has an HTTP client, not Docker write access |
| Evidence quotes checked verbatim by code | The jury penalises invented evidence; a prompt cannot guarantee that, a substring check can |
| Deterministic fallbacks for every LLM agent | A rate limit or outage must not stall an incident (N3), and tests run without a model |
| Endpoints discovered from Docker port mappings | OpenSearch and Jaeger are not published on fixed host ports in the demo's compose files |

---

## Log

### CP0 — documents (2026-10-23)

Wrote REQUIREMENTS.md (brief + integrity, safety, Control Room, practice and non-functional requirements, with
assumptions for the missing Action API slide and the first half of the report schema), CHECKPOINTS.md (exit test
per checkpoint) and this file.

Facts established before writing code:

- The demo's metrics reach Prometheus over OTLP. Request rate, errors and latency come from the collector's span
  metrics connector: `traces_span_metrics_calls_total` and `traces_span_metrics_duration_milliseconds_bucket`,
  labelled by `service_name`, `span_name`, `span_kind`, `status_code`.
- Container metrics come from the collector's `docker_stats` receiver; Valkey and PostgreSQL have their own receivers.
- Traces go to Jaeger (reachable through Envoy at `/jaeger/`), logs to OpenSearch (port not published to a fixed
  host port).
- Services recommendation, product-catalog, checkout, cart and shipping use OpenFeature tracing hooks, so spans carry
  `feature_flag.*` data: the reason for R2.

### CP1 — foundation

- `models.py` holds the agents' contract; `report_schema.py` the jury's schema as JSON Schema, validated with
  `jsonschema` (added as the `incident_zero` extra). The extracted half is exact; the missing first half is marked
  ASSUMED in one place.
- `store.py` writes atomically (temp file + rename). Evidence ids are assigned by the store under its lock, so the
  Sentinel attaching an alert while the Investigator runs can never produce two `E4`s (found and fixed in CP4).
- `redact.py` drops flag tags, span events whose marker is a flag evaluation, flag-named attribute keys, log lines
  mentioning flags, and masks the demo's 20 flag names in any string; it keeps `flagd` as a service name, because
  flagd being down is a real fault the agent must see.
- `evidence.audit`: ids must exist, quotes must appear verbatim (whitespace- and case-normalised, at least 3
  characters), components must be known, every active alert explained, no invented alert ids.

### CP2 — telemetry tools

- `telemetry/backends.py`: Prometheus, Jaeger (`/jaeger/ui/api`), OpenSearch (`otel-logs-*`, several field layouts
  tried) with `docker logs` fallback, a read-only Docker reader, topology from the stack's own compose file (found via
  the containers' `com.docker.compose.project.config_files` label, parsed with Break Point's compose parser), and
  endpoint discovery from published ports.
- `telemetry/queries.py`: PromQL on the span metrics: inbound (server + consumer) rate, error rate and p95 per
  service, now and `offset baseline_s`; outbound errors/latency per operation (which dependency fails a service);
  seconds since last span (silence).
- `telemetry/tools.py`: the Toolbox's ten tools: `overview`, `service_metrics`, `find_traces`, `trace_detail`,
  `search_logs`, `container_status`, `container_resources`, `dependencies`, `datastore_metrics`, `promql`. Each
  returns `E<n>: <content>`. Trace summaries name the deepest failing span ("error origin") and client-server
  latency gaps (network delay). Backend failures are recorded as evidence (silence is a signal). Flag queries and
  flagd logs are refused and record nothing.

### CP3 — Action API and safety

- `actions/catalog.py`: six mock runbook actions (`restart_service`, `start_service`, `update_resources`,
  `clear_network_impairment`, `reconnect_network`, `rollback_release`) and `validate` (allowed name, required and
  typed parameters, bounds, known service).
- `actions/mock_server.py`: a separate HTTP process that owns every write: Docker through Break Point's
  DockerController, netem cleanup through Break Point's `clear_qdisc`, and `rollback_release` through the flag file
  (`actions/flags.py`, which agents and telemetry are forbidden to import: a test parses their imports). Records each
  execution with its undo; its own kill switch returns 423; one action per service at a time (409).
- `safety.py`: dry run persisted in `state.json` (on at first start); "Stop all" writes STOP, kills the API,
  rolls back reversible actions from the last `kill_rollback_window_s`; explicit resume.

### CP4 — agents and the orchestrator

- Sentinel: six rules, `confirm_ticks` debounce (container rules fire at once), baseline learned from quiet ticks
  (falls back to `offset`), correlation into the incident still being investigated, re-alert suppression after an
  unresolved close (stops dry-run incidents re-opening every tick).
- Investigator: a LangGraph `think`/`act` loop over the Toolbox with a budget and a `finish_investigation` tool;
  node functions are unannotated (LangGraph resolves hints by name). Any model failure falls back to the playbook
  on the same ledger: overview → containers → alerted and flagged services → follow outbound errors and trace origins
  downstream → datastores → logs and resources of the origins.
- Diagnostician: model JSON, or rules over `facts.py` (container state, OOM, trace origins, network gaps, CPU and
  memory, stale datastores, traffic spikes). Rule claims quote evidence lines verbatim by construction.
- Auditor → back to the Investigator with the problems (`audit_retries` rounds), then rules, then drop unsupported
  claims; no supported cause → escalate.
- Planner: model or rules by category, `validate` by code, a dry-run preview from the API for each action, sha256
  of exactly what is shown.
- Approval Gate: approver name required, hash must match, send-back needs a note, timeout escalates; email via
  Break Point's SMTP settings.
- Executor: kill switch before each call, mode read at execution time. Verifier: settle, then the Sentinel's rules on
  a 1m window until clear `verify_consecutive` times; else roll back and escalate. Reporter: report built by code,
  validated, plus Markdown and `submission.json`.
- Orchestrator: one worker per incident, phase saved after every step, Frozen pauses, any agent exception ends in
  `failed` with a report; a resumed incident whose actions were attempted never executes again.
