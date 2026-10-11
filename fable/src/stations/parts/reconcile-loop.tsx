// The loop, drawn: the Scout's predictions on the left, the experiments that ran on the right, a line
// wherever an experiment's finding confirmed a prediction. Dashed where an experiment bore on a
// prediction without confirming it. Unconnected rows are what nobody has tested yet.
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import type { ExperimentSummary, PredictedWeakness, Reconciliation, WeaknessOutcome } from "../../lib/types";
import { categoryLabel, failureLabel } from "../../lib/format";
import { Instrument } from "../../components/Station";
import { cx } from "../../components/ui";
import { OutcomeMark, outcomeMark } from "./predict-util";
import { STATE_ORDER, confirmingByExperiment } from "./reconcile-text";

const CONFIRMED = outcomeMark("confirmed");
const HEAD = 34;
const ROW = 26;
const NODE = 48;
const GAP = 8;

type LeftItem =
  | { kind: "head"; y: number; state: WeaknessOutcome["state"]; count: number }
  | { kind: "none"; y: number }
  | { kind: "row"; y: number; o: WeaknessOutcome };

interface Link {
  wid: string;
  eid: string;
  y1: number;
  y2: number;
  confirms: boolean;
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    const ro = new ResizeObserver(([e]) => setW(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

interface Props {
  rec: Reconciliation;
  weaknesses: Map<string, PredictedWeakness>;
  experiments: ExperimentSummary[];
  onOpen: (eid: string) => void;
}

export function ReconcileLoop({ rec, weaknesses, experiments, onOpen }: Props) {
  const reduce = useReducedMotion();
  const [midRef, midW] = useWidth<HTMLDivElement>();
  const [focus, setFocus] = useState<{ eid?: string; wid?: string } | null>(null);

  const layout = useMemo(() => {
    const left: LeftItem[] = [];
    const rowY = new Map<string, number>();
    let y = 0;
    for (const state of STATE_ORDER) {
      const rows = rec.outcomes.filter((o) => o.state === state);
      left.push({ kind: "head", y, state, count: rows.length });
      y += HEAD;
      if (!rows.length) {
        left.push({ kind: "none", y });
        y += ROW;
      }
      for (const o of rows) {
        left.push({ kind: "row", y, o });
        rowY.set(o.weakness_id, y + ROW / 2);
        y += ROW;
      }
    }
    const leftH = y;

    // every pairing of a prediction and an experiment that bore on it
    const pairs: { wid: string; eid: string; confirms: boolean }[] = [];
    for (const o of rec.outcomes) {
      const conf = confirmingByExperiment(o);
      const eids = [...new Set([...o.experiments, ...conf.keys()])];
      for (const eid of eids) pairs.push({ wid: o.weakness_id, eid, confirms: conf.has(eid) });
    }

    // experiment nodes sit at the mean height of the rows they connect to, then get pushed apart
    const known = new Map(experiments.map((e) => [e.experiment_id, e]));
    const ids = [...new Set([...pairs.map((p) => p.eid), ...experiments.map((e) => e.experiment_id)])];
    const target = ids.map((eid) => {
      const ys = pairs.filter((p) => p.eid === eid).map((p) => rowY.get(p.wid) ?? 0);
      return { eid, ty: ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Number.POSITIVE_INFINITY, linked: ys.length > 0 };
    });
    target.sort((a, b) => a.ty - b.ty);
    let cursor = HEAD;
    const nodes = target.map((t) => {
      const top = Math.max(cursor, Number.isFinite(t.ty) ? t.ty - NODE / 2 : cursor);
      cursor = top + NODE + GAP;
      return { ...t, top, exp: known.get(t.eid) ?? null };
    });
    // pull back up from the bottom if the stack overran the predictions column
    let limit = Math.max(leftH, HEAD + nodes.length * (NODE + GAP));
    for (let i = nodes.length - 1; i >= 0; i--) {
      nodes[i].top = Math.min(nodes[i].top, limit - NODE);
      limit = nodes[i].top - GAP;
    }
    const nodeY = new Map(nodes.map((n) => [n.eid, n.top + NODE / 2]));
    const height = Math.max(leftH, ...nodes.map((n) => n.top + NODE));
    const links: Link[] = pairs.map((p) => ({ ...p, y1: rowY.get(p.wid) ?? 0, y2: nodeY.get(p.eid) ?? 0 }));
    // draw confirmations last so they sit on top
    links.sort((a, b) => Number(a.confirms) - Number(b.confirms));
    const confirmCount = new Map<string, number>();
    for (const p of pairs) if (p.confirms) confirmCount.set(p.eid, (confirmCount.get(p.eid) ?? 0) + 1);
    return { left, nodes, links, height, confirmCount };
  }, [rec, experiments]);

  const lit = (l: Link) => !focus || (focus.eid ? l.eid === focus.eid : l.wid === focus.wid);
  const confirmedTotal = rec.outcomes.filter((o) => o.state === "confirmed").length;

  return (
    <Instrument title="The loop, closed" meta={`${confirmedTotal} of ${rec.predicted_total} predictions confirmed by a measured finding`}>
      <figure className="flex flex-col gap-4">
        <div className="grid grid-cols-[minmax(0,1fr)_1.75rem_7.25rem] gap-x-0 text-[12px] sm:grid-cols-[minmax(0,30rem)_minmax(3rem,1fr)_minmax(9rem,15rem)]">
          <p className="pb-2 text-model">Predicted by the Scout, before anything broke</p>
          <span />
          <p className="pb-2 pl-3 text-fg">Measured by experiments</p>

          {/* predictions */}
          <ul aria-label="Predictions, confirmed first" className="relative" style={{ height: layout.height }}>
            {layout.left.map((it, i) => {
              if (it.kind === "head") {
                return (
                  <li key={`h-${it.state}`} className="absolute inset-x-0 flex items-end border-b border-line pb-1.5 text-[13px]" style={{ top: it.y, height: HEAD }}>
                    <OutcomeMark state={it.state} />
                    <span className="ml-2 font-mono tabular text-fg">{it.count}</span>
                    {it.state === "untested" && <span className="ml-2 hidden truncate text-fg-2 sm:inline">what to break next</span>}
                  </li>
                );
              }
              if (it.kind === "none") {
                return (
                  <li key={`n-${i}`} className="absolute inset-x-0 flex items-center text-[12px] text-fg-2" style={{ top: it.y, height: ROW }}>
                    none
                  </li>
                );
              }
              const o = it.o;
              const w = weaknesses.get(o.weakness_id);
              const dim = focus && !(focus.wid === o.weakness_id || (focus.eid && layout.links.some((l) => l.wid === o.weakness_id && l.eid === focus.eid)));
              return (
                <li
                  key={o.weakness_id}
                  className={cx("absolute inset-x-0 flex items-center gap-2 pr-2 transition-opacity duration-150", dim && "opacity-35")}
                  style={{ top: it.y, height: ROW }}
                  title={w ? w.title : o.weakness_id}
                  onMouseEnter={() => setFocus({ wid: o.weakness_id })}
                  onMouseLeave={() => setFocus(null)}
                >
                  <span className={outcomeMark(o.state).ink}>{outcomeMark(o.state).glyph(13)}</span>
                  <span className="min-w-0 flex-1 truncate">
                    <span className="sr-only">{o.state}: </span>
                    <span className="text-fg">{categoryLabel(o.category)}</span>{" "}
                    <span className="font-mono text-[11.5px] text-fg-2">{o.services.join(", ")}</span>
                  </span>
                  <span className="shrink-0 font-mono tabular text-[11.5px] text-model">
                    <span className="sr-only">predicted severity </span>
                    {o.predicted_severity.toFixed(1)}
                  </span>
                </li>
              );
            })}
          </ul>

          {/* links */}
          <div ref={midRef} aria-hidden="true" className="relative" style={{ height: layout.height }}>
            {midW > 0 && (
              <svg key={rec.generated_at} width={midW} height={layout.height} viewBox={`0 0 ${midW} ${layout.height}`} className="absolute inset-0 overflow-visible">
                {layout.links.map((l, i) => {
                  const d = `M0 ${l.y1} C${midW * 0.55} ${l.y1} ${midW * 0.45} ${l.y2} ${midW} ${l.y2}`;
                  const on = lit(l);
                  return l.confirms ? (
                    <motion.path
                      key={`${l.wid}-${l.eid}`}
                      d={d}
                      fill="none"
                      stroke={CONFIRMED.stroke}
                      strokeWidth={on && focus ? 2.25 : 1.5}
                      initial={reduce ? false : { pathLength: 0 }}
                      animate={{ pathLength: 1, opacity: on ? 1 : 0.12 }}
                      transition={{ pathLength: { duration: 0.7, delay: 0.15 + i * 0.07, ease: [0.16, 1, 0.3, 1] }, opacity: { duration: 0.15 } }}
                    />
                  ) : (
                    <motion.path
                      key={`${l.wid}-${l.eid}`}
                      d={d}
                      fill="none"
                      stroke="var(--color-fg-2)"
                      strokeWidth={1}
                      strokeDasharray="3 4"
                      initial={reduce ? false : { opacity: 0 }}
                      animate={{ opacity: on ? 0.75 : 0.08 }}
                      transition={{ duration: 0.3 }}
                    />
                  );
                })}
              </svg>
            )}
          </div>

          {/* experiments */}
          <div className="relative" style={{ height: layout.height }}>
            {layout.nodes.map((n) => {
              const c = layout.confirmCount.get(n.eid) ?? 0;
              const label = n.exp ? `${failureLabel(n.exp.failure_type)} on ${n.exp.target_service}` : "report not loaded";
              const dim = focus && focus.eid !== n.eid && !(focus.wid && layout.links.some((l) => l.eid === n.eid && l.wid === focus.wid));
              return (
                <button
                  key={n.eid}
                  type="button"
                  onClick={() => onOpen(n.eid)}
                  onMouseEnter={() => setFocus({ eid: n.eid })}
                  onMouseLeave={() => setFocus(null)}
                  onFocus={() => setFocus({ eid: n.eid })}
                  onBlur={() => setFocus(null)}
                  aria-label={`Experiment ${n.eid}, ${label}. ${c ? `Confirmed ${c} ${c === 1 ? "prediction" : "predictions"}.` : "Confirmed no prediction."} Open it in Score.`}
                  className={cx(
                    "absolute inset-x-0 flex flex-col justify-center border-l-2 pl-2 pr-1 text-left sm:pl-3 transition-opacity duration-150 hover:bg-bg/50 focus-visible:outline-offset-[-2px]",
                    c ? CONFIRMED.border : "border-line",
                    dim && "opacity-35",
                  )}
                  style={{ top: n.top, height: NODE }}
                >
                  <span className="flex items-baseline justify-between gap-2">
                    <span className="truncate font-mono tabular text-[11px] text-fg underline decoration-line underline-offset-4 sm:text-[12px]">{n.eid}</span>
                    {c > 0 && <span className={cx("hidden shrink-0 text-[11px] sm:inline", CONFIRMED.ink)}><span className="font-mono tabular">{c}</span> confirmed</span>}
                  </span>
                  <span className="hidden truncate text-[11.5px] text-fg-2 sm:block">{label}</span>
                </button>
              );
            })}
          </div>
        </div>

        <figcaption className="flex flex-col gap-2 border-t border-line pt-3 text-[12px] leading-snug text-fg-2 sm:flex-row sm:flex-wrap sm:gap-x-8">
          <span className="inline-flex items-center gap-2">
            <svg width="28" height="8" aria-hidden="true"><line x1="0" y1="4" x2="28" y2="4" stroke={CONFIRMED.stroke} strokeWidth="1.5" /></svg>
            a finding from this experiment confirmed the prediction
          </span>
          <span className="inline-flex items-center gap-2">
            <svg width="28" height="8" aria-hidden="true"><line x1="0" y1="4" x2="28" y2="4" stroke="var(--color-fg-2)" strokeWidth="1" strokeDasharray="3 4" /></svg>
            it bore on the prediction but raised no confirming finding
          </span>
          <span className="inline-flex items-center gap-2">
            <span className={outcomeMark("untested").ink}>{outcomeMark("untested").glyph(13)}</span>
            no line: nothing has tested it yet
          </span>
        </figcaption>
      </figure>
    </Instrument>
  );
}
