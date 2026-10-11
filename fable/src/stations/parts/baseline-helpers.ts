// Hooks and formatting shared by the Baseline, Plan and Break stations (named baseline-* to stay
// inside this work's file ownership). Components live in baseline-kit.tsx.
import { useEffect, useState } from "react";
import { actions, useConnection, useFState } from "../../lib/store";
import { announce } from "../../lib/a11y";
import type { CatalogFailure } from "../../lib/types";

export type GlyphTone = "ok" | "blocked" | "note" | "wait" | "off";

/** How to fix what just failed, in the interface's voice. Null when the error already says it. */
export function fixFor(error: string | null | undefined, again: string): string | null {
  const e = error ?? "";
  if (!e) return null;
  if (/replay mode/i.test(e)) return null;
  if (/docker|daemon|socket|connection (refused|aborted)|ECONNREFUSED|FileNotFoundError|errno 2|errno 61|errno 111/i.test(e)) {
    return `Docker is not reachable. Start Docker Desktop, then run scripts/up.sh, then ${again}.`;
  }
  if (/already running/i.test(e)) return `One job runs at a time. Wait for it to finish, or press Stop everything, then ${again}.`;
  if (/no application map|topology/i.test(e)) return `There is no map yet. Map the system, then ${again}.`;
  return `The full error is in the terminal running faultline dashboard. Fix it there, then ${again}.`;
}

/** Arm and disarm the probes, with the failure reason kept visible until the next good arm. */
export function useArm() {
  const s = useFState();
  const conn = useConnection();
  const [pending, setPending] = useState<"arm" | "disarm" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const armed = !!s?.session.armed;

  const go = async (which: "arm" | "disarm") => {
    setPending(which);
    setActionError(null);
    const r = await (which === "arm" ? actions.arm() : actions.disarm());
    setPending(null);
    if (!r.ok) {
      setActionError(r.error ?? "The server refused.");
      announce(which === "arm" ? "Could not arm the probes." : "Could not disarm the probes.");
    } else {
      announce(which === "arm" ? "Probes armed. Samples are climbing." : "Probes disarmed.");
    }
  };

  const error = armed ? null : actionError ?? s?.session.error ?? null;
  return {
    armed,
    pending,
    error,
    fix: fixFor(error, "arm again"),
    samples: s?.session.probe.samples ?? 0,
    rps: s?.session.probe.rps ?? null,
    live: conn === "live",
    arm: () => go("arm"),
    disarm: () => go("disarm"),
  };
}

/** Seconds since `from` (unix seconds), ticking once a second while `from` is set. */
export function useElapsed(from: number | null): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (from == null) return;
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [from]);
  return from == null ? 0 : Math.max(0, now - from);
}

const PARAM_LABEL: Record<string, string> = {
  duration_s: "Duration",
  delay_ms: "Delay",
  jitter_ms: "Jitter",
  loss_percent: "Packet loss",
  workers: "Workers",
  writers: "Writers",
  size_mb: "Size",
  rps: "Rate",
  block_restart: "Block restart",
};

export function paramLabel(name: string): string {
  return PARAM_LABEL[name] ?? name.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}

export function paramUnit(name: string, failure?: CatalogFailure | null): string {
  const u = failure?.params.find((p) => p.name === name)?.unit;
  if (u != null) return u;
  if (name.endsWith("_s")) return "s";
  if (name.endsWith("_ms")) return "ms";
  if (name.endsWith("_mb")) return "MB";
  if (name.endsWith("_percent")) return "%";
  return "";
}

/** The mechanism in a catalog title, e.g. "SIGSTOP" from "Process freeze (SIGSTOP)". */
export function mechanism(f: CatalogFailure | null | undefined): string | null {
  const m = f ? /\(([^)]+)\)/.exec(f.title) : null;
  return m ? m[1] : null;
}

export function paramValue(name: string, v: number | boolean, failure?: CatalogFailure | null): string {
  if (typeof v === "boolean") return v ? "on" : "off";
  const unit = paramUnit(name, failure);
  return unit ? `${v} ${unit}` : String(v);
}
