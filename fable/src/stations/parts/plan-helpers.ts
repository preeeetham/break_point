import type { Catalog, CatalogFailure, CatalogParam, Topology } from "../../lib/types";
import { clean } from "../../lib/format";
import { chaosServices } from "../../components/TopologyMap";

export type Params = Record<string, number | boolean>;

/** Services no available, policy-allowed failure can touch, with the reason in plain words. */
export function lockReasons(topology: Topology, catalog: Catalog): Record<string, string> {
  const out: Record<string, string> = {};
  for (const id of chaosServices(topology)) {
    const svc = topology.services[id];
    if (!svc) continue;
    if (!svc.chaos_relevant) {
      out[id] = `excluded from testing, role ${svc.role}`;
      continue;
    }
    const fits = catalog.failures.filter((f) => f.applies_to.includes(id) || f.policy_blocked_on.includes(id));
    if (!fits.length) {
      out[id] = "no failure type applies to it";
      continue;
    }
    const allowed = fits.filter((f) => !f.policy_blocked_on.includes(id));
    if (!allowed.length) {
      out[id] = catalog.protected[id]?.reason ? `protected: ${clean(catalog.protected[id].reason)}` : "the safety policy blocks every failure that fits";
      continue;
    }
    if (!allowed.some((f) => f.available)) out[id] = "every failure that fits needs a helper image that is missing";
  }
  return out;
}

/** Why this failure cannot run on this service, or null when it can. */
export function whyNot(f: CatalogFailure, service: string): string | null {
  if (f.policy_blocked_on.includes(service)) return `Blocked by the safety policy: ${service} is protected.`;
  if (!f.available) return `Unavailable: ${clean(f.unavailable_reason ?? "a helper image is missing")}.`;
  return null;
}

export function clampParam(p: CatalogParam, v: number | boolean | undefined): number | boolean {
  if (p.type === "bool") return typeof v === "boolean" ? v : Boolean(p.default);
  const n = typeof v === "number" && Number.isFinite(v) ? v : Number(p.default);
  const lo = p.lo ?? -Infinity;
  const hi = p.hi ?? Infinity;
  return Math.min(hi, Math.max(lo, n));
}

/** Catalog defaults with the operator's changes on top, every number clamped to its bounds. */
export function effectiveParams(f: CatalogFailure | null, overrides: Params): Params {
  if (!f) return {};
  return Object.fromEntries(f.params.map((p) => [p.name, clampParam(p, overrides[p.name])]));
}

/** A slider step that keeps the default reachable and gives at least ten stops. */
export function stepFor(p: CatalogParam): number {
  const lo = p.lo ?? 0;
  const hi = p.hi ?? 100;
  const def = Number(p.default);
  for (const s of [100, 64, 50, 32, 25, 20, 10, 5, 2]) {
    if ((def - lo) % s === 0 && (hi - lo) % s === 0 && (hi - lo) / s >= 10) return s;
  }
  return 1;
}
