// Risk as visible arithmetic: each factor is a step in a waterfall on the 0 to 10 scale, the level
// bands drawn behind it, the sum written out underneath. Mirrors faultline/agents/risk.py.
import type { ReactNode } from "react";
import { motion } from "motion/react";
import type { Finding, Risk } from "../../lib/types";
import { AiMark, cx } from "../../components/ui";
import { FACTOR_INFO, LEVEL_EDGES, byModel, levelInk, num, orderedFactors } from "./score-text";
import { Rich } from "./score-marks";

const pct = (v: number) => `${Math.max(0, Math.min(100, v * 10))}%`;
const ROW = "grid items-center gap-x-4 gap-y-1 grid-cols-[minmax(0,1fr)_auto] md:grid-cols-[13.5rem_minmax(0,1fr)_3.5rem]";

function Track({ children }: { children?: ReactNode }) {
  return (
    <div aria-hidden="true" className="relative col-span-2 h-3 md:col-span-1 md:col-start-2 md:row-start-1">
      <span className="absolute inset-x-0 top-1/2 h-px bg-line" />
      {LEVEL_EDGES.slice(1).map((b) => (
        <span key={b.from} className="absolute inset-y-[-4px] w-px bg-line" style={{ left: pct(b.from) }} />
      ))}
      {children}
    </div>
  );
}

export function RiskPanel({ risk, findings, eid }: { risk: Risk | null; findings: Finding[]; eid: string }) {
  if (!risk) {
    return <p className="text-[14px] text-fg-2">Not scored yet. Run analysis to compute this experiment's severity.</p>;
  }
  const factors = orderedFactors(risk.factors);
  const sum = factors.reduce((a, [, v]) => a + v, 0);
  const ink = levelInk(risk.level);
  const forced = risk.level === "critical" && findings.some((f) => f.code === "data_loss_suspected") && risk.severity < 8;
  const steps = factors.map(([k, v], i) => ({ k, v, start: factors.slice(0, i).reduce((a, [, x]) => a + x, 0) }));

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className={cx("font-mono tabular text-[44px] leading-none", ink.text)}>{risk.severity.toFixed(2)}</span>
        <span className="font-mono text-[13px] text-fg-2">out of 10</span>
        <span className={cx("text-[15px] font-medium", ink.text)}>{risk.level} risk</span>
        <span className="text-[12px] text-fg-2">computed by code from the measurements</span>
      </div>

      <div className="flex flex-col gap-2.5" role="list" aria-label="How the severity adds up">
        {/* axis: level bands with their edges */}
        <div aria-hidden="true" className={cx(ROW, "text-[11px] text-fg-2")}>
          <span className="hidden md:block" />
          <div className="relative col-span-2 h-4 md:col-span-1">
            {LEVEL_EDGES.map((b) => (
              <span key={b.level} className="absolute top-0 whitespace-nowrap pl-1" style={{ left: pct(b.from) }}>
                <span className="font-mono tabular">{num(b.from)}</span> <span className={b.level === risk.level ? ink.text : ""}>{b.level}</span>
              </span>
            ))}
          </div>
          <span className="hidden md:block" />
        </div>

        {steps.map(({ k, v, start }, i) => {
          const info = FACTOR_INFO[k];
          return (
            <div key={`${eid}-${k}`} role="listitem" className={ROW}>
              <div className="min-w-0 md:row-start-1">
                <p className="text-[13px] text-fg">{info?.label ?? k.replaceAll("_", " ")}</p>
                {info && <p className="text-[11px] leading-snug text-fg-2">{info.rule}</p>}
              </div>
              <span className="text-right font-mono tabular text-[13px] text-fg md:col-start-3 md:row-start-1">
                <span className="sr-only">adds </span>{num(v)}
              </span>
              <Track>
                {v > 0 && (
                  <motion.span
                    className="absolute top-0 h-3 origin-left bg-fg-2"
                    style={{ left: pct(start), width: pct(v) }}
                    initial={{ scaleX: 0 }}
                    animate={{ scaleX: 1 }}
                    transition={{ duration: 0.35, delay: 0.08 * i, ease: [0.16, 1, 0.3, 1] }}
                  />
                )}
              </Track>
            </div>
          );
        })}

        <div role="listitem" className={cx(ROW, "border-t border-line pt-2.5")}>
          <p className={cx("text-[13px] font-medium md:row-start-1", ink.text)}>Severity, {risk.level}</p>
          <span className={cx("text-right font-mono tabular text-[13px] md:col-start-3 md:row-start-1", ink.text)}>{risk.severity.toFixed(2)}</span>
          <Track>
            <motion.span
              key={`${eid}-total`}
              className={cx("absolute left-0 top-0 h-3 origin-left", ink.bg)}
              style={{ width: pct(risk.severity) }}
              initial={{ scaleX: 0 }}
              animate={{ scaleX: 1 }}
              transition={{ duration: 0.45, delay: 0.08 * steps.length, ease: [0.16, 1, 0.3, 1] }}
            />
          </Track>
        </div>
      </div>

      <p className="font-mono tabular text-[12px] leading-relaxed text-fg-2">
        {steps.map((s) => num(s.v)).join(" + ")} = {num(sum)}
        {sum > 10 ? ", capped at 10" : Math.abs(sum - risk.severity) > 0.0001 ? `, rounded to ${num(risk.severity, 2)}` : ""}
      </p>
      {forced && (
        <p className="text-[13px] text-fail">The level is critical even though the sum is lower: a suspected data loss finding always forces critical.</p>
      )}

      <div className="flex max-w-[68ch] flex-col gap-3 border-t border-line pt-4">
        <div className="flex flex-wrap items-center gap-2 text-[12px] text-fg-2">
          {byModel(risk.generated_by) ? (
            <>
              <AiMark />
              <span>The model rewrote these two sentences. It cannot move the number or the level.</span>
            </>
          ) : (
            <span>Templated by code from the same numbers.</span>
          )}
        </div>
        <div>
          <p className="text-[12px] text-fg-2">Summary</p>
          <p className={cx("text-[15px] leading-relaxed", byModel(risk.generated_by) ? "text-model" : "text-fg")}><Rich text={risk.summary} /></p>
        </div>
        <div>
          <p className="text-[12px] text-fg-2">Production implication</p>
          <p className={cx("text-[15px] leading-relaxed", byModel(risk.generated_by) ? "text-model" : "text-fg")}><Rich text={risk.production_implication} /></p>
        </div>
      </div>
    </div>
  );
}
