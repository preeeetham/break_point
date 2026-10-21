import { useId, useState } from "react";
import { CaretDown } from "@phosphor-icons/react";
import { askPlanner, usePlanRequest } from "../../lib/draft";
import { useCatalog, useConnection, useFState } from "../../lib/store";
import { clean, failureLabel, fmtPct, fmtS } from "../../lib/format";
import type { Plan, PlanCandidate, PlanPick } from "../../lib/types";
import { Instrument, Notice } from "../../components/Station";
import { MarkerGlyph } from "../../components/Marker";
import { AiMark, Button, cx } from "../../components/ui";
import { ErrorNotice, FailureName, GroupLabel, ParamLedger } from "./baseline-kit";
import { fixFor, useElapsed } from "./baseline-helpers";
import { Toggle } from "./plan-mode";

const TOP = 5;

/** Left column, planner mode: the model switch, the ask, and what came back. */
export function PlannerControls({ llm, setLlm, onContinue }: { llm: boolean; setLlm: (v: boolean) => void; onContinue: (plan: Plan) => void }) {
  const s = useFState();
  const conn = useConnection();
  const req = usePlanRequest();
  const asking = req.status === "asking";
  const elapsed = useElapsed(asking ? req.startedAt : null);
  const model = s?.llm ?? null;
  const modelName = model?.model || "the model";
  const plan = req.plan;
  const pick = plan?.pick ?? null;
  const askedModel = req.llm;
  const reasonId = useId();

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <GroupLabel>Planner</GroupLabel>
        <Toggle
          checked={llm}
          onChange={setLlm}
          disabled={asking}
          label={llm ? "Ask the model to pick from the top five" : "Code only: take the top-ranked pair"}
          detail={model ? `${modelName}${model.available ? "" : ", not reachable"}` : undefined}
        />
        {llm && model && !model.available && (
          <p className="text-[12px] leading-snug text-fg-2">
            The model is not reachable{model.error ? `: ${clean(model.error)}` : ""}. The planner will fall back to the top-ranked pair.
          </p>
        )}
      </div>

      <div className="flex flex-wrap gap-3">
        {pick && !asking && (
          <Button onClick={() => plan && onContinue(plan)} aria-describedby={reasonId}>
            Continue to Break
          </Button>
        )}
        <Button onClick={() => askPlanner(llm)} disabled={asking || conn === "connecting"} variant={pick && !asking ? "ghost" : "primary"}>
          {asking ? "Asking the planner" : pick ? "Ask again" : "Ask the planner"}
        </Button>
      </div>
      {pick && !asking && (
        <p id={reasonId} className="-mt-1 text-[12px] leading-snug text-fg-2">
          Nothing has been injected. At launch the planner runs once more on the live system, so the pick can change within the top five.
        </p>
      )}

      {asking && (
        <div role="status" className="flex flex-col gap-2">
          <p className="text-[14px] leading-snug text-fg">
            Ranking every service and failure pair{req.llm ? `, then asking ${modelName} to choose from the top five` : ""}.{" "}
            <span className="font-mono tabular text-fg-2">{Math.floor(elapsed)} s</span>
          </p>
          {req.llm && <p className="text-[12px] text-fg-2">A model answer can take a while. It is allowed to finish, and nothing runs either way.</p>}
        </div>
      )}

      {req.status === "error" && req.error && (
        <ErrorNotice
          what="The planner did not answer."
          error={req.error}
          fix={/abort|timeout/i.test(req.error) ? "Ask again with the model switched off. Code alone does not wait on it." : fixFor(req.error, "ask again")}
        />
      )}

      {!asking && plan && askedModel && pick && pick.planned_by !== "llm" && (
        <Notice tone="neutral">
          The model gave no usable answer{plan.llm.error ? ` (${clean(plan.llm.error)})` : plan.llm.available ? "" : " because it is not reachable"}, so the top-ranked pair is planned
          with a templated rationale: planned_by deterministic. To retry with the model, check LLM_API_KEY in .env and ask again.
        </Notice>
      )}
      {!asking && plan && !pick && <Notice tone="neutral">No candidates are left under the current safety policy. Nothing can be planned.</Notice>}

    </div>
  );
}

/** Right column, planner mode: the pick, or the invitation to ask for one. */
export function PickPanel() {
  const req = usePlanRequest();
  const s = useFState();
  const pick = req.plan?.pick ?? null;
  const asking = req.status === "asking";

  if (!pick) {
    return (
      <Instrument title="The pick" meta={asking ? "planning" : "nothing planned"}>
        {asking ? (
          <div className="flex flex-col gap-3" aria-hidden="true">
            <div className="h-7 w-40 bg-line/60" />
            <div className="h-4 w-56 bg-line/60" />
            <div className="mt-3 h-3 w-full bg-line/40" />
            <div className="h-3 w-5/6 bg-line/40" />
            <div className="h-3 w-2/3 bg-line/40" />
          </div>
        ) : (
          <p className="max-w-[56ch] text-[14px] leading-relaxed text-fg-2">
            No plan yet. Ask the planner to see every pair ranked by code and the one it would test next. Nothing runs here.
          </p>
        )}
      </Instrument>
    );
  }
  return <PickDetail pick={pick} model={req.plan?.llm.model || s?.llm.model || "the model"} stale={asking} />;
}

function PickDetail({ pick, model, stale }: { pick: PlanPick; model: string; stale: boolean }) {
  const catalog = useCatalog();
  const failure = catalog?.failures.find((f) => f.key === pick.failure_type) ?? null;
  const byModel = pick.planned_by === "llm";
  const h = pick.hypothesis;
  return (
    <Instrument title="The pick" meta={byModel ? `planned_by llm, ${model}` : `planned_by ${pick.planned_by}, top of the ranking`} className={cx(stale && "opacity-60")}>
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
          <MarkerGlyph size={30} className="mt-1 shrink-0" />
          <div className="min-w-0 flex-1">
            <p className="font-mono text-[24px] leading-tight text-fg">{pick.target_service}</p>
            <FailureName k={pick.failure_type} failure={failure} className="block text-[15px] text-fg" />
          </div>
          {byModel && <AiMark>chosen by the model</AiMark>}
        </div>
        <ParamLedger params={pick.parameters} failure={failure} />

        <Block label="Rationale" ai={byModel}>
          <p className={cx("max-w-[64ch] text-[14px] leading-relaxed", byModel ? "text-model" : "text-fg")}>{clean(pick.rationale)}</p>
          {!byModel && <p className="mt-1 text-[12px] text-fg-2">Templated by code from the ranking. No model wrote this.</p>}
        </Block>

        <Block label="Hypothesis" ai={byModel}>
          <p className={cx("max-w-[64ch] text-[15px] leading-relaxed", byModel ? "text-model" : "text-fg")}>{clean(h.statement)}</p>
          <dl className="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,1fr)]">
            <div className="min-w-0">
              <dt className="text-[12px] text-fg-2">Expected to be affected</dt>
              <dd className="mt-1 flex flex-wrap gap-1.5">
                {h.expected_affected.length ? (
                  h.expected_affected.map((x) => (
                    <span key={x} className="rounded-[2px] border border-dashed border-model/70 px-1.5 font-mono text-[12px] leading-5 text-fg">
                      {x}
                    </span>
                  ))
                ) : (
                  <span className="text-[13px] text-fg-2">no other service</span>
                )}
              </dd>
            </div>
            <div>
              <dt className="text-[12px] text-fg-2">Error rate at most</dt>
              <dd className="font-mono tabular text-[15px] text-fg">{fmtPct(h.expected_max_error_rate)}</dd>
            </div>
            <div>
              <dt className="text-[12px] text-fg-2">Recovers within</dt>
              <dd className="font-mono tabular text-[15px] text-fg">{fmtS(h.expected_recovery_s)}</dd>
            </div>
          </dl>
          <p className="mt-2 text-[12px] text-fg-2">Falsifiable: the Injector checks all three after the run and marks it held or violated.</p>
        </Block>

        {pick.planner_notes.length > 0 && (
          <Block label="Planner notes">
            <ul className="flex flex-col gap-1 text-[13px] leading-snug text-fg">
              {pick.planner_notes.map((n, i) => (
                <li key={i} className="border-l-2 border-line pl-3">{noteText(n)}</li>
              ))}
            </ul>
          </Block>
        )}

        {pick.reproduce.length > 0 && (
          <Block label="Reproduce by hand">
            <pre className="whitespace-pre-wrap break-all rounded-[2px] border border-line bg-bg px-3 py-2 font-mono text-[12px] leading-5 text-fg">
              {pick.reproduce.join("\n")}
            </pre>
          </Block>
        )}
      </div>
    </Instrument>
  );
}

// The planner counts candidates from 0; the ranking on screen counts from 1.
function noteText(n: string): string {
  const m = /chose candidate (\d+)/.exec(n);
  return clean(n) + (m ? `, #${Number(m[1]) + 1} in the ranking below.` : "");
}

function Block({ label, ai, children }: { label: string; ai?: boolean; children: React.ReactNode }) {
  return (
    <section className="border-t border-line pt-3">
      <h4 className="mb-1.5 flex items-center gap-2 text-[12px] text-fg-2">
        {label}
        {ai && <AiMark />}
      </h4>
      {children}
    </section>
  );
}

/** Below both columns: every pair as code ranked it, top five set apart as the model's whole menu. */
export function CandidateRanking() {
  const req = usePlanRequest();
  const [open, setOpen] = useState(false);
  const listId = useId();
  const plan = req.plan;
  if (!plan || !plan.candidates.length) return null;
  const pick = plan.pick;
  const isPick = (c: PlanCandidate) => !!pick && c.service === pick.target_service && c.failure_type === pick.failure_type;
  const top = plan.candidates.slice(0, TOP);
  const rest = plan.candidates.slice(TOP);

  return (
    <Instrument title="Ranked by code" meta={`${plan.candidates.length} service and failure pairs`}>
      <p className="max-w-[72ch] text-[13px] leading-relaxed text-fg-2">
        Priority is criticality, a bonus for pairs never tested and a penalty for pairs already tested, filtered by the safety policy.
        The model is shown only the top five and must answer with one of them. It cannot add a pair or raise a limit.
      </p>
      <div className="mt-4 grid gap-3 lg:grid-cols-[9.5rem_1fr]">
        <p className="label-condensed text-[13px] leading-snug text-fg lg:border-r lg:border-line lg:pr-4 lg:pt-2.5">What the model may choose from</p>
        <CandidateTable rows={top} startAt={1} isPick={isPick} emphasised />
      </div>
      {rest.length > 0 && (
        <div className="mt-4 lg:pl-[calc(9.5rem+0.75rem)]">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={listId}
            onClick={() => setOpen((v) => !v)}
            className="inline-flex min-h-11 items-center gap-2 text-[14px] text-fg hover:underline"
          >
            <CaretDown size={14} aria-hidden="true" className={cx("transition-transform motion-reduce:transition-none", open && "rotate-180")} />
            {open ? "Hide" : "Show"} the other {rest.length}, never shown to the model
          </button>
          <div id={listId} hidden={!open}>
            {open && <CandidateTable rows={rest} startAt={TOP + 1} isPick={isPick} />}
          </div>
        </div>
      )}
    </Instrument>
  );
}

function CandidateTable({ rows, startAt, isPick, emphasised = false }: { rows: PlanCandidate[]; startAt: number; isPick: (c: PlanCandidate) => boolean; emphasised?: boolean }) {
  return (
    <div className="min-w-0">
      <div aria-hidden="true" className="hidden grid-cols-[3.25rem_8.5rem_10.5rem_4.5rem_4.5rem_1fr] gap-x-3 border-b border-line pb-1.5 text-[11px] text-fg-2 md:grid">
        <span>Rank</span>
        <span>Service</span>
        <span>Failure</span>
        <span className="text-right">Priority</span>
        <span className="text-right">Criticality</span>
        <span>Why it ranks here</span>
      </div>
      <ol className="flex flex-col">
        {rows.map((c, i) => {
          const picked = isPick(c);
          return (
            <li
              key={`${c.service}/${c.failure_type}`}
              className={cx(
                "grid grid-cols-[3.25rem_1fr_auto] gap-x-3 gap-y-1 border-b border-line py-2.5 md:grid-cols-[3.25rem_8.5rem_10.5rem_4.5rem_4.5rem_1fr]",
                emphasised ? "text-fg" : "text-fg-2",
              )}
            >
              <span className="flex items-center gap-1.5 self-start font-mono tabular text-[13px] leading-5">
                {picked ? <MarkerGlyph size={14} /> : null}#{startAt + i}
                {picked && <span className="sr-only">, the pick</span>}
              </span>
              <span className="min-w-0 font-mono text-[13px] leading-5 md:truncate">
                {c.service}
                <span className="block font-sans text-[13px] md:hidden">{failureLabel(c.failure_type)}</span>
              </span>
              <span className="hidden text-[13px] leading-5 md:block">{failureLabel(c.failure_type)}</span>
              <span className="text-right font-mono tabular text-[13px] leading-5">
                {c.priority.toFixed(2)}
                <span className="block text-[11px] text-fg-2 md:hidden">crit {c.criticality}</span>
              </span>
              <span className="hidden text-right font-mono tabular text-[13px] leading-5 md:block">{c.criticality}</span>
              <Reasons reasons={c.reasons} picked={picked} />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Reasons({ reasons, picked }: { reasons: string[]; picked: boolean }) {
  return (
    <span className="col-span-3 col-start-1 text-[12px] leading-snug md:col-span-1 md:col-start-auto">
      {picked && <span className="mr-2 font-mono text-[11px] text-fg">picked.</span>}
      {reasons.map((r, i) => {
        const predicted = /predicted/i.test(r);
        return (
          <span key={i} className={predicted ? "text-model" : "text-fg-2"}>
            {clean(r)}
            {i < reasons.length - 1 ? "; " : ""}
          </span>
        );
      })}
    </span>
  );
}
