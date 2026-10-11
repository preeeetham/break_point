import { useReducedMotion } from "motion/react";

export type MarkerState = "idle" | "target" | "affected" | "down" | "recovered" | "predicted" | "dimmed";

interface MarkerProps {
  cx: number;
  cy: number;
  r: number;
  state?: MarkerState;
  pulse?: boolean;
}

// The crash-test target marker: a quartered circle. The ring and the quadrant ink carry state.
export function Marker({ cx, cy, r, state = "idle", pulse: pulseWanted = false }: MarkerProps) {
  const reduce = useReducedMotion();
  const pulse = pulseWanted && !reduce;
  const dark = state === "down" ? "var(--color-fail)" : "var(--color-fg)";
  const ring =
    state === "target" ? "var(--color-marker)"
    : state === "affected" || state === "down" ? "var(--color-fail)"
    : state === "recovered" ? "var(--color-pass)"
    : state === "predicted" ? "var(--color-model)"
    : "var(--color-fg)";
  const opacity = state === "dimmed" ? 0.3 : 1;
  const rr = Math.max(r, 2);
  return (
    <g opacity={opacity} style={{ transition: "opacity 400ms var(--ease-out-expo)" }}>
      {pulse && (
        <circle cx={cx} cy={cy} r={rr + 6} fill="none" stroke={ring} strokeWidth={1.5} opacity={0.5}>
          <animate attributeName="r" values={`${rr + 4};${rr + 14}`} dur="1.4s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.6;0" dur="1.4s" repeatCount="indefinite" />
        </circle>
      )}
      <circle cx={cx} cy={cy} r={rr} fill="var(--color-marker)" />
      <path d={`M${cx} ${cy - rr}A${rr} ${rr} 0 0 1 ${cx + rr} ${cy}L${cx} ${cy}ZM${cx} ${cy + rr}A${rr} ${rr} 0 0 1 ${cx - rr} ${cy}L${cx} ${cy}Z`} fill={dark} style={{ transition: "fill 300ms" }} />
      <circle cx={cx} cy={cy} r={rr} fill="none" stroke={ring} strokeWidth={state === "target" ? 3 : 1.5} strokeDasharray={state === "predicted" ? "3 3" : undefined} style={{ transition: "stroke 300ms" }} />
    </g>
  );
}

export function MarkerGlyph({ size = 14, className = "" }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className={className}>
      <circle cx="16" cy="16" r="15" fill="var(--color-marker)" />
      <path d="M16 1a15 15 0 0 1 15 15H16zM16 31A15 15 0 0 1 1 16h15z" fill="var(--color-fg)" />
      <circle cx="16" cy="16" r="15" fill="none" stroke="var(--color-fg)" strokeWidth="2" />
    </svg>
  );
}
