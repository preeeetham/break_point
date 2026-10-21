import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties } from "react";
import {
  animate, motion, useMotionValue, useMotionValueEvent, useReducedMotion, useTransform,
  type AnimationPlaybackControls,
} from "motion/react";
import { ArrowCounterClockwise } from "@phosphor-icons/react";
import type { EdgeKind, Topology } from "../lib/types";
import { useCatalog, useConnection, useFState } from "../lib/store";
import { timecode } from "../lib/format";
import { announce } from "../lib/a11y";
import { TopologyMap, chaosServices, dependentsOf, edgeKindLabel } from "../components/TopologyMap";
import { Marker, type MarkerState } from "../components/Marker";
import { Button, cx } from "../components/ui";
import { CompactMap } from "./CompactMap";
import { useWorkflow } from "../lib/workflow";

/** Seconds of playback per hop. A playback pace for the animation, not a measured propagation time. */
const HOP_S = 0.7;

const KIND_ORDER: EdgeKind[] = ["observed", "depends_on", "proxy", "env", "documented"];
const KIND_SHORT: Record<EdgeKind, string> = {
  depends_on: "depends_on",
  env: "env var",
  proxy: "nginx route",
  documented: "docs",
  observed: "observed",
};

export interface Hit {
  service: string;
  hop: number;
  via: string;
  kind: EdgeKind;
}

/** Hop distance for every transitive dependent of `target`. The set is exactly dependentsOf();
 *  the breadth-first pass only adds how many edges away each one is and the strongest evidence. */
export function impactOf(topology: Topology, target: string): Hit[] {
  const members = new Set(dependentsOf(topology, target));
  const out: Hit[] = [];
  const seen = new Set([target]);
  let frontier = new Set([target]);
  let hop = 0;
  while (frontier.size) {
    hop += 1;
    const next = new Map<string, Hit>();
    for (const e of topology.edges) {
      if (!frontier.has(e.dst) || seen.has(e.src) || !members.has(e.src)) continue;
      const cur = next.get(e.src);
      if (!cur || KIND_ORDER.indexOf(e.kind) < KIND_ORDER.indexOf(cur.kind)) next.set(e.src, { service: e.src, hop, via: e.dst, kind: e.kind });
    }
    for (const [k, v] of next) {
      seen.add(k);
      out.push(v);
    }
    frontier = new Set(next.keys());
  }
  return out.sort((a, b) => a.hop - b.hop || a.service.localeCompare(b.service));
}

function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (cb) => {
      const m = window.matchMedia(query);
      m.addEventListener("change", cb);
      return () => m.removeEventListener("change", cb);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

const MAP_LABEL = "Eclipse Ditto services. Activate a service to simulate taking it down.";

// Labels on the map knock out the grid with a halo in --color-bg; inside the sheet that halo should be the sheet.
const SHEET_HALO = { "--color-bg": "var(--color-sheet)" } as CSSProperties;

export function Rig() {
  const s = useFState();
  if (!s) return <RigFrame status="connecting" />;
  if (!s.topology) return <RigFrame status="empty" />;
  return <LiveRig topology={s.topology} />;
}

function RigFrame({ status }: { status: "connecting" | "empty" }) {
  const { go } = useWorkflow();
  return (
    <div className="flex min-w-0 flex-col rounded-[2px] border border-line bg-sheet" aria-busy={status === "connecting"}>
      <RigHeader meta={status === "connecting" ? "connecting" : "no map"} />
      <div className="mm-grid flex h-[380px] flex-col items-start justify-end gap-3 p-5 xl:h-[440px]">
        {status === "connecting" ? (
          <p className="text-[14px] text-fg-2">Reading the system map from the Faultline server…</p>
        ) : (
          <>
            <p className="max-w-[40ch] text-[15px] text-fg">No map yet. Map the system to draw the rig, then come back and hit a service.</p>
            <Button variant="ghost" onClick={() => go("map")}>Map the system</Button>
          </>
        )}
      </div>
      <RigFooter />
    </div>
  );
}

function RigHeader({ meta }: { meta: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-line px-4 py-2.5">
      <p className="label-condensed text-[14px] text-fg">Test rig: Eclipse Ditto</p>
      <p className="font-mono text-[11px] text-fg-2 tabular">{meta}</p>
    </div>
  );
}

function RigFooter() {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-t border-line px-4 py-3">
      <p className="text-[13px] font-medium text-fg">Simulated in your browser. Nothing was broken.</p>
      <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-fg-2" aria-label="Marker key">
        <LegendItem state="idle" label="service, sized by criticality" />
        <LegendItem state="target" label="hit" />
        <LegendItem state="affected" label="loses its path" />
      </ul>
    </div>
  );
}

function LegendItem({ state, label }: { state: MarkerState; label: string }) {
  return (
    <li className="flex items-center gap-1.5">
      <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
        <Marker cx={8} cy={8} r={5.5} state={state} />
      </svg>
      {label}
    </li>
  );
}

function LiveRig({ topology }: { topology: Topology }) {
  const reduce = useReducedMotion();
  const catalog = useCatalog();
  const conn = useConnection();
  const wide = useMedia("(min-width: 1280px)");
  // The shared map labels beside each marker, which needs about 680 px; narrower rigs use CompactMap.
  const mapBox = useRef<HTMLDivElement>(null);
  const [mapW, setMapW] = useState(0);
  useLayoutEffect(() => {
    const el = mapBox.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setMapW(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const [target, setTarget] = useState<string | null>(null);
  const [reached, setReached] = useState(0);
  const [done, setDone] = useState(false);
  const clock = useMotionValue(0);
  const tc = useTransform(clock, (v) => timecode(v));
  const ctl = useRef<AnimationPlaybackControls | null>(null);
  // the invitation waits for the rig to draw itself only on first load, not after a reset
  const [intro, setIntro] = useState(true);

  const ids = useMemo(() => chaosServices(topology), [topology]);
  const scope = useMemo(() => Object.entries(topology.services).filter(([, v]) => v.chaos_relevant).map(([k]) => k), [topology]);
  const suggested = useMemo(
    () => [...ids].sort((a, b) => (topology.services[b]?.criticality ?? 0) - (topology.services[a]?.criticality ?? 0))[0] ?? null,
    [ids, topology],
  );
  const hits = useMemo(() => (target ? impactOf(topology, target) : []), [topology, target]);
  const maxHop = hits.length ? hits[hits.length - 1].hop : 0;

  useMotionValueEvent(clock, "change", (v) => setReached(Math.min(maxHop, Math.floor(v / HOP_S + 1e-6))));
  useEffect(() => () => ctl.current?.stop(), []);

  const hit = useCallback(
    (id: string) => {
      ctl.current?.stop();
      setIntro(false);
      const h = impactOf(topology, id);
      const last = h.length ? h[h.length - 1].hop : 0;
      const inScope = h.filter((x) => scope.includes(x.service)).length;
      const others = scope.filter((x) => x !== id).length;
      setTarget(id);
      setDone(false);
      setReached(0);
      clock.jump(0);
      const finish = () => {
        setReached(last);
        setDone(true);
        announce(`Simulated: ${inScope} of ${others} services in the test scope lose their path to ${id}. Nothing was broken.`);
      };
      if (reduce || last === 0) {
        clock.jump(last * HOP_S);
        finish();
        return;
      }
      ctl.current = animate(clock, last * HOP_S, { duration: last * HOP_S, ease: "linear", onComplete: finish });
    },
    [topology, scope, clock, reduce],
  );

  const reset = useCallback(() => {
    ctl.current?.stop();
    clock.jump(0);
    setTarget(null);
    setReached(0);
    setDone(false);
  }, [clock]);

  const hitMap = useMemo(() => new Map(hits.map((h) => [h.service, h])), [hits]);
  const states = useMemo(() => {
    const out: Record<string, MarkerState> = {};
    for (const id of ids) {
      if (!target) out[id] = "idle";
      else if (id === target) out[id] = "target";
      else {
        const h = hitMap.get(id);
        out[id] = h && h.hop <= reached ? "affected" : done ? "dimmed" : "idle";
      }
    }
    return out;
  }, [ids, target, hitMap, reached, done]);

  const pulse = reduce ? [] : !target && suggested ? [suggested] : target && !done ? [target] : [];
  const others = target ? scope.filter((x) => x !== target).length : scope.length;
  const lost = hits.filter((h) => h.hop <= reached && scope.includes(h.service)).length;
  const svc = target ? topology.services[target] : null;
  const prot = target ? catalog?.protected?.[target] : undefined;

  return (
    <motion.div
      className="flex min-w-0 flex-col rounded-[2px] border border-line bg-sheet"
      initial={reduce ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
    >
      <RigHeader meta={`${ids.length} services, ${conn === "live" ? "live map" : "replay map"}`} />
      <div className="grid min-w-0 xl:grid-cols-[minmax(0,1fr)_17.5rem]">
        <div ref={mapBox} className="mm-grid min-w-0" style={SHEET_HALO}>
          {mapW === 0 ? (
            <div style={{ height: wide ? 440 : 400 }} />
          ) : mapW < 680 ? (
            <CompactMap
              topology={topology}
              ariaLabel={MAP_LABEL}
              states={states}
              pulse={pulse}
              onSelect={hit}
              blastFrom={done ? target : null}
              height={400}
              halo="var(--color-sheet)"
            />
          ) : (
            <TopologyMap
              topology={topology}
              ariaLabel={MAP_LABEL}
              states={states}
              pulse={pulse}
              onSelect={hit}
              blastFrom={done ? target : null}
              height={wide ? 440 : 400}
            />
          )}
        </div>
        <div className="flex min-w-0 flex-col gap-3.5 border-t border-line p-4 xl:border-l xl:border-t-0">
          {!target ? (
            <motion.div
              className="flex flex-col gap-3"
              initial={reduce ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.3, delay: reduce || !intro ? 0 : 1.1 }}
            >
              <p className="display text-[22px] text-fg">Hit a service.</p>
              <p className="text-[14px] leading-snug text-fg-2">
                Pick any marker to see what loses its path when that service goes down.
                {suggested && <> Start with <span className="font-mono text-[13px] text-fg">{suggested}</span>, the largest.</>}
              </p>
              {suggested && (
                <Button variant="ghost" className="self-start" onClick={() => hit(suggested)}>
                  Hit {suggested}
                </Button>
              )}
            </motion.div>
          ) : (
            <>
              <div className="flex items-baseline justify-between gap-3 font-mono text-[12px] tabular">
                <span className="text-fg">{target} down</span>
                <motion.span className="text-fg-2" aria-hidden="true">{tc}</motion.span>
              </div>
              <div>
                <p className="font-mono text-[48px] leading-none tabular text-fg">
                  {lost}
                  <span className="text-[20px] text-fg-2"> of {others}</span>
                </p>
                <p className="mt-2 text-[14px] leading-snug text-fg-2">
                  services in the test scope lose their path to <span className="font-mono text-[13px] text-fg">{target}</span>.
                </p>
              </div>
              {maxHop > 0 && (
                <ol className="flex flex-col gap-2.5 border-t border-line pt-3" aria-label="How the failure travels">
                  {Array.from({ length: maxHop }, (_, i) => i + 1)
                    .filter((k) => k <= reached)
                    .map((k) => (
                      <motion.li
                        key={`${target}-${k}`}
                        className="grid grid-cols-[3.25rem_minmax(0,1fr)] gap-2"
                        initial={reduce ? false : { opacity: 0 }}
                        animate={{ opacity: 1 }}
                        transition={{ duration: 0.2 }}
                      >
                        <span className="font-mono text-[11px] leading-5 text-fg-2 tabular">hop {k}</span>
                        <ul className="flex flex-col">
                          {hits.filter((h) => h.hop === k).map((h) => (
                            <li key={h.service} className="flex items-baseline justify-between gap-2 font-mono text-[12px] leading-5">
                              <span className="truncate text-fg">{h.service}</span>
                              <span className="shrink-0 text-fg-2" title={`via ${h.via}, ${edgeKindLabel(h.kind)}`}>{KIND_SHORT[h.kind]}</span>
                            </li>
                          ))}
                        </ul>
                      </motion.li>
                    ))}
                </ol>
              )}
              {done && (
                <div className="flex flex-col gap-2 text-[13px] leading-snug text-fg-2">
                  {hits.length === 0 && svc?.entrypoint && (
                    <p>Nothing inside depends on {target}, but every outside request enters through it.</p>
                  )}
                  {hits.length === 0 && !svc?.entrypoint && <p>Nothing in the map depends on {target}.</p>}
                  {svc && !svc.chaos_relevant && <p>{target} is outside the test scope, so a real run never targets it.</p>}
                  {prot && <p>A real test never takes {target} down: it is protected, degrade faults only.</p>}
                </div>
              )}
              <div className={cx("mt-auto", !done && "invisible")}>
                <Button variant="ghost" size="sm" className="min-h-11" onClick={reset}>
                  <ArrowCounterClockwise size={15} aria-hidden="true" /> Reset the rig
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
      <RigFooter />
    </motion.div>
  );
}
