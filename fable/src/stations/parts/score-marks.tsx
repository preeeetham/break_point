// Small shared marks for the Score and Reconcile stations. Every status is a glyph plus a word.
import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
  Check, CheckCircle, Copy, Info, MinusCircle, Question, StopCircle, Warning, WarningCircle, WarningOctagon, XCircle,
} from "@phosphor-icons/react";
import type { ExperimentSummary, Finding, Risk } from "../../lib/types";
import { announce } from "../../lib/a11y";
import { cx } from "../../components/ui";
import { levelInk, tidy } from "./score-text";

function Mark({ icon, word, ink, className = "" }: { icon: ReactNode; word: string; ink: string; className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1.5 whitespace-nowrap", ink, className)}>
      {icon}
      {word}
    </span>
  );
}

const I = { size: 14, "aria-hidden": true } as const;

export function StatusMark({ status }: { status: ExperimentSummary["status"] }) {
  if (status === "completed") return <Mark ink="text-pass" icon={<CheckCircle {...I} weight="fill" />} word="completed" />;
  if (status === "aborted") return <Mark ink="text-fg" icon={<StopCircle {...I} weight="fill" />} word="aborted" />;
  if (status === "failed") return <Mark ink="text-fail" icon={<WarningCircle {...I} weight="fill" />} word="failed" />;
  return <Mark ink="text-fg-2" icon={<MinusCircle {...I} />} word="skipped" />;
}

export function HeldMark({ held, long = false }: { held: boolean | null; long?: boolean }) {
  if (held === true) return <Mark ink="text-pass" icon={<CheckCircle {...I} />} word={long ? "hypothesis held" : "held"} />;
  if (held === false) return <Mark ink="text-fail" icon={<XCircle {...I} />} word={long ? "hypothesis violated" : "violated"} />;
  return <Mark ink="text-fg-2" icon={<Question {...I} />} word={long ? "hypothesis not judged" : "not judged"} />;
}

export function SeverityMark({ severity }: { severity: Finding["severity"] }) {
  if (severity === "critical") return <Mark ink="text-fail" icon={<WarningOctagon {...I} weight="fill" />} word="critical" />;
  if (severity === "high") return <Mark ink="text-fail" icon={<Warning {...I} weight="fill" />} word="high" />;
  if (severity === "medium") return <Mark ink="text-fg" icon={<Warning {...I} />} word="medium" />;
  if (severity === "low") return <Mark ink="text-fg-2" icon={<Info {...I} />} word="low" />;
  return <Mark ink="text-fg-2" icon={<Info {...I} />} word="info" />;
}

export function RiskMark({ risk }: { risk: Risk | null }) {
  if (!risk) return <span className="whitespace-nowrap text-fg-2">risk not scored</span>;
  const ink = levelInk(risk.level);
  return (
    <span className={cx("inline-flex items-center gap-1.5 whitespace-nowrap", ink.text)}>
      {risk.level === "critical" || risk.level === "high" ? <Warning {...I} weight="fill" /> : <Info {...I} />}
      risk {risk.level}
      <span className="font-mono tabular">{risk.severity.toFixed(1)}</span>
    </span>
  );
}

/** Backend prose: dashes cleaned, `backticked` spans set in mono. */
export function Rich({ text, className = "" }: { text: string | null | undefined; className?: string }) {
  const parts = tidy(text).split(/`([^`]+)`/);
  return (
    <span className={className}>
      {parts.map((p, i) =>
        i % 2 ? (
          <code key={i} className="font-mono text-[0.92em] text-fg">{p}</code>
        ) : (
          <Fragment key={i}>{p}</Fragment>
        ),
      )}
    </span>
  );
}

export function CopyButton({ text, label = "Copy", className = "" }: { text: string; label?: string; className?: string }) {
  const [done, setDone] = useState(false);
  const t = useRef(0);
  useEffect(() => () => window.clearTimeout(t.current), []);
  const copy = async () => {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      ta.remove();
    }
    announce(ok ? "Copied to the clipboard." : "Copy failed. Select the commands and copy them by hand.");
    if (ok) {
      setDone(true);
      window.clearTimeout(t.current);
      t.current = window.setTimeout(() => setDone(false), 1800);
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      className={cx("inline-flex min-h-11 shrink-0 items-center gap-2 rounded-[2px] border border-line px-3 text-[13px] text-fg hover:bg-bg", className)}
    >
      {done ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
      {done ? "Copied" : label}
    </button>
  );
}

/** A labelled section of a report sheet: the label sits in a gutter on wide screens, above on narrow ones. */
export function SheetSection({ label, aside, children }: { label: string; aside?: ReactNode; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="grid gap-x-8 gap-y-3 border-t border-line py-5 md:grid-cols-[8.5rem_minmax(0,1fr)]">
      <div className="flex flex-col gap-1">
        <h5 id={id} className="label-condensed text-[14px] text-fg">{label}</h5>
        {aside && <div className="text-[12px] leading-snug text-fg-2">{aside}</div>}
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}
