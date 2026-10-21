import { useMemo } from "react";
import { motion } from "motion/react";
import type { EdgeKind, Topology } from "../../lib/types";
import { chaosServices, dependentsOf } from "../../components/TopologyMap";
import { cx } from "../../components/ui";
import { LAYERS } from "./map-util";


/** Evidence layers as toggles. Each one redraws the map and re-ranks the ledger. */
export function LayerToggles({ topology, kinds, onChange }: { topology: Topology; kinds: EdgeKind[]; onChange: (k: EdgeKind[]) => void }) {
  const counts = useMemo(() => {
    const c: Partial<Record<EdgeKind, number>> = {};
    for (const e of topology.edges) c[e.kind] = (c[e.kind] ?? 0) + 1;
    return c;
  }, [topology]);
  const all = LAYERS.every((l) => kinds.includes(l.kind));
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-[13px] text-fg-2">Evidence shown. Start with what compose declares, then add the rest.</p>
        <button
          type="button"
          onClick={() => onChange(all ? ["depends_on"] : LAYERS.map((l) => l.kind))}
          className="min-h-11 text-[13px] text-fg underline decoration-line underline-offset-4 hover:decoration-fg"
        >
          {all ? "Compose only" : "Show all evidence"}
        </button>
      </div>
      <div role="group" aria-label="Evidence layers" className="grid grid-cols-2 gap-1.5 md:grid-cols-4">
        {LAYERS.map((l) => {
          const on = kinds.includes(l.kind);
          const n = counts[l.kind] ?? 0;
          return (
            <button
              key={l.kind}
              type="button"
              aria-pressed={on}
              onClick={() => onChange(on ? kinds.filter((k) => k !== l.kind) : [...kinds, l.kind])}
              className={cx(
                "flex min-h-11 flex-col items-start justify-center gap-1 rounded-[2px] border px-2.5 py-2 text-left transition-colors",
                on ? "border-fg bg-bg text-fg" : "border-line text-fg-2 hover:border-fg-2 hover:text-fg",
              )}
            >
              <span className="flex w-full items-center gap-2">
                <svg width="22" height="8" aria-hidden="true" className="shrink-0">
                  <line x1="0" y1="4" x2="22" y2="4" stroke={on ? (l.strong ? "var(--color-fg)" : "var(--color-fg-2)") : "var(--color-line)"} strokeWidth={l.width + 0.5} strokeDasharray={l.dash} />
                </svg>
                <span className="font-mono text-[11px] tabular">{n} {n === 1 ? "edge" : "edges"}</span>
                <span className={cx("ml-auto font-mono text-[11px]", on ? "text-fg" : "text-fg-2")}>{on ? "on" : "off"}</span>
              </span>
              <span className="text-[13px] leading-tight">{l.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

interface Row {
  id: string;
  deps: string[];
  base: number;
}

/**
 * Services ranked by how many others would lose their path to the API if they failed, computed from
 * only the layers switched on. Rows reorder in place, so adding a layer shows what it changed.
 */
export function PathLedger({ topology, kinds, selected, onSelect }: { topology: Topology; kinds: EdgeKind[]; selected: string | null; onSelect: (id: string) => void }) {
  const ids = useMemo(() => chaosServices(topology), [topology]);
  const crit = useMemo(() => new Map(topology.criticality.map((c) => [c.service, c.score])), [topology]);
  const rows: Row[] = useMemo(() => {
    const inSet = (d: string) => ids.includes(d);
    return ids
      .map((id) => ({
        id,
        deps: dependentsOf(topology, id, kinds).filter(inSet).sort(),
        base: dependentsOf(topology, id, ["depends_on"]).filter(inSet).length,
      }))
      .sort((a, b) => b.deps.length - a.deps.length || (crit.get(b.id) ?? 0) - (crit.get(a.id) ?? 0) || a.id.localeCompare(b.id));
  }, [ids, topology, kinds, crit]);
  const max = Math.max(1, ids.length - 1);
  const composeOnly = kinds.length === 1 && kinds[0] === "depends_on";
  const anyMoved = !composeOnly && rows.some((r) => r.deps.length !== r.base);

  return (
    <div className="flex min-w-0 flex-col gap-2.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5">
        <h4 className="label-condensed text-[14px] text-fg">If it failed, who loses their path</h4>
        <p className="text-[12px] text-fg-2">Transitive dependents under the evidence shown, of {max} other services</p>
      </div>
      <ol aria-label="Services ranked by dependents" className="grid grid-cols-1 gap-px border border-line bg-line min-[440px]:grid-cols-2 md:grid-cols-4">
        {rows.map((r, i) => {
          const sel = selected === r.id;
          const moved = !composeOnly && r.deps.length !== r.base;
          return (
            <motion.li key={r.id} layout="position" transition={{ type: "spring", stiffness: 300, damping: 30 }} className="min-w-0 bg-sheet">
              <button
                type="button"
                aria-pressed={sel}
                onClick={() => onSelect(r.id)}
                aria-label={`Rank ${i + 1}, ${r.id}: ${r.deps.length} of ${max} services would lose their path${moved ? `, ${r.base} from compose alone` : ""}`}
                className={cx("flex min-h-11 w-full flex-col gap-1.5 px-2.5 py-2 text-left", sel ? "bg-bg" : "hover:bg-bg/60")}
              >
                <span className="flex w-full items-baseline gap-2">
                  <span className="font-mono text-[11px] tabular text-fg-2">{i + 1}</span>
                  <span className={cx("min-w-0 truncate font-mono text-[13px] text-fg", sel && "underline decoration-fg decoration-2 underline-offset-4")}>{r.id}</span>
                  <span className="ml-auto font-mono text-[18px] leading-none tabular text-fg">{r.deps.length}</span>
                </span>
                <span aria-hidden="true" className="relative block h-1.5 w-full">
                  <span className="absolute inset-y-0 left-0 w-px bg-fg-2/50" />
                  <motion.span
                    className="absolute inset-y-0 left-0 w-full origin-left bg-fg"
                    initial={false}
                    animate={{ scaleX: r.deps.length / max }}
                    transition={{ type: "spring", stiffness: 260, damping: 30 }}
                  />
                </span>
                {anyMoved && (moved ? <span className="text-[11px] leading-tight text-fg-2">compose alone: {r.base}</span> : <span aria-hidden="true" className="hidden text-[11px] leading-tight min-[440px]:block">{"\u00a0"}</span>)}
              </button>
            </motion.li>
          );
        })}
      </ol>
    </div>
  );
}
