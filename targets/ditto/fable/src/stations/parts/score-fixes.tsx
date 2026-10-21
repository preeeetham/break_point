// Fix recommendations as disclosure rows. Advisory only: nothing here is ever applied.
import { useId, useState } from "react";
import { motion } from "motion/react";
import { ArrowSquareOut, CaretRight } from "@phosphor-icons/react";
import type { ExperimentSummary, Recommendation } from "../../lib/types";
import { AiMark, cx } from "../../components/ui";
import { Rich } from "./score-marks";
import { stripLlmTag, tidy } from "./score-text";

const EFFORT: Record<string, string> = { S: "small", M: "medium", L: "large" };

function isModelExtra(r: Recommendation): boolean {
  return stripLlmTag(r.title).llm || stripLlmTag(r.rationale).llm;
}

export function Fixes({ fix }: { fix: ExperimentSummary["fix"] }) {
  if (!fix) {
    return <p className="text-[14px] text-fg-2">No fixes yet. Run analysis to match this experiment against the rule library.</p>;
  }
  const recs = fix.recommendations;
  const extras = recs.filter(isModelExtra).length;
  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="text-[16px] font-medium text-fg">Advisory only. Nothing was applied.</p>
        <p className="mt-1 max-w-[68ch] text-[13px] leading-snug text-fg-2">
          {recs.length} {recs.length === 1 ? "recommendation" : "recommendations"} from the Ditto rule library
          {extras > 0 ? `, ${extras} of them added by the model and tagged unverified` : ""}. Read them, then decide.
        </p>
      </div>
      {recs.length > 0 && (
        <ul className="divide-y divide-line border-t border-line">
          {recs.map((r, i) => (
            <FixRow key={`${i}-${r.title}`} rec={r} />
          ))}
        </ul>
      )}
    </div>
  );
}

function FixRow({ rec }: { rec: Recommendation }) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const llm = isModelExtra(rec);
  const title = stripLlmTag(rec.title).text;
  const rationale = stripLlmTag(rec.rationale).text;
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? bodyId : undefined}
        onClick={() => setOpen((v) => !v)}
        className="grid min-h-11 w-full grid-cols-[1.25rem_minmax(0,1fr)] gap-x-2 py-3 text-left hover:bg-bg/40 focus-visible:outline-offset-[-2px]"
      >
        <CaretRight size={14} aria-hidden="true" className={cx("mt-1 text-fg-2 transition-transform duration-150 motion-reduce:transition-none", open && "rotate-90")} />
        <span className="flex min-w-0 flex-col gap-1.5">
          <span className={cx("text-[15px] leading-snug", llm ? "text-model" : "text-fg")}>{tidy(title)}</span>
          <span className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-fg-2">
            {llm && (
              <span className="inline-flex items-center gap-1.5 text-model">
                <AiMark /> model suggestion, unverified
              </span>
            )}
            <span>{rec.category}</span>
            <span>effort {EFFORT[rec.effort] ?? rec.effort}</span>
            <span>
              confidence <span className="font-mono tabular text-fg">{Math.round(rec.confidence * 100)}&nbsp;%</span>
              {llm && <span> (capped at 60&nbsp;%)</span>}
            </span>
          </span>
        </span>
      </button>
      {open && (
        <motion.div
          id={bodyId}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.18 }}
          className="flex flex-col gap-4 pb-5 pl-[1.75rem]"
        >
          {rationale && (
            <p className={cx("max-w-[68ch] text-[14px] leading-relaxed", llm ? "text-model" : "text-fg-2")}>
              <Rich text={rationale} />
            </p>
          )}
          {rec.steps.length > 0 && (
            <div>
              <h6 className="label-condensed text-[13px] text-fg">Steps</h6>
              <ol className="mt-1.5 flex max-w-[72ch] list-decimal flex-col gap-1.5 pl-5 text-[14px] leading-relaxed text-fg marker:font-mono marker:text-fg-2">
                {rec.steps.map((s, i) => (
                  <li key={i}><Rich text={s} /></li>
                ))}
              </ol>
            </div>
          )}
          {rec.verify.length > 0 && (
            <div>
              <h6 className="label-condensed text-[13px] text-fg">Verify</h6>
              <ul className="mt-1.5 flex flex-col gap-1.5 border-l-2 border-line pl-3">
                {rec.verify.map((v, i) => (
                  <li key={i} className="whitespace-pre-wrap break-words font-mono text-[12.5px] leading-relaxed text-fg">{tidy(v).replaceAll("`", "")}</li>
                ))}
              </ul>
            </div>
          )}
          {rec.references.length > 0 && (
            <div>
              <h6 className="label-condensed text-[13px] text-fg">References</h6>
              <ul className="mt-1 flex flex-col">
                {rec.references.map((u) => (
                  <li key={u}>
                    <a href={u} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center gap-1.5 break-all font-mono text-[12.5px] text-fg underline decoration-line underline-offset-4 hover:decoration-fg">
                      {u.replace(/^https?:\/\//, "")}
                      <ArrowSquareOut size={12} aria-hidden="true" className="shrink-0" />
                      <span className="sr-only">(opens in a new tab)</span>
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {llm && rec.references.length === 0 && (
            <p className="text-[12px] text-fg-2">No references. The model's suggestions are not checked against the rule library.</p>
          )}
        </motion.div>
      )}
    </li>
  );
}
