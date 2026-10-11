import { Instrument, Station } from "../components/Station";
import { StripChart } from "../components/StripChart";
import { Readout } from "../components/ui";
import { useCatalog, useFState } from "../lib/store";
import { clean, fmtPct, fmtS, relTime } from "../lib/format";
import { CaptureControl, ProbeControl } from "./parts/baseline-controls";
import { HealthAvailability, LimitsList } from "./parts/baseline-limits";

export function BaselineStation() {
  const s = useFState();
  const rps = s?.session.probe.rps;
  return (
    <Station
      id="baseline"
      headline="Measure calm before you cause a jolt"
      lede={
        <p>
          A synthetic client calls the Ditto API through nginx while health checks watch every service. The abort limits for every
          experiment are derived from this calm window.
        </p>
      }
      code={`Measures success and latency from a synthetic client${rps != null ? ` at about ${rps} requests a second` : ""}, then derives the abort limits from that window.`}
      controls={
        <>
          <ProbeControl />
          <hr className="border-0 border-t border-line" />
          <CaptureControl />
        </>
      }
      instrument={<BaselineInstrument />}
    />
  );
}

function ms(v: number | null | undefined): string {
  if (v == null) return "n/a";
  return v < 100 ? v.toFixed(1) : String(Math.round(v));
}

function BaselineInstrument() {
  const s = useFState();
  const catalog = useCatalog();
  const b = s?.baseline ?? null;
  const t = b?.thresholds ?? catalog?.thresholds ?? null;
  const points = s?.telemetry.points ?? [];
  const last = [...points].reverse().find((p) => p.success != null);
  const armed = !!s?.session.armed;

  return (
    <div className="flex flex-col gap-6">
      <Instrument
        title="Live probe trace"
        meta={armed ? (last ? `now ${fmtPct(last.success)}, last 300 s` : "armed, waiting for the first sample") : "probes off"}
        grid
      >
        <StripChart
          points={points}
          bands={s?.telemetry.phases ?? []}
          now={s?.now}
          window={300}
          successFloor={t ? 1 - t.abort_error_rate_above : null}
          p95Ceiling={t?.abort_p95_ms_above ?? null}
          ariaLabel="API success rate and p95 latency from the synthetic probe, with the abort limits"
        />
        {!points.length && (
          <p className="mt-3 text-[13px] text-fg-2">
            {armed ? "Armed. The first samples land here within seconds." : "No samples yet. Arm the probes and the trace starts here."}
          </p>
        )}
      </Instrument>

      <Instrument
        title="Baseline on disk"
        meta={b ? `captured ${relTime(b.captured_at, s?.now ?? 0)}` : "none yet"}
      >
        {b ? (
          <div className="flex flex-col gap-5">
            <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
              <Readout label="Success" value={fmtPct(b.api.success_rate, b.api.success_rate < 1 ? 1 : 0)} />
              <Readout label="p50 latency" value={ms(b.api.p50_ms)} unit="ms" />
              <Readout label="p95 latency" value={ms(b.api.p95_ms)} unit="ms" />
              <Readout label="p99 latency" value={ms(b.api.p99_ms)} unit="ms" />
            </div>
            <p className="text-[13px] text-fg-2">
              <span className="font-mono tabular text-fg">{b.api.sample_count.toLocaleString()}</span> requests over a{" "}
              <span className="font-mono tabular text-fg">{fmtS(b.duration_s)}</span> window. Health checks passed per service:
            </p>
            <HealthAvailability avail={b.health_availability ?? {}} />
            {b.notes.length > 0 && (
              <ul className="flex flex-col gap-1 text-[13px] text-fg">
                {b.notes.map((n, i) => (
                  <li key={i} className="border-l-2 border-line pl-3">{clean(n)}</li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="max-w-[60ch] text-[14px] text-fg-2">
            No baseline. Until you capture one, the abort limits fall back to the floors in config/thresholds.yaml, shown below.
          </p>
        )}
      </Instrument>

      <Instrument title="Abort limits" meta={b ? "derived from this baseline" : "config floors"}>
        {t ? <LimitsList t={t} /> : <p className="text-[13px] text-fg-2">Loading the limits from the server.</p>}
      </Instrument>
    </div>
  );
}
