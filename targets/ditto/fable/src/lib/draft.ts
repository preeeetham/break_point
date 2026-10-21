import { useSyncExternalStore } from "react";
import { actions } from "./store";
import type { Plan, PlanPick, RunRequest } from "./types";

/**
 * The experiment the operator has set up in Plan and will launch in Break. A tiny external store so
 * it survives station swaps (stations unmount) and a page reload (sessionStorage, best effort).
 *
 * Also holds the last planner answer and an in-flight planner request, because a model call can take
 * 30 s and the operator may wander to another station while it runs.
 */

export type DraftMode = "auto" | "manual";

export interface DraftSummary {
  /** null in auto mode when the planner preview had no pick */
  target: string | null;
  failure: string | null;
  params: Record<string, number | boolean>;
  plannedBy: "llm" | "deterministic" | "operator" | null;
  /** the hypothesis statement when the planner wrote one; operator tests get theirs at launch */
  hypothesis: string | null;
  expected: string[];
  /** where the idea came from, e.g. the Weakness Scout's reason */
  origin: string | null;
}

export interface Draft {
  mode: DraftMode;
  request: RunRequest;
  /** the planner's preview pick, display only; auto mode plans again at launch */
  pick: PlanPick | null;
  summary: DraftSummary;
  savedAt: number;
}

export interface PlanRequestState {
  status: "idle" | "asking" | "done" | "error";
  llm: boolean;
  startedAt: number | null;
  finishedAt: number | null;
  plan: Plan | null;
  error: string | null;
}

/** When this tab last launched an experiment, so Break can tell "just finished" from "last on disk". */
export interface LaunchMark {
  at: number;
  target: string | null;
  failure: string | null;
}

function createStore<T>(initial: T) {
  let value = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => value,
    set(next: T) {
      value = next;
      listeners.forEach((l) => l());
    },
    subscribe(l: () => void) {
      listeners.add(l);
      return () => {
        listeners.delete(l);
      };
    },
  };
}

const KEY = "fable.draft.v1";

function readSaved(): Draft | null {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    if (!raw) return null;
    const d = JSON.parse(raw) as Draft;
    return d && d.request && (d.mode === "auto" || d.mode === "manual") ? d : null;
  } catch {
    return null;
  }
}

function writeSaved(d: Draft | null) {
  try {
    if (d) window.sessionStorage.setItem(KEY, JSON.stringify(d));
    else window.sessionStorage.removeItem(KEY);
  } catch {
    /* storage can be blocked; the draft still lives in memory */
  }
}

const draftStore = createStore<Draft | null>(typeof window === "undefined" ? null : readSaved());
const planStore = createStore<PlanRequestState>({ status: "idle", llm: false, startedAt: null, finishedAt: null, plan: null, error: null });
const launchStore = createStore<LaunchMark | null>(null);

export function getDraft(): Draft | null {
  return draftStore.get();
}

export function saveDraft(d: Omit<Draft, "savedAt">): Draft {
  const next: Draft = { ...d, savedAt: Date.now() / 1000 };
  draftStore.set(next);
  writeSaved(next);
  return next;
}

export function clearDraft() {
  draftStore.set(null);
  writeSaved(null);
}

export function useDraft(): Draft | null {
  return useSyncExternalStore(draftStore.subscribe, draftStore.get, draftStore.get);
}

export function usePlanRequest(): PlanRequestState {
  return useSyncExternalStore(planStore.subscribe, planStore.get, planStore.get);
}

/** Ask the planner for a preview. Never runs anything. Resolves into the store, so callers may unmount. */
export async function askPlanner(llm: boolean): Promise<PlanRequestState> {
  if (planStore.get().status === "asking") return planStore.get();
  planStore.set({ ...planStore.get(), status: "asking", llm, startedAt: Date.now() / 1000, finishedAt: null, error: null });
  const r = await actions.plan(llm);
  const prev = planStore.get();
  planStore.set(
    r.ok && r.data
      ? { status: "done", llm, startedAt: prev.startedAt, finishedAt: Date.now() / 1000, plan: r.data, error: null }
      : { status: "error", llm, startedAt: prev.startedAt, finishedAt: Date.now() / 1000, plan: prev.plan, error: r.error ?? "The planner did not answer." },
  );
  return planStore.get();
}

export function markLaunch(mark: Omit<LaunchMark, "at">) {
  launchStore.set({ ...mark, at: Date.now() / 1000 });
}

export function useLaunchMark(): LaunchMark | null {
  return useSyncExternalStore(launchStore.subscribe, launchStore.get, launchStore.get);
}

/** The RunRequest every launch from this UI sends: one experiment, analysed right after. */
export function autoRequest(llm: boolean): RunRequest {
  return { mode: "auto", llm, experiments: 1, analyze: true };
}

export function manualRequest(target: string, failure: string, params: Record<string, number | boolean>): RunRequest {
  return { mode: "manual", target, failure, params, experiments: 1, analyze: true };
}
