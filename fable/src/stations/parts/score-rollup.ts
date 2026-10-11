// Cross-experiment rollups for the Score station's summary panels (automatic stops, all findings,
// coverage) and the findings report. Pure functions over /api/state; nothing here is invented.
// A few rules are duplicated from score-detail/score-fixes/score-text on purpose, so those files stay untouched.
import type { ExperimentSummary, Finding, Recommendation } from "../../lib/types";
import { stripLlmTag } from "./score-text";

export const SEV_RANK: Record<Finding["severity"], number> = { critical: 4, high: 3, medium: 2, low: 1, info: 0 };
export const MASKED = "health_check_masked_failure";

export function isModelExtra(r: Recommendation): boolean {
  return stripLlmTag(r.title).llm || stripLlmTag(r.rationale).llm;
}

export function isOperatorStop(e: ExperimentSummary): boolean {
  return e.aborted_early && (e.operator_stop || e.abort_reason === "operator_stop");
}

/** Stopped by the safety monitor or the watchdog, not by an operator.
 *  The watchdog only sets watchdog_fired and logs "watchdog_fired"; it never sets aborted_early. */
export function isAutoStop(e: ExperimentSummary): boolean {
  return (e.aborted_early || e.watchdog_fired) && !isOperatorStop(e);
}

/** t+ of the moment the fault was cut short: the safety monitor's stop, else the watchdog's. */
export function stopAt(e: ExperimentSummary): number | null {
  return tPlus(e, "safety_abort") ?? tPlus(e, "watchdog_fired");
}

/** The fault actually ran: pre-flight skips never injected anything. */
export function didRun(e: ExperimentSummary): boolean {
  return e.status !== "skipped";
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The numbers the safety monitor logged when it fired: the JSON tail of the timeline's safety_abort detail. */
export function abortDetail(e: ExperimentSummary): Record<string, number | null> {
  const raw = e.timeline?.find((t) => t.event === "safety_abort")?.detail ?? "";
  const brace = raw.indexOf("{");
  if (brace < 0) return {};
  try {
    return JSON.parse(raw.slice(brace)) as Record<string, number | null>;
  } catch {
    return {};
  }
}

/** t+ seconds of one timeline event, or null when the injector never logged it. */
export function tPlus(e: ExperimentSummary, event: string): number | null {
  return e.timeline?.find((t) => t.event === event)?.t_plus_s ?? null;
}

export type Outcome = "skipped" | "failed" | "aborted" | "violated" | "held" | "not judged";

export function outcomeOf(e: ExperimentSummary): Outcome {
  if (e.status === "skipped" || e.status === "failed") return e.status;
  if (e.aborted_early || e.watchdog_fired || e.status === "aborted") return "aborted";
  if (e.hypothesis_held === false) return "violated";
  if (e.hypothesis_held === true) return "held";
  return "not judged";
}

export interface FindingRow {
  rank: number;
  /** index of the finding within its experiment: a key that survives re-ranking */
  idx: number;
  finding: Finding;
  exp: ExperimentSummary;
  fix: Recommendation | null;
  fixByModel: boolean;
}

/** Every non-info finding, worst first: finding severity, then the experiment's risk, then the order the experiments ran.
 *  Same order as the Day-2 docs/FINDINGS.md, which keeps ties chronological. */
export function rankFindings(experiments: ExperimentSummary[]): FindingRow[] {
  const rows: Omit<FindingRow, "rank">[] = [];
  for (const exp of experiments) {
    const fix = exp.fix?.recommendations?.[0] ?? null;
    for (const [idx, finding] of (exp.findings ?? []).entries()) {
      if (finding.severity === "info") continue;
      rows.push({ idx, finding, exp, fix, fixByModel: !!fix && isModelExtra(fix) });
    }
  }
  const t = (e: ExperimentSummary) => Date.parse(e.started_at) || 0;
  const sev = (f: Finding) => SEV_RANK[f.severity] ?? 0;
  rows.sort(
    (a, b) =>
      sev(b.finding) - sev(a.finding) ||
      (b.exp.risk?.severity ?? -1) - (a.exp.risk?.severity ?? -1) ||
      t(a.exp) - t(b.exp),
  );
  return rows.map((r, i) => ({ ...r, rank: i + 1 }));
}
