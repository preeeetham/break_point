import { useEffect, useRef } from "react";
import { motion, useReducedMotion } from "motion/react";
import type { EventItem, EventLevel } from "../../lib/types";
import { timecode } from "../../lib/format";
import { cx } from "../../components/ui";
import { prose } from "./load-util";

const TAG: Record<EventLevel, { word: string; ink: string }> = {
  info: { word: "", ink: "text-fg-2" },
  success: { word: "ok", ink: "text-pass" },
  warn: { word: "warn", ink: "text-fg" },
  error: { word: "fail", ink: "text-fail" },
  chaos: { word: "fault", ink: "text-fg" },
  ai: { word: "AI", ink: "text-model" },
};

interface TranscriptProps {
  lines: EventItem[];
  /** the zero point for the t+ column */
  t0: number | null;
  running: boolean;
  /** the command line this run is equivalent to, echoed first */
  command?: string | null;
  empty: string;
}

/** The agent's own log lines, newest at the bottom, in the terminal's order. */
export function Transcript({ lines, t0, running, command, empty }: TranscriptProps) {
  const box = useRef<HTMLDivElement>(null);
  const reduce = useReducedMotion();

  useEffect(() => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length, running]);

  if (!lines.length && !running && !command) {
    return <p className="py-6 text-[14px] leading-relaxed text-fg-2">{empty}</p>;
  }
  const zero = t0 ?? lines[0]?.ts ?? 0;
  return (
    <div ref={box} className="max-h-[300px] min-h-[140px] overflow-y-auto overscroll-contain">
      {command && (
        <p className="mb-1.5 break-all font-mono text-[12px] leading-5 text-fg">
          <span aria-hidden="true" className="mr-2 text-fg-2">$</span>
          {command}
        </p>
      )}
      <ol role="log" aria-label="Onboarding transcript" aria-live="polite" className="font-mono text-[12px] leading-5">
        {lines.map((e) => {
          const tag = TAG[e.level] ?? TAG.info;
          return (
            <motion.li
              key={e.seq}
              initial={reduce ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.25 }}
              className="grid grid-cols-[4.25rem_2.25rem_minmax(0,1fr)] gap-x-2 py-[3px]"
            >
              <span className="tabular text-fg-2">{timecode(Math.max(0, e.ts - zero))}</span>
              <span className={cx("tabular", tag.ink)}>{tag.word}</span>
              <span className={cx("min-w-0 break-words", e.level === "info" ? "text-fg" : tag.ink)}>{prose(e.text)}</span>
            </motion.li>
          );
        })}
        {running && (
          <li className="grid grid-cols-[4.25rem_2.25rem_minmax(0,1fr)] gap-x-2 py-[3px]" aria-hidden="true">
            <span />
            <span />
            <span className="inline-block h-4 w-2 animate-pulse bg-fg-2 motion-reduce:animate-none" />
          </li>
        )}
      </ol>
    </div>
  );
}
