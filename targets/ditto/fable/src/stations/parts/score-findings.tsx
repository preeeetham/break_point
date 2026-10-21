// F1: every finding across every experiment, info included, in one sortable table. Worst first by default (the same
// rule as rankFindings, which keeps the "above info" order for Proof and the report). A real table when the panel is
// wide, a stacked list with a Sort by control when it is narrow. Every row opens its finding in the experiment log.
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowsDownUp, ArrowUp, CaretDown, DownloadSimple } from "@phosphor-icons/react";
import type { ExperimentSummary, State } from "../../lib/types";
import { Instrument } from "../../components/Station";
import { AiMark, Button, cx } from "../../components/ui";
import { failureLabel } from "../../lib/format";
import { announce } from "../../lib/a11y";
import { plural } from "./score-rollup";
import { downloadFindingsReport } from "./score-report";
import { SeverityMark } from "./score-marks";
import { stripLlmTag, tidy, when } from "./score-text";
import { findingHelp } from "./finding-names";
import { buildRows, dirWords, NATURAL, SORT_LABEL, sortRows, type SortDir, type SortKey, type TableRow } from "./findings-sort";

type OnOpen = (id: string, findingIdx?: number) => void;
const SORT_KEYS: SortKey[] = ["severity", "name", "service", "experiment", "when"];
/** The column in a sentence: "sorted by experiment start", not "sorted by when". */
const phrase = (k: SortKey) => (k === "when" ? "experiment start" : SORT_LABEL[k].toLowerCase());

/** F1: every finding across all experiments, sortable, worst first by default, with each experiment's top fix and the report download. */
export function AllFindings({ experiments, state, onOpen }: { experiments: ExperimentSummary[]; state: State | null; onOpen: OnOpen }) {
  const all = useMemo(() => buildRows(experiments), [experiments]);
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: "severity", dir: "desc" });
  const rows = useMemo(() => sortRows(all, sort.key, sort.dir), [all, sort]);
  if (experiments.length === 0) return null;

  const apply = (key: SortKey, dir: SortDir) => {
    setSort({ key, dir });
    announce(`Findings sorted by ${phrase(key)}, ${dirWords(key, dir)}.`);
  };
  // the active column flips; another column starts in its natural direction
  const pick = (key: SortKey) => apply(key, key === sort.key ? (sort.dir === "asc" ? "desc" : "asc") : NATURAL[key]);

  const contributing = new Set(all.map((r) => r.exp.experiment_id)).size;
  const meta = all.length ? `${plural(all.length, "finding")} across ${plural(contributing, "experiment")}` : undefined;
  const order = `${phrase(sort.key)}, ${dirWords(sort.key, sort.dir)}`;

  return (
    <Instrument title="All findings" meta={meta}>
      <div className="@container flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
          <p className="max-w-[64ch] text-[13px] leading-snug text-fg-2">
            {all.length
              ? "Worst first by default: finding severity, then the experiment's risk, then the order the experiments ran. Select a row to open that finding in its experiment."
              : "No findings recorded yet. Run analysis after an experiment finishes."}
          </p>
          <DownloadReport state={state} />
        </div>

        {all.length > 0 && (
          <>
            {/* wide: a real table */}
            <div className="hidden @4xl:block">
              <table className="w-full table-fixed border-collapse text-[13px]">
                <caption className="sr-only">
                  All findings, sorted by {order}. Each row opens that finding in its experiment.
                </caption>
                <colgroup>
                  <col className="w-[6rem]" />
                  <col />
                  <col className="w-[8.5rem]" />
                  <col className="w-[8.5rem]" />
                  <col className="w-[8rem]" />
                  <col className="w-[21%]" />
                </colgroup>
                <thead>
                  <tr className="border-b border-line">
                    {SORT_KEYS.map((k) => (
                      <SortHeader key={k} k={k} sort={sort} onPick={pick} />
                    ))}
                    <th scope="col" className="py-1 pl-3 text-left align-top text-[12px] font-normal text-fg-2">
                      <span className="label-condensed flex min-h-11 items-center">Top fix</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <TableLine key={`${r.exp.experiment_id}-${r.idx}`} row={r} onOpen={onOpen} />
                  ))}
                </tbody>
              </table>
            </div>

            {/* narrow: the same rows, stacked, with a compact sort control */}
            <div className="flex flex-col gap-3 @4xl:hidden">
              <SortControl sort={sort} onKey={(k) => apply(k, NATURAL[k])} onFlip={() => apply(sort.key, sort.dir === "asc" ? "desc" : "asc")} />
              <ol aria-label={`All findings, sorted by ${order}`} className="flex flex-col divide-y divide-line border-y border-line">
                {rows.map((r) => (
                  <ListLine key={`${r.exp.experiment_id}-${r.idx}`} row={r} onOpen={onOpen} />
                ))}
              </ol>
            </div>
          </>
        )}
      </div>
    </Instrument>
  );
}

function SortHeader({ k, sort, onPick }: { k: SortKey; sort: { key: SortKey; dir: SortDir }; onPick: (k: SortKey) => void }) {
  const active = sort.key === k;
  const Icon = !active ? ArrowsDownUp : sort.dir === "desc" ? ArrowDown : ArrowUp;
  return (
    <th
      scope="col"
      aria-sort={active ? (sort.dir === "desc" ? "descending" : "ascending") : "none"}
      className="px-3 py-1 text-left align-top text-[12px] font-normal first:pl-0"
    >
      <button
        type="button"
        onClick={() => onPick(k)}
        className={cx(
          "label-condensed -mx-1.5 inline-flex min-h-11 items-center gap-1.5 rounded-[2px] px-1.5 text-left hover:bg-bg",
          active ? "text-fg" : "text-fg-2",
        )}
      >
        {SORT_LABEL[k]}
        <Icon size={13} aria-hidden="true" className={active ? "text-fg" : "text-fg-2"} />
      </button>
      {k === "when" && <span className="block text-[11px] leading-tight text-fg-2">experiment start</span>}
    </th>
  );
}

function SortControl({ sort, onKey, onFlip }: { sort: { key: SortKey; dir: SortDir }; onKey: (k: SortKey) => void; onFlip: () => void }) {
  const id = useId();
  const Icon = sort.dir === "desc" ? ArrowDown : ArrowUp;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <label htmlFor={id} className="label-condensed text-[13px] text-fg-2">Sort by</label>
      <span className="relative inline-flex">
        <select
          id={id}
          value={sort.key}
          onChange={(e) => onKey(e.target.value as SortKey)}
          className="min-h-11 appearance-none rounded-[2px] border border-line bg-sheet py-2 pr-9 pl-3 text-[14px] text-fg hover:bg-bg"
        >
          {SORT_KEYS.map((k) => (
            <option key={k} value={k}>{k === "when" ? "When (experiment start)" : SORT_LABEL[k]}</option>
          ))}
        </select>
        <CaretDown size={14} aria-hidden="true" className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-fg-2" />
      </span>
      <button
        type="button"
        onClick={onFlip}
        className="inline-flex min-h-11 items-center gap-2 rounded-[2px] border border-line px-3 text-[14px] text-fg hover:bg-bg"
      >
        <Icon size={14} aria-hidden="true" />
        {dirWords(sort.key, sort.dir).replace(/^./, (c) => c.toUpperCase())}
        <span className="sr-only">, select to reverse the order</span>
      </button>
    </div>
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

// ---------------------------------------------------------------- one row, two layouts

function TableLine({ row, onOpen }: { row: TableRow; onOpen: OnOpen }) {
  const { finding, exp } = row;
  return (
    <tr className="relative border-b border-line last:border-b-0 hover:bg-bg/40 [&>td]:align-top">
      <td className="py-3 pr-3 text-[13px]">
        <SeverityMark severity={finding.severity} />
      </td>
      <td className="min-w-0 px-3 py-3">
        <FindingCell row={row} onOpen={onOpen} />
      </td>
      <td className="px-3 py-3">
        <span className="font-mono text-[13px] tabular break-all text-fg">{exp.target_service}</span>
      </td>
      <td className="px-3 py-3">
        <ExperimentCell exp={exp} />
      </td>
      <td className="px-3 py-3">
        <When iso={exp.started_at} />
      </td>
      <td className="py-3 pl-3 text-[13px] leading-snug">
        <TopFix row={row} />
      </td>
    </tr>
  );
}

function ListLine({ row, onOpen }: { row: TableRow; onOpen: OnOpen }) {
  const { finding, exp } = row;
  return (
    <li className="relative flex flex-col gap-2 py-3 hover:bg-bg/40">
      <span className="text-[13px]">
        <SeverityMark severity={finding.severity} />
      </span>
      <FindingCell row={row} onOpen={onOpen} />
      <dl className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-[13px] leading-snug">
        <dt className="label-condensed text-[12px] text-fg-2">Service</dt>
        <dd className="min-w-0 font-mono tabular break-all text-fg">{exp.target_service}</dd>
        <dt className="label-condensed text-[12px] text-fg-2">Experiment</dt>
        <dd className="min-w-0"><ExperimentCell exp={exp} /></dd>
        <dt className="label-condensed text-[12px] text-fg-2">Experiment start</dt>
        <dd className="min-w-0"><When iso={exp.started_at} /></dd>
        <dt className="label-condensed text-[12px] text-fg-2">Top fix</dt>
        <dd className="min-w-0"><TopFix row={row} /></dd>
      </dl>
    </li>
  );
}

/** The plain-English name is the row's one control; its ::after stretches over the whole row. */
function FindingCell({ row, onOpen }: { row: TableRow; onOpen: OnOpen }) {
  const helpId = useId();
  const { finding, exp, name, idx } = row;
  const id = exp.experiment_id;
  const help = findingHelp(finding);
  const detail = tidy(finding.title);
  const label = `${name}: ${finding.severity}, ${exp.target_service}, ${id}. Open this finding in the experiment`;

  return (
    <div className="min-w-0">
      <button
        type="button"
        aria-label={label}
        aria-describedby={help ? helpId : undefined}
        onClick={() => onOpen(id, idx)}
        className={cx(
          "text-left text-[14px] leading-snug font-medium text-fg underline decoration-line underline-offset-4 hover:decoration-fg",
          "after:absolute after:inset-0 after:content-['']",
          "focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-[-2px] focus-visible:after:outline-fg focus-visible:after:outline-solid",
        )}
      >
        {name}
      </button>
      {help && <span id={helpId} hidden>{help}</span>}
      {detail && detail !== name && <p className="mt-1 text-[13px] leading-snug break-words text-fg-2">{detail}</p>}
      <p className="mt-0.5 font-mono text-[11px] break-words text-fg-2">{finding.code}</p>
      {finding.evidence && <Evidence text={tidy(finding.evidence)} />}
    </div>
  );
}

function ExperimentCell({ exp }: { exp: ExperimentSummary }) {
  return (
    <span className="flex flex-col text-[13px] leading-snug">
      <span className="font-mono tabular text-fg">{exp.experiment_id}</span>
      <span className="text-fg-2">{failureLabel(exp.failure_type)}</span>
    </span>
  );
}

function When({ iso }: { iso: string }) {
  const text = when(iso);
  if (!text) return <span className="text-[13px] text-fg-2">not recorded</span>;
  return (
    <time dateTime={iso} className="font-mono text-[13px] tabular whitespace-nowrap text-fg">
      {text}
    </time>
  );
}

function TopFix({ row }: { row: TableRow }) {
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
