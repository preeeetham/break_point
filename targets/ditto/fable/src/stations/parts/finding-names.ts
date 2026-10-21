// Plain-English names for the finding codes the injector raises (faultline/agents/injector.py), so the findings
// table reads for a non-technical viewer. The raw code stays visible next to the name; the backend's own title,
// which carries the measured numbers, stays as the detail line. Help text follows the control room's copy.js,
// corrected against the conditions the injector actually checks.
import type { Finding } from "../../lib/types";

type Named = Pick<Finding, "code" | "severity">;

export const FINDING_NAMES: Record<string, { name: string; help: string }> = {
  health_check_masked_failure: {
    name: "Health check stayed green while real requests failed",
    help: "The platform reported this service as ready while real requests were failing, so an orchestrator would have kept routing traffic to it. This mirrors Eclipse Ditto issue #1865.",
  },
  dependency_blind_readiness: {
    name: "Dependents stayed ready while a dependency was down",
    help: "Readiness only reflects each service's own process, not whether it can actually serve requests.",
  },
  cascade_beyond_hypothesis: {
    name: "Failure spread further than predicted",
    help: "Services the hypothesis did not expect to be affected went down or failed their health checks, so the blast radius was larger than predicted.",
  },
  data_loss_suspected: {
    name: "An acknowledged write appears lost",
    help: "The canary's sequence number read back after recovery was lower than the last write the API acknowledged. This is the most serious class of finding.",
  },
  recovery_failed: {
    name: "Never recovered within the timeout",
    help: "The system did not come back within the recovery timeout.",
  },
  manual_restart_required: {
    name: "Needed restarts beyond the rollback to recover",
    help: "Removing the fault was not enough. Faultline had to restart the target or its dependents before the API came back. In production someone would do that by hand.",
  },
  no_automated_remediation: {
    name: "Nothing reacted to the outage",
    help: "The target container stayed running and Docker never marked it unhealthy, so nothing restarted or replaced it. A restart policy only acts when a container exits.",
  },
  relapse_after_recovery: {
    name: "Degraded again after looking recovered",
    help: "It looked recovered, then degraded again, which is why the soak phase exists.",
  },
  slow_recovery: {
    name: "Recovered slower than predicted",
    help: "Recovery took more than one and a half times as long as the hypothesis predicted.",
  },
  safety_abort: {
    name: "Safety limit crossed, fault rolled back early",
    help: "A limit on error rate, p95 latency or a protected service's health was crossed, so the experiment was rolled back early, by design.",
  },
  api_impact: {
    name: "How the API held up during the fault",
    help: "How the synthetic client traffic experienced the fault: success rate and p95 latency against the baseline.",
  },
  auto_recovered: {
    name: "Recovered on its own",
    help: "Once the fault was removed the system came back with no extra restarts. This is the good outcome.",
  },
};

// api_impact is raised at every severity (injector.py): high below 50 % success, medium below 95 %,
// low when success held but p95 more than doubled, info when the API was unaffected.
const API_IMPACT_NAME: Record<Finding["severity"], string> = {
  critical: "Most API requests failed during the fault",
  high: "Most API requests failed during the fault",
  medium: "Some API requests failed during the fault",
  low: "API slowed down during the fault",
  info: "API held up during the fault",
};

// safety_abort at info is an operator stop, not a crossed limit (injector.py).
const OPERATOR_STOP = {
  name: "Stopped by the operator, fault rolled back",
  help: "An operator pressed stop, so the fault was rolled back early. This is not a weakness of the system.",
};

/** The plain-English name for a finding; unknown codes fall back to the code in words, in sentence case. */
export function findingName(f: Named): string {
  if (f.code === "safety_abort" && f.severity === "info") return OPERATOR_STOP.name;
  if (f.code === "api_impact") return API_IMPACT_NAME[f.severity] ?? FINDING_NAMES.api_impact.name;
  const known = FINDING_NAMES[f.code]?.name;
  if (known) return known;
  const words = f.code.replace(/_+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Unnamed finding";
}

export function findingHelp(f: Named): string | null {
  if (f.code === "safety_abort" && f.severity === "info") return OPERATOR_STOP.help;
  return FINDING_NAMES[f.code]?.help ?? null;
}
