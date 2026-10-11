import { useWorkflow } from "../../lib/workflow";
import { clean, failureLabel, fmtMs, fmtPct, fmtS, relTime } from "../../lib/format";
import type { ExperimentSummary } from "../../lib/types";
import { Instrument } from "../../components/Station";
import { MarkerGlyph } from "../../components/Marker";
import { Button } from "../../components/ui";
import { GlyphWord } from "./baseline-kit";
import type { GlyphTone } from "./baseline-helpers";
import { abortText } from "./break-helpers";

const STATUS: Record<ExperimentSummary["status"], { tone: GlyphTone; word: string }> = {
  completed: { tone: "ok", word: "completed" },
  aborted: { tone: "note", word: "aborted" },
  failed: { tone: "blocked", word: "failed" },
  skipped: { tone: "note", word: "skipped" },
};

/** Why preflight refused, mapped to what to do about it. */
function skipFixes(detail: string): string[] {
  const out: string[] = [];
  if (/STOP/i.test(detail)) out.push("Press Resume in the header to clear STOP.");
  if (/probe samples/i.test(detail)) out.push("Arm the probes and let them run a few seconds.");
  if (/not steady/i.test(detail)) out.push("Faultline does not break a sick system. Heal the sandbox, wait for a flat trace, then launch again.");
  if (/not running|refusing to inject/i.test(detail)) out.push("Start the stack with scripts/up.sh, or run faultline heal, then launch again.");
  if (/another experiment is active/i.test(detail)) out.push("Wait for the active experiment to finish. The Guardian clears a stale lock after its deadline.");
  return out;
}

// The server sends the verdict notes as one string although the type says a list; accept both.
function verdictNotes(exp: ExperimentSummary): string {
  const n: unknown = exp.hypothesis_notes;
  return clean(Array.isArray(n) ? n.join("; ") : typeof n === "string" ? n : "");
}

export function Outcome({ exp, fresh, now }: { exp: ExperimentSummary; fresh: boolean; now: number }) {
  const { go } = useWorkflow();
  const st = STATUS[exp.status] ?? { tone: "off" as GlyphTone, word: exp.status };
  const skipped = exp.status === "skipped";
  const skipDetail = skipped ? exp.timeline.find((t) => t.event === "skipped")?.detail ?? "" : "";
  const fixes = skipped ? skipFixes(skipDetail) : [];
  const abort = exp.watchdog_fired ? "the watchdog forced the rollback at the hard deadline" : abortText(exp.abort_reason);
  const held = exp.hypothesis_held;

  return (
    <Instrument
      title={fresh ? (skipped ? "Refused at preflight" : "Experiment finished") : "Last experiment"}
      meta={`${exp.experiment_id.slice(0, 8)}, ${relTime(exp.started_at, now)}`}
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-3">
            <MarkerGlyph size={22} className="mt-0.5 shrink-0" />
            <div className="min-w-0">
              <p className="font-mono text-[17px] leading-tight text-fg">{exp.target_service}</p>
              <p className="text-[14px] text-fg">{failureLabel(exp.failure_type)}</p>
            </div>
          </div>
          <GlyphWord tone={st.tone} word={st.word} className="text-[13px]" />
        </div>

        {skipped ? (
          <div className="flex flex-col gap-1.5 border-l-2 border-line pl-3 text-[13px] leading-snug">
            <p className="text-fg">
              <span className="font-semibold">Preflight refused to inject. Nothing was broken.</span> {clean(skipDetail)}
            </p>
            {fixes.map((f) => (
              <p key={f} className="text-fg-2">{f}</p>
            ))}
          </div>
        ) : (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 border-t border-line pt-3 sm:grid-cols-4">
            <div>
              <dt className="text-[12px] text-fg-2">Hypothesis</dt>
              <dd className="mt-0.5">
                {held == null ? (
                  <GlyphWord tone="off" word="no verdict" />
                ) : held ? (
                  <GlyphWord tone="ok" word="held" />
                ) : (
                  <GlyphWord tone="blocked" word="violated" />
                )}
              </dd>
            </div>
            <div>
              <dt className="text-[12px] text-fg-2">Success during</dt>
              <dd className="font-mono tabular text-[17px] text-fg">{fmtPct(exp.during_success_rate)}</dd>
            </div>
            <div>
              <dt className="text-[12px] text-fg-2">p95 during</dt>
              <dd className="font-mono tabular text-[17px] text-fg">{fmtMs(exp.during_p95_ms)}</dd>
            </div>
            <div>
              <dt className="text-[12px] text-fg-2">Recovered in</dt>
              <dd className="font-mono tabular text-[17px] text-fg">
                {exp.recovered === false ? <span className="text-fail">did not</span> : fmtS(exp.time_to_recover_s, 1)}
              </dd>
            </div>
          </dl>
        )}

        {!skipped && (
          <p className="text-[13px] leading-snug text-fg-2">
            {abort ? (
              <>
                Stopped early: <span className="text-fg">{abort}</span>.
              </>
            ) : (
              "Ran its full course. No limit was crossed."
            )}
          </p>
        )}
        {!skipped && verdictNotes(exp) && (
          <p className="font-mono text-[12px] leading-snug text-fg-2">
            <span className="font-sans">How the verdict was reached: </span>
            {verdictNotes(exp)}
          </p>
        )}

        {!skipped && (
          <div>
            <Button onClick={() => go("score", { kind: "inspect-experiment", experimentId: exp.experiment_id })}>Score it</Button>
          </div>
        )}
      </div>
    </Instrument>
  );
}
