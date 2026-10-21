import { useId, type ReactNode } from "react";
import { CaretDown, Crosshair } from "@phosphor-icons/react";
import type { PredictedWeakness, Topology, WeaknessOutcome } from "../../lib/types";
import { categoryLabel, failureLabel } from "../../lib/format";
import { AiMark, Button, cx } from "../../components/ui";
import { Ticks } from "./load-shared";
import { prose } from "./load-util";
import { OutcomeMark, detailDomId, predictedLabel, sentence, toggleDomId } from "./predict-util";
import { PredictMap } from "./predict-map";

/** The measured outcome for one prediction, always glyph plus word, in the same marks Reconcile uses. */
function Outcome({ outcome }: { outcome: WeaknessOutcome | undefined }) {
  const state = outcome?.state ?? null;
  return <OutcomeMark state={state} size={13} suffix={state === "confirmed" || state === "cleared" ? " by experiment" : undefined} />;
}

/** Hatched, dashed, in the model's ink: a predicted severity must never read like a measured bar. */
function SeverityBar({ value, patternId }: { value: number; patternId: string }) {
  const pct = Math.max(0, Math.min(10, value)) * 10;
  return (
    <svg width="100%" height="10" aria-hidden="true" className="block overflow-visible">
      <line x1="0" y1="0" x2="0" y2="10" stroke="var(--color-line)" strokeWidth="1" />
      <line x1="100%" y1="0" x2="100%" y2="10" stroke="var(--color-line)" strokeWidth="1" />
      <rect x="0.5" y="1.5" width={`${pct}%`} height="7" fill={`url(#${patternId})`} stroke="var(--color-model)" strokeWidth="1" strokeDasharray="3 2" />
    </svg>
  );
}

export function HatchDefs({ id }: { id: string }) {
  return (
    <svg width="0" height="0" aria-hidden="true" className="absolute">
      <defs>
        <pattern id={id} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="4" stroke="var(--color-model)" strokeWidth="1.5" />
        </pattern>
      </defs>
    </svg>
  );
}

function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <h4 className="flex flex-wrap items-center gap-2 label-condensed text-[13px] text-fg">{title}{aside}</h4>
      {children}
    </div>
  );
}

function WeaknessDetail({ w, outcome, topology, onPlan, id }: { w: PredictedWeakness; outcome?: WeaknessOutcome; topology: Topology | null; onPlan: () => void; id: string }) {
  const llm = w.generated_by.includes("llm");
  const exp = w.suggested_experiment;
  const target = exp ? topology?.services[exp.target_service] : undefined;
  const exempt = !!exp && target?.chaos_relevant === false;
  const factors = Object.entries(w.factors ?? {}).filter(([, v]) => v);
  return (
    <div id={id} className="flex flex-col gap-4 pb-5 pl-0 pt-1 sm:pl-8">
      <PredictMap topology={topology} weakness={w} />
      <Section title="Evidence, quoted from the compose file">
        <blockquote className="border-l-2 border-line pl-3 font-mono text-[12px] leading-relaxed text-fg-2">
          <Ticks text={w.evidence} />
        </blockquote>
      </Section>

      <Section title="Why it matters" aside={llm ? <AiMark /> : <span className="font-mono text-[11px] text-fg-2">template, no model</span>}>
        <p className={cx("max-w-[68ch] text-[14px] leading-relaxed", llm ? "text-model" : "text-fg")}>{prose(w.why_it_matters)}</p>
      </Section>

      {factors.length > 0 && (
        <Section title="How code scored the guess">
          <p className="font-mono text-[12px] leading-relaxed tabular text-fg-2">
            {factors.map(([k, v], i) => (
              <span key={k}>{i > 0 && " + "}{k} {v.toFixed(2)}</span>
            ))}
            {" = "}<span className="text-model">{w.predicted_severity.toFixed(2)}</span> <span className="text-model">{predictedLabel(w)}</span>
          </p>
        </Section>
      )}

      <Section title="Suggested experiment">
        {exp ? (
          <div className="flex flex-col gap-2">
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[14px] text-fg">
              <Crosshair size={15} aria-hidden="true" className="text-fg-2" />
              <span>{failureLabel(exp.failure_type)}</span>
              <span className="text-fg-2">on</span>
              <span className="font-mono">{exp.target_service}</span>
            </p>
            <p className="text-[13px] leading-snug text-fg-2">{sentence(exp.reason)}</p>
            {exp.policy_note && (
              <p className="border-l-2 border-line pl-3 text-[13px] leading-snug text-fg">
                <span className="text-fg-2">Safety policy: </span><Ticks text={exp.policy_note} />
              </p>
            )}
          </div>
        ) : (
          <p className="text-[13px] text-fg-2">None. No failure type the policy allows can test this.</p>
        )}
      </Section>

      <Section title="Confirmed only if an experiment raises">
        <ul className="flex flex-wrap gap-1.5">
          {w.confirming_finding_codes.map((c) => (
            <li key={c} className="rounded-[2px] border border-line px-2 py-0.5 font-mono text-[11px] leading-5 text-fg">{c}</li>
          ))}
        </ul>
      </Section>

      {outcome && (
        <Section title="Measured so far">
          <p className="text-[13px]"><Outcome outcome={outcome} /></p>
          {outcome.detail && <p className="max-w-[72ch] break-words text-[13px] leading-snug text-fg-2">{prose(outcome.detail)}</p>}
        </Section>
      )}

      <div className="flex flex-col gap-2 pt-1">
        <div>
          <Button onClick={onPlan} disabled={!exp || exempt}>Plan this test</Button>
        </div>
        {exp && !exempt && (
          <p className="text-[12px] text-fg-2">Opens the planner with {failureLabel(exp.failure_type).toLowerCase()} on {exp.target_service} filled in. Nothing runs until you launch it there.</p>
        )}
        {exempt && <p className="text-[12px] text-fg-2">Faultline will not inject into {exp?.target_service}: it is outside the system under test.</p>}
      </div>
    </div>
  );
}

interface ListProps {
  items: PredictedWeakness[];
  rankOf: Map<string, number>;
  outcomes: Map<string, WeaknessOutcome>;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onPlan: (w: PredictedWeakness) => void;
  topology: Topology | null;
}

export function WeaknessList({ items, rankOf, outcomes, selected, onSelect, onPlan, topology }: ListProps) {
  const pattern = `hatch-${useId().replace(/:/g, "")}`;
  return (
    <>
      <HatchDefs id={pattern} />
      <ol aria-label="Predicted weaknesses, worst first" className="flex flex-col">
        {items.map((w) => {
          const open = selected === w.weakness_id;
          const outcome = outcomes.get(w.weakness_id);
          const detailId = detailDomId(w.weakness_id);
          return (
            <li key={w.weakness_id} className={cx("border-b border-line last:border-b-0", open && "border-l-2 border-l-fg pl-2.5 sm:pl-3")}>
              <button
                id={toggleDomId(w.weakness_id)}
                type="button"
                aria-expanded={open}
                aria-controls={open ? detailId : undefined}
                onClick={() => onSelect(open ? null : w.weakness_id)}
                className="grid w-full grid-cols-[1.5rem_minmax(0,1fr)] gap-x-2 gap-y-2 py-3 text-left sm:grid-cols-[1.5rem_minmax(0,1fr)_minmax(8.5rem,10rem)] sm:gap-x-4"
              >
                <span className="pt-0.5 font-mono text-[12px] tabular text-fg-2">{rankOf.get(w.weakness_id)}</span>
                <span className="flex min-w-0 flex-col gap-1">
                  <span className="flex items-start gap-2 text-[15px] leading-snug text-fg">
                    <span className="min-w-0">{prose(w.title)}</span>
                    <CaretDown size={14} aria-hidden="true" className={cx("mt-1 shrink-0 text-fg-2 transition-transform duration-150 motion-reduce:transition-none", open && "rotate-180")} />
                  </span>
                  <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
                    <span className="text-fg-2">{categoryLabel(w.category)}</span>
                    <span className="font-mono text-fg">{w.services.join(", ")}</span>
                    <Outcome outcome={outcome} />
                  </span>
                </span>
                <span className="col-start-2 flex flex-col gap-1 sm:col-start-3">
                  <span className="flex items-baseline gap-1.5">
                    <span className="font-mono text-[17px] leading-none tabular text-model">{w.predicted_severity.toFixed(1)}</span>
                    <span className="font-mono text-[11px] text-fg-2">/10</span>
                    <span className="ml-auto font-mono text-[11px] text-fg-2">{w.predicted_level}</span>
                  </span>
                  <SeverityBar value={w.predicted_severity} patternId={pattern} />
                  <span className="text-[11px] leading-tight text-model">{predictedLabel(w)}</span>
                </span>
              </button>
              {open && <WeaknessDetail id={detailId} w={w} outcome={outcome} topology={topology} onPlan={() => onPlan(w)} />}
            </li>
          );
        })}
      </ol>
    </>
  );
}

/** Category filter. Counts come from the full list, not the filtered one. */
export function CategoryFilter({ all, value, onChange }: { all: PredictedWeakness[]; value: string | null; onChange: (c: string | null) => void }) {
  const cats: [string, number][] = [];
  for (const w of all) {
    const row = cats.find(([c]) => c === w.category);
    if (row) row[1] += 1;
    else cats.push([w.category, 1]);
  }
  const chip = (key: string | null, label: string, n: number) => {
    const on = value === key;
    return (
      <button
        key={key ?? "all"}
        type="button"
        aria-pressed={on}
        onClick={() => onChange(key)}
        className={cx("inline-flex min-h-11 items-center gap-2 rounded-[2px] border px-2.5 text-[13px] transition-colors", on ? "border-fg text-fg" : "border-line text-fg-2 hover:border-fg-2 hover:text-fg")}
      >
        {label}
        <span className="font-mono text-[11px] tabular">{n}</span>
      </button>
    );
  };
  return (
    <div role="group" aria-label="Filter by category" className="flex flex-wrap gap-1.5">
      {chip(null, "All", all.length)}
      {cats.map(([c, n]) => chip(c, categoryLabel(c), n))}
    </div>
  );
}
