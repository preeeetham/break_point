// Data hooks for the Score station: the Narrator's play-by-play and the full report's phase numbers.
// Both are cached per experiment id; reports on disk do not change once written.
import { useEffect, useState } from "react";
import { actions, useConnection } from "../../lib/store";
import type { Narrative } from "../../lib/types";

type Load<T> = { status: "loading" } | { status: "ready"; data: T } | { status: "missing" };

const narratives = new Map<string, Narrative | null>();

export function useNarrative(eid: string): Load<Narrative> {
  const conn = useConnection();
  const cached = narratives.get(eid);
  const [, bump] = useState(0);
  useEffect(() => {
    if (narratives.has(eid) || conn === "connecting") return;
    let alive = true;
    actions.narrative(eid).then((n) => {
      narratives.set(eid, n);
      if (alive) bump((v) => v + 1);
    });
    return () => {
      alive = false;
    };
  }, [eid, conn]);
  if (cached === undefined) return { status: "loading" };
  return cached ? { status: "ready", data: cached } : { status: "missing" };
}

export interface PhaseNumbers {
  sample_count: number | null;
  success_rate: number | null;
  p95_ms: number | null;
  status_counts?: Record<string, number>;
}

export interface FullPhases {
  baseline: PhaseNumbers | null;
  during: PhaseNumbers | null;
  after: PhaseNumbers | null;
}

const phases = new Map<string, FullPhases | null>();

/** GET /api/experiment/<id> for the per-phase sample counts and the after p95 the summary leaves out. Live only. */
export function usePhases(eid: string): FullPhases | null {
  const conn = useConnection();
  const [, bump] = useState(0);
  useEffect(() => {
    if (conn !== "live" || phases.has(eid)) return;
    const ctl = new AbortController();
    fetch(`/api/experiment/${encodeURIComponent(eid)}`, { signal: ctl.signal, cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: { experiment?: Partial<FullPhases> } | null) => {
        const e = d?.experiment;
        phases.set(eid, e ? { baseline: e.baseline ?? null, during: e.during ?? null, after: e.after ?? null } : null);
        bump((v) => v + 1);
      })
      .catch(() => {
        if (!ctl.signal.aborted) phases.set(eid, null);
      });
    return () => ctl.abort();
  }, [eid, conn]);
  return phases.get(eid) ?? null;
}
