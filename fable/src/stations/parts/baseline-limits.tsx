import type { Thresholds } from "../../lib/types";
import { fmtMs, fmtPct } from "../../lib/format";
import { cx } from "../../components/ui";
import { GlyphWord } from "./baseline-kit";

interface Row {
  value: string;
  name: string;
  text: string;
}

/** The safety limits written out, each with what it means. Every number is a field of `t`. */
export function LimitsList({ t, compact = false, columns = false }: { t: Thresholds; compact?: boolean; columns?: boolean }) {
  const abort: Row[] = [
    {
      value: `> ${fmtPct(t.abort_error_rate_above)}`,
      name: "Error rate",
      text: `Share of the last ${t.trailing_samples} probe requests that failed.`,
    },
    {
      value: `> ${fmtMs(t.abort_p95_ms_above)}`,
      name: "p95 latency",
      text:
        t.baseline_p95_ms != null
          ? `Set from the baseline p95 of ${fmtMs(t.baseline_p95_ms)}, never below the config floor.`
          : "The config floor. No baseline has been measured yet.",
    },
    {
      value: `${t.non_target_health_failures_in_a_row} in a row`,
      name: "Collateral",
      text: "Failed health checks on a service the hypothesis did not name.",
    },
    {
      value: "any time",
      name: "Stop",
      text: "Stop everything, the Esc key, or faultline stop from any shell.",
    },
  ];
  const always: Row[] = [
    {
      value: `${t.max_experiment_seconds} s`,
      name: "Hard cap",
      text: `No fault stays longer. A watchdog forces rollback at ${t.max_experiment_seconds + t.watchdog_grace_seconds} s even if the main loop hangs.`,
    },
    {
      value: `≥ ${fmtPct(t.recovery_success_rate_at_least)}`,
      name: "Recovered",
      text: `Success at or above it with p95 under ${fmtMs(t.recovery_p95_ms_below)}, ${t.recovery_consecutive_checks} checks in a row. Gives up after ${t.recovery_timeout_seconds} s.`,
    },
  ];
  return (
    <div className={columns ? "grid gap-x-8 gap-y-4 lg:grid-cols-2" : "flex flex-col gap-4"}>
      <RowGroup caption="Rolls the fault back the moment any of these is crossed" rows={abort} compact={compact} />
      <RowGroup caption="Always" rows={always} compact={compact} />
    </div>
  );
}

function RowGroup({ caption, rows, compact }: { caption: string; rows: Row[]; compact: boolean }) {
  return (
    <div>
      <p className="mb-1.5 text-[12px] text-fg-2">{caption}</p>
      <dl className="border-t border-line">
        {rows.map((r) => (
          <div key={r.name} className={cx("grid grid-cols-[6.75rem_1fr] gap-x-4 border-b border-line", compact ? "py-2" : "py-2.5")}>
            <dt className="flex flex-col">
              <span className="font-mono tabular text-[15px] leading-tight text-fg">{r.value}</span>
              <span className="text-[12px] text-fg-2">{r.name}</span>
            </dt>
            <dd className="self-center text-[13px] leading-snug text-fg-2">{r.text}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** Share of health checks each service passed during the baseline window. */
export function HealthAvailability({ avail }: { avail: Record<string, number> }) {
  const rows = Object.entries(avail).sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]));
  if (!rows.length) return <p className="text-[13px] text-fg-2">No health checks were recorded in this window.</p>;
  return (
    <ul className="grid gap-x-8 sm:grid-cols-2" aria-label="Health check availability per service">
      {rows.map(([svc, v]) => {
        const full = v >= 0.999;
        return (
          <li key={svc} className="flex min-h-9 items-center justify-between gap-3 border-b border-line py-1.5">
            <span className="min-w-0 truncate font-mono text-[13px] text-fg">{svc}</span>
            <span className="flex items-center gap-3">
              <span className="font-mono tabular text-[13px] text-fg">{fmtPct(v, full ? 0 : 1)}</span>
              <GlyphWord tone={full ? "ok" : "note"} word={full ? "all passed" : "some failed"} />
            </span>
          </li>
        );
      })}
    </ul>
  );
}
