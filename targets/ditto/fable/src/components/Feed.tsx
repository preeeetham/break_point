import { useMemo, useState } from "react";
import type { EventItem, EventLevel } from "../lib/types";
import { clean, clock } from "../lib/format";
import { cx } from "./ui";

const LEVEL_INK: Record<EventLevel, string> = {
  info: "text-fg-2",
  success: "text-pass",
  warn: "text-fg",
  error: "text-fail",
  chaos: "text-fg",
  ai: "text-model",
};

const LEVEL_WORD: Record<EventLevel, string> = {
  info: "", success: "ok", warn: "warn", error: "fail", chaos: "fault", ai: "AI",
};

interface FeedProps {
  events: EventItem[];
  /** only show these sources; all when omitted */
  sources?: string[];
  limit?: number;
  newestFirst?: boolean;
  empty?: string;
  className?: string;
  ariaLabel?: string;
}

export function Feed({ events, sources, limit = 120, newestFirst = true, empty = "Quiet. Actions you take here will narrate themselves.", className = "", ariaLabel = "Agent activity" }: FeedProps) {
  const rows = useMemo(() => {
    const f = sources ? events.filter((e) => sources.includes(e.source)) : events;
    const cut = f.slice(-limit);
    return newestFirst ? [...cut].reverse() : cut;
  }, [events, sources, limit, newestFirst]);

  if (!rows.length) return <p className={cx("text-sm text-fg-2", className)}>{empty}</p>;
  return (
    <ol aria-label={ariaLabel} className={cx("divide-y divide-line font-mono text-[12px] leading-5", className)}>
      {rows.map((e) => (
        <li key={e.seq} className="grid grid-cols-[4.6rem_5.8rem_1fr] gap-2 py-1.5">
          <time dateTime={new Date(e.ts * 1000).toISOString()} className="tabular text-fg-2">{clock(e.ts)}</time>
          <span className={cx("truncate", e.level === "chaos" ? "text-fg" : "text-fg-2")}>
            {e.level === "chaos" && <span aria-hidden="true" className="mr-1 inline-block size-2 bg-marker align-middle" />}
            {e.source}
          </span>
          <span className={cx("min-w-0 break-words", LEVEL_INK[e.level])}>
            {LEVEL_WORD[e.level] && <span className="sr-only">{LEVEL_WORD[e.level]}: </span>}
            {clean(e.text)}
          </span>
        </li>
      ))}
    </ol>
  );
}

/** Collapsible narrator drawer used in the camera act. */
export function FeedDrawer({ events }: { events: EventItem[] }) {
  const [open, setOpen] = useState(false);
  const last = events[events.length - 1];
  return (
    <section aria-label="Narrator feed" className="border-t border-line bg-bg">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex min-h-11 w-full items-center gap-3 px-4 text-left text-sm md:px-8"
      >
        <span className="label-condensed text-fg">Narrator</span>
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-fg-2">{last ? `${clock(last.ts)}  ${last.source}  ${clean(last.text)}` : "quiet"}</span>
        <span className="text-fg-2">{open ? "Hide" : "Show all"}</span>
      </button>
      {open && (
        <div className="max-h-[40svh] overflow-y-auto px-4 pb-4 md:px-8">
          <Feed events={events} />
        </div>
      )}
    </section>
  );
}
