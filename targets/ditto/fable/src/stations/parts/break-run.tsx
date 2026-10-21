import { useState } from "react";
import { useReducedMotion } from "motion/react";
import { CheckCircle, Stop } from "@phosphor-icons/react";
import { actions, useCatalog, useFState } from "../../lib/store";
import { announce } from "../../lib/a11y";
import { clean, failureLabel, fmtMs, fmtPct } from "../../lib/format";
import type { RunPhase, RunState, Thresholds, Topology } from "../../lib/types";
import { Instrument, Notice } from "../../components/Station";
import { TopologyMap } from "../../components/TopologyMap";
import { Marker, MarkerGlyph, type MarkerState } from "../../components/Marker";
import { AiMark, Readout, cx } from "../../components/ui";
import { FailureName, GlyphWord, ParamLedger } from "./baseline-kit";
import { abortText, isDown, useBlastStates } from "./break-helpers";

const STEPS = ["Preflight", "Inject", "Observe", "Rollback", "Recover", "Soak", "Analyze"] as const;
// The injector reports preflight, injecting, rollback, recovering and soaking. Inject is a moment, not a
// phase: once it reports injecting, the fault is in and the ribbon sits on Observe with Inject done.
const CURRENT: Record<RunPhase, number> = { idle: -1, preflight: 0, injecting: 2, rollback: 3, recovering: 4, soaking: 5, analyzing: 6 };

export function PhaseRibbon({ phase }: { phase: RunPhase }) {
  const cur = CURRENT[phase];
  return (
    <ol aria-label="Experiment phases" className="flex flex-wrap gap-px border border-line bg-line">
      {STEPS.map((label, i) => {
        const st = i < cur ? "done" : i === cur ? "now" : "next";
        return (
          <li
            key={label}
            aria-current={st === "now" ? "step" : undefined}
            className={cx(
              "flex min-h-12 grow basis-[22%] flex-col justify-center gap-0.5 px-2.5 py-1.5 transition-colors duration-200 sm:basis-0",
              st === "now" ? "bg-fg text-bg" : "bg-sheet",
              st === "done" && "text-fg",
              st === "next" && "text-fg-2",
            )}
          >
            <span className="flex items-center gap-1.5 label-condensed text-[14px] leading-none">
              {st === "done" && <CheckCircle size={13} weight="fill" aria-hidden="true" className="text-pass" />}
              {label}
            </span>
            <span className={cx("font-mono text-[10px] leading-none", st === "now" ? "text-bg" : "text-fg-2")}>
              {st === "done" ? "done" : st === "now" ? "now" : " "}
              <span className="sr-only">{st === "next" ? "not yet" : ""}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** The burned-in timecode of a crash-test camera: time since the fault, and time left before the hard stop. */
export function RunClock({ run, phase, t }: { run: RunState; phase: RunPhase; t: Thresholds | null }) {
  const elapsed = run.elapsed_s ?? 0;
  const left = run.deadline_in_s;
  const total = left != null ? elapsed + Math.max(0, left) : run.max_s ?? null;
  const frac = total ? Math.min(1, Math.max(0, elapsed / total)) : 0;
  const floor = t ? 1 - t.abort_error_rate_above : null;
  const success = run.success_rate;
  const p95 = run.p95_ms;
  const preflight = phase === "preflight";
  const healing = phase === "recovering" || phase === "soaking" || phase === "analyzing";
  const id = run.experiment_id ? run.experiment_id.slice(0, 8) : "pending";

  return (
    <Instrument
      title={`Experiment ${id}`}
      meta={run.target_service ? `${failureLabel(run.failure_type).toLowerCase()} on ${run.target_service}` : "planning"}
    >
      <div className="flex flex-col gap-5">
        <PhaseRibbon phase={phase} />
        <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
          <div className="min-w-0">
            <p className="text-[12px] text-fg-2">{preflight ? "Preflight checks, nothing injected yet" : "Since the fault went in"}</p>
            <p className="font-mono tabular text-[clamp(2.75rem,7vw,5rem)] leading-[0.95] tracking-tight text-fg" aria-live="off">
              t+{elapsed.toFixed(1)}
              <span className="ml-1 text-[0.45em] text-fg-2">s</span>
            </p>
          </div>
          <div className="text-left sm:text-right">
            {healing ? (
              <>
                <p className="font-mono tabular text-[30px] leading-none text-pass">fault removed</p>
                <p className="mt-1 text-[12px] text-fg-2">
                  {phase === "soaking"
                    ? "watching for a relapse before it may claim recovery"
                    : phase === "recovering"
                      ? "waiting for good checks in a row"
                      : "scoring the run"}
                </p>
              </>
            ) : (
              <>
                <p className="font-mono tabular text-[30px] leading-none text-fg">{left != null ? `${Math.max(0, left).toFixed(1)} s` : "n/a"}</p>
                <p className="mt-1 text-[12px] text-fg-2">to the deadline{run.max_s != null ? `, fault capped at ${run.max_s} s` : ""}</p>
              </>
            )}
          </div>
        </div>
        {!healing && (
          <div className="relative h-[3px] w-full overflow-hidden bg-line" aria-hidden="true">
            <div className="absolute inset-0 origin-left bg-fg transition-transform duration-500 ease-linear motion-reduce:transition-none" style={{ transform: `scaleX(${frac})` }} />
          </div>
        )}
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 border-t border-line pt-4 sm:grid-cols-4">
          <Readout
            label={floor != null && success != null && success < floor ? "Success, below the limit" : "Success now"}
            value={fmtPct(success)}
            tone={floor != null && success != null && success < floor ? "fail" : "neutral"}
          />
          <Readout
            label={t && p95 != null && p95 > t.abort_p95_ms_above ? "p95, above the limit" : "p95 now"}
            value={fmtMs(p95)}
            tone={t && p95 != null && p95 > t.abort_p95_ms_above ? "fail" : "neutral"}
          />
          <Readout label="Samples" value={run.sample_count ?? "n/a"} />
          {phase === "soaking" ? (
            <Readout label="Soaking for" value={run.soak_elapsed_s != null ? `${run.soak_elapsed_s.toFixed(0)} s` : "n/a"} />
          ) : phase === "recovering" ? (
            <Readout label="Good checks in a row" value={`${run.consecutive_ok ?? 0}${t ? ` of ${t.recovery_consecutive_checks}` : ""}`} />
          ) : (
            <Readout label="Abort when below" value={floor != null ? fmtPct(floor) : "n/a"} tone="muted" />
          )}
        </div>
      </div>
    </Instrument>
  );
}

export function BlastPanel({ topology, run, phase }: { topology: Topology; run: RunState; phase: RunPhase }) {
  const reduce = useReducedMotion();
  const { states, recovered } = useBlastStates(run, phase);
  const expected = run.blast?.expected ?? [];
  const observed = (run.blast?.observed ?? []).filter((x) => x !== run.target_service);
  const surprise = observed.filter((x) => !expected.includes(x));
  return (
    <Instrument title="Blast radius" meta="predicted against observed">
      <TopologyMap
        topology={topology}
        ariaLabel={`Application map during the experiment. Target ${run.target_service ?? "none"}. Predicted: ${expected.join(", ") || "none"}. Observed: ${observed.join(", ") || "none"}.`}
        states={states}
        selected={run.target_service}
        pulse={reduce || phase !== "injecting" || !run.target_service ? [] : [run.target_service]}
        height={340}
      />
      <MarkerKey />
      <dl className="mt-3 grid gap-2 border-t border-line pt-3 text-[13px] sm:grid-cols-2">
        <div>
          <dt className="text-[12px] text-fg-2">Predicted by the hypothesis</dt>
          <dd className="font-mono text-fg">{expected.join(", ") || "no other service"}</dd>
        </div>
        <div>
          <dt className="text-[12px] text-fg-2">Observed failing now</dt>
          <dd className="font-mono text-fg">{observed.join(", ") || "none"}</dd>
        </div>
      </dl>
      {surprise.length > 0 && (
        <p className="mt-2 text-[13px] leading-snug text-fail">
          <span className="font-semibold">Outside the prediction:</span> <span className="font-mono">{surprise.join(", ")}</span>. A cascade the hypothesis did not
          expect becomes a finding.
        </p>
      )}
      {recovered.length > 0 && <p className="mt-2 text-[13px] text-pass">Recovered: <span className="font-mono">{recovered.join(", ")}</span></p>}
    </Instrument>
  );
}

export function MarkerKey({ only }: { only?: MarkerState[] }) {
  const items: { st: MarkerState; label: string }[] = [
    { st: "target", label: "target" },
    { st: "predicted", label: "predicted" },
    { st: "affected", label: "affected" },
    { st: "down", label: "down" },
    { st: "recovered", label: "recovered" },
  ];
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-fg-2" aria-label="Marker key">
      {items
        .filter((x) => !only || only.includes(x.st))
        .map((x) => (
          <li key={x.st} className="flex items-center gap-1.5">
            <svg width="18" height="18" aria-hidden="true">
              <Marker cx={9} cy={9} r={6} state={x.st} />
            </svg>
            {x.label}
          </li>
        ))}
    </ul>
  );
}

function httpWord(v: number | null, down: boolean): { text: string; ink: string } {
  if (v == null) return down ? { text: "none", ink: "text-fail" } : { text: "n/a", ink: "text-fg-2" };
  if (v >= 200 && v < 300) return { text: String(v), ink: "text-fg" };
  return { text: String(v), ink: "text-fail" };
}

export function HealthTable({ run }: { run: RunState }) {
  const health = run.health ?? {};
  const target = run.target_service;
  const observed = new Set(run.blast?.observed ?? []);
  const rows = Object.entries(health).sort(([a], [b]) => {
    const rank = (x: string) => (x === target ? 0 : observed.has(x) ? 1 : 2);
    return rank(a) - rank(b) || a.localeCompare(b);
  });
  return (
    <Instrument title="Service health" meta="container, /alive, /ready">
      {rows.length ? (
        <table className="w-full text-left text-[12px]">
          <caption className="sr-only">Health of every watched service during the experiment. None means the check got no answer.</caption>
          <thead>
            <tr className="border-b border-line text-[11px] text-fg-2">
              <th scope="col" className="py-1.5 pr-2 font-normal">Service</th>
              <th scope="col" className="py-1.5 pr-2 font-normal">Container</th>
              <th scope="col" className="py-1.5 pr-2 text-right font-normal">Alive</th>
              <th scope="col" className="py-1.5 text-right font-normal">Ready</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([svc, h]) => {
              const down = isDown(h);
              const a = httpWord(h.alive, down);
              const r = httpWord(h.ready, down);
              return (
                <tr key={svc} className="border-b border-line">
                  <th scope="row" className="py-2 pr-2 font-mono font-normal text-fg">
                    <span className="flex items-center gap-1.5">
                      {svc === target && <MarkerGlyph size={12} />}
                      <span className="truncate">{svc}</span>
                    </span>
                  </th>
                  <td className="py-2 pr-2">
                    <GlyphWord tone={down ? "blocked" : h.status ? "ok" : "off"} word={h.status ?? "unknown"} />
                  </td>
                  <td className={cx("py-2 pr-2 text-right font-mono tabular", a.ink)}>{a.text}</td>
                  <td className={cx("py-2 text-right font-mono tabular", r.ink)}>{r.text}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <p className="text-[13px] text-fg-2">No health samples yet. They arrive with the first observation.</p>
      )}
    </Instrument>
  );
}

export function SafetyAbort({ run, t }: { run: RunState; t: Thresholds | null }) {
  const reason = run.safety?.abort_reason;
  if (!reason) return null;
  const d = run.safety?.detail ?? {};
  const num = (k: string) => (typeof d[k] === "number" ? (d[k] as number) : null);
  const rate = num("error_rate");
  const thr = num("threshold") ?? (t ? t.abort_error_rate_above : null);
  const thrMs = num("threshold_ms") ?? (t ? t.abort_p95_ms_above : null);
  let extra = "";
  if (reason === "error_rate" && rate != null && thr != null) extra = ` ${fmtPct(rate)} errors against a limit of ${fmtPct(thr)}.`;
  if (reason === "latency" && thrMs != null) extra = ` Limit ${fmtMs(thrMs)}.`;
  return (
    <div role="alert" className="flex items-start gap-3 rounded-[2px] border border-fail px-4 py-3">
      <Stop size={18} weight="fill" aria-hidden="true" className="mt-0.5 shrink-0 text-fail" />
      <p className="text-[14px] leading-snug text-fg">
        <span className="font-semibold text-fail">Safety abort:</span> {abortText(reason)}.{extra} The fault is being rolled back.
      </p>
    </div>
  );
}

/** The station's own kill switch while a test runs. The header one does the same thing. */
export function StopEverything() {
  const s = useFState();
  const [sent, setSent] = useState(false);
  const latched = !!s?.stop_requested;
  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        onClick={async () => {
          setSent(true);
          await actions.stop();
          announce("Stopped everything. The fault rolls back on the next safety check.");
        }}
        className="inline-flex min-h-16 w-full items-center justify-center gap-3 rounded-[2px] bg-fail px-6 text-[19px] font-semibold text-bg hover:brightness-110 active:scale-[0.99]"
      >
        <Stop size={22} weight="fill" aria-hidden="true" />
        Stop everything
      </button>
      <p className="text-[12px] leading-snug text-fg-2" role="status">
        {latched || sent
          ? "STOP is latched. The safety monitor rolls the fault back on its next check and the Guardian restores anything still down. Resume in the header before the next launch."
          : "Writes STOP. The safety monitor checks it every second and rolls back. Esc does the same."}
      </p>
    </div>
  );
}

/** What is running, as the server reports it. */
export function RunCard({ run }: { run: RunState }) {
  const catalog = useCatalog();
  const failure = catalog?.failures.find((f) => f.key === run.failure_type) ?? null;
  const statement = typeof run.hypothesis === "string" ? run.hypothesis : run.hypothesis?.statement ?? null;
  const byModel = run.planned_by === "llm";
  return (
    <section aria-label="Running test" className="flex flex-col gap-3 rounded-[2px] border border-line bg-sheet p-4">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="label-condensed text-[13px] text-fg">Running test</h3>
        <span className="font-mono text-[11px] text-fg-2">planned_by {run.planned_by ?? "n/a"}</span>
      </div>
      {run.target_service ? (
        <div className="flex items-start gap-3">
          <MarkerGlyph size={24} className="mt-0.5 shrink-0" />
          <div className="min-w-0">
            <p className="font-mono text-[19px] leading-tight text-fg">{run.target_service}</p>
            <FailureName k={run.failure_type} failure={failure} className="block text-[14px] text-fg" />
          </div>
        </div>
      ) : (
        <p className="text-[14px] text-fg-2">The planner is choosing the experiment.</p>
      )}
      {run.parameters && Object.keys(run.parameters).length > 0 && <ParamLedger params={run.parameters} failure={failure} />}
      {statement && (
        <div className="border-t border-line pt-2.5">
          <p className="mb-1 flex items-center gap-2 text-[12px] text-fg-2">Hypothesis {byModel && <AiMark />}</p>
          <p className={cx("line-clamp-5 text-[13px] leading-snug", byModel ? "text-model" : "text-fg")}>{clean(statement)}</p>
        </div>
      )}
      {run.rationale && (
        <p className={cx("border-t border-line pt-2.5 text-[12px] leading-snug", byModel ? "text-model" : "text-fg-2")}>
          {byModel && <AiMark />} {clean(run.rationale)}
        </p>
      )}
    </section>
  );
}

export function PhaseSentence({ phase, run }: { phase: RunPhase; run: RunState }) {
  const text: Record<RunPhase, string> = {
    idle: "No experiment running.",
    preflight: "Preflight: checking that the system is healthy enough to break. Nothing is injected yet.",
    injecting: "Experiment running. Phase: observe. Abort limits armed, watchdog counting down.",
    rollback: "Rolling back: removing the fault.",
    recovering: "Fault removed. Waiting for the system to recover.",
    soaking: "Looks recovered. Soaking, because a system can look recovered and then relapse.",
    analyzing: "Fault removed. Scoring the run: risk, fixes, scorecard.",
  };
  return (
    <Notice tone={run.safety?.abort_reason ? "fail" : "neutral"}>
      <span className="text-fg">{text[phase]}</span>
    </Notice>
  );
}
