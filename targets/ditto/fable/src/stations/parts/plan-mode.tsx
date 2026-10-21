import { useRef, type KeyboardEvent } from "react";
import type { DraftMode } from "../../lib/draft";
import { cx } from "../../components/ui";

const OPTIONS: { id: DraftMode; label: string }[] = [
  { id: "auto", label: "Let the planner choose" },
  { id: "manual", label: "Choose it myself" },
];

/** Segmented control as a radio group: arrow keys move and select, Tab leaves. */
export function ModeSwitch({ value, onChange }: { value: DraftMode; onChange: (m: DraftMode) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = (i + step + OPTIONS.length) % OPTIONS.length;
    onChange(OPTIONS[next].id);
    refs.current[next]?.focus();
  };
  return (
    <div role="radiogroup" aria-label="Who chooses the experiment" className="grid grid-cols-2 gap-0.5 rounded-[2px] border border-line p-0.5">
      {OPTIONS.map((o, i) => {
        const on = value === o.id;
        return (
          <button
            key={o.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(o.id)}
            onKeyDown={(e) => onKey(e, i)}
            className={cx(
              "min-h-11 rounded-[2px] px-3 py-2 text-[14px] font-medium leading-tight transition-colors duration-150",
              on ? "bg-fg text-bg" : "text-fg-2 hover:text-fg",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/** An on/off switch with its label and a one-line detail. */
export function Toggle({ checked, onChange, label, detail, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; detail?: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="flex min-h-11 w-full items-center gap-3 text-left disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span aria-hidden="true" className={cx("relative inline-flex h-5 w-9 shrink-0 items-center rounded-[2px] border transition-colors", checked ? "border-model bg-model" : "border-line bg-sheet")}>
        <span className={cx("absolute size-3.5 rounded-[1px] transition-transform duration-150 motion-reduce:transition-none", checked ? "translate-x-[18px] bg-bg" : "translate-x-[2px] bg-fg-2")} />
      </span>
      <span className="flex min-w-0 flex-col">
        <span className="text-[14px] text-fg">{label}</span>
        {detail && <span className="truncate font-mono text-[11px] text-fg-2">{detail}</span>}
      </span>
    </button>
  );
}
