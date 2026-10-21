import type { ReactNode } from "react";
import { CheckCircle, CircleDashed, WarningOctagon } from "@phosphor-icons/react";
import type { PredictedWeakness, WeaknessOutcome } from "../../lib/types";
import { cx } from "../../components/ui";
import { prose } from "./load-util";

export const sentence = (s: string) => {
  const p = prose(s).trim();
  return p ? `${p[0].toUpperCase()}${p.slice(1)}${/[.!?]$/.test(p) ? "" : "."}` : "";
};

export const predictedLabel = (w: PredictedWeakness) => (w.label || "predicted, unconfirmed").replace(/^\[|\]$/g, "");

/** DOM ids for one prediction row: its expandable detail and the button that toggles it. */
export const detailDomId = (weaknessId: string) => `wk-${weaknessId.replace(/[^a-z0-9]/gi, "-")}`;
export const toggleDomId = (weaknessId: string) => `${detailDomId(weaknessId)}-toggle`;

export type OutcomeState = WeaknessOutcome["state"];

/**
 * The one mapping from a reconciled prediction to its ink, glyph and word, shared by Predict and Reconcile.
 * Confirmed means an experiment proved the weakness real, so it reads as a failure; cleared reads as a pass;
 * untested stays neutral. `stroke` is the same ink as a CSS color for SVG lines.
 */
export function outcomeMark(state: OutcomeState | null) {
  const m = outcomeInk(state);
  return { ...m, label: `${m.word[0].toUpperCase()}${m.word.slice(1)}` };
}

function outcomeInk(state: OutcomeState | null) {
  if (state === "confirmed") {
    return { word: "confirmed", ink: "text-fail", border: "border-fail", stroke: "var(--color-fail)", glyph: (size = 14) => <WarningOctagon size={size} weight="fill" aria-hidden="true" className="shrink-0" /> };
  }
  if (state === "cleared") {
    return { word: "cleared", ink: "text-pass", border: "border-pass", stroke: "var(--color-pass)", glyph: (size = 14) => <CheckCircle size={size} weight="fill" aria-hidden="true" className="shrink-0" /> };
  }
  return {
    word: state === "untested" ? "untested" : "not reconciled",
    ink: "text-fg-2",
    border: "border-line",
    stroke: "var(--color-fg-2)",
    glyph: (size = 14) => <CircleDashed size={size} aria-hidden="true" className="shrink-0" />,
  };
}

/** Glyph plus word, always. `suffix` adds context after the word in the same ink. */
export function OutcomeMark({ state, size = 14, suffix, className }: { state: OutcomeState | null; size?: number; suffix?: ReactNode; className?: string }) {
  const m = outcomeMark(state);
  return (
    <span className={cx("inline-flex items-center gap-1.5 whitespace-nowrap", m.ink, className)}>
      {m.glyph(size)}
      {m.word}
      {suffix}
    </span>
  );
}
