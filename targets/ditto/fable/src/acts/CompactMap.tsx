import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import type { EdgeKind, Topology } from "../lib/types";
import { layerGraph } from "../lib/layout";
import { chaosServices, dependentsOf, edgeKindLabel } from "../components/TopologyMap";
import { Marker, type MarkerState } from "../components/Marker";

/** Phone-width variant of components/TopologyMap for the hero rig. Same layout (lib/layout), markers,
 *  edge styles and blast rule; labels sit under each marker instead of beside it, because at 390 px
 *  two markers in one layer leave no room for side labels. */

const EDGE: Record<EdgeKind, { dash?: string; width: number }> = {
  depends_on: { width: 1.5 },
  env: { dash: "5 4", width: 1.5 },
  proxy: { dash: "1 4", width: 2 },
  documented: { dash: "8 3 2 3", width: 1.25 },
  observed: { width: 2.5 },
};
const ORDER: EdgeKind[] = ["observed", "depends_on", "proxy", "env", "documented"];

interface Props {
  topology: Topology;
  ariaLabel: string;
  states: Record<string, MarkerState>;
  pulse: string[];
  onSelect: (id: string) => void;
  blastFrom: string | null;
  height?: number;
  /** CSS color for the outline that lifts labels off the surface beneath the map. */
  halo?: string;
}

export function CompactMap({ topology, ariaLabel, states, pulse, onSelect, blastFrom, height = 400, halo = "var(--color-bg)" }: Props) {
  const reduce = useReducedMotion();
  const uid = useId();
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(340);
  const [focus, setFocus] = useState<string | null>(null);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(260, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const ids = useMemo(() => chaosServices(topology).sort(), [topology]);
  const inner = useMemo(() => topology.edges.filter((e) => ids.includes(e.src) && ids.includes(e.dst)), [topology, ids]);
  const layout = useMemo(
    () => layerGraph({ ids, edges: inner, roots: ids.filter((id) => topology.services[id]?.entrypoint) }),
    [ids, inner, topology],
  );
  const drawn = useMemo(() => {
    const best = new Map<string, (typeof inner)[number]>();
    for (const e of inner) {
      const k = `${e.src}->${e.dst}`;
      const cur = best.get(k);
      if (!cur || ORDER.indexOf(e.kind) < ORDER.indexOf(cur.kind)) best.set(k, e);
    }
    return [...best.values()];
  }, [inner]);
  const blast = useMemo(() => new Set(blastFrom ? [blastFrom, ...dependentsOf(topology, blastFrom)] : []), [blastFrom, topology]);

  const maxCrit = Math.max(1, ...ids.map((id) => topology.services[id]?.criticality ?? 0));
  const radius = (id: string) => 7 + 13 * Math.sqrt((topology.services[id]?.criticality ?? 0) / maxCrit);
  const padX = 44;
  const padTop = 30;
  const padBottom = 44;
  const px = (x: number) => padX + x * (w - padX * 2);
  const py = (y: number) => padTop + y * (height - padTop - padBottom);

  return (
    <div ref={box} className="relative w-full">
      <svg width={w} height={height} viewBox={`0 0 ${w} ${height}`} role="group" aria-label={ariaLabel} className="block select-none">
        <defs>
          <marker id={`${uid}-a`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0 0L8 4L0 8z" fill="var(--color-fg-2)" />
          </marker>
          <marker id={`${uid}-f`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
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
            const r1 = radius(e.src) + 3, r2 = radius(e.dst) + 5;
            const sx = x1 + Math.cos(ang) * r1, sy = y1 + Math.sin(ang) * r1;
            const ex = x2 - Math.cos(ang) * r2, ey = y2 - Math.sin(ang) * r2;
            const bend = (b.depth === a.depth ? 28 : 10) * (i % 2 ? 1 : -1);
            const mx = (sx + ex) / 2 - Math.sin(ang) * bend, my = (sy + ey) / 2 + Math.cos(ang) * bend;
            const hot = blast.has(e.src) && blast.has(e.dst);
            const st = EDGE[e.kind];
            const dim = focus && focus !== e.src && focus !== e.dst;
            return (
              <motion.path
                key={`${e.src}-${e.dst}`}
                d={`M${sx} ${sy}Q${mx} ${my} ${ex} ${ey}`}
                stroke={hot ? "var(--color-fail)" : e.kind === "env" ? "var(--color-fg)" : "var(--color-fg-2)"}
                strokeWidth={hot ? st.width + 1 : st.width}
                strokeDasharray={st.dash}
                markerEnd={`url(#${uid}-${hot ? "f" : "a"})`}
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
          const crit = topology.services[id]?.criticality ?? 0;
          const st = states[id] ?? "idle";
          return (
            <g key={id}>
              <Marker cx={x} cy={y} r={r} state={st} pulse={pulse.includes(id)} />
              <text
                aria-hidden="true"
                x={x}
                y={y + r + 13}
                textAnchor="middle"
                className="fill-fg font-mono text-[11px]"
                opacity={st === "dimmed" ? 0.45 : 1}
                style={{ paintOrder: "stroke", stroke: halo, strokeWidth: 4, strokeLinejoin: "round" }}
              >
                {id} <tspan className="fill-fg-2">{crit.toFixed(1)}</tspan>
              </text>
              <circle
                cx={x}
                cy={y}
                r={Math.max(r + 6, 22)}
                fill="transparent"
                role="button"
                tabIndex={0}
                aria-label={`${id}, criticality ${crit}${st !== "idle" ? `, ${st}` : ""}`}
                className="cursor-pointer outline-none"
                onMouseEnter={() => setFocus(id)}
                onMouseLeave={() => setFocus(null)}
                onFocus={() => setFocus(id)}
                onBlur={() => setFocus(null)}
                onClick={() => onSelect(id)}
                onKeyDown={(ev) => {
                  if (ev.key === "Enter" || ev.key === " ") {
                    ev.preventDefault();
                    onSelect(id);
                  }
                }}
              />
              {focus === id && <circle cx={x} cy={y} r={r + 5} fill="none" stroke="var(--color-fg)" strokeWidth={1.5} pointerEvents="none" />}
            </g>
          );
        })}
      </svg>
      <ul className="sr-only">
        {ids.map((id) => {
          const out = inner.filter((e) => e.src === id).map((e) => `${e.dst} (${edgeKindLabel(e.kind)})`);
          return <li key={id}>{`${id}. Depends on: ${out.length ? out.join(", ") : "nothing"}.`}</li>;
        })}
      </ul>
    </div>
  );
}
