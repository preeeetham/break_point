// The count: how many predictions were confirmed, cleared or left untested, and the Scout's accuracy.
import type { ReactNode } from "react";
import { Binoculars, Flask } from "@phosphor-icons/react";
import type { Reconciliation } from "../../lib/types";
import { relTime } from "../../lib/format";
import { Instrument } from "../../components/Station";
import { cx } from "../../components/ui";
import { Rich } from "./score-marks";
import { hitRate } from "./reconcile-text";
import { outcomeMark } from "./predict-util";

function Count({ label, value, ink, icon }: { label: string; value: number; ink: string; icon: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <dt className={cx("inline-flex items-center gap-1.5 text-[12px]", ink)}>
        {icon}
        {label}
      </dt>
      <dd className={cx("font-mono tabular text-[44px] leading-none", ink)}>{value}</dd>
    </div>
  );
}

export function ReconcileTally({ rec, now, predicted, experiments }: { rec: Reconciliation | null; now: number; predicted: number | null; experiments: number }) {
  if (!rec) {
    return (
      <Instrument title="Predicted against measured">
        <p className="max-w-[56ch] text-[14px] leading-relaxed text-fg-2">
          {predicted == null
            ? "Nothing to reconcile. Predict weaknesses and run at least one experiment first."
            : `Not reconciled yet. Reconcile predictions to score ${predicted} predictions against ${experiments} experiment ${experiments === 1 ? "report" : "reports"}.`}
        </p>
      </Instrument>
    );
  }
  const rate = hitRate(rec.confirmed, rec.cleared);
  const I = { size: 14, "aria-hidden": true } as const;
  return (
    <Instrument title="Predicted against measured" meta={`reconciled ${relTime(rec.generated_at, now)}`}>
      <div className="flex flex-col gap-6">
        <dl className="grid grid-cols-[repeat(auto-fit,minmax(6.5rem,1fr))] gap-x-6 gap-y-5">
          <Count label="Predicted" value={rec.predicted_total} ink="text-model" icon={<Binoculars {...I} />} />
          {(["confirmed", "cleared", "untested"] as const).map((st) => {
            const m = outcomeMark(st);
            return <Count key={st} label={m.label} value={rec[st]} ink={m.ink} icon={m.glyph(14)} />;
          })}
          <Count label="Experiments" value={rec.experiments_considered} ink="text-fg" icon={<Flask {...I} />} />
        </dl>

        <div className="flex flex-wrap items-end gap-x-8 gap-y-3 border-t border-line pt-5">
          <p className="flex flex-col gap-1.5">
            <span className="text-[12px] text-fg">Scout accuracy</span>
            <span className="font-mono tabular text-[44px] leading-none text-fg">{rate == null ? "n/a" : Math.round(rate * 100)}{rate != null && <span className="ml-1.5 text-[20px] text-fg-2">%</span>}</span>
          </p>
          <p className="max-w-[48ch] pb-0.5 text-[13px] leading-snug text-fg-2">
            <span className="font-mono tabular text-fg">
              confirmed / (confirmed + cleared) = {rec.confirmed} / {rec.confirmed + rec.cleared}
            </span>
            <br />
            {rate == null
              ? "Nothing has been confirmed or cleared yet, so there is nothing to divide. "
              : "How often the static predictor was right where an experiment tested it. "}
            It grades the Scout, not the system: it is not a severity.
          </p>
        </div>

        <p className="max-w-[68ch] text-[14px] leading-relaxed text-fg">
          <Rich text={rec.summary} />
        </p>

        <div className="border-t border-line pt-4">
          <h4 className="label-condensed text-[13px] text-fg">Two gates keep the count honest</h4>
          <ul className="mt-2 flex max-w-[68ch] flex-col gap-2 text-[13px] leading-snug text-fg-2">
            <li>An info-severity finding never confirms a prediction, because the injector writes api_impact at info even when the API was fine.</li>
            <li>An aborted run never clears a prediction, because nothing going wrong after the safety monitor cut it short says nothing about the system.</li>
          </ul>
        </div>
      </div>
    </Instrument>
  );
}
