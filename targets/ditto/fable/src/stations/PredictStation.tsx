import { useMemo, useState } from "react";
import { Instrument, Notice, Station } from "../components/Station";
import { Button, cx } from "../components/ui";
import { useConnection, useFState } from "../lib/store";
import { useWorkflow } from "../lib/workflow";
import { AGENT_LEDGER } from "../lib/stages";
import type { PredictedWeakness, WeaknessOutcome } from "../lib/types";
import { Switch, Working } from "./parts/load-shared";
import { prose, stripExc, useStationJob } from "./parts/load-util";
import { CategoryFilter, WeaknessList } from "./parts/predict-list";
import { outcomeMark, predictedLabel, toggleDomId } from "./parts/predict-util";
import { SystemicPatterns } from "./parts/predict-patterns";

const LEDGER = AGENT_LEDGER.find((a) => a.name === "Weakness Scout")!;

function failFix(error: string): string {
  if (/topology|map/i.test(error)) return "The Scout predicts against the map. Map the system, then predict again.";
  if (/compose/i.test(error)) return "The target's compose file could not be read. Check the target on the Load station, then predict again.";
  if (/already running/i.test(error)) return "Wait for that job to finish, then predict again.";
  return "Read the narrator feed for the last step that ran, then predict again.";
}

export function PredictStation() {
  const s = useFState();
  const replay = useConnection() === "replay";
  const { go } = useWorkflow();
  const job = useStationJob("scout");
  const llmReady = !!s?.llm.available && !!s?.llm.enabled;
  const [modelChoice, setModelChoice] = useState<boolean | null>(null);
  const useModel = modelChoice ?? llmReady;
  const [category, setCategory] = useState<string | null>(null);
  // undefined: nothing chosen yet, so the worst guess in view is open; null: the reader closed it
  const [choice, setChoice] = useState<string | null | undefined>(undefined);

  const report = s?.weaknesses ?? null;
  const all = useMemo(() => report?.weaknesses ?? [], [report]);
  const rankOf = useMemo(() => new Map(all.map((w, i) => [w.weakness_id, i + 1])), [all]);
  const items = useMemo(() => (category ? all.filter((w) => w.category === category) : all), [all, category]);
  const outcomes = useMemo(
    () => new Map<string, WeaknessOutcome>((s?.reconciliation?.outcomes ?? []).map((o) => [o.weakness_id, o])),
    [s?.reconciliation],
  );
  const rec = s?.reconciliation ?? null;

  const selected =
    choice === undefined || (choice && !items.some((w) => w.weakness_id === choice)) ? items[0]?.weakness_id ?? null : choice;
  const top = all[0];

  function plan(w: PredictedWeakness) {
    const e = w.suggested_experiment;
    if (!e) return;
    go("plan", { kind: "plan-manual", target: e.target_service, failure: e.failure_type, reason: prose(w.title) });
  }

  function cite(id: string) {
    setCategory(null);
    setChoice(id);
    requestAnimationFrame(() => {
      const toggle = document.getElementById(toggleDomId(id));
      if (!toggle) return;
      toggle.scrollIntoView({ block: "center" });
      toggle.focus({ preventScroll: true });
    });
  }

  const controls = (
    <>
      <Switch
        id="predict-model"
        checked={useModel}
        onChange={setModelChoice}
        tone="model"
        label="Use the model for explanations"
        hint={
          llmReady
            ? useModel
              ? "The model rewrites one sentence per prediction and may name two patterns. It cannot add, drop, rerank or rescore anything."
              : "Templated sentences, no model call. The list and every score are identical either way."
            : `The model is unavailable${s?.llm.error ? ` (${s.llm.error})` : ""}, so every explanation is templated.`
        }
      />
      <div>
        <Button onClick={() => job.start({ llm: useModel })} disabled={job.busy || !!job.other}>Predict weaknesses</Button>
      </div>
      {job.busy && (
        <Working>
          Predicting weaknesses: {report?.checks_run?.length ? `${report.checks_run.length} checks` : "checks"} over the compose file and the map…
        </Working>
      )}
      {job.other && <Notice>{job.other.label} is running. Predict weaknesses is available when it finishes.</Notice>}
      {job.refusal && <Notice tone="fail">Refused: {stripExc(job.refusal)}<span className="mt-1 block text-fg">{failFix(job.refusal)}</span></Notice>}
      {job.failed && !job.busy && !replay && (
        <Notice tone="fail" live={job.mine}>
          Prediction failed: {stripExc(job.failed)}
          <span className="mt-1 block text-fg">{failFix(job.failed)}</span>
        </Notice>
      )}
      {report && top && !job.busy && (
        <Notice tone="model">
          {all.length} weaknesses {predictedLabel(top)}. Top: {prose(top.title)}, {top.predicted_severity.toFixed(1)}/10.
        </Notice>
      )}
      {report && report.notes.length > 0 && !job.busy && (
        <ul className="flex flex-col gap-1 text-[12px] leading-snug text-fg-2">
          {report.notes.map((n) => <li key={n}>{prose(n)}</li>)}
        </ul>
      )}
      {rec && (
        <div className="flex flex-col gap-3 border-t border-line pt-4">
          <dl className="grid grid-cols-3 gap-3">
            {(["confirmed", "cleared", "untested"] as const).map((st) => {
              const m = outcomeMark(st);
              return (
                <div key={st} className="flex flex-col gap-1">
                  <dt className={cx("inline-flex items-center gap-1.5 text-[12px]", m.ink)}>{m.glyph(13)}{m.label}</dt>
                  <dd className={cx("font-mono text-[22px] leading-none tabular", m.ink)}>{rec[st]}</dd>
                </div>
              );
            })}
          </dl>
          <p className="text-[12px] leading-snug text-fg-2">Measured by {rec.experiments_considered} experiments on the Reconcile station. The severities stay predictions either way.</p>
        </div>
      )}
      {replay && <Notice>Replay: these are recorded predictions. Predicting again needs the live server.</Notice>}
    </>
  );

  const list = report ? (
    <Instrument title="Predicted weaknesses" meta={`${all.length}, worst first, ${report.generated_by === "rules" ? "rules only" : report.generated_by}`}>
      <div className="flex flex-col gap-4">
        <CategoryFilter all={all} value={category} onChange={(c) => { setCategory(c); setChoice(undefined); }} />
        {items.length ? (
          <WeaknessList items={items} rankOf={rankOf} outcomes={outcomes} selected={selected} onSelect={setChoice} onPlan={plan} topology={s?.topology ?? null} />
        ) : (
          <p className="text-[14px] text-fg-2">Nothing in this category.</p>
        )}
      </div>
    </Instrument>
  ) : (
    <Instrument title="Predicted weaknesses">
      <p className="py-10 text-[15px] text-fg-2">No predictions yet. Map the system, then predict weaknesses.</p>
    </Instrument>
  );

  const instrument = list;

  return (
    <Station
      id="predict"
      headline={all.length ? `${all.length} guesses, every one labelled a guess` : "Every guess labelled a guess"}
      lede="The Weakness Scout runs deterministic checks over the compose file and the map before anything is injected. Every prediction quotes the field it was read from. The model may write one sentence per prediction; it cannot add, remove, rerank or rescore anything."
      code={LEDGER.code}
      model={LEDGER.model}
      controls={controls}
      instrument={instrument}
      below={report ? <SystemicPatterns report={report} onCite={cite} /> : undefined}
    />
  );
}
