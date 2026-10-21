import { useState } from "react";
import { useConnection, useFState } from "../../lib/store";
import { clean, relTime } from "../../lib/format";
import type { Draft } from "../../lib/draft";
import type { RunPhase, RunState, ServiceHealth } from "../../lib/types";
import type { MarkerState } from "../../components/Marker";
import type { GlyphTone } from "./baseline-helpers";

export interface Check {
  id: "server" | "probes" | "baseline" | "stop" | "job" | "guardian";
  tone: GlyphTone;
  word: string;
  label: string;
  detail?: string;
  /** the sentence shown under the launch control when this check blocks, else null */
  blocks: string | null;
}

/** Every precondition, glyph and word, and the first one that blocks the launch. */
export function useChecks(draft: Draft | null): { checks: Check[]; blocked: string | null } {
  const s = useFState();
  const conn = useConnection();
  const checks: Check[] = [];

  checks.push(
    conn === "live"
      ? { id: "server", tone: "ok", word: "ok", label: "Server live", blocks: null }
      : conn === "replay"
        ? { id: "server", tone: "blocked", word: "blocked", label: "Server not reachable", detail: "Showing recorded data. Start it with faultline dashboard --port 8777.", blocks: "The Faultline server is not reachable." }
        : { id: "server", tone: "wait", word: "wait", label: "Connecting to the server", blocks: "Still connecting to the server." },
  );

  const armed = !!s?.session.armed;
  const samples = s?.session.probe.samples ?? 0;
  checks.push(
    !armed
      ? { id: "probes", tone: "blocked", word: "blocked", label: "Probes off", detail: "Preflight refuses to inject without probe samples.", blocks: "Arm the probes first." }
      : samples < 5
        ? { id: "probes", tone: "wait", word: "wait", label: `Probes warming up, ${samples} samples`, detail: "Preflight needs a few seconds of samples.", blocks: "The probes are still warming up." }
        : { id: "probes", tone: "ok", word: "ok", label: `Probes armed, ${samples.toLocaleString()} samples`, blocks: null },
  );

  checks.push(
    s?.baseline
      ? { id: "baseline", tone: "ok", word: "ok", label: `Baseline captured ${relTime(s.baseline.captured_at, s.now)}`, blocks: null }
      : { id: "baseline", tone: "note", word: "note", label: "No baseline", detail: "The limits fall back to the config floors.", blocks: null },
  );

  checks.push(
    s?.stop_requested
      ? { id: "stop", tone: "blocked", word: "blocked", label: "STOP is latched", detail: "Clear it on purpose before the next launch.", blocks: "STOP is latched. Resume first." }
      : { id: "stop", tone: "ok", word: "ok", label: "No STOP latched", blocks: null },
  );

  const job = s?.job?.status === "running" ? s.job : null;
  checks.push(
    job
      ? { id: "job", tone: "blocked", word: "blocked", label: `${clean(job.label)} is running`, detail: "One job runs at a time.", blocks: `${clean(job.label)} is still running.` }
      : { id: "job", tone: "ok", word: "ok", label: "No other job running", blocks: null },
  );

  const g = s?.guardian;
  const age = g && s ? s.now - g.at : null;
  checks.push(
    age != null && age < 15
      ? { id: "guardian", tone: "ok", word: "ok", label: `Guardian on, heartbeat ${Math.max(0, Math.round(age))} s ago`, blocks: null }
      : {
          id: "guardian", tone: "note", word: "off", label: "Guardian off: start faultline guardian",
          detail: "The in-process safety monitor still guards the test. The Guardian is the second, independent kill switch.", blocks: null,
        },
  );

  const blocked = !draft ? "Plan a test first." : checks.find((c) => c.blocks)?.blocks ?? null;
  return { checks, blocked };
}

const ABORT_WORD: Record<string, string> = {
  error_rate: "the error rate crossed the limit",
  latency: "p95 latency crossed the limit",
  operator_stop: "an operator pressed Stop everything",
};

export function abortText(reason: string | null | undefined): string | null {
  if (!reason) return null;
  if (reason.startsWith("blast_radius:")) return `${reason.slice("blast_radius:".length)} failed health checks outside the hypothesis`;
  return ABORT_WORD[reason] ?? clean(reason).replaceAll("_", " ");
}

export function isDown(h: ServiceHealth | undefined): boolean {
  return !!h && !!h.status && h.status !== "running";
}

/**
 * Marker state per service: predicted outlined, observed in red (down when the container is not
 * running), recovered in green once a service seen failing stops failing, the target in the marker
 * ring. "Seen failing" is remembered per experiment, adjusted during render as React recommends.
 */
export function useBlastStates(run: RunState, phase: RunPhase): { states: Record<string, MarkerState>; recovered: string[] } {
  const target = run.target_service;
  const observed = (run.blast?.observed ?? []).filter((x) => x !== target);
  const [seen, setSeen] = useState<{ id: string | null; list: string[] }>({ id: run.experiment_id, list: [] });
  const base = seen.id === run.experiment_id ? seen.list : [];
  const added = observed.filter((x) => !base.includes(x));
  if (seen.id !== run.experiment_id || added.length) setSeen({ id: run.experiment_id, list: [...base, ...added] });
  const ever = [...base, ...added];

  const states: Record<string, MarkerState> = {};
  for (const x of run.blast?.expected ?? []) states[x] = "predicted";
  const healing = phase === "recovering" || phase === "soaking" || phase === "analyzing";
  const recovered = healing ? ever.filter((x) => !observed.includes(x)) : [];
  for (const x of recovered) states[x] = "recovered";
  for (const x of observed) states[x] = isDown(run.health?.[x]) ? "down" : "affected";
  if (target) states[target] = "target";
  return { states, recovered };
}
