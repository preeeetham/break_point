// Rows and sort orders for the All findings table. Every finding across every experiment, info included.
// rankFindings (score-rollup) stays the "above info" order that Proof and the findings report use; this is its
// superset, with the same worst first rule as the default order.
import type { ExperimentSummary, Finding, Recommendation } from "../../lib/types";
import { isModelExtra, SEV_RANK } from "./score-rollup";
import { findingName } from "./finding-names";

export interface TableRow {
  /** index of the finding within exp.findings: the key the experiment detail jumps to */
  idx: number;
  finding: Finding;
  exp: ExperimentSummary;
  /** plain-English name from finding-names */
  name: string;
  fix: Recommendation | null;
  fixByModel: boolean;
}

export type SortKey = "severity" | "name" | "service" | "experiment" | "when";
export type SortDir = "asc" | "desc";

export const SORT_LABEL: Record<SortKey, string> = {
  severity: "Severity",
  name: "Finding",
  service: "Service",
  experiment: "Experiment",
  when: "When",
};

/** The direction a column takes when it is first chosen. */
export const NATURAL: Record<SortKey, SortDir> = { severity: "desc", when: "desc", name: "asc", service: "asc", experiment: "asc" };

/** The order in words, for the narrow toggle and the announcement. */
export function dirWords(key: SortKey, dir: SortDir): string {
  if (key === "severity") return dir === "desc" ? "worst first" : "mildest first";
  if (key === "when") return dir === "desc" ? "newest first" : "oldest first";
  return dir === "asc" ? "A to Z" : "Z to A";
}

export function buildRows(experiments: ExperimentSummary[]): TableRow[] {
  const rows: TableRow[] = [];
  for (const exp of experiments) {
    const fix = exp.fix?.recommendations?.[0] ?? null;
    const fixByModel = !!fix && isModelExtra(fix);
    for (const [idx, finding] of (exp.findings ?? []).entries()) {
      rows.push({ idx, finding, exp, name: findingName(finding), fix, fixByModel });
    }
  }
  return rows;
}

const t = (e: ExperimentSummary) => Date.parse(e.started_at) || 0;
const sev = (r: TableRow) => SEV_RANK[r.finding.severity] ?? 0;
const risk = (r: TableRow) => r.exp.risk?.severity ?? -1;
const text = (a: string, b: string) => a.localeCompare(b, undefined, { sensitivity: "base", numeric: true });

/** Worst first: finding severity, then the experiment's risk (unscored last), then the order the experiments ran,
 *  then the finding's place in its experiment. The experiment id only splits two runs that started together. */
export function worstFirst(a: TableRow, b: TableRow): number {
  return (
    sev(b) - sev(a) ||
    risk(b) - risk(a) ||
    t(a.exp) - t(b.exp) ||
    text(a.exp.experiment_id, b.exp.experiment_id) ||
    a.idx - b.idx
  );
}

function ascending(key: Exclude<SortKey, "severity">, a: TableRow, b: TableRow): number {
  if (key === "name") return text(a.name, b.name);
  if (key === "service") return text(a.exp.target_service, b.exp.target_service);
  if (key === "experiment") return text(a.exp.experiment_id, b.exp.experiment_id);
  return t(a.exp) - t(b.exp);
}

/** Severity flips the whole worst first order. Every other column sorts on its own value, ties worst first. */
export function sortRows(rows: TableRow[], key: SortKey, dir: SortDir): TableRow[] {
  const sign = dir === "asc" ? 1 : -1;
  const cmp =
    key === "severity"
      ? (a: TableRow, b: TableRow) => -sign * worstFirst(a, b)
      : (a: TableRow, b: TableRow) => sign * ascending(key, a, b) || worstFirst(a, b);
  return [...rows].sort(cmp);
}
