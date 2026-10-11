import { useSyncExternalStore } from "react";
import type { Catalog, EventItem, JobKind, Narrative, Plan, RunRequest, State } from "./types";

export type Connection = "connecting" | "live" | "replay";

export interface ActionResult<T = unknown> {
  ok: boolean;
  status: number;
  error?: string;
  data?: T;
}

interface Snapshot {
  connection: Connection;
  state: State | null;
  events: EventItem[];
  catalog: Catalog | null;
  lastError: string | null;
  lastPollAt: number;
}

const REPLAY = `${import.meta.env.BASE_URL}replay/`;
const IDLE_MS = 1000;
const LIVE_MS = 400;
const HIDDEN_MS = 5000;
const REPLAY_RETRY_MS = 15000;
const MAX_EVENTS = 400;

let snap: Snapshot = { connection: "connecting", state: null, events: [], catalog: null, lastError: null, lastPollAt: 0 };
const listeners = new Set<() => void>();
let seq = 0;
let failures = 0;
let timer = 0;
let started = false;

function set(patch: Partial<Snapshot>) {
  snap = { ...snap, ...patch };
  listeners.forEach((l) => l());
}

async function getJSON<T>(url: string, timeoutMs = 6000): Promise<T> {
  const ctl = new AbortController();
  const t = window.setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctl.signal, cache: "no-store" });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(t);
  }
}

async function post<T = unknown>(path: string, body: unknown = {}, timeoutMs = 15000): Promise<ActionResult<T>> {
  const ctl = new AbortController();
  const t = window.setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctl.signal });
    const data = (await res.json().catch(() => ({}))) as T & { error?: string };
    return { ok: res.ok, status: res.status, error: res.ok ? undefined : data.error ?? `${res.status} ${res.statusText}`, data };
  } catch (exc) {
    const aborted = exc instanceof DOMException && exc.name === "AbortError";
    return { ok: false, status: 0, error: aborted ? `no answer from the server within ${timeoutMs / 1000} s` : exc instanceof Error ? exc.message : String(exc) };
  } finally {
    window.clearTimeout(t);
  }
}

async function loadReplay() {
  try {
    const [state, catalog] = await Promise.all([getJSON<State>(`${REPLAY}state.json`), getJSON<Catalog>(`${REPLAY}catalog.json`)]);
    set({ state, catalog, events: state.events.slice(-MAX_EVENTS) });
  } catch (exc) {
    set({ lastError: `replay data unavailable: ${exc instanceof Error ? exc.message : exc}` });
  }
}

function schedule(ms: number) {
  window.clearTimeout(timer);
  timer = window.setTimeout(tick, ms);
}

function isBusy(s: State | null): boolean {
  return !!s && (s.run.phase !== "idle" || s.job?.status === "running");
}

async function tick() {
  if (document.hidden && snap.connection === "live") return schedule(HIDDEN_MS);
  try {
    const s = await getJSON<State>(`/api/state?since=${seq}`, 8000);
    failures = 0;
    if (snap.connection !== "live") {
      set({ connection: "live", events: [] });
      getJSON<Catalog>("/api/catalog").then((catalog) => set({ catalog })).catch(() => {});
    }
    seq = s.seq;
    const events = s.events.length ? [...snap.events, ...s.events].slice(-MAX_EVENTS) : snap.events;
    set({ state: s, events, lastError: null, lastPollAt: Date.now() });
    schedule(isBusy(s) ? LIVE_MS : IDLE_MS);
  } catch (exc) {
    failures += 1;
    set({ lastError: exc instanceof Error ? exc.message : String(exc) });
    if (failures >= 2 && snap.connection !== "replay") {
      set({ connection: "replay" });
      await loadReplay();
    }
    schedule(snap.connection === "replay" ? REPLAY_RETRY_MS : IDLE_MS);
  }
}

function start() {
  if (started) return;
  started = true;
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) schedule(0);
  });
  tick();
}

function subscribe(l: () => void) {
  start();
  listeners.add(l);
  return () => listeners.delete(l);
}

/** Subscribe to one slice. The selector must return a stable reference (a field of the snapshot, not a new object). */
export function useFaultline<T>(select: (s: Snapshot) => T): T {
  return useSyncExternalStore(subscribe, () => select(snap), () => select(snap));
}

export const useConnection = () => useFaultline((s) => s.connection);
export const useFState = () => useFaultline((s) => s.state);
export const useEvents = () => useFaultline((s) => s.events);
export const useCatalog = () => useFaultline((s) => s.catalog);

const replayRefusal = (): ActionResult => ({
  ok: false,
  status: 0,
  error: "Replay mode: the Faultline server is not reachable, so nothing can run. Start it with faultline dashboard --port 8777.",
});

function guarded<A extends unknown[], R>(fn: (...a: A) => Promise<ActionResult<R>>) {
  return (...a: A): Promise<ActionResult<R>> => (snap.connection === "live" ? fn(...a) : Promise.resolve(replayRefusal() as ActionResult<R>));
}

function kick<R>(p: Promise<ActionResult<R>>) {
  return p.then((r) => {
    schedule(0);
    return r;
  });
}

export const actions = {
  job: guarded((kind: Exclude<JobKind, "plan" | "run">, body: Record<string, unknown> = {}) => kick(post(`/api/jobs/${kind}`, body))),
  run: guarded((req: RunRequest) => kick(post("/api/jobs/run", req))),
  cancel: guarded(() => kick(post("/api/jobs/cancel"))),
  arm: guarded(() => kick(post("/api/arm"))),
  disarm: guarded(() => kick(post("/api/disarm"))),
  resume: guarded(() => kick(post("/api/resume"))),
  adoptTarget: guarded((compose: string | null, knowledge: string | null) => kick(post("/api/target", { compose, knowledge }))),
  // the kill switch is never guarded: it must always try
  stop: () => kick(post("/api/stop", {}, 4000)),
  retryLive: () => {
    failures = 0;
    set({ connection: "connecting" });
    schedule(0);
  },
  async plan(llm: boolean, allowProtected = false): Promise<ActionResult<Plan>> {
    try {
      const data =
        snap.connection === "live"
          ? await getJSON<Plan>(`/api/plan?llm=${llm ? 1 : 0}&allow_protected=${allowProtected ? 1 : 0}`, llm ? 60000 : 15000)
          : await getJSON<Plan>(`${REPLAY}plan.json`);
      return { ok: true, status: 200, data };
    } catch (exc) {
      return { ok: false, status: 0, error: exc instanceof Error ? exc.message : String(exc) };
    }
  },
  async narrative(eid: string): Promise<Narrative | null> {
    try {
      return snap.connection === "live" ? await getJSON<Narrative>(`/api/narrative/${eid}`) : await getJSON<Narrative>(`${REPLAY}narrative-${eid}.json`);
    } catch {
      return null;
    }
  },
  async replayOnboarding(): Promise<{ events: EventItem[]; onboarding: State["onboarding"] } | null> {
    try {
      return await getJSON(`${REPLAY}onboarding.json`);
    } catch {
      return null;
    }
  },
};

export function isRunActive(s: State | null): boolean {
  return !!s && s.run.phase !== "idle";
}

export function jobRunning(s: State | null, kind?: JobKind): boolean {
  return !!s?.job && s.job.status === "running" && (!kind || s.job.kind === kind);
}
