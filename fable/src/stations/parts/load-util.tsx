// Hooks and plain functions shared by the Load, Map and Predict stations (kept apart from the
// components so fast refresh works). The load- prefix is the file namespace this work owns.
import { useState } from "react";
import { actions, jobRunning, useFState } from "../../lib/store";
import type { JobKind, State } from "../../lib/types";
import { clean } from "../../lib/format";

type StationJobKind = Exclude<JobKind, "plan" | "run">;

/**
 * One station's view of the single job slot: is our kind running, did it fail, and did the server
 * refuse the request outright (409 busy, 400 bad path, replay mode).
 */
export function useStationJob(kind: StationJobKind) {
  const s = useFState();
  const [submitting, setSubmitting] = useState(false);
  const [startedId, setStartedId] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);

  const job = s?.job && s.job.kind === kind ? s.job : null;
  const running = jobRunning(s, kind);
  // between the 202 and the next poll the job record still shows the previous job
  const pending = submitting || (startedId != null && s?.job?.id !== startedId);
  const other = s?.job && s.job.status === "running" && s.job.kind !== kind ? s.job : null;

  async function start(body: Record<string, unknown>) {
    setRefusal(null);
    setSubmitting(true);
    const r = await actions.job(kind, body);
    setSubmitting(false);
    if (!r.ok) {
      setStartedId(null);
      setRefusal(r.error ?? "The server refused the job.");
    } else {
      const id = (r.data as { job?: { id?: string } } | undefined)?.job?.id;
      setStartedId(id ?? null);
    }
    return r;
  }

  return {
    job,
    // true only for the job this station started in this session; a failure already on the server when the
    // station opened is persisted state and must not be announced as an alert
    mine: startedId != null && job?.id === startedId,
    busy: pending || running,
    running,
    failed: job?.status === "failed" ? job.error ?? "The job failed without a message." : null,
    done: job?.status === "done",
    other,
    refusal,
    clearRefusal: () => setRefusal(null),
    start,
  };
}

/** Backend text for display: no em or en dashes, no spaced double hyphens, no exception class prefix. */
export function prose(text: string | null | undefined): string {
  return clean(text).replace(/\s+--\s+/g, ": ");
}

export function stripExc(text: string | null | undefined): string {
  return prose(text).replace(/^[A-Z][A-Za-z]*(Error|Exception): /, "");
}

// --------------------------------------------------------------------------- paths

const ROOT_RE = /^(.*?)\/(deploy|config|reports|\.cache\/onboarding)\//;

/** The Faultline checkout on the server, inferred from the configured target paths. */
export function repoRoot(target: State["target"]): string | null {
  for (const p of [target?.compose_file, target?.knowledge_file]) {
    const m = p && p.startsWith("/") ? ROOT_RE.exec(p) : null;
    if (m) return m[1];
  }
  return null;
}

export function repoRel(path: string | null | undefined, root: string | null): string {
  if (!path) return "";
  if (root && path.startsWith(`${root}/`)) return path.slice(root.length + 1);
  return path.replace(/^\.\//, "");
}

const ADOPTABLE = ["deploy/", "config/", ".cache/onboarding/"];

/** Mirrors the server's rule for POST /api/target, so the refusal can be explained before the click. */
export function adoptable(rel: string): boolean {
  return !rel.startsWith("/") && !rel.includes("..") && ADOPTABLE.some((p) => rel.startsWith(p));
}
