// Pure helpers for the Reconcile station. No components live here.
import type { Catalog, PredictedWeakness, WeaknessOutcome } from "../../lib/types";

export const STATE_ORDER: WeaknessOutcome["state"][] = ["confirmed", "cleared", "untested"];

/** confirming_findings arrive as "experimentId:finding_code". Group the codes by experiment. */
export function confirmingByExperiment(o: WeaknessOutcome): Map<string, string[]> {
  const m = new Map<string, string[]>();
  for (const cf of o.confirming_findings) {
    const i = cf.indexOf(":");
    const eid = i > 0 ? cf.slice(0, i) : cf;
    const code = i > 0 ? cf.slice(i + 1) : "";
    if (!m.has(eid)) m.set(eid, []);
    if (code) m.get(eid)!.push(code);
  }
  return m;
}

/** The Scout's hit rate: confirmed / (confirmed + cleared). Null when nothing was confirmed or cleared. */
export function hitRate(confirmed: number, cleared: number): number | null {
  const d = confirmed + cleared;
  return d > 0 ? confirmed / d : null;
}

/** Does the catalog let this failure target this service at all? Unknown (no catalog) counts as yes; the Planner checks again. */
export function testable(sug: PredictedWeakness["suggested_experiment"], catalog: Catalog | null): boolean {
  if (!sug) return false;
  if (!catalog) return true;
  const f = catalog.failures.find((x) => x.key === sug.failure_type);
  return !!f && f.applies_to.includes(sug.target_service);
}
