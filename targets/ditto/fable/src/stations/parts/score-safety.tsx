// F3: the experiments that stopped themselves. The organisers ask for "stop automatically if impact exceeds
// defined thresholds"; this panel shows each time it happened, with the numbers the safety monitor logged.
import type { ReactNode } from "react";
import { ArrowDown, CheckCircle, XCircle } from "@phosphor-icons/react";
import type { ExperimentSummary } from "../../lib/types";
import { failureLabel, fmtMs, fmtS, timecode } from "../../lib/format";
import { Instrument } from "../../components/Station";
import { Button, cx } from "../../components/ui";
import { abortDetail, isAutoStop, isOperatorStop, plural, stopAt, tPlus } from "./score-rollup";
import { abortExplain, pctSmart, tidy, when } from "./score-text";

type Open = (id: string) => void;

/** F3: experiments the safety monitor stopped on its own. Renders nothing when there are none to show. */
export function AutoStops({ experiments, onOpen }: { experiments: ExperimentSummary[]; onOpen: Open }) {
  const auto = experiments.filter(isAutoStop);
  const manual = experiments.filter(isOperatorStop);
  if (!auto.length && !manual.length) return null;
  const of = plural(experiments.length, "experiment");
  const meta = manual.length ? `${auto.length} automatic, ${manual.length} by an operator, of ${of}` : `${auto.length} of ${of}`;
  return (
    <Instrument title="Automatic stops" meta={meta}>
      <div className="flex flex-col gap-6">
        <p className="max-w-[68ch] text-[14px] leading-relaxed text-fg-2">
          While a fault is live, the safety monitor watches the error rate and p95 latency of the last few probe requests, and the health of
          services the hypothesis did not name. When a limit is crossed it rolls the fault back without waiting for anyone.
        </p>
        {auto.length > 0 && (
          <ul className="-mx-4 divide-y divide-line border-t border-line">
            {auto.map((e) => (
              <StopRow key={e.experiment_id} e={e} onOpen={onOpen} />
            ))}
          </ul>
        )}
        {manual.length > 0 && (
          <section aria-labelledby="stops-operator" className="flex flex-col gap-2">
            <h4 id="stops-operator" className="label-condensed text-[14px] text-fg">
              Stopped by an operator <span className="ml-2 font-mono tabular text-[12px] text-fg-2">{manual.length}</span>
            </h4>
            <ul className="-mx-4 divide-y divide-line border-t border-line">
              {manual.map((e) => (
                <StopRow key={e.experiment_id} e={e} onOpen={onOpen} operator />
              ))}
            </ul>
          </section>
        )}
      </div>
    </Instrument>
  );
}

function headline(e: ExperimentSummary, operator: boolean): string {
  if (operator) return "An operator pressed Stop everything.";
  if (e.watchdog_fired) return "The watchdog stopped this experiment at its deadline.";
  return "The safety monitor stopped this experiment on its own.";
}

/** The numbers the monitor logged, labelled by the rule that fired. Only keys that exist are shown. */
function logged(e: ExperimentSummary): { k: string; v: string }[] {
  const d = abortDetail(e);
  const out: { k: string; v: string }[] = [];
  const has = (key: string) => typeof d[key] === "number";
  const reason = e.abort_reason ?? "";
  if (reason === "latency") {
    if (has("p95_ms")) out.push({ k: "Measured p95", v: fmtMs(d.p95_ms) });
    if (has("threshold_ms")) out.push({ k: "Limit", v: fmtMs(d.threshold_ms) });
  } else if (reason.startsWith("blast_radius:")) {
    if (has("consecutive_failures")) out.push({ k: "Failed checks in a row", v: String(d.consecutive_failures) });
  } else {
    if (has("error_rate")) out.push({ k: "Measured error rate", v: pctSmart(d.error_rate) });
    if (has("threshold")) out.push({ k: "Limit", v: pctSmart(d.threshold) });
  }
  if (has("trailing_samples")) out.push({ k: "Window", v: `last ${d.trailing_samples} requests` });
  return out;
}

const EVENTS: { event: string; label: string }[] = [
  { event: "injected", label: "Fault injected" },
  { event: "safety_abort", label: "Safety stop" },
  { event: "watchdog_fired", label: "Watchdog stop" },
  { event: "rollback_done", label: "Rollback done" },
  { event: "recovered", label: "Recovered" },
];

/** The events that cut a fault short: the safety monitor's (or an operator's) stop and the watchdog's. */
const STOP_EVENTS = new Set(["safety_abort", "watchdog_fired"]);

type Mark = { event: string; label: string; t: number | null };

/** The sequence for one row. The watchdog step shows only when the watchdog fired; when it fired and the safety monitor
 *  never logged a stop, the safety step is left out rather than shown as "not logged". Operator stops log "safety_abort" too. */
function marksFor(e: ExperimentSummary, operator: boolean): Mark[] {
  const safetyLogged = tPlus(e, "safety_abort") != null;
  return EVENTS.filter((x) => {
    if (x.event === "watchdog_fired") return e.watchdog_fired;
    if (x.event === "safety_abort") return safetyLogged || !e.watchdog_fired;
    return true;
  }).map((x) => ({
    ...x,
    label: operator && x.event === "safety_abort" ? "Stopped" : x.label,
    t: tPlus(e, x.event),
  }));
}

/** Plain words for why the row stopped. The watchdog logs no numbers, so its line says only what it did. */
function reasonText(e: ExperimentSummary): string {
  if (e.watchdog_fired && tPlus(e, "safety_abort") == null) return "the watchdog hit the hard deadline and forced a rollback";
  return tidy(abortExplain(e).text);
}

function StopRow({ e, onOpen, operator = false }: { e: ExperimentSummary; onOpen: Open; operator?: boolean }) {
  const id = e.experiment_id;
  const facts = operator ? [] : logged(e);
  const marks = marksFor(e, operator);
  const injected = tPlus(e, "injected");
  const stopped = stopAt(e);
  const after = injected != null && stopped != null ? stopped - injected : null;

  return (
    <li className="grid gap-x-10 gap-y-5 px-4 py-5 lg:grid-cols-[20rem_minmax(0,1fr)]">
      <div className="flex min-w-0 flex-col gap-3">
        <p className="text-[16px] font-medium leading-snug text-fg">{headline(e, operator)}</p>
        <p className="text-[14px] leading-snug text-fg-2">
          {failureLabel(e.failure_type)} on <span className="font-mono text-[13px] text-fg">{e.target_service}</span>
          {when(e.started_at) && <>, {when(e.started_at)}</>}
          <span id={`stop-${id}`} className="mt-0.5 block font-mono tabular text-[12px]">{id}</span>
        </p>
        {!operator && (
          <p className="text-[14px] leading-relaxed text-fg">
            <span className="text-fg-2">Why: </span>
            {reasonText(e)}.
          </p>
        )}
        {facts.length > 0 && (
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 border-l-2 border-line pl-3 text-[13px]">
            {facts.map((f) => (
              <Fact key={f.k} k={f.k}>
                {f.v}
              </Fact>
            ))}
          </dl>
        )}
        <div className="pt-1">
          <Button variant="ghost" size="sm" onClick={() => onOpen(id)} aria-describedby={`stop-${id}`}>
            Open in the log <ArrowDown size={14} aria-hidden="true" />
          </Button>
        </div>
      </div>

      <div className="flex min-w-0 flex-col gap-5">
        <Ruler marks={marks} stop={stopped} />
        <ol aria-label={`Sequence for ${id}`} className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
          {marks.map((m) => (
            <li key={m.event} className="flex flex-col gap-0.5">
              <span className="text-[12px] text-fg-2">{m.label}</span>
              <span className={cx("font-mono tabular text-[15px]", m.t == null ? "text-fg-2" : STOP_EVENTS.has(m.event) ? "text-fail" : "text-fg")}>
                {m.t == null ? "not logged" : timecode(m.t)}
              </span>
            </li>
          ))}
        </ol>
        <dl className="grid gap-x-6 gap-y-3 border-t border-line pt-4 sm:grid-cols-2 xl:grid-cols-4">
          <Readout k="Stopped after">
            <span className="font-mono tabular text-[17px] text-fg">{fmtS(after, 1)}</span>
          </Readout>
          <Readout k="Time to recover after rollback">
            <span className="font-mono tabular text-[17px] text-fg">{fmtS(e.time_to_recover_s, 1)}</span>
          </Readout>
          <Readout k="Remediation needed">
            {e.remediation_actions.length ? (
              <span className="font-mono text-[13px] text-fg">{e.remediation_actions.map((a) => tidy(a)).join(", ")}</span>
            ) : (
              <span className="text-[14px] text-fg">none</span>
            )}
          </Readout>
          <Readout k="Data integrity">
            <span className="text-[14px]">
              {e.data_integrity_ok === true && <span className="inline-flex items-center gap-1.5 text-pass"><CheckCircle size={14} aria-hidden="true" />intact</span>}
              {e.data_integrity_ok === false && <span className="inline-flex items-center gap-1.5 text-fail"><XCircle size={14} aria-hidden="true" />writes missing</span>}
              {e.data_integrity_ok == null && <span className="text-fg-2">not checked</span>}
            </span>
          </Readout>
        </dl>
      </div>
    </li>
  );
}

function Fact({ k, children }: { k: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-fg-2">{k}</dt>
      <dd className="font-mono tabular text-fg">{children}</dd>
    </>
  );
}

function Readout({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] text-fg-2">{k}</dt>
      <dd className="break-words">{children}</dd>
    </div>
  );
}

/** A slim, decorative scale from t+0 to the last logged event. The fault window (injected to the stop) is in the fail ink. */
function Ruler({ marks, stop }: { marks: Mark[]; stop: number | null }) {
  const times = marks.map((m) => m.t).filter((t): t is number => t != null);
  if (!times.length) return null;
  const end = Math.max(...times) || 1;
  const pos = (t: number) => `${Math.min(100, Math.max(0, (t / end) * 100))}%`;
  const inj = marks.find((m) => m.event === "injected")?.t ?? null;
  return (
    <div aria-hidden="true" className="relative h-5">
      <span className="absolute inset-x-0 top-1/2 h-px bg-line" />
      {inj != null && stop != null && stop > inj && (
        <span className="absolute top-1/2 h-[3px] -translate-y-1/2 bg-fail" style={{ left: pos(inj), width: `${((stop - inj) / end) * 100}%` }} />
      )}
      {marks.map((m) =>
        m.t == null ? null : (
          <span
            key={m.event}
            className={cx("absolute top-0 h-full w-px -translate-x-1/2", STOP_EVENTS.has(m.event) ? "bg-fail" : "bg-fg-2")}
            style={{ left: pos(m.t) }}
          />
        ),
      )}
    </div>
  );
}
