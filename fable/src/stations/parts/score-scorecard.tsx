// The Scorecard: every tested service graded worst first; untested services last, with no score and
// no bar, never a flattering default. Mirrors faultline/agents/scorecard.py.
import { motion } from "motion/react";
import { CircleDashed } from "@phosphor-icons/react";
import type { ScoreRow, Scorecard } from "../../lib/types";
import { failureLabel, fmtS, grade as gradeOf } from "../../lib/format";
import { Instrument } from "../../components/Station";
import { cx } from "../../components/ui";
import { Rich } from "./score-marks";
import { GRADE_EDGES, pctSmart, tidy } from "./score-text";

function gradeInk(g: string): { text: string; bg: string; border: string } {
  if (g === "A" || g === "B") return { text: "text-pass", bg: "bg-pass", border: "border-pass/60" };
  if (g === "C") return { text: "text-fg", bg: "bg-fg", border: "border-fg/50" };
  return { text: "text-fail", bg: "bg-fail", border: "border-fail/60" };
}

function note(n: string): string {
  const m = n.match(/^tested:\s*(.*)$/);
  if (!m) return tidy(n);
  return `tested with ${m[1].split(/,\s*/).map((k) => failureLabel(k).toLowerCase()).join(", ")}`;
}

// Grade | Service | Score (bar + number) | Tests | Worst success | Slowest recovery
const COLS = "@2xl:grid-cols-[2.5rem_minmax(7rem,11rem)_minmax(9rem,1fr)_3rem_5.5rem_6.5rem] @2xl:items-center";

export function ScorecardPanel({ sc }: { sc: Scorecard | null }) {
  if (!sc) {
    return (
      <Instrument title="Scorecard">
        <p className="max-w-[52ch] text-[14px] leading-relaxed text-fg-2">
          No scorecard yet. Run analysis to grade every tested service from 0 to 100, A to F, worst first.
        </p>
      </Instrument>
    );
  }
  const scored = sc.ranking.filter((r) => r.score != null).sort((a, b) => (a.score ?? 0) - (b.score ?? 0));
  const untested = sc.ranking.filter((r) => r.score == null);
  return (
    <Instrument title="Scorecard" meta={sc.experiments_total != null ? `${sc.experiments_total} experiments scored` : undefined}>
      <div className="@container flex flex-col gap-5">
        <div className="flex flex-wrap items-end gap-x-8 gap-y-2">
          <p className="flex flex-col gap-1">
            <span className="text-[12px] text-fg-2">Overall</span>
            <span className="font-mono tabular leading-none">
              <span className="text-[64px] tracking-tight text-fg">{sc.overall_score != null ? sc.overall_score.toFixed(1) : "n/a"}</span>
              <span className="ml-2 text-[15px] text-fg-2">/ 100</span>
            </span>
          </p>
          <p className="max-w-[40ch] pb-1 text-[13px] leading-snug text-fg-2">
            The mean of the {scored.length} tested {scored.length === 1 ? "service" : "services"}.
            {untested.length > 0 && ` The ${untested.length} untested ones are left out, not counted as passing.`}
          </p>
        </div>
        <p className="max-w-[68ch] text-[14px] leading-relaxed text-fg"><Rich text={sc.summary} /></p>

        {scored.length > 0 && (
          <div role="table" aria-label="Tested services, worst first" className="flex flex-col">
            <div role="rowgroup" className="sr-only @2xl:not-sr-only">
              <div role="row" className={cx("grid gap-x-4 border-b border-line pb-2 text-[12px] text-fg-2", COLS)}>
                <span role="columnheader">Grade</span>
                <span role="columnheader">Service</span>
                <span role="columnheader">Score out of 100</span>
                <span role="columnheader" className="text-right">Tests</span>
                <span role="columnheader" className="text-right">Worst success</span>
                <span role="columnheader" className="text-right">Slowest recovery</span>
              </div>
            </div>
            <div role="rowgroup" className="divide-y divide-line">
              {scored.map((r, i) => (
                <ScoredRow key={r.service} r={r} i={i} />
              ))}
            </div>
          </div>
        )}

        {untested.length > 0 && (
          <div className="flex flex-col gap-2 border-t border-line pt-4">
            <p className="text-[13px] text-fg-2">Untested. No experiment has targeted these yet, so they get no score and no grade.</p>
            <ul aria-label="Untested services" className="flex flex-wrap gap-x-6 gap-y-1">
              {untested.map((r) => (
                <li key={r.service} className="inline-flex min-h-9 items-center gap-2.5">
                  <span aria-hidden="true" className="grid size-7 place-items-center rounded-[2px] border border-dashed border-line text-fg-2">
                    <CircleDashed size={13} />
                  </span>
                  <span className="font-mono text-[13px] text-fg">{r.service}</span>
                  <span className="text-[12px] text-fg-2">untested</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Instrument>
  );
}

function ScoredRow({ r, i }: { r: ScoreRow; i: number }) {
  const score = r.score ?? 0;
  const g = /^[A-F]$/.test(r.grade) ? r.grade : gradeOf(score);
  const ink = gradeInk(g);
  const notes = r.notes.filter((n) => n !== "not tested");
  return (
    <div role="row" className={cx("grid grid-cols-[2.5rem_minmax(0,1fr)] gap-x-4 gap-y-2 py-3", COLS)}>
      <span role="cell" className="row-span-3 @2xl:row-span-1">
        <span className={cx("grid size-10 place-items-center rounded-[2px] border font-mono text-[20px] leading-none", ink.text, ink.border)}>
          <span className="sr-only">grade </span>
          {g}
        </span>
      </span>
      <span role="cell" className="min-w-0">
        <span className="block font-mono text-[14px] text-fg">{r.service}</span>
        {notes.length > 0 && <span className="block text-[12px] leading-snug text-fg-2">{notes.map(note).join("; ")}</span>}
      </span>
      <span role="cell" className="flex items-center gap-3">
        <span aria-hidden="true" className="relative h-2 flex-1">
          <span className="absolute inset-x-0 top-1/2 h-px bg-line" />
          {GRADE_EDGES.map((e) => (
            <span key={e} className="absolute top-[-3px] h-[14px] w-px bg-line" style={{ left: `${e}%` }} />
          ))}
          <motion.span
            className={cx("absolute left-0 top-0 h-2 origin-left", ink.bg)}
            style={{ width: `${Math.max(0, Math.min(100, score))}%` }}
            initial={{ scaleX: 0 }}
            animate={{ scaleX: 1 }}
            transition={{ duration: 0.5, delay: 0.06 * i, ease: [0.16, 1, 0.3, 1] }}
          />
        </span>
        <span className={cx("w-12 text-right font-mono tabular text-[16px]", ink.text)}>{score.toFixed(1)}</span>
      </span>
      <span className="col-start-2 flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-fg-2 @2xl:contents">
        <span role="cell" className="@2xl:text-right">
          <span className="@2xl:sr-only">tests </span>
          <span className="font-mono tabular text-[13px] text-fg">{r.experiments}</span>
        </span>
        <span role="cell" className="@2xl:text-right">
          <span className="@2xl:sr-only">worst success </span>
          <span className="font-mono tabular text-[13px] text-fg">{pctSmart(r.worst_success_rate)}</span>
        </span>
        <span role="cell" className="@2xl:text-right">
          <span className="@2xl:sr-only">slowest recovery </span>
          <span className="font-mono tabular text-[13px] text-fg">{fmtS(r.slowest_recovery_s, 1)}</span>
        </span>
      </span>
    </div>
  );
}
