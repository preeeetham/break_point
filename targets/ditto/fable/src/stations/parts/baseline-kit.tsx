// Small components shared by the Baseline, Plan and Break stations. Named baseline-* only to stay
// inside the file ownership for this work; nothing here is Baseline specific.
import type { ReactNode } from "react";
import { CheckCircle, CircleNotch, MinusCircle, WarningCircle, XCircle } from "@phosphor-icons/react";
import { clean, failureLabel } from "../../lib/format";
import type { CatalogFailure } from "../../lib/types";
import { cx } from "../../components/ui";
import { mechanism, paramLabel, paramValue, type GlyphTone } from "./baseline-helpers";

const GLYPH: Record<GlyphTone, { ink: string; icon: ReactNode }> = {
  ok: { ink: "text-pass", icon: <CheckCircle size={16} weight="fill" aria-hidden="true" /> },
  blocked: { ink: "text-fail", icon: <XCircle size={16} weight="fill" aria-hidden="true" /> },
  note: { ink: "text-fg", icon: <WarningCircle size={16} weight="fill" aria-hidden="true" /> },
  wait: { ink: "text-fg-2", icon: <CircleNotch size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> },
  off: { ink: "text-fg-2", icon: <MinusCircle size={16} aria-hidden="true" /> },
};

/** Status is never colour alone: a glyph and a word, in the status ink. */
export function GlyphWord({ tone, word, className = "" }: { tone: GlyphTone; word: string; className?: string }) {
  const g = GLYPH[tone];
  return (
    <span className={cx("inline-flex shrink-0 items-center gap-1.5 font-mono text-[11px] leading-5", g.ink, className)}>
      {g.icon}
      {word}
    </span>
  );
}

export function ErrorNotice({ what, error, fix }: { what: string; error: string; fix?: string | null }) {
  return (
    <div role="alert" className="flex flex-col gap-1.5 border-l-2 border-fail pl-3 text-[13px] leading-snug">
      <p className="text-fail">
        <span className="font-semibold">{what}</span> <span className="break-words">{clean(error)}</span>
      </p>
      {fix && <p className="text-fg">{fix}</p>}
    </div>
  );
}

/** Parameters as a quiet ledger: label above a mono value. */
export function ParamLedger({ params, failure, className = "" }: { params: Record<string, number | boolean>; failure?: CatalogFailure | null; className?: string }) {
  const entries = Object.entries(params ?? {});
  if (!entries.length) return <p className={cx("text-[13px] text-fg-2", className)}>No parameters.</p>;
  return (
    <dl className={cx("flex flex-wrap gap-x-6 gap-y-2", className)}>
      {entries.map(([k, v]) => (
        <div key={k} className="flex flex-col">
          <dt className="text-[12px] text-fg-2">{paramLabel(k)}</dt>
          <dd className="font-mono tabular text-[14px] text-fg">{paramValue(k, v, failure)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One name for a failure everywhere (failureLabel), with the catalog's mechanism as a quiet detail. */
export function FailureName({ k, failure, className = "" }: { k: string | null | undefined; failure?: CatalogFailure | null; className?: string }) {
  const mech = mechanism(failure);
  return (
    <span className={className}>
      {failureLabel(k)}
      {mech && <span className="ml-2 font-mono text-[0.8em] text-fg-2">{mech}</span>}
    </span>
  );
}

/** A small caption above a group of controls. Sentence case, no tracking. */
export function GroupLabel({ children, htmlFor, id }: { children: ReactNode; htmlFor?: string; id?: string }) {
  const cls = "label-condensed text-[13px] text-fg";
  return htmlFor ? <label htmlFor={htmlFor} id={id} className={cls}>{children}</label> : <p id={id} className={cls}>{children}</p>;
}
