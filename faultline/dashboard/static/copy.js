// Plain-English explanations. Shown inline via the "?" disclosure on each panel and all at once
// via the header Explain toggle. Written for someone who has never seen chaos engineering.
export const EXPLAIN = {
  ribbon: {
    title: "What the phases mean",
    body: "Every experiment walks the same seven steps. PREFLIGHT refuses to start if the system is already unhealthy. INJECT applies the fault. OBSERVE watches real API traffic and aborts the moment a safety limit is crossed. ROLLBACK removes the fault. RECOVER waits for the API to come back. SOAK keeps watching for 20 more seconds so a relapse cannot be mistaken for a recovery. REPORT writes the evidence to disk.",
  },
  launcher: {
    title: "Choosing what to break",
    body: "In AI PLANNER mode the system ranks every (service, failure) pair by how much it would teach us — criticality from the dependency map, minus anything already tested — and a local language model picks from the top five, tunes the parameters inside hard-coded bounds and writes a falsifiable hypothesis. Code always validates the choice, so the model can make an experiment safer but never more dangerous. MANUAL mode lets you drive it yourself.",
  },
  pipeline: {
    title: "The agent pipeline",
    body: "Faultline is not one script, it is a pipeline of agents — one node per agent in the strip above, left to right. Onboarding documents a repository nobody has seen before, Topology maps the application, Scout reads the deployment and predicts where it is weak before anything is broken, Planner decides what to break, Injector breaks it and measures, Guardian is an independent watchdog that can restore the sandbox even if the Injector crashes, then Risk scores severity, Fix recommends remediations and Scorecard ranks the services. They communicate only through JSON files in reports/, so you can watch them work independently.",
  },
  weaknesses: {
    title: "Predictions, not measurements",
    body: "The Scout reads the Compose file, the declared health checks, the resource limits and the restart policies, and predicts where this deployment is likely to break — before a single fault is injected. Everything in this panel is marked [predicted, unconfirmed] and coloured violet, the same way everything else the AI authored is: it is an argument from configuration, not evidence. Each prediction names the experiment that would actually confirm or clear it; once that experiment has run, the measured finding below is the real answer.",
  },
  graph: {
    title: "The application map, built automatically",
    body: "Services, dependencies and data flows are discovered from the Compose file, the containers' environment variables, the nginx routes and live Docker inspection. The key finding: Compose's depends_on never mentions MongoDB, yet every Ditto service reaches it through MONGO_DB_HOSTNAME. Only the environment scan reveals that it is the single biggest point of failure. Node size follows criticality; during an experiment the blast radius spreads across the edges in real time. When the failure's path has been worked out, only that path pulses, travelling from the broken dependency to the services that rely on it; a dashed pulse means the timing could not prove which way it went.",
  },
  chart: {
    title: "What the probe sees",
    body: "A synthetic client calls the Ditto Things API about four times a second — create, read, update, search — exactly like a real device would. The green area is the share of those calls that succeeded; the cyan line is 95th-percentile latency. Shaded bands mark what the system was doing. Dashed lines are the abort thresholds: cross one and the experiment is rolled back automatically.",
  },
  kpis: {
    title: "Live numbers",
    body: "Success rate and p95 latency come from the synthetic probe over the last five seconds. The baseline column is the calibrated steady state captured before any fault, which is what the abort thresholds are derived from. Resilience score is the aggregate grade across every experiment run so far.",
  },
  feed: {
    title: "Agent activity",
    body: "Every decision and measurement each agent makes, in order, as it happens. Violet lines are written by the language model; orange lines are moments when a fault was actually applied to a container; red lines are safety aborts and failures.",
  },
  experiments: {
    title: "Evidence, not anecdotes",
    body: "Each experiment records what was predicted, what actually happened, how long recovery took and whether an acknowledged write survived. Expand a row for the findings, the risk score and the recommended fixes. Every report also carries the exact shell commands to reproduce it by hand.",
  },
  scorecard: {
    title: "Ranked resilience score",
    body: "Each service is scored 0–100 from the experiments that targeted it: how far the API success rate fell, how long recovery took, whether it needed manual intervention, and how severe the findings were. Services with no experiments are shown as untested rather than given a flattering default.",
  },
  safety: {
    title: "Why this is safe to run live",
    body: "Safety limits live in code, never in a prompt. The run aborts if the error rate over the last ten requests exceeds the threshold, if p95 latency passes the ceiling, or if any service outside the predicted blast radius fails three health checks. An independent watchdog timer force-restores the target even if the main loop hangs, and a separate Guardian process restores anything still down. The kill switch top-right stops everything within about a second.",
  },
  fixes: {
    title: "What to fix, and how",
    body: "One card per recommendation, pulled from every experiment's report and sorted by the risk it addresses. Each card names the problem, says why it happened, and lists concrete steps plus a way to verify the fix worked. This is advice only: Faultline never changes the target system, so every step here is something a person still has to do.",
  },
  cascade: {
    title: "How the failure travelled",
    body: "When one service breaks, the services that depend on it fail too. This reconstructs the order from timestamps and the dependency map, not from a model: who failed first, who failed next, through which dependency, and how (a timeout, a refused connection, a server error). Where the timing cannot prove the direction it says so, and 'masked' marks a service whose own health check stayed green while real requests through it were failing.",
  },
  topologyFindings: {
    title: "What the mapper noticed",
    body: "Gaps between what the deployment declares and what the system actually does. These are reported rather than silently corrected, because each one is a real risk: an undeclared dependency means nothing will start the services in the right order after an outage.",
  },
};

export const PHASES = [
  { key: "preflight", label: "Preflight", hint: "refusing to inject into an unhealthy system" },
  { key: "injecting", label: "Inject", hint: "applying the fault" },
  { key: "observe", label: "Observe", hint: "watching real traffic, safety armed" },
  { key: "rollback", label: "Rollback", hint: "removing the fault" },
  { key: "recovering", label: "Recover", hint: "waiting for the API to come back" },
  { key: "soaking", label: "Soak", hint: "confirming it is not a relapse" },
  { key: "analyzing", label: "Report", hint: "scoring risk and recommending fixes" },
];

// run.phase -> index in PHASES; app.js ribbonIndex() splits injecting into Inject then Observe
export const PHASE_INDEX = {
  idle: -1, preflight: 0, injecting: 2, rollback: 3, recovering: 4, soaking: 5, analyzing: 6,
};

export const FINDING_HELP = {
  health_check_masked_failure: "The platform reported this service as ready while real requests were failing — an orchestrator would have kept routing traffic to it. This mirrors Eclipse Ditto issue #1865.",
  dependency_blind_readiness: "Dependents stayed 'ready' while their dependency was gone, because readiness only reflects the service's own process.",
  no_automated_remediation: "Nothing reacted to the outage. Docker's restart policy only fires when a container exits, never when it is merely unhealthy.",
  manual_restart_required: "The system did not heal by itself; the tool had to restart containers. In production this is a page at 3am.",
  recovery_failed: "The system never came back within the recovery timeout.",
  relapse_after_recovery: "It looked recovered, then degraded again — which is why the soak phase exists.",
  data_loss_suspected: "A write the API had already acknowledged was missing afterwards. This is the most serious class of finding.",
  cascade_beyond_hypothesis: "The failure spread further than predicted. The blast radius is larger than the dependency map suggested.",
  slow_recovery: "Recovery took substantially longer than the hypothesis predicted.",
  auto_recovered: "The system healed on its own, with no intervention. This is the good outcome.",
  safety_abort: "A safety limit was crossed and the experiment was rolled back early, by design.",
  api_impact: "How the synthetic client experienced the fault.",
};

export const EMPTY = {
  topology: { title: "No application map yet", hint: "Map the services, dependencies and data flows.", action: "Rebuild map" },
  baseline: { title: "No baseline captured", hint: "Measure steady state so the abort thresholds are calibrated rather than guessed.", action: "Capture baseline" },
  experiments: { title: "No experiments yet", hint: "Pick a target and a failure on the left, or let the AI planner choose.", action: null },
  scorecard: { title: "No resilience score yet", hint: "Run an experiment, then the analysis agents will score each service.", action: null },
  weaknesses: { title: "No predictions yet", hint: "The Scout reads the deployment's config and predicts where it is weak, without breaking anything.", action: "Scout weaknesses" },
  fixes: { title: "No fixes yet", hint: "Run an experiment and the fix agent will recommend what to change and how.", action: null },
};

// Cascade attribution (faultline/observe/cascade.py). Short labels for CASCADE_MECHANISMS, mirroring
// MECHANISM_TEXT there; never add a mechanism here that the backend cannot emit.
export const CASCADE_MECHANISM = {
  injected_fault: "fault injected",
  process_down: "process down",
  process_paused: "process paused",
  liveness_failed: "/alive failed",
  readiness_failed: "/ready failed",
  dependency_timeout: "dependency timeout",
  dependency_unreachable: "dependency unreachable",
  dependency_error: "dependency error (5xx)",
  auth_failure: "auth failure",
  elevated_error_rate: "elevated error rate",
  latency_degraded: "latency degraded",
};

// PATH_CONFIDENCE in words. `upstream` is the dependency the hop failed through, `lag` its lag in seconds.
// ordered = direction proven by timing; the rest are honest about what the timing cannot prove.
export function cascadeConfidence(confidence, upstream, lag) {
  switch (confidence) {
    case "origin": return "origin: the fault was injected here";
    case "ordered":
      return typeof lag === "number" && upstream ? `ordered, +${lag.toFixed(1)} s after ${upstream}`
        : upstream ? `ordered, after ${upstream}` : "ordered";
    case "same_interval": return "same polling interval — direction unclear";
    case "ambiguous": return "one of several plausible paths";
    case "no_graph_path": return "no graph path to the origin";
    default: return confidence || "unknown";
  }
}
