import { useEffect, useState, type ReactNode } from "react";
import type { Catalog, Thresholds } from "../lib/types";
import { useCatalog, useConnection } from "../lib/store";
import { Container, cx } from "../components/ui";

/** config/thresholds.yaml abort.p95_latency_factor_of_baseline. /api/catalog exposes the resulting
 *  limit (abort_p95_ms_above = max(floor, factor x baseline p95)) but not the factor itself. */
const P95_FACTOR = 5;

type Source = "live" | "replay";

/** Thresholds from /api/catalog; in replay the store already loads the replay catalog. If the live
 *  catalog never arrives, read the replay copy rather than print a number from memory. */
function useThresholds(): { t: Thresholds | null; source: Source | null } {
  const catalog = useCatalog();
  const conn = useConnection();
  const [fallback, setFallback] = useState<Thresholds | null>(null);

  useEffect(() => {
    if (catalog || fallback || conn === "connecting") return;
    let alive = true;
    const timer = window.setTimeout(() => {
      fetch(`${import.meta.env.BASE_URL}replay/catalog.json`, { cache: "no-store" })
        .then((r) => (r.ok ? (r.json() as Promise<Catalog>) : null))
        .then((c) => alive && c && setFallback(c.thresholds))
        .catch(() => {});
    }, 3000);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [catalog, fallback, conn]);

  if (catalog) return { t: catalog.thresholds, source: conn === "live" ? "live" : "replay" };
  if (fallback) return { t: fallback, source: "replay" };
  return { t: null, source: null };
}

const num = (v: number, digits = 2) => (Number.isInteger(v) ? String(v) : v.toFixed(digits).replace(/\.?0+$/, ""));

export function Barrier() {
  const { t, source } = useThresholds();
  return (
    <section id="the-barrier" aria-labelledby="barrier-title" className="border-t border-line">
      <Container className="py-20 md:py-28">
        <h2 id="barrier-title" className="display max-w-[22ch] text-balance text-[clamp(2rem,4.2vw,3.5rem)] text-fg">
          Hard limits, set in config, checked every second.
        </h2>
        <p className="mt-5 max-w-[56ch] text-[17px] leading-relaxed text-fg">
          The model never sees these limits and cannot change them.
        </p>

        <dl className="mt-12 grid border-t-[3px] border-fg sm:grid-cols-2 xl:grid-cols-4" aria-busy={!t}>
          <Limit
            index={0}
            title="Error rate"
            value={t ? num(t.abort_error_rate_above * 100) : null}
            unit="%"
            caption={t ? <>over the last {t.trailing_samples} requests aborts the test and rolls the fault back.</> : null}
          />
          <Limit
            index={1}
            title="p95 latency"
            value={t ? num(t.abort_p95_ms_above) : null}
            unit="ms"
            caption={t ? <>aborts the test. The limit is the larger of a floor and {P95_FACTOR} times the baseline p95.</> : null}
            reading={t?.baseline_p95_ms != null ? <P95Reading t={t} /> : null}
          />
          <Limit
            index={2}
            title="Other services"
            value={t ? String(t.non_target_health_failures_in_a_row) : null}
            unit="in a row"
            caption={t ? <>failed health checks on any service outside the test abort it.</> : null}
          />
          <Limit
            index={3}
            title="Time"
            value={t ? String(t.max_experiment_seconds) : null}
            unit="s"
            caption={t ? <>is the longest any experiment runs, whatever the plan asks for.</> : null}
            reading={t ? <>watchdog: fires at {t.max_experiment_seconds + t.watchdog_grace_seconds}&nbsp;s ({t.max_experiment_seconds} + {t.watchdog_grace_seconds}&nbsp;s grace) even if the main loop hangs</> : null}
          />
        </dl>

        <div className="grid border-t border-line md:grid-cols-2">
          <div className="border-line py-7 md:border-r md:pr-8">
            <p className="display flex min-h-10 items-center text-[22px] text-fg">The Guardian</p>
            <p className="mt-2 max-w-[48ch] text-[15px] leading-relaxed text-fg-2">
              A second, independent kill switch. A separate process with its own Docker client restores containers on the deadline or on a stop request.
            </p>
          </div>
          <div className="border-t border-line py-7 md:border-t-0 md:pl-8">
            <p className="flex min-h-10 items-center gap-3 text-fg">
              <kbd className="inline-flex min-h-9 min-w-12 items-center justify-center rounded-[2px] border border-fg border-b-[3px] px-2 font-mono text-[14px] text-fg">Esc</kbd>
              <span className="display text-[22px]">Stop everything</span>
            </p>
            <p className="mt-2 max-w-[48ch] text-[15px] leading-relaxed text-fg-2">
              Press Esc, or the Stop everything button in the workflow header, at any time during a run. The fault is rolled back.
            </p>
          </div>
        </div>

        <p className="mt-2 border-t border-line pt-4 font-mono text-[12px] text-fg-2">
          {source === "live" && "Read from config/thresholds.yaml through /api/catalog."}
          {source === "replay" && "Read from the replay copy of /api/catalog. The Faultline server is not reachable."}
          {!source && "Reading the limits from /api/catalog…"}
        </p>
      </Container>
    </section>
  );
}

function P95Reading({ t }: { t: Thresholds }) {
  const b = t.baseline_p95_ms!;
  const scaled = b * P95_FACTOR;
  const floorHolds = t.abort_p95_ms_above > scaled + 0.5;
  return (
    <>
      baseline p95 {num(b)}&nbsp;ms, ×{P95_FACTOR} = {num(scaled, 1)}&nbsp;ms{floorHolds ? ", so the floor holds" : ", above the floor"}
    </>
  );
}

/** Rules between the four readings: stacked on phones, 2 by 2 from 640 px, one row from 1280 px. */
function cellRules(i: number): string {
  return cx(
    i < 3 && "border-b",
    i % 2 === 0 ? "sm:pr-6" : "sm:border-l sm:pl-6",
    i >= 2 && "sm:border-b-0",
    "xl:border-b-0",
    i > 0 && "xl:border-l xl:pl-6",
  );
}

function Limit({ index, title, value, unit, caption, reading }: { index: number; title: string; value: string | null; unit: string; caption: ReactNode; reading?: ReactNode }) {
  return (
    <div className={cx("flex flex-col border-line py-7", cellRules(index))}>
      <dt className="label-condensed text-[15px] text-fg">{title}</dt>
      <dd className="mt-4 font-mono leading-none text-fg tabular">
        {value != null ? (
          <>
            <span className="text-[clamp(3.25rem,5.6vw,5.25rem)] tracking-tight">{value}</span>
            <span className="ml-2 text-[clamp(1.1rem,1.6vw,1.5rem)] text-fg-2">{unit}</span>
          </>
        ) : (
          <span className="block h-[clamp(3.25rem,5.6vw,5.25rem)] w-32 rounded-[2px] bg-grid" />
        )}
      </dd>
      {caption && <dd className="mt-4 max-w-[30ch] text-[15px] leading-snug text-fg-2">{caption}</dd>}
      {reading && <dd className="mt-3 max-w-[34ch] font-mono text-[12px] leading-snug text-fg-2 tabular">{reading}</dd>}
    </div>
  );
}
