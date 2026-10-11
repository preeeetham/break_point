import { useId, useMemo, useRef, useState, useLayoutEffect } from "react";
import { motion, useReducedMotion } from "motion/react";
import type { EdgeKind, Topology } from "../lib/types";
import { layerGraph } from "../lib/layout";
import { Marker, type MarkerState } from "./Marker";
import { cx } from "./ui";

export interface TopologyMapProps {
  topology: Topology;
  ariaLabel: string;
  /** which evidence layers to draw; all by default */
  kinds?: EdgeKind[];
  /** per-service marker state */
  states?: Record<string, MarkerState>;
  pulse?: string[];
  selected?: string | null;
  onSelect?: (service: string) => void;
  /** services that cannot be selected, with the reason shown to assistive tech */
  locked?: Record<string, string>;
  showObservability?: boolean;
  showScores?: boolean;
  /** draw edges along the blast path from this service in the fail ink */
  blastFrom?: string | null;
  /** colour of the outline behind labels: the surface the map sits on */
  halo?: string;
  height?: number;
  className?: string;
}

const EDGE_STYLE: Record<EdgeKind, { dash?: string; width: number; label: string }> = {
  depends_on: { width: 1.5, label: "compose depends_on" },
  env: { dash: "5 4", width: 1.5, label: "environment variable" },
  proxy: { dash: "1 4", width: 2, label: "nginx route" },
  documented: { dash: "8 3 2 3", width: 1.25, label: "documented in the repo" },
  observed: { width: 2.5, label: "observed during an experiment" },
};

export function edgeKindLabel(kind: EdgeKind): string {
  return EDGE_STYLE[kind]?.label ?? kind;
}

/** Every service that transitively depends on `service` (requests flow src -> dst). */
export function dependentsOf(topology: Topology, service: string, kinds?: EdgeKind[]): string[] {
  const rev = new Map<string, string[]>();
  for (const e of topology.edges) {
    if (kinds && !kinds.includes(e.kind)) continue;
    if (!rev.has(e.dst)) rev.set(e.dst, []);
    rev.get(e.dst)!.push(e.src);
  }
  const seen = new Set<string>();
  const stack = [service];
  while (stack.length) {
    const cur = stack.pop()!;
    for (const p of rev.get(cur) ?? []) if (!seen.has(p) && p !== service) { seen.add(p); stack.push(p); }
  }
  return [...seen];
}

export function chaosServices(topology: Topology, showObservability = false): string[] {
  return Object.entries(topology.services)
    .filter(([, s]) => showObservability || s.role !== "other")
    .map(([k]) => k);
}

export function TopologyMap({
  topology, ariaLabel, kinds, states = {}, pulse = [], selected, onSelect, locked = {},
  showObservability = false, showScores = true, blastFrom = null, halo = "var(--color-bg)", height = 440, className = "",
}: TopologyMapProps) {
  const reduce = useReducedMotion();
  const uid = useId();
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(720);
  const [focus, setFocus] = useState<string | null>(null);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const ids = useMemo(() => chaosServices(topology, showObservability).sort(), [topology, showObservability]);
  const edges = useMemo(
    () => topology.edges.filter((e) => ids.includes(e.src) && ids.includes(e.dst) && (!kinds || kinds.includes(e.kind))),
    [topology, ids, kinds],
  );
  const layout = useMemo(() => {
    const roots = ids.filter((id) => topology.services[id]?.entrypoint);
    return layerGraph({ ids, edges: topology.edges.filter((e) => ids.includes(e.src) && ids.includes(e.dst)), roots });
  }, [ids, topology]);

  const maxCrit = Math.max(1, ...ids.map((id) => topology.services[id]?.criticality ?? 0));
  const padX = Math.min(90, w * 0.12);
  const padY = 44;
  const px = (x: number) => padX + x * (w - padX * 2);
  const py = (y: number) => padY + y * (height - padY * 2);
  const radius = (id: string) => {
    const c = topology.services[id]?.criticality ?? 0;
    return 9 + 17 * Math.sqrt(c / maxCrit);
  };

  const blastSet = useMemo(() => new Set(blastFrom ? [blastFrom, ...dependentsOf(topology, blastFrom, kinds)] : []), [blastFrom, topology, kinds]);

  // de-duplicate drawn edges per pair, keeping the strongest evidence for the stroke
  const drawn = useMemo(() => {
    const order: EdgeKind[] = ["observed", "depends_on", "proxy", "env", "documented"];
    const best = new Map<string, (typeof edges)[number] & { all: EdgeKind[] }>();
    for (const e of edges) {
      const k = `${e.src}->${e.dst}`;
      const cur = best.get(k);
      if (!cur) best.set(k, { ...e, all: [e.kind] });
      else {
        cur.all.push(e.kind);
        if (order.indexOf(e.kind) < order.indexOf(cur.kind)) cur.kind = e.kind;
      }
    }
    return [...best.values()];
  }, [edges]);

  const selectable = !!onSelect;
  const label = (id: string) => {
    const s = states[id];
    const parts = [id, `criticality ${topology.services[id]?.criticality ?? 0}`];
    if (s && s !== "idle") parts.push(s);
    if (locked[id]) parts.push(`locked: ${locked[id]}`);
    return parts.join(", ");
  };

  return (
    <div ref={box} className={cx("relative w-full", className)}>
      <svg width={w} height={height} viewBox={`0 0 ${w} ${height}`} role={onSelect ? "group" : "img"} aria-label={ariaLabel} className="block select-none">
        <defs>
          <marker id={`${uid}-arrow`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0L8 4L0 8z" fill="var(--color-fg-2)" />
          </marker>
          <marker id={`${uid}-arrow-fail`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0L8 4L0 8z" fill="var(--color-fail)" />
          </marker>
        </defs>
        <g fill="none">
          {drawn.map((e, i) => {
            const a = layout.get(e.src);
            const b = layout.get(e.dst);
            if (!a || !b) return null;
            const x1 = px(a.x), y1 = py(a.y), x2 = px(b.x), y2 = py(b.y);
            const ang = Math.atan2(y2 - y1, x2 - x1);
            const r1 = radius(e.src) + 4, r2 = radius(e.dst) + 6;
            const sx = x1 + Math.cos(ang) * r1, sy = y1 + Math.sin(ang) * r1;
            const ex = x2 - Math.cos(ang) * r2, ey = y2 - Math.sin(ang) * r2;
            const bend = (b.depth === a.depth ? 40 : 14) * (i % 2 ? 1 : -1);
            const mx = (sx + ex) / 2 - Math.sin(ang) * bend, my = (sy + ey) / 2 + Math.cos(ang) * bend;
            const hot = blastSet.has(e.src) && blastSet.has(e.dst);
            const st = EDGE_STYLE[e.kind];
            const dim = focus && focus !== e.src && focus !== e.dst;
            return (
              <motion.path
                key={`${e.src}-${e.dst}`}
                d={`M${sx} ${sy}Q${mx} ${my} ${ex} ${ey}`}
                stroke={hot ? "var(--color-fail)" : e.kind === "env" ? "var(--color-fg)" : "var(--color-fg-2)"}
                strokeWidth={hot ? st.width + 1 : st.width}
                strokeDasharray={st.dash}
                markerEnd={`url(#${uid}-${hot ? "arrow-fail" : "arrow"})`}
                initial={reduce ? false : { pathLength: 0, opacity: 0 }}
                animate={{ pathLength: 1, opacity: dim ? 0.15 : 1 }}
                transition={{ duration: reduce ? 0 : 0.9, delay: reduce ? 0 : 0.15 + i * 0.03, ease: [0.16, 1, 0.3, 1] }}
              />
            );
          })}
        </g>
        {ids.map((id) => {
          const p = layout.get(id);
          if (!p) return null;
          const x = px(p.x), y = py(p.y), r = radius(id);
          const st: MarkerState = states[id] ?? (blastFrom ? (id === blastFrom ? "target" : blastSet.has(id) ? "affected" : "dimmed") : "idle");
          const isSel = selected === id;
          const isLocked = !!locked[id];
          const crit = topology.services[id]?.criticality ?? 0;
          const labelRight = p.x <= 0.5;
          return (
            <g key={id}>
              {isSel && <circle cx={x} cy={y} r={r + 9} fill="none" stroke="var(--color-marker)" strokeWidth={2} strokeDasharray="4 3" />}
              <Marker cx={x} cy={y} r={r} state={st} pulse={pulse.includes(id)} />
              <text
                x={labelRight ? x + r + 8 : x - r - 8}
                y={y - 2}
                textAnchor={labelRight ? "start" : "end"}
                className="fill-fg font-mono text-[12px]"
                style={{ paintOrder: "stroke", stroke: halo, strokeWidth: 4, strokeLinejoin: "round" }}
              >
                {id}
              </text>
              {showScores && (
                <text
                  x={labelRight ? x + r + 8 : x - r - 8}
                  y={y + 13}
                  textAnchor={labelRight ? "start" : "end"}
                  className="fill-fg-2 font-mono text-[11px]"
                  style={{ paintOrder: "stroke", stroke: halo, strokeWidth: 4, strokeLinejoin: "round" }}
                >
                  {crit.toFixed(1)}{isLocked ? " locked" : ""}
                </text>
              )}
              {selectable && (
                <circle
                  cx={x} cy={y} r={Math.max(r + 6, 22)}
                  fill="transparent"
                  role="button"
                  tabIndex={0}
                  aria-label={label(id)}
                  aria-pressed={isSel}
                  aria-disabled={isLocked || undefined}
                  className={cx("outline-none", isLocked ? "cursor-not-allowed" : "cursor-pointer")}
                  onMouseEnter={() => setFocus(id)}
                  onMouseLeave={() => setFocus(null)}
                  onFocus={() => setFocus(id)}
                  onBlur={() => setFocus(null)}
                  onClick={() => !isLocked && onSelect?.(id)}
                  onKeyDown={(ev) => {
                    if ((ev.key === "Enter" || ev.key === " ") && !isLocked) {
                      ev.preventDefault();
                      onSelect?.(id);
                    }
                  }}
                />
              )}
              {selectable && focus === id && (
                <circle cx={x} cy={y} r={r + 5} fill="none" stroke="var(--color-fg)" strokeWidth={1.5} pointerEvents="none" />
              )}
            </g>
          );
        })}
      </svg>
      <ul className="sr-only">
        {ids.map((id) => {
          const out = edges.filter((e) => e.src === id).map((e) => `${e.dst} (${edgeKindLabel(e.kind)})`);
          return <li key={id}>{`${label(id)}. Depends on: ${out.length ? out.join(", ") : "nothing"}.`}</li>;
        })}
      </ul>
    </div>
  );
}

export function EdgeLegend({ kinds, className = "" }: { kinds: EdgeKind[]; className?: string }) {
  return (
    <ul className={cx("flex flex-wrap gap-x-5 gap-y-2 text-[12px] text-fg-2", className)}>
      {kinds.map((k) => (
        <li key={k} className="flex items-center gap-2">
          <svg width="28" height="8" aria-hidden="true">
            <line x1="0" y1="4" x2="28" y2="4" stroke={k === "env" ? "var(--color-fg)" : "var(--color-fg-2)"} strokeWidth={EDGE_STYLE[k].width} strokeDasharray={EDGE_STYLE[k].dash} />
          </svg>
          {EDGE_STYLE[k].label}
        </li>
      ))}
    </ul>
  );
}
