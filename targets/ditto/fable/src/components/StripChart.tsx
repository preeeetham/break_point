import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PhaseBand, TelemetryPoint } from "../lib/types";
import { fmtMs, fmtPct } from "../lib/format";
import { cx } from "./ui";

export interface StripChartProps {
  points: TelemetryPoint[];
  bands?: PhaseBand[];
  ariaLabel: string;
  /** seconds of history to show, ending at `now` */
  window?: number;
  now?: number;
  /** abort when success falls below this (1 - abort error rate) */
  successFloor?: number | null;
  /** abort when p95 rises above this */
  p95Ceiling?: number | null;
  height?: number;
  className?: string;
}

const PHASE_LABEL: Record<string, string> = {
  preflight: "preflight", injecting: "fault", rollback: "rollback", recovering: "recover", soaking: "soak", analyzing: "analyze",
};

/** Success rate as a filled trace (top = 100 %), p95 latency as a line on a log scale. SVG so it re-skins with the theme. */
export function StripChart({ points, bands = [], ariaLabel, window: span = 300, now, successFloor, p95Ceiling, height = 220, className = "" }: StripChartProps) {
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(800);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(260, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const end = now ?? (points.length ? points[points.length - 1].t : Date.now() / 1000);
  const start = end - span;
  const visible = useMemo(() => points.filter((p) => p.t >= start), [points, start]);
  const padL = 44, padR = 56, padT = 12, padB = 22;
  const iw = w - padL - padR, ih = height - padT - padB;
  const x = (t: number) => padL + ((t - start) / span) * iw;
  const ys = (v: number) => padT + (1 - v) * ih;
  const pMax = Math.max(3000, p95Ceiling ?? 0, ...visible.map((p) => p.p95 ?? 0));
  const yl = (ms: number) => padT + (1 - Math.log10(Math.max(1, ms)) / Math.log10(pMax * 1.2)) * ih;

  const successPath = useMemo(() => {
    let d = "";
    let open = false;
    for (const p of visible) {
      if (p.success == null) { open = false; continue; }
      d += `${open ? "L" : "M"}${x(p.t).toFixed(1)} ${ys(p.success).toFixed(1)}`;
      open = true;
    }
    return d;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, w, height, start]);

  const area = useMemo(() => {
    const segs: string[] = [];
    let cur: TelemetryPoint[] = [];
    const flush = () => {
      if (cur.length > 1) {
        segs.push(`M${x(cur[0].t)} ${ys(0)}` + cur.map((p) => `L${x(p.t).toFixed(1)} ${ys(p.success!).toFixed(1)}`).join("") + `L${x(cur[cur.length - 1].t)} ${ys(0)}Z`);
      }
      cur = [];
    };
    for (const p of visible) (p.success == null ? flush() : cur.push(p));
    flush();
    return segs.join("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, w, height, start]);

  const p95Path = useMemo(() => {
    let d = "";
    let open = false;
    for (const p of visible) {
      if (p.p95 == null) { open = false; continue; }
      d += `${open ? "L" : "M"}${x(p.t).toFixed(1)} ${yl(p.p95).toFixed(1)}`;
      open = true;
    }
    return d;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, w, height, start, pMax]);

  const last = [...visible].reverse().find((p) => p.success != null);
  const lastP95 = [...visible].reverse().find((p) => p.p95 != null);
  const summary = last
    ? `Latest success ${fmtPct(last.success, 0)}, p95 ${fmtMs(lastP95?.p95 ?? null)}, over the last ${span} seconds.`
    : "No telemetry yet. Arm the probes to start sampling.";

  return (
    <div ref={box} className={cx("relative w-full", className)}>
      <svg width={w} height={height} role="img" aria-label={`${ariaLabel}. ${summary}`} className="block">
        {[0, 0.5, 1].map((v) => (
          <g key={v}>
            <line x1={padL} x2={padL + iw} y1={ys(v)} y2={ys(v)} stroke="var(--color-grid)" />
            <text x={padL - 8} y={ys(v) + 4} textAnchor="end" className="fill-fg-2 font-mono text-[10px]">{v * 100}%</text>
          </g>
        ))}
        {bands.filter((b) => (b.to ?? end) >= start).map((b, i) => {
          const bx = x(Math.max(start, b.from));
          const bw = Math.max(2, x(Math.min(end, b.to ?? end)) - bx);
          const fault = b.phase === "injecting";
          return (
            <g key={i}>
              <rect x={bx} y={padT} width={bw} height={ih} fill={fault ? "var(--color-marker)" : "var(--color-fg-2)"} opacity={fault ? 0.18 : 0.07} />
              {bw > 34 && <text x={bx + 4} y={padT + 11} className="fill-fg-2 font-mono text-[10px]">{PHASE_LABEL[b.phase] ?? b.phase}</text>}
            </g>
          );
        })}
        {successFloor != null && (
          <g>
            <line x1={padL} x2={padL + iw} y1={ys(successFloor)} y2={ys(successFloor)} stroke="var(--color-fail)" strokeDasharray="6 4" />
            <text x={padL + iw + 6} y={ys(successFloor) + 4} className="fill-fail font-mono text-[10px]">abort</text>
          </g>
        )}
        {p95Ceiling != null && (
          <g>
            <line x1={padL} x2={padL + iw} y1={yl(p95Ceiling)} y2={yl(p95Ceiling)} stroke="var(--color-fail)" strokeDasharray="2 4" />
            <text x={padL + iw + 6} y={yl(p95Ceiling) + 4} className="fill-fail font-mono text-[10px]">{fmtMs(p95Ceiling)}</text>
          </g>
        )}
        <path d={area} fill="var(--color-fg)" opacity={0.08} />
        <path d={successPath} fill="none" stroke="var(--color-fg)" strokeWidth={1.75} strokeLinejoin="round" />
        {/* yellow on an ink casing, like marker tape, so it reads on both the lab and the camera theme */}
        <path d={p95Path} fill="none" stroke="var(--color-fg)" strokeWidth={3.5} strokeLinejoin="round" opacity={0.85} />
        <path d={p95Path} fill="none" stroke="var(--color-marker)" strokeWidth={1.75} strokeLinejoin="round" />
        <text x={padL} y={height - 6} className="fill-fg-2 font-mono text-[10px]">-{span}s</text>
        <text x={padL + iw} y={height - 6} textAnchor="end" className="fill-fg-2 font-mono text-[10px]">now</text>
      </svg>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-fg-2">
        <span className="flex items-center gap-2"><span aria-hidden="true" className="h-0.5 w-5 bg-fg" />API success</span>
        <span className="flex items-center gap-2"><span aria-hidden="true" className="h-0.5 w-5 bg-marker" />p95 latency, log scale</span>
        {(successFloor != null || p95Ceiling != null) && <span className="flex items-center gap-2"><span aria-hidden="true" className="h-0 w-5 border-t border-dashed border-fail" />abort limit</span>}
      </div>
      <p className="sr-only" aria-live="off">{summary}</p>
    </div>
  );
}
