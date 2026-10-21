import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import { announce } from "../lib/a11y";
import { cx } from "./ui";

interface HoldButtonProps {
  label: string;
  holdingLabel?: string;
  disabled?: boolean;
  describedBy?: string;
  /** ms the pointer or Space/Enter must be held */
  holdMs?: number;
  onConfirm: () => void | Promise<unknown>;
  className?: string;
}

// A deliberate action: press and hold so a stray click cannot start an experiment. A plain click arms a
// two-step confirm instead, which keeps it operable for anyone who cannot hold a key or pointer.
export function HoldButton({ label, holdingLabel = "Keep holding", disabled, describedBy, holdMs = 1200, onConfirm, className = "" }: HoldButtonProps) {
  const reduce = useReducedMotion();
  const [holding, setHolding] = useState(false);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef(0);
  const startAt = useRef(0);
  const fired = useRef(false);

  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(t);
  }, [armed]);

  const fire = async () => {
    fired.current = true;
    setHolding(false);
    setArmed(false);
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
    }
  };

  const begin = () => {
    if (disabled || busy) return;
    fired.current = false;
    startAt.current = performance.now();
    setHolding(true);
    timer.current = window.setTimeout(fire, holdMs);
  };
  const end = () => {
    window.clearTimeout(timer.current);
    setHolding(false);
  };

  const onClick = () => {
    if (disabled || busy || fired.current) return;
    if (performance.now() - startAt.current > 250 && startAt.current) return;
    if (armed) fire();
    else {
      setArmed(true);
      announce("Press again to confirm, or press and hold.");
    }
  };

  return (
    <button
      type="button"
      disabled={disabled || busy}
      aria-describedby={describedBy}
      onPointerDown={begin}
      onPointerUp={end}
      onPointerLeave={end}
      onPointerCancel={end}
      onKeyDown={(e) => {
        if ((e.key === " " || e.key === "Enter") && !e.repeat) {
          e.preventDefault();
          begin();
        }
      }}
      onKeyUp={(e) => {
        if (e.key === " " || e.key === "Enter") {
          const held = performance.now() - startAt.current;
          end();
          if (!fired.current && held < 250) onClick();
        }
      }}
      onClick={(e) => {
        if (e.detail === 0) return;
        onClick();
      }}
      className={cx(
        "relative isolate min-h-14 w-full overflow-hidden rounded-[2px] bg-marker px-6 text-[17px] font-semibold text-marker-ink",
        "disabled:cursor-not-allowed disabled:opacity-40 touch-none select-none",
        className,
      )}
    >
      <motion.span
        aria-hidden="true"
        className="absolute inset-y-0 left-0 -z-10 bg-fg"
        initial={false}
        animate={{ width: holding ? "100%" : "0%" }}
        transition={{ duration: holding ? (reduce ? 0 : holdMs / 1000) : 0.2, ease: "linear" }}
      />
      <span className={cx("relative transition-colors", holding && "text-marker")}>
        {busy ? "Starting…" : holding ? holdingLabel : armed ? "Press again to confirm" : label}
      </span>
    </button>
  );
}
