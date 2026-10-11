import type { ButtonHTMLAttributes, ReactNode } from "react";

export type Tone = "neutral" | "marker" | "pass" | "fail" | "model" | "muted";

const toneText: Record<Tone, string> = {
  neutral: "text-fg",
  marker: "text-fg",
  pass: "text-pass",
  fail: "text-fail",
  model: "text-model",
  muted: "text-fg-2",
};

const toneDot: Record<Tone, string> = {
  neutral: "bg-fg",
  marker: "bg-marker",
  pass: "bg-pass",
  fail: "bg-fail",
  model: "bg-model",
  muted: "bg-fg-2",
};

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "ghost" | "marker" | "danger";
  size?: "md" | "sm";
}

export function Button({ variant = "primary", size = "md", className = "", ...rest }: ButtonProps) {
  const base = "inline-flex items-center justify-center gap-2 rounded-[2px] font-medium transition-[background-color,color,opacity] duration-150 disabled:cursor-not-allowed disabled:opacity-40 min-h-11";
  const sz = size === "sm" ? "px-3 py-2 text-sm" : "px-5 py-3 text-[15px]";
  const look = {
    primary: "bg-fg text-bg hover:opacity-90",
    ghost: "border border-line bg-transparent text-fg hover:bg-sheet",
    marker: "bg-marker text-marker-ink hover:brightness-95",
    danger: "bg-fail text-on-fail hover:brightness-95",
  }[variant];
  return <button className={cx(base, sz, look, className)} {...rest} />;
}

export function Chip({ tone = "neutral", children, className = "" }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1.5 rounded-[2px] border border-line px-2 py-0.5 font-mono text-[11px] leading-5", toneText[tone], className)}>
      <span aria-hidden="true" className={cx("size-1.5 rounded-[1px]", toneDot[tone])} />
      {children}
    </span>
  );
}

export function Readout({ label, value, unit, tone = "neutral", className = "" }: { label: string; value: ReactNode; unit?: string; tone?: Tone; className?: string }) {
  return (
    <div className={cx("flex flex-col gap-1", className)}>
      <span className="text-[12px] text-fg-2">{label}</span>
      <span className={cx("font-mono tabular text-2xl leading-none", toneText[tone])}>
        {value}
        {unit && <span className="ml-1 text-sm text-fg-2">{unit}</span>}
      </span>
    </div>
  );
}

export function Sheet({ children, className = "", grid = false }: { children: ReactNode; className?: string; grid?: boolean }) {
  return <div className={cx("rounded-[2px] border border-line bg-sheet", grid && "mm-grid", className)}>{children}</div>;
}

export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={cx("font-mono tabular text-[13px]", className)}>{children}</span>;
}

export function AiMark({ children = "AI" }: { children?: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-[2px] border border-model/50 px-1.5 py-px font-mono text-[10px] leading-4 text-model" title="Written by the model">
      <span aria-hidden="true" className="size-1.5 bg-model" />
      {children}
    </span>
  );
}

export function Container({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={cx("mx-auto w-full max-w-[1400px] px-4 md:px-8", className)}>{children}</div>;
}
