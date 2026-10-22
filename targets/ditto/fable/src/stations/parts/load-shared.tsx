// Small components shared by the Load, Map and Predict stations. The load- prefix is only the file
// namespace this work owns; nothing here is specific to onboarding.
import { useEffect, useRef, type ReactNode } from "react";
import { CircleNotch } from "@phosphor-icons/react";
import { cx } from "../../components/ui";
import { prose } from "./load-util";

/** Render `backticked` spans from backend prose as inline code. */
export function Ticks({ text, className = "" }: { text: string; className?: string }) {
  const parts = prose(text).split(/`([^`]+)`/g);
  return (
    <span className={className}>
      {parts.map((p, i) => (i % 2 ? <code key={i} className="font-mono text-[0.92em] text-fg">{p}</code> : <span key={i}>{p}</span>))}
    </span>
  );
}

/** A two-state switch. The word on/off sits next to it so state never rests on colour alone. */
export function Switch({ checked, onChange, label, hint, tone = "fg", disabled = false, id }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: ReactNode;
  tone?: "fg" | "model";
  disabled?: boolean;
  id: string;
}) {
  const on = tone === "model" ? "border-model" : "border-fg";
  const knob = tone === "model" ? "bg-model" : "bg-fg";
  return (
    <div className="flex flex-col gap-0.5">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-describedby={hint ? `${id}-hint` : undefined}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className="group flex min-h-11 items-center gap-3 text-left disabled:cursor-not-allowed disabled:opacity-40"
      >
        <span aria-hidden="true" className={cx("relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-[2px] border transition-colors duration-150", checked ? on : "border-line group-hover:border-fg-2")}>
          <span className={cx("absolute left-[2px] size-3 rounded-[1px] transition-transform duration-150 motion-reduce:transition-none", checked ? cx("translate-x-[12px]", knob) : "bg-fg-2")} />
        </span>
        <span className="text-[14px] text-fg">{label}</span>
        <span className={cx("font-mono text-[11px]", checked ? (tone === "model" ? "text-model" : "text-fg") : "text-fg-2")}>{checked ? "on" : "off"}</span>
      </button>
      {hint && <p id={`${id}-hint`} className="pl-11 text-[12px] leading-snug text-fg-2">{hint}</p>}
    </div>
  );
}

/** The running line under a primary action. Same verb as the button, present tense. */
export function Working({ children }: { children: ReactNode }) {
  return (
    <p role="status" className="flex items-start gap-2 text-[13px] leading-snug text-fg">
      <CircleNotch size={14} className="mt-[2px] shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" />
      <span>{children}</span>
    </p>
  );
}

/**
 * TopologyMap lays labels out for roughly 660 px and up; narrower, they collide. On a phone the map
 * scrolls sideways inside its own frame, starting centred on the main chain, and the page itself
 * never scrolls horizontally.
 */
export function MapFrame({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el && el.scrollWidth > el.clientWidth) el.scrollLeft = (el.scrollWidth - el.clientWidth) / 2;
  }, []);
  return (
    <div className="flex flex-col gap-1">
      <div ref={ref} className="overflow-x-auto overscroll-x-contain">
        <div className="min-w-[660px]">{children}</div>
      </div>
      <p className="text-[11px] text-fg-2 min-[700px]:hidden">Swipe the map sideways to see all of it.</p>
    </div>
  );
}
