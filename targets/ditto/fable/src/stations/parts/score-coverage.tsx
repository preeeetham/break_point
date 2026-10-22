// F2: what was tested. Summary readings, then services by failure types: every cell links the newest run
// to the experiment log. Untested services keep their row, labelled the way the Scorecard labels them.
import { useMemo, type ReactNode } from "react";
import { CircleDashed } from "@phosphor-icons/react";
import type { ExperimentSummary, Scorecard, Service } from "../../lib/types";
import { FAILURE_LABEL, failureLabel } from "../../lib/format";
import { useCatalog } from "../../lib/store";
import { Instrument } from "../../components/Station";
import { Readout } from "../../components/ui";
import { HeldMark, StatusMark } from "./score-marks";
import { didRun, isAutoStop, outcomeOf, plural, type Outcome } from "./score-rollup";
import { newestFirst } from "./score-text";

function OutcomeMark({ outcome }: { outcome: Outcome }) {
  if (outcome === "aborted" || outcome === "skipped" || outcome === "failed") return <StatusMark status={outcome} />;
  return <HeldMark held={outcome === "held" ? true : outcome === "violated" ? false : null} />;
}

interface Row {
  service: string;
  /** runs per failure type, newest first */
  cells: Map<string, ExperimentSummary[]>;
}

function cellLabel(service: string, failure: string, runs: ExperimentSummary[]): string {
  const more = runs.length > 1 ? `, newest of ${runs.length} runs` : "";
  const f = failureLabel(failure);
  // lowercase the first word for mid sentence, but keep acronyms such as CPU
  const mid = /^[A-Z][a-z]/.test(f) ? f[0].toLowerCase() + f.slice(1) : f;
  return `${service}, ${mid}: ${outcomeOf(runs[0])}${more}. Open in the log`;
}

function Small({ children }: { children: ReactNode }) {
  return <span className="text-sm text-fg-2">{children}</span>;
}

export function Coverage({ experiments, scorecard, services, onOpen }: { experiments: ExperimentSummary[]; scorecard: Scorecard | null; services: Record<string, Service> | null; onOpen: (id: string) => void }) {
  const catalog = useCatalog();

  const model = useMemo(() => {
    // newest first here, so the order never depends on what the caller passed
    const list = newestFirst(experiments);
    // columns: failure types actually run, in catalog order; anything the catalog does not list goes last
    const order = catalog?.failures?.length ? catalog.failures.map((f) => f.key) : Object.keys(FAILURE_LABEL);
    const ran = new Set(list.map((e) => e.failure_type));
    const cols = [...order.filter((k) => ran.has(k)), ...[...ran].filter((k) => !order.includes(k)).sort()];

    // rows: Scorecard order, then any target the Scorecard does not list, in the order it was first run
    const targetsByFirstRun: string[] = [];
    for (const e of [...list].reverse()) if (!targetsByFirstRun.includes(e.target_service)) targetsByFirstRun.push(e.target_service);
    const ranked = scorecard?.ranking.map((r) => r.service) ?? [];
    const names = [...ranked, ...targetsByFirstRun.filter((s) => !ranked.includes(s))];
    const rows: Row[] = names.map((service) => {
      const cells = new Map<string, ExperimentSummary[]>();
      for (const e of list) {
        if (e.target_service !== service) continue;
        const list = cells.get(e.failure_type) ?? [];
        list.push(e);
        cells.set(e.failure_type, list);
      }
      return { service, cells };
    });

    // scope: the services the map marks as testable, the same rule the Map station and the rig use
    const scope = services ? Object.keys(services).filter((k) => services[k].chaos_relevant) : null;
    const total = scope?.length || scorecard?.ranking.length || null;
    const tested = scope ? targetsByFirstRun.filter((t) => scope.includes(t)).length : targetsByFirstRun.length;
    const planned = { llm: 0, operator: 0, deterministic: 0 };
    for (const e of list) if (e.planned_by in planned) planned[e.planned_by] += 1;

    // skipped experiments never injected anything: they do not count as run
    const runs = list.filter(didRun);
    return {
      cols,
      rows,
      tested,
      total,
      planned,
      runCount: runs.length,
      runTypes: new Set(runs.map((e) => e.failure_type)).size,
      autoStops: list.filter(isAutoStop).length,
    };
  }, [experiments, scorecard, services, catalog]);

  if (experiments.length === 0) return null;
  const { cols, rows, tested, total, planned, runCount, runTypes, autoStops } = model;
  // one table column per failure type: past six, the cells need a wider container before the table fits
  const fewCols = cols.length <= 6;

  return (
    <Instrument title="Coverage" meta={`${plural(rows.length, "service")} by ${plural(cols.length, "failure type")}`}>
      <div className="@container flex flex-col gap-6">
        <div className="grid grid-cols-2 gap-x-6 gap-y-5 @xl:grid-cols-3 @4xl:grid-cols-5">
          <Readout label="Experiments run" value={runCount} />
          <Readout label="Distinct failure types" value={runTypes} />
          <Readout
            label="Services tested"
            value={total != null ? <>{tested} <Small>of</Small> {total}</> : tested}
          />
          <Readout label="Automatic stops" value={autoStops} />
          <Readout
            label="Planned by"
            className="col-span-2 @xl:col-span-1"
            value={
              <span className="inline-flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span>{planned.llm} <Small>model</Small></span>
                <span>{planned.operator} <Small>operator</Small></span>
                <span>{planned.deterministic} <Small>code</Small></span>
              </span>
            }
          />
        </div>

        {/* wide: a real table */}
        <div className={fewCols ? "hidden @3xl:block" : "hidden @6xl:block"}>
          <table className="w-full table-fixed border-collapse text-[13px]">
            <caption className="sr-only">
              Coverage: services by the failure types run against them. Each tested cell opens its newest experiment in the log.
            </caption>
            <colgroup>
              <col className="w-[10rem]" />
              {cols.map((k) => <col key={k} />)}
            </colgroup>
            <thead>
              <tr className="border-b border-line">
                <th scope="col" className="py-2 pr-3 text-left align-bottom text-[12px] font-normal text-fg-2">Service</th>
                {cols.map((k) => (
                  <th key={k} scope="col" className="px-2 py-2 text-left align-bottom text-[12px] font-normal leading-snug text-fg-2">
                    {failureLabel(k)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.service} className="border-b border-line last:border-b-0">
                  <th scope="row" className="py-1 pr-3 text-left align-middle font-mono text-[13px] font-normal text-fg">
                    <span className="break-all">{r.service}</span>
                  </th>
                  {r.cells.size === 0 ? (
                    <td colSpan={Math.max(1, cols.length)} className="px-2 py-1 align-middle">
                      <Untested />
                    </td>
                  ) : (
                    cols.map((k) => {
                      const runs = r.cells.get(k);
                      return (
                        <td key={k} className="px-0.5 py-1 align-middle">
                          {runs ? (
                            <button
                              type="button"
                              onClick={() => onOpen(runs[0].experiment_id)}
                              aria-label={cellLabel(r.service, k, runs)}
                              className="flex min-h-11 w-full flex-col items-start justify-center gap-0.5 rounded-[2px] px-1.5 text-left hover:bg-bg"
                            >
                              <OutcomeMark outcome={outcomeOf(runs[0])} />
                              {runs.length > 1 && <span className="font-mono tabular text-[11px] text-fg-2">{runs.length} runs</span>}
                            </button>
                          ) : (
                            <span className="flex min-h-11 items-center px-1.5 text-fg-2">not run</span>
                          )}
                        </td>
                      );
                    })
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* narrow: one block per service, one line per run, no horizontal scroll */}
        <ul aria-label="Coverage by service" className={fewCols ? "flex flex-col divide-y divide-line border-y border-line @3xl:hidden" : "flex flex-col divide-y divide-line border-y border-line @6xl:hidden"}>
          {rows.map((r) => {
            const runs = cols.flatMap((k) => r.cells.get(k) ?? []);
            return (
              <li key={r.service} className="flex flex-col gap-1 py-3">
                <span className="font-mono text-[13px] text-fg break-all">{r.service}</span>
                {runs.length === 0 ? (
                  <Untested />
                ) : (
                  <ul aria-label={`Runs against ${r.service}`} className="flex flex-col">
                    {runs.map((e) => (
                      <li key={e.experiment_id}>
                        <button
                          type="button"
                          onClick={() => onOpen(e.experiment_id)}
                          aria-label={cellLabel(r.service, e.failure_type, [e])}
                          className="flex min-h-11 w-full flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-[2px] px-1.5 text-left text-[13px] hover:bg-bg"
                        >
                          <span className="text-fg">{failureLabel(e.failure_type)}</span>
                          <OutcomeMark outcome={outcomeOf(e)} />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>

        <p className="max-w-[72ch] text-[12px] leading-snug text-fg-2">
          Held: the hypothesis held. Violated: it did not. Not judged: the fault ran but no verdict was recorded.
          Aborted: stopped before the fault ran its course. Skipped: refused before injection, nothing ran.
          Failed: the experiment itself errored. Not run: that failure type was never tried on the service.
          Untested: no experiment has targeted the service.
        </p>
      </div>
    </Instrument>
  );
}

function Untested() {
  return (
    <span className="flex min-h-11 items-center gap-2.5 px-1.5 text-[12px] text-fg-2">
      <span aria-hidden="true" className="grid size-7 place-items-center rounded-[2px] border border-dashed border-line">
        <CircleDashed size={13} />
      </span>
      untested
    </span>
  );
}
