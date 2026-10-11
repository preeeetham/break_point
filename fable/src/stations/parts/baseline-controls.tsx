import { useId, useState } from "react";
import { actions, isRunActive, useConnection, useFState } from "../../lib/store";
import { announce } from "../../lib/a11y";
import { clean, fmtMs, fmtPct } from "../../lib/format";
import { Notice } from "../../components/Station";
import { Button, cx } from "../../components/ui";
import { ErrorNotice, GlyphWord, GroupLabel } from "./baseline-kit";
import { fixFor, useArm } from "./baseline-helpers";

/** Arm or disarm the synthetic client and health checks. */
export function ProbeControl() {
  const arm = useArm();
  const busy = arm.pending !== null;
  return (
    <div className="flex flex-col gap-3">
      <GroupLabel>Probes</GroupLabel>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[14px] text-fg">
        {arm.armed ? (
          <>
            <GlyphWord tone="ok" word="armed" />
            <span>
              <span className="font-mono tabular">{arm.samples.toLocaleString()}</span> samples
              {arm.rps != null && <span className="text-fg-2">, about {arm.rps} requests a second</span>}
            </span>
          </>
        ) : arm.pending === "arm" ? (
          <>
            <GlyphWord tone="wait" word="arming" />
            <span className="text-fg-2">Mapping services and starting the synthetic client.</span>
          </>
        ) : (
          <>
            <GlyphWord tone="off" word="off" />
            <span className="text-fg-2">Nothing is calling the API.</span>
          </>
        )}
      </p>
      <div>
        {arm.armed ? (
          <Button variant="ghost" onClick={arm.disarm} disabled={busy}>
            {arm.pending === "disarm" ? "Disarming probes" : "Disarm probes"}
          </Button>
        ) : (
          <Button variant={arm.error ? "ghost" : "primary"} onClick={arm.arm} disabled={busy || !arm.live}>
            {arm.pending === "arm" ? "Arming probes" : "Arm probes"}
          </Button>
        )}
      </div>
      {arm.error && <ErrorNotice what="Could not arm the probes." error={arm.error} fix={arm.fix} />}
    </div>
  );
}

const MIN = 20;
const MAX = 300;

/** Pick a window, capture the baseline, follow it while it runs. */
export function CaptureControl() {
  const s = useFState();
  const conn = useConnection();
  const sliderId = useId();
  const reasonId = useId();
  const [duration, setDuration] = useState(60);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<{ status: number; error: string } | null>(null);

  const job = s?.job ?? null;
  const capturing = !!job && job.kind === "baseline" && job.status === "running";
  const otherJob = job && job.status === "running" && job.kind !== "baseline" ? job : null;
  const failed = job && job.kind === "baseline" && job.status === "failed" ? job : null;
  const finished = job && job.kind === "baseline" && job.status === "done" ? job : null;
  const armed = !!s?.session.armed;

  const blocked =
    conn === "replay" ? "Replay data: the Faultline server is not reachable, so nothing can be measured."
    : conn !== "live" ? "Connecting to the Faultline server."
    : !armed ? "Arm the probes first. The baseline is measured from their samples."
    : isRunActive(s) ? "An experiment is running. Measure calm after it has finished."
    : otherJob ? `${clean(otherJob.label)} is running. One job runs at a time.`
    : null;

  // the running job's own window, from the agent detail, so a reload still shows the right total
  const detail = s?.agents.baseline?.detail ?? "";
  const total = Number(/(\d+)\s*s\b/.exec(detail)?.[1] ?? duration);
  // server clock on both sides; the store polls every 400 ms while a job runs
  const shown = capturing && s && job ? Math.min(total, Math.max(0, s.now - job.started_at)) : 0;

  const capture = async () => {
    setSending(true);
    setSendError(null);
    const r = await actions.job("baseline", { duration });
    setSending(false);
    if (!r.ok) {
      setSendError({ status: r.status, error: r.error ?? "The server refused." });
      announce("Could not start the baseline capture.");
    } else {
      announce(`Capturing baseline: ${duration} seconds of steady state.`);
    }
  };

  const b = s?.baseline;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between gap-3">
        <GroupLabel htmlFor={sliderId}>Measurement window</GroupLabel>
        <output htmlFor={sliderId} className="font-mono tabular text-[15px] text-fg">{duration} s</output>
      </div>
      <input
        id={sliderId}
        type="range"
        min={MIN}
        max={MAX}
        step={10}
        value={duration}
        onChange={(e) => setDuration(Number(e.target.value))}
        disabled={capturing}
        aria-valuetext={`${duration} seconds`}
        className="h-11 w-full cursor-pointer accent-fg disabled:cursor-not-allowed disabled:opacity-40"
      />
      <div className="-mt-2 flex justify-between font-mono text-[11px] text-fg-2">
        <span>{MIN} s</span>
        <span>{MAX} s</span>
      </div>

      <div>
        <Button onClick={capture} disabled={!!blocked || capturing || sending} aria-describedby={blocked ? reasonId : undefined}>
          {capturing ? "Capturing baseline" : sending ? "Starting capture" : "Capture baseline"}
        </Button>
      </div>
      {blocked && !capturing && (
        <p id={reasonId} className="text-[13px] leading-snug text-fg-2">
          {blocked}
        </p>
      )}

      {capturing && (
        <div className="flex flex-col gap-2" role="status">
          <p className="text-[14px] text-fg">
            Capturing baseline: <span className="font-mono tabular">{Math.floor(shown)} s</span> of{" "}
            <span className="font-mono tabular">{total} s</span> of steady state.
          </p>
          <div className="h-[3px] w-full overflow-hidden bg-line" aria-hidden="true">
            <div
              className="h-full origin-left bg-fg transition-transform duration-700 ease-linear motion-reduce:transition-none"
              style={{ transform: `scaleX(${total ? shown / total : 0})` }}
            />
          </div>
          <p className="text-[12px] text-fg-2">Do not launch anything while it measures. The limits are set from this window.</p>
        </div>
      )}

      {sendError && (
        <ErrorNotice
          what="Could not start the capture."
          error={sendError.error}
          fix={sendError.status === 409 ? "One job runs at a time. Wait for it to finish, then capture again." : fixFor(sendError.error, "capture again")}
        />
      )}
      {failed && !capturing && <ErrorNotice what="The baseline capture failed." error={failed.error ?? "No reason was given."} fix={fixFor(failed.error, "arm the probes and capture again")} />}
      {finished && !capturing && b && (
        <Notice tone="pass">
          Baseline captured. Success {fmtPct(b.api.success_rate, b.api.success_rate < 1 ? 1 : 0)}, p95 {fmtMs(b.api.p95_ms)}. Abort above{" "}
          {fmtPct(b.thresholds.abort_error_rate_above)} errors or p95 {fmtMs(b.thresholds.abort_p95_ms_above)}.
        </Notice>
      )}
    </div>
  );
}

export function Divider({ className = "" }: { className?: string }) {
  return <hr className={cx("border-0 border-t border-line", className)} />;
}
