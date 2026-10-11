// F1: every non-info finding across all experiments in one ranked list, with each experiment's first fix.
// Order comes from rankFindings (score-rollup), the same order as the Day-2 docs/FINDINGS.md. One row per finding,
// masked health check findings included, so the numbering always matches that order.
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { DownloadSimple } from "@phosphor-icons/react";
import type { ExperimentSummary, State } from "../../lib/types";
import { Instrument } from "../../components/Station";
import { AiMark, Button, cx } from "../../components/ui";
import { failureLabel } from "../../lib/format";
import { announce } from "../../lib/a11y";
import { plural, rankFindings, type FindingRow } from "./score-rollup";
import { downloadFindingsReport } from "./score-report";
import { RiskMark, SeverityMark } from "./score-marks";
import { stripLlmTag, tidy } from "./score-text";

const COLS = "md:grid-cols-[2.5rem_5.5rem_minmax(0,1.5fr)_minmax(0,1fr)_7.5rem_minmax(0,1.3fr)]";

/** F1: every non-info finding across all experiments, worst first, with its first fix and the report download. */
export function AllFindings({ experiments, state, onOpen }: { experiments: ExperimentSummary[]; state: State | null; onOpen: (id: string) => void }) {
  const rows = useMemo(() => rankFindings(experiments), [experiments]);
  if (experiments.length === 0) return null;

  const contributing = new Set(rows.map((r) => r.exp.experiment_id)).size;
  const meta = rows.length
    ? `${plural(rows.length, "finding")} across ${plural(contributing, "experiment")}, info left out`
    : undefined;

  return (
    <Instrument title="All findings, worst first" meta={meta}>
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
          <p className="max-w-[60ch] text-[13px] leading-snug text-fg-2">
            {rows.length
              ? "Ranked by finding severity, then the experiment's risk, then the order they ran. Select a row to open its experiment in the log."
              : "No findings above info yet."}
          </p>
          <DownloadReport state={state} />
        </div>

        {rows.length > 0 && (
          <div>
            <div aria-hidden="true" className={cx("hidden gap-x-4 border-b border-line pb-2 text-[12px] text-fg-2 md:grid", COLS)}>
              <span className="label-condensed">Rank</span>
              <span className="label-condensed">Severity</span>
              <span className="label-condensed">Finding</span>
              <span className="label-condensed">Where</span>
              <span className="label-condensed">Risk</span>
              <span className="label-condensed">First fix</span>
            </div>
            <ol className="divide-y divide-line">
              {rows.map((r) => (
                <Row key={`${r.exp.experiment_id}-${r.idx}`} row={r} onOpen={onOpen} />
              ))}
            </ol>
          </div>
        )}
      </div>
    </Instrument>
  );
}

function DownloadReport({ state }: { state: State | null }) {
  const save = () => {
    if (!state) return;
    try {
      downloadFindingsReport(state);
      announce("Findings report downloaded.");
    } catch {
      announce("The findings report could not be saved. Try again.");
    }
  };
  return (
    <Button variant="ghost" size="sm" onClick={save} disabled={!state}>
      <DownloadSimple size={16} aria-hidden="true" />
      Download findings report
    </Button>
  );
}

function Row({ row, onOpen }: { row: FindingRow; onOpen: (id: string) => void }) {
  const { rank, finding, exp } = row;
  const id = exp.experiment_id;
  const title = tidy(finding.title);
  const label = `${title}: ${finding.severity}, ${exp.target_service}. Open ${id} in the log`;

  return (
    <li
      className={cx(
        "relative -mx-4 grid grid-cols-[2rem_minmax(0,1fr)] gap-x-3 gap-y-2 px-4 py-3 hover:bg-bg/40 md:gap-x-4 md:gap-y-0",
        COLS,
      )}
    >
      <span className="row-span-5 pt-px font-mono text-[14px] tabular text-fg md:row-span-1">{rank}</span>

      <span className="col-start-2 text-[13px] md:col-start-auto md:pt-px">
        <SeverityMark severity={finding.severity} />
      </span>

      <div className="col-start-2 min-w-0 md:col-start-auto">
        <p className="text-[14px] leading-snug text-fg">
          <button
            type="button"
            aria-label={label}
            onClick={() => onOpen(id)}
            className={cx(
              "text-left font-medium underline decoration-line underline-offset-4 hover:decoration-fg",
              "after:absolute after:inset-0 after:content-['']",
              "focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-[-2px] focus-visible:after:outline-fg focus-visible:after:outline-solid",
            )}
          >
            {title}
          </button>{" "}
          <span className="font-mono text-[11px] break-words text-fg-2">{finding.code}</span>
        </p>
        {finding.evidence && <Evidence text={tidy(finding.evidence)} />}
      </div>

      <p className="col-start-2 min-w-0 text-[13px] leading-snug text-fg-2 md:col-start-auto">
        <span className="sr-only">Where: </span>
        <span className="font-mono tabular text-fg">{exp.target_service}</span>
        <span>, {failureLabel(exp.failure_type)}</span>
        <span className="block font-mono text-[12px] tabular">{id}</span>
      </p>

      <span className="col-start-2 text-[13px] md:col-start-auto md:pt-px">
        <RiskMark risk={exp.risk} />
      </span>

      <div className="col-start-2 min-w-0 text-[13px] leading-snug md:col-start-auto">
        <span className="label-condensed block text-[12px] text-fg-2 md:sr-only">First fix</span>
        <FirstFix row={row} />
      </div>
    </li>
  );
}

function FirstFix({ row }: { row: FindingRow }) {
  const { fix, fixByModel, exp } = row;
  if (!fix) {
    return <p className="text-fg-2">{exp.fix ? "No rule matched this experiment." : "No fix yet. Run analysis."}</p>;
  }
  const text = tidy(stripLlmTag(fix.title).text);
  if (!fixByModel) return <p className="text-fg">{text}</p>;
  return (
    <p className="text-model">
      <span className="relative z-10 mr-1.5 inline-flex align-[1px]"><AiMark /></span>
      {text}
      <span className="sr-only"> (model suggestion, unverified)</span>
    </p>
  );
}

/** Evidence quoted from the report, clamped to two lines; the disclosure only appears when there is more to show. */
function Evidence({ text }: { text: string }) {
  const id = useId();
  const ref = useRef<HTMLParagraphElement>(null);
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || open || typeof ResizeObserver === "undefined") return;
    // the observer fires once on observe, then on every width change, so the clamp is measured at the real width
    const ro = new ResizeObserver(() => setOverflows(el.scrollHeight > el.clientHeight + 1));
    ro.observe(el);
    return () => ro.disconnect();
  }, [open, text]);

  return (
    <div className="mt-1">
      <p
        ref={ref}
        id={id}
        className={cx("relative z-10 break-words font-mono text-[12px] leading-relaxed text-fg-2", !open && "line-clamp-2")}
      >
        {text}
      </p>
      {(overflows || open) && (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen((v) => !v)}
          className="relative z-10 -my-2 inline-flex min-h-11 items-center text-[12px] text-fg underline decoration-line underline-offset-4 hover:decoration-fg"
        >
          {open ? "Show less" : "Show full evidence"}
        </button>
      )}
    </div>
  );
}
