import { useEffect, useRef, type MouseEvent, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, CircleNotch, CheckCircle, WarningCircle } from "@phosphor-icons/react";
import { STAGES, STAGE_INDEX, stageStatus, type StageId } from "../lib/stages";
import { useFState } from "../lib/store";
import { stageHref, takePendingFocus, useWorkflow } from "../lib/workflow";
import { cx } from "./ui";

/** Stations are links so they can be opened in a new tab; a plain click stays in the page. */
export function useStageLink() {
  const { go } = useWorkflow();
  return (id: StageId) => ({
    href: stageHref(id),
    onClick: (e: MouseEvent) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
      e.preventDefault();
      go(id);
    },
  });
}

interface StationProps {
  id: StageId;
  headline: string;
  /** two or three sentences: what this agent does */
  lede: ReactNode;
  /** "code decides" / "the model may" pair; omit the model line for agents with no model */
  code: string;
  model?: string;
  /** primary controls for this station */
  controls: ReactNode;
  /** the big right-hand instrument */
  instrument: ReactNode;
  /** optional full-width content under both columns */
  below?: ReactNode;
}

/** One station of the workflow: a test card on the left, the instrument on the right. */
export function Station({ id, headline, lede, code, model, controls, instrument, below }: StationProps) {
  const s = useFState();
  const link = useStageLink();
  const title = useRef<HTMLHeadingElement>(null);
  const def = STAGES[STAGE_INDEX[id]];
  const status = stageStatus(id, s);
  const i = STAGE_INDEX[id];
  const prev = i > 0 ? STAGES[i - 1] : null;
  const next = i < STAGES.length - 1 ? STAGES[i + 1] : null;

  useEffect(() => {
    if (takePendingFocus(id)) title.current?.focus({ preventScroll: true });
  }, [id]);

  return (
    <article aria-labelledby={`station-${id}-title`} className="grid gap-x-10 gap-y-8 lg:grid-cols-[minmax(320px,400px)_1fr]">
      <div className="flex min-w-0 flex-col gap-6">
        <header className="flex flex-col gap-3">
          <p className="flex items-center gap-3 text-[13px] text-fg-2">
            <span className="font-mono tabular text-fg">{String(def.n).padStart(2, "0")}</span>
            <span>{def.agentNames}</span>
            <StatusWord status={status} />
          </p>
          <h2 ref={title} id={`station-${id}-title`} tabIndex={-1} className="display scroll-mt-40 text-[clamp(1.9rem,3.2vw,2.6rem)] text-fg outline-none">
            {headline}
          </h2>
          <div className="max-w-[46ch] text-[15px] leading-relaxed text-fg-2">{lede}</div>
        </header>
        <dl className="grid gap-3 border-y border-line py-4 text-[13px] leading-snug">
          <div className="grid grid-cols-[6.5rem_1fr] gap-3">
            <dt className="label-condensed text-fg">Code decides</dt>
            <dd className="text-fg-2">{code}</dd>
          </div>
          <div className="grid grid-cols-[6.5rem_1fr] gap-3">
            <dt className="label-condensed text-model">The model may</dt>
            <dd className="text-fg-2">{model ?? "Nothing. No model is involved."}</dd>
          </div>
        </dl>
        <div className="flex flex-col gap-4">{controls}</div>
        <nav aria-label="Stations" className="mt-auto flex items-center justify-between gap-3 pt-2 text-sm">
          {prev ? (
            <a {...link(prev.id)} className="inline-flex min-h-11 items-center gap-2 text-fg-2 hover:text-fg">
              <ArrowLeft size={16} aria-hidden="true" /> {prev.name}
            </a>
          ) : <span />}
          {next && (
            <a {...link(next.id)} className="inline-flex min-h-11 items-center gap-2 text-fg hover:underline">
              Next: {next.name} <ArrowRight size={16} aria-hidden="true" />
            </a>
          )}
        </nav>
      </div>
      <div className="min-w-0">{instrument}</div>
      {below && <div className="min-w-0 lg:col-span-2">{below}</div>}
    </article>
  );
}

export function StatusWord({ status }: { status: ReturnType<typeof stageStatus> }) {
  if (status === "working") return <span className="inline-flex items-center gap-1.5 text-fg"><CircleNotch size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />working</span>;
  if (status === "done") return <span className="inline-flex items-center gap-1.5 text-pass"><CheckCircle size={14} weight="fill" aria-hidden="true" />done</span>;
  if (status === "error") return <span className="inline-flex items-center gap-1.5 text-fail"><WarningCircle size={14} weight="fill" aria-hidden="true" />failed</span>;
  return <span className="text-fg-2">not run yet</span>;
}

/** An instrument panel: hairline frame on the sheet surface, with a caption row. */
export function Instrument({ title, meta, children, className = "", grid = false }: { title: string; meta?: ReactNode; children: ReactNode; className?: string; grid?: boolean }) {
  return (
    <section aria-label={title} className={cx("rounded-[2px] border border-line bg-sheet", className)}>
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-line px-4 py-2.5">
        <h3 className="label-condensed text-[13px] text-fg">{title}</h3>
        {meta && <div className="min-w-0 break-all font-mono text-[11px] text-fg-2">{meta}</div>}
      </header>
      <div className={cx("p-4", grid && "mm-grid")}>{children}</div>
    </section>
  );
}

/** Inline result line under a control: what happened, in the interface's voice.
 *  `live={false}` for state that was already true when the station opened, so it is not re-announced. */
export function Notice({ tone = "neutral", live = true, children }: { tone?: "neutral" | "fail" | "pass" | "model"; live?: boolean; children: ReactNode }) {
  const ink = { neutral: "text-fg-2 border-line", fail: "text-fail border-fail/50", pass: "text-pass border-pass/50", model: "text-model border-model/50" }[tone];
  const role = !live ? undefined : tone === "fail" ? "alert" : "status";
  return <p role={role} className={cx("border-l-2 pl-3 text-[13px] leading-snug", ink)}>{children}</p>;
}
