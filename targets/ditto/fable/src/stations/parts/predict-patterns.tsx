import type { WeaknessReport } from "../../lib/types";
import { Instrument } from "../../components/Station";
import { AiMark } from "../../components/ui";
import { prose } from "./load-util";

/** The model's only structural say: at most two pattern notes, each citing at least two existing predictions. */
export function SystemicPatterns({ report, onCite }: { report: WeaknessReport; onCite: (id: string) => void }) {
  const known = new Set(report.weaknesses.map((w) => w.weakness_id));
  const patterns = report.systemic_patterns ?? [];
  return (
    <Instrument title="Patterns across the guesses" meta={patterns.length ? "written by the model" : undefined}>
      {patterns.length ? (
        <ul className="grid gap-x-10 gap-y-6 md:grid-cols-2">
          {patterns.map((p) => (
            <li key={p.title} className="flex min-w-0 flex-col gap-2">
              <h4 className="flex flex-wrap items-center gap-2 text-[16px] text-fg">
                {prose(p.title)}
                {p.generated_by.includes("llm") && <AiMark />}
              </h4>
              <p className="max-w-[64ch] text-[14px] leading-relaxed text-model">{prose(p.narrative)}</p>
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] text-fg-2">Cites</span>
                {p.weakness_ids.map((id) =>
                  known.has(id) ? (
                    <button
                      key={id}
                      type="button"
                      onClick={() => onCite(id)}
                      className="min-h-11 rounded-[2px] border border-line px-2 font-mono text-[11px] text-fg hover:border-fg-2"
                    >
                      {id}
                    </button>
                  ) : (
                    <span key={id} className="font-mono text-[11px] text-fg-2">{id}</span>
                  ),
                )}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="max-w-[70ch] text-[14px] leading-relaxed text-fg-2">
          No patterns written for this run. The model may name at most two, only when Predict runs with the model on, and each
          must cite at least two of the predictions above or code discards it.
        </p>
      )}
    </Instrument>
  );
}
