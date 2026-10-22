import { useMemo } from "react";
import type { PredictedWeakness, Topology } from "../../lib/types";
import { TopologyMap } from "../../components/TopologyMap";
import { Marker, type MarkerState } from "../../components/Marker";
import { MapFrame } from "./load-shared";

function Key({ state, children }: { state: MarkerState; children: string }) {
  return (
    <li className="flex items-center gap-2">
      <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" className="shrink-0">
        <Marker cx={11} cy={11} r={7} state={state} />
      </svg>
      {children}
    </li>
  );
}

/** Where the selected guess points: its services ringed as predicted, the suggested target ringed as the target. */
export function PredictMap({ topology, weakness }: { topology: Topology | null; weakness: PredictedWeakness | null }) {
  const target = weakness?.suggested_experiment?.target_service ?? null;
  const states = useMemo(() => {
    if (!topology || !weakness) return {};
    const out: Record<string, MarkerState> = {};
    for (const id of Object.keys(topology.services)) out[id] = "dimmed";
    for (const id of weakness.services) out[id] = "predicted";
    if (target) out[target] = "target";
    return out;
  }, [topology, weakness, target]);
  const observability = !!topology && !!weakness && [...weakness.services, target].some((id) => id && topology.services[id]?.role === "other");

  if (!topology) return <p className="text-[13px] text-fg-2">No map on disk yet. Map the system to see where this guess points.</p>;
  return (
    <figure className="flex flex-col gap-2 border-y border-line py-3">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <span className="label-condensed text-[13px] text-fg">Where the guess points</span>
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-fg-2">
          <Key state="predicted">predicted weak, not measured</Key>
          <Key state="target">suggested injection target</Key>
        </ul>
      </figcaption>
      <MapFrame>
        <TopologyMap
          topology={topology}
          halo="var(--color-sheet)"
          states={states}
          showObservability={observability}
          ariaLabel={weakness ? `Map with ${weakness.services.join(", ")} marked as predicted weak${target ? ` and ${target} as the suggested test target` : ""}` : "Dependency map"}
          height={320}
          showScores={false}
        />
      </MapFrame>
      {weakness && target && !weakness.services.includes(target) && (
        <p className="text-[12px] leading-snug text-fg-2">
          The target is not the weak service: to test <span className="font-mono text-fg">{weakness.services.join(", ")}</span>, the experiment breaks what it depends on.
        </p>
      )}
    </figure>
  );
}
