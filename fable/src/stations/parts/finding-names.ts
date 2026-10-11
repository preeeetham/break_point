// Plain-English names for the finding codes the injector and analyser raise (faultline/agents), so the findings
// table reads for a non-technical viewer. The raw code stays visible next to the name; the backend's own title,
// which carries the measured numbers, stays as the detail line. Help text follows the control room's copy.js.
import type { Finding } from "../../lib/types";

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
    help: "Services outside the hypothesis were affected, so the blast radius is larger than the dependency map suggested.",
  },
  data_loss_suspected: {
    name: "A write the API acknowledged went missing",
    help: "A write the API had already acknowledged was missing afterwards. This is the most serious class of finding.",
  },
  recovery_failed: {
    name: "Never recovered within the timeout",
    help: "The system did not come back within the recovery timeout.",
  },
  manual_restart_required: {
    name: "Needed a manual restart to recover",
    help: "The system did not heal by itself; containers had to be restarted for it. In production this is a page at 3am.",
  },
  no_automated_remediation: {
    name: "Nothing reacted to the outage",
    help: "Docker's restart policy only fires when a container exits, never when it is merely unhealthy.",
  },
  relapse_after_recovery: {
    name: "Degraded again after looking recovered",
    help: "It looked recovered, then degraded again, which is why the soak phase exists.",
  },
  slow_recovery: {
    name: "Recovered slower than predicted",
    help: "Recovery took substantially longer than the hypothesis predicted.",
  },
  safety_abort: {
    name: "Safety limit crossed, fault rolled back early",
    help: "A safety limit was crossed and the experiment was rolled back early, by design.",
  },
  api_impact: {
    name: "How the API held up during the fault",
    help: "How the synthetic client traffic experienced the fault: success rate and p95 latency against the baseline.",
  },
  auto_recovered: {
    name: "Recovered on its own",
    help: "The system healed with no intervention. This is the good outcome.",
  },
};

/** The plain-English name for a finding; unknown codes fall back to the code in words, in sentence case. */
export function findingName(f: Pick<Finding, "code">): string {
  const known = FINDING_NAMES[f.code]?.name;
  if (known) return known;
  const words = f.code.replace(/_+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Unnamed finding";
}

export function findingHelp(f: Pick<Finding, "code">): string | null {
  return FINDING_NAMES[f.code]?.help ?? null;
}
