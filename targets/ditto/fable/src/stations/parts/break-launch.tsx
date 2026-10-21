import { useId, useState } from "react";
import { actions, useCatalog, useFState } from "../../lib/store";
import { markLaunch, type Draft } from "../../lib/draft";
import { useWorkflow } from "../../lib/workflow";
import { announce } from "../../lib/a11y";
import { clean } from "../../lib/format";
import { HoldButton } from "../../components/HoldButton";
import { MarkerGlyph } from "../../components/Marker";
import { AiMark, Button, cx } from "../../components/ui";
import { ErrorNotice, FailureName, GlyphWord, ParamLedger } from "./baseline-kit";
import { fixFor, useArm } from "./baseline-helpers";
import { useChecks } from "./break-helpers";

/** The armed test, as it will be sent. Or the way back to Plan when there is none. */
export function TestCard({ draft }: { draft: Draft | null }) {
  const { go } = useWorkflow();
  const catalog = useCatalog();
  if (!draft) {
    return (
      <section aria-label="Armed test" className="flex flex-col gap-3 rounded-[2px] border border-dashed border-line p-4">
        <p className="text-[15px] text-fg">No test planned.</p>
        <p className="text-[13px] leading-snug text-fg-2">Plan one, read the hypothesis, then come back and press and hold Launch experiment.</p>
        <div>
          <Button variant="ghost" onClick={() => go("plan")}>Go to Plan</Button>
        </div>
      </section>
    );
  }
  const sm = draft.summary;
  const failure = catalog?.failures.find((f) => f.key === sm.failure) ?? null;
  const byModel = sm.plannedBy === "llm";
  return (
    <section aria-label="Armed test" className="flex flex-col gap-3 rounded-[2px] border border-line bg-sheet p-4">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="label-condensed text-[13px] text-fg">Armed test</h3>
        <span className="font-mono text-[11px] text-fg-2">{draft.mode === "auto" ? "planner's pick" : "your choice"}</span>
      </div>
      {sm.target ? (
        <div className="flex items-start gap-3">
          <MarkerGlyph size={24} className="mt-0.5 shrink-0" />
          <div className="min-w-0">
            <p className="font-mono text-[19px] leading-tight text-fg">{sm.target}</p>
            <FailureName k={sm.failure} failure={failure} className="block text-[14px] text-fg" />
          </div>
        </div>
      ) : (
        <p className="text-[14px] text-fg">The planner chooses at launch.</p>
      )}
      {sm.target && <ParamLedger params={sm.params} failure={failure} />}
      {sm.hypothesis && (
        <div className="border-t border-line pt-2.5">
          <p className="mb-1 flex items-center gap-2 text-[12px] text-fg-2">Hypothesis {byModel && <AiMark />}</p>
          <p className={cx("line-clamp-4 text-[13px] leading-snug", byModel ? "text-model" : "text-fg")}>{clean(sm.hypothesis)}</p>
        </div>
      )}
      {sm.origin && <p className="text-[12px] leading-snug text-model">From the Scout's prediction, unconfirmed: {clean(sm.origin)}</p>}
      <p className="border-t border-line pt-2.5 text-[12px] leading-snug text-fg-2">
        {draft.mode === "auto"
          ? `At launch the planner runs again on the live system${draft.request.llm ? " and the model picks again" : ""}, so this is a preview.`
          : "Recorded as planned_by operator. The hypothesis is written from the map at launch."}{" "}
        <button type="button" onClick={() => go("plan")} className="inline min-h-0 text-fg underline underline-offset-2">
          Change it
        </button>
      </p>
    </section>
  );
}

export function Preconditions({ draft }: { draft: Draft | null }) {
  const { checks } = useChecks(draft);
  const arm = useArm();
  const [resuming, setResuming] = useState(false);
  return (
    <section aria-label="Preconditions" className="flex flex-col gap-2">
      <h3 className="label-condensed text-[13px] text-fg">Before it can launch</h3>
      <ul className="border-t border-line">
        {checks.map((c) => (
          <li key={c.id} className="grid grid-cols-[4.75rem_1fr] gap-x-3 border-b border-line py-2">
            <GlyphWord tone={c.tone} word={c.word} className="self-start pt-px" />
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-[14px] leading-snug text-fg">{c.label}</span>
              {c.detail && <span className="text-[12px] leading-snug text-fg-2">{c.detail}</span>}
              {c.id === "probes" && !arm.armed && (
                <div className="flex flex-col gap-2">
                  <div>
                    <Button size="sm" variant="ghost" className="min-h-11" onClick={arm.arm} disabled={arm.pending !== null || !arm.live}>
                      {arm.pending === "arm" ? "Arming probes" : "Arm probes"}
                    </Button>
                  </div>
                  {arm.error && <ErrorNotice what="Could not arm the probes." error={arm.error} fix={arm.fix} />}
                </div>
              )}
              {c.id === "stop" && c.tone === "blocked" && (
                <div>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="min-h-11"
                    disabled={resuming}
                    onClick={async () => {
                      setResuming(true);
                      const r = await actions.resume();
                      setResuming(false);
                      announce(r.ok ? "Resumed. Launches are allowed again." : r.error ?? "Resume failed.");
                    }}
                  >
                    {resuming ? "Resuming" : "Resume"}
                  </Button>
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Press and hold to launch. Explains a refusal instead of failing quietly. */
export function LaunchControl({ draft, blocked }: { draft: Draft | null; blocked: string | null }) {
  const reasonId = useId();
  const [err, setErr] = useState<{ status: number; error: string } | null>(null);

  const launch = async () => {
    if (!draft || blocked) return;
    setErr(null);
    const r = await actions.run(draft.request);
    if (r.ok) {
      markLaunch({ target: draft.summary.target, failure: draft.summary.failure });
      announce("Experiment launched. Preflight checks first.");
    } else {
      setErr({ status: r.status, error: r.error ?? "The server refused." });
      announce("Launch refused.");
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <HoldButton label="Launch experiment" holdingLabel="Keep holding to launch" disabled={!!blocked} describedBy={reasonId} onConfirm={launch} />
      <p id={reasonId} className={cx("text-[12px] leading-snug", blocked ? "text-fg" : "text-fg-2")}>
        {blocked ? `Blocked: ${blocked}` : "Press and hold, or press twice. A stray click cannot start an experiment."}
      </p>
      {err && (
        <ErrorNotice
          what={err.status === 409 ? "Launch refused: another job is running." : "Launch refused."}
          error={err.error}
          fix={err.status === 409 ? "One job runs at a time. Wait for it to finish, or press Stop everything, then launch again." : fixFor(err.error, "launch again")}
        />
      )}
    </div>
  );
}

/** The last run job failed outright (not a skipped or aborted experiment: the job itself). */
export function RunJobFailure() {
  const s = useFState();
  const job = s?.job;
  if (!job || job.kind !== "run" || job.status !== "failed" || !job.error) return null;
  return <ErrorNotice what="The experiment job failed before it could report." error={job.error} fix={fixFor(job.error, "arm the probes and launch again")} />;
}
