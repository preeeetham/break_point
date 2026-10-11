// Pure helpers for the Score and Reconcile stations. No components live here.
import { clean, fmtMs, fmtPct } from "../../lib/format";
import type { ExperimentSummary, Risk } from "../../lib/types";

const SHOUT = /\b(CRITICAL|HIGH|MEDIUM|LOW|INFO)\b/g;
const PY_LIST = /\[((?:'[^'\]]*'(?:,\s*)?)*)\]/g;

/** Backend prose made fit for the UI: no dashes, no shouted severity words, python list reprs as plain lists. */
export function tidy(text: string | null | undefined): string {
  return clean(text)
    .replace(SHOUT, (m) => m.toLowerCase())
    .replace(PY_LIST, (_m, inner: string) => inner.replace(/'/g, "") || "none");
}

export const LLM_TAG = "[LLM suggestion, unverified]";

/** The Fix agent force-prefixes model extras with LLM_TAG. Split it off so the UI can tag it visibly instead. */
export function stripLlmTag(text: string | null | undefined): { llm: boolean; text: string } {
  const t = (text ?? "").trim();
  return t.startsWith(LLM_TAG) ? { llm: true, text: t.slice(LLM_TAG.length).trim() } : { llm: false, text: t };
}

export function byModel(generatedBy: string | null | undefined): boolean {
  return /llm/i.test(generatedBy ?? "");
}

/** hypothesis_notes arrives as one "; " joined string from the server, or as a list from older reports. */
export function noteList(notes: string | string[] | null | undefined): string[] {
  if (!notes) return [];
  const arr = Array.isArray(notes) ? notes : notes.split(/;\s*/);
  return arr.map((n) => tidy(n)).filter(Boolean);
}

export function newestFirst(list: ExperimentSummary[]): ExperimentSummary[] {
  return [...list].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at));
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
}

/** Trim a number to at most `digits` decimals without trailing zeros: 4.000 -> 4, 0.2860 -> 0.286. */
export function num(v: number, digits = 3): string {
  return String(Number(v.toFixed(digits)));
}

export function pctSmart(v: number | null | undefined): string {
  if (v == null) return "n/a";
  const p = v * 100;
  return fmtPct(v, Math.abs(p - Math.round(p)) < 0.05 ? 0 : 1);
}

// ---------------------------------------------------------------- abort

export function abortWho(e: ExperimentSummary): string {
  if (e.operator_stop) return "An operator pressed Stop everything";
  if (e.watchdog_fired) return "The watchdog fired at the deadline";
  return "The safety monitor rolled the fault back";
}

/** Plain words for the safety monitor's verdict, using the numbers it logged when it fired. */
export function abortExplain(e: ExperimentSummary): { at: number | null; text: string } {
  const entry = e.timeline?.find((t) => t.event === "safety_abort");
  const reason = e.abort_reason ?? "";
  let d: Record<string, number> = {};
  const raw = entry?.detail ?? "";
  const brace = raw.indexOf("{");
  if (brace >= 0) {
    try {
      d = JSON.parse(raw.slice(brace)) as Record<string, number>;
    } catch {
      d = {};
    }
  }
  const n = d.trailing_samples;
  const over = n != null ? ` over the last ${n} requests` : "";
  let text: string;
  if (reason === "error_rate") {
    text = d.error_rate != null && d.threshold != null
      ? `error rate ${pctSmart(d.error_rate)}${over}, above the ${pctSmart(d.threshold)} limit`
      : "error rate above the limit";
  } else if (reason === "latency") {
    text = d.p95_ms != null && d.threshold_ms != null
      ? `p95 ${fmtMs(d.p95_ms)}${over}, above the ${fmtMs(d.threshold_ms)} limit`
      : "p95 latency above the limit";
  } else if (reason.startsWith("blast_radius:")) {
    const svc = reason.slice("blast_radius:".length);
    text = d.consecutive_failures != null
      ? `${svc}, outside the hypothesis, failed ${d.consecutive_failures} health checks in a row`
      : `${svc}, outside the hypothesis, kept failing its health checks`;
  } else if (reason === "operator_stop") {
    text = "operator stop";
  } else if (reason) {
    text = reason.replaceAll("_", " ");
  } else {
    text = "no reason recorded";
  }
  return { at: entry ? entry.t_plus_s : null, text };
}

// ---------------------------------------------------------------- risk arithmetic (faultline/agents/risk.py)

export const FACTOR_ORDER = ["impact", "blast", "recovery", "intervention", "findings"];

export const FACTOR_INFO: Record<string, { label: string; rule: string }> = {
  impact: { label: "User impact", rule: "4 × (1 - success during the fault)" },
  blast: { label: "Blast radius", rule: "2 × services affected / services mapped" },
  recovery: { label: "Recovery time", rule: "2 × time to recover / 120\u00a0s, at most 2" },
  intervention: { label: "Intervention", rule: "1.5 if recovery needed a manual step" },
  findings: { label: "Findings", rule: "+1 masked health check or data loss, +0.5 cascade or manual restart" },
};

export const LEVEL_EDGES: { level: Risk["level"]; from: number; to: number }[] = [
  { level: "low", from: 0, to: 3 },
  { level: "medium", from: 3, to: 5.5 },
  { level: "high", from: 5.5, to: 8 },
  { level: "critical", from: 8, to: 10 },
];

export function levelInk(level: string | null | undefined): { text: string; bg: string } {
  if (level === "critical" || level === "high") return { text: "text-fail", bg: "bg-fail" };
  if (level === "medium") return { text: "text-fg", bg: "bg-fg" };
  return { text: "text-fg-2", bg: "bg-fg-2" };
}

export function orderedFactors(f: Record<string, number> | null | undefined): [string, number][] {
  const entries = Object.entries(f ?? {});
  const known = FACTOR_ORDER.filter((k) => k in (f ?? {})).map((k) => [k, f![k]] as [string, number]);
  const rest = entries.filter(([k]) => !FACTOR_ORDER.includes(k));
  return [...known, ...rest];
}

// ---------------------------------------------------------------- scorecard (faultline/agents/scorecard.py)

export const GRADE_EDGES = [50, 65, 80, 90];
