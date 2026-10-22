// Every prediction with its outcome: confirmed first, then cleared, then the untested ones framed as
// what to break next. A table on wide screens, stacked rows on narrow ones.
import { Crosshair } from "@phosphor-icons/react";
import type { Catalog, ExperimentSummary, PredictedWeakness, Reconciliation, WeaknessOutcome } from "../../lib/types";
import { categoryLabel, failureLabel } from "../../lib/format";
import { Instrument } from "../../components/Station";
import { cx } from "../../components/ui";
import { Rich } from "./score-marks";
import { OutcomeMark, outcomeMark } from "./predict-util";
import { tidy } from "./score-text";
import { STATE_ORDER, confirmingByExperiment, testable } from "./reconcile-text";

// State | Prediction | Services | Predicted severity | Experiments and findings | Next (untested only)
const COLS_SETTLED = "xl:grid-cols-[6.5rem_minmax(13rem,1.1fr)_minmax(8rem,0.6fr)_6.5rem_minmax(20rem,2fr)]";
const COLS_UNTESTED = "xl:grid-cols-[6.5rem_minmax(13rem,1.1fr)_minmax(8rem,0.6fr)_6.5rem_minmax(14rem,1.3fr)_15rem]";
const colsFor = (state: WeaknessOutcome["state"]) => (state === "untested" ? COLS_UNTESTED : COLS_SETTLED);

const GROUP: Record<WeaknessOutcome["state"], { title: string; line: string; empty: string }> = {
  confirmed: {
    title: "Confirmed",
    line: "An experiment that bore on it raised a finding the Scout nominated, above info severity.",
    empty: "None yet. Run experiments against the predicted services, then reconcile.",
  },
  cleared: {
    title: "Cleared",
    line: "An experiment that bore on it completed with no confirming finding and nothing high or critical.",
    empty: "None. Clearing needs a bearing experiment that ran the distance and raised neither a confirming finding nor anything high or critical.",
  },
  untested: {
    title: "What to break next",
    line: "Untested: no experiment settled these either way. Each names the experiment that would.",
    empty: "None. Every prediction has been tested.",
  },
};

interface Props {
  rec: Reconciliation;
  weaknesses: Map<string, PredictedWeakness>;
  experiments: ExperimentSummary[];
  catalog: Catalog | null;
  onOpen: (eid: string) => void;
  onPlan: (target: string, failure: string, reason: string) => void;
}

export function ReconcileOutcomes({ rec, weaknesses, experiments, catalog, onOpen, onPlan }: Props) {
  const known = new Map(experiments.map((e) => [e.experiment_id, e]));
  return (
    <Instrument title="Every prediction, settled or not" meta={`${rec.predicted_total} predictions, confirmed first`}>
      <div className="flex flex-col gap-8">
        {STATE_ORDER.map((state) => {
          const rows = rec.outcomes.filter((o) => o.state === state);
          const g = GROUP[state];
          return (
            <section key={state} aria-label={`${g.title}: ${rows.length}`} className="flex flex-col">
              <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-line pb-2">
                <h4 className="display flex items-baseline gap-2 text-[22px] text-fg">
                  {g.title}
                  <span className="font-mono tabular text-[16px] text-fg-2">{rows.length}</span>
                </h4>
                <p className="text-[13px] text-fg-2">{g.line}</p>
              </header>
              {rows.length === 0 ? (
                <p className="py-3 text-[13px] text-fg-2">{g.empty}</p>
              ) : (
                <div role="table" aria-label={`${g.title} predictions`} className="flex flex-col">
                  <div role="rowgroup" className="sr-only xl:not-sr-only">
                    <div role="row" className={cx("grid gap-x-5 border-b border-line py-2 text-[12px] text-fg-2", colsFor(state))}>
                      <span role="columnheader">State</span>
                      <span role="columnheader">Prediction</span>
                      <span role="columnheader">Services</span>
                      <span role="columnheader">Severity</span>
                      <span role="columnheader">Experiments and findings</span>
                      {state === "untested" && <span role="columnheader">Next</span>}
                    </div>
                  </div>
                  <div role="rowgroup" className="divide-y divide-line">
                    {rows.map((o) => (
                      <OutcomeRow key={o.weakness_id} o={o} w={weaknesses.get(o.weakness_id)} known={known} catalog={catalog} onOpen={onOpen} onPlan={onPlan} />
                    ))}
                  </div>
                </div>
              )}
            </section>
          );
        })}
      </div>
    </Instrument>
  );
}

function OutcomeRow({
  o, w, known, catalog, onOpen, onPlan,
}: { o: WeaknessOutcome; w: PredictedWeakness | undefined; known: Map<string, ExperimentSummary> } & Pick<Props, "catalog" | "onOpen" | "onPlan">) {
  const conf = confirmingByExperiment(o);
  const eids = [...new Set([...o.experiments, ...conf.keys()])];
  const sug = w?.suggested_experiment ?? null;
  const canPlan = o.state === "untested" && testable(sug, catalog);
  const title = w ? tidy(w.title) : categoryLabel(o.category);

  return (
    <div role="row" className={cx("grid grid-cols-[minmax(0,1fr)_auto] gap-x-5 gap-y-2 py-4 xl:items-start", colsFor(o.state))}>
      <span role="cell" className="col-start-1 row-start-1 text-[13px] xl:pt-0.5">
        <OutcomeMark state={o.state} />
      </span>

      <span role="cell" className="col-span-2 min-w-0 xl:col-span-1 xl:row-start-1 xl:col-start-2">
        <span className="block text-[15px] leading-snug text-fg">{title}</span>
        <span className="block break-all font-mono text-[11px] text-fg-2">{o.weakness_id}</span>
      </span>

      <span role="cell" className="col-span-2 font-mono text-[13px] text-fg xl:col-span-1 xl:pt-0.5">
        <span className="font-sans text-[12px] text-fg-2 xl:hidden">Services </span>
        {o.services.join(", ")}
      </span>

      <span role="cell" className="col-start-2 row-start-1 text-right text-model xl:col-start-4 xl:pt-0.5 xl:text-left">
        <span className="font-mono tabular text-[15px]">{o.predicted_severity.toFixed(1)}</span>{" "}
        <span className="text-[13px]">{o.predicted_level}</span>
        <span className="block text-[11px] leading-tight opacity-80">{o.state === "untested" ? "predicted, unconfirmed" : "predicted severity"}</span>
      </span>

      <span role="cell" className="col-span-2 min-w-0 xl:col-span-1">
        {eids.length > 0 && (
          <ul className="flex flex-col">
            {eids.map((eid) => {
              const e = known.get(eid);
              const codes = conf.get(eid) ?? [];
              return (
                <li key={eid} className="flex flex-wrap items-center gap-x-3">
                  <button
                    type="button"
                    onClick={() => onOpen(eid)}
                    className="inline-flex min-h-11 items-center font-mono tabular text-[12.5px] text-fg underline decoration-line underline-offset-4 hover:decoration-fg"
                    aria-label={`Open experiment ${eid} in Score`}
                  >
                    {eid}
                  </button>
                  {e && <span className="text-[12px] text-fg-2">{failureLabel(e.failure_type)} on {e.target_service}</span>}
                  <span className={cx("font-mono text-[12px]", codes.length ? outcomeMark("confirmed").ink : "text-fg-2")}>
                    {codes.length ? codes.join(", ") : <span className="font-sans">no confirming finding</span>}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
        <p className={cx("max-w-[70ch] break-words text-[13px] leading-snug text-fg-2", eids.length > 0 && "mt-1")}>
          <Rich text={o.detail} />
        </p>
      </span>

      {o.state === "untested" && (
      <span role="cell" className="col-span-2 min-w-0 xl:col-span-1">
        {sug && (
          <span className="flex flex-col items-start gap-1.5">
            {canPlan ? (
              <button
                type="button"
                onClick={() => onPlan(sug.target_service, sug.failure_type, `Test the predicted weakness ${o.weakness_id}: ${tidy(sug.reason)}`)}
                className="inline-flex min-h-11 items-center gap-2 rounded-[2px] border border-line px-3 text-[13px] font-medium text-fg hover:bg-bg"
              >
                <Crosshair size={15} aria-hidden="true" />
                Plan this test
              </button>
            ) : (
              <span className="text-[13px] text-fg">Cannot be tested here</span>
            )}
            <span className="text-[12px] text-fg-2">
              <span className="text-fg">{failureLabel(sug.failure_type)}</span> on <span className="font-mono">{sug.target_service}</span>
            </span>
            {sug.policy_note && (
              <span className="text-[12px] leading-snug text-fg-2">
                <Rich text={sug.policy_note} />
              </span>
            )}
          </span>
        )}
        {!sug && <span className="text-[12px] text-fg-2">The Scout suggested no experiment for this one.</span>}
      </span>
      )}
    </div>
  );
}
