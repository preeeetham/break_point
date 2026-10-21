import { useId } from "react";
import { LockSimple } from "@phosphor-icons/react";
import type { Catalog, CatalogFailure, CatalogParam, Topology } from "../../lib/types";
import { clean, failureLabel, fmtS } from "../../lib/format";
import { Instrument, Notice } from "../../components/Station";
import { TopologyMap } from "../../components/TopologyMap";
import type { MarkerState } from "../../components/Marker";
import { Button, cx } from "../../components/ui";
import { FailureName, GroupLabel, ParamLedger } from "./baseline-kit";
import { paramLabel, paramUnit } from "./baseline-helpers";
import { stepFor, whyNot, type Params } from "./plan-helpers";

function sentence(text: string): string {
  const c = clean(text).trim();
  return /[.!?]$/.test(c) ? c : `${c}.`;
}

/** Catalog prose quotes config in backticks; show those runs in mono. */
function withCode(text: string) {
  return text.split("`").map((part, i) => (i % 2 ? <code key={i} className="font-mono text-[11px] text-fg">{part}</code> : part));
}

export function TargetMap({ topology, locks, target, onTarget }: { topology: Topology; locks: Record<string, string>; target: string | null; onTarget: (s: string) => void }) {
  const states: Record<string, MarkerState> = {};
  for (const id of Object.keys(locks)) states[id] = "dimmed";
  if (target) states[target] = "target";
  const locked = Object.entries(locks).sort((a, b) => a[0].localeCompare(b[0]));
  return (
    <Instrument title="Choose the target" meta="marker size is criticality">
      <TopologyMap
        topology={topology}
        ariaLabel="Application map. Select a service as the test target."
        selected={target}
        onSelect={onTarget}
        locked={locks}
        states={states}
        height={360}
      />
      <p className="mt-2 text-[13px] text-fg-2">
        {target ? (
          <>
            Target: <span className="font-mono text-fg">{target}</span>. Select another marker to change it.
          </>
        ) : (
          "Select a marker, with a click or Tab then Enter. Larger markers break more when they fail."
        )}
      </p>
      {locked.length > 0 && (
        <ul className="mt-3 border-t border-line pt-2 text-[12px] leading-snug" aria-label="Locked services">
          {locked.map(([id, why]) => (
            <li key={id} className="flex gap-2 py-1 text-fg-2">
              <LockSimple size={14} aria-hidden="true" className="mt-0.5 shrink-0" />
              <span>
                <span className="font-mono text-fg">{id}</span> locked: {why}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Instrument>
  );
}

export function FailurePicker({
  catalog, target, failure, onFailure, params, onParam, onReset,
}: {
  catalog: Catalog;
  target: string | null;
  failure: string | null;
  onFailure: (key: string) => void;
  params: Params;
  onParam: (name: string, v: number | boolean) => void;
  onReset: () => void;
}) {
  const groupId = useId();
  // what fits the target, then what would fit but the policy locks, so the barrier is visible
  const fits = target
    ? [
        ...catalog.failures.filter((f) => f.applies_to.includes(target) && !whyNot(f, target)),
        ...catalog.failures.filter((f) => (f.applies_to.includes(target) || f.policy_blocked_on.includes(target)) && whyNot(f, target)),
      ]
    : [];
  const open = target ? fits.filter((f) => !whyNot(f, target)).length : 0;
  const chosen = catalog.failures.find((f) => f.key === failure) ?? null;
  const prot = target ? catalog.protected[target] : undefined;
  const anyBlocked = !!target && fits.some((f) => f.policy_blocked_on.includes(target));

  return (
    <Instrument
      title={target ? `Choose the failure for ${target}` : "Choose the failure"}
      meta={target ? `${open} of ${catalog.failures.length} failure types allowed${fits.length > open ? `, ${fits.length - open} locked` : ""}` : undefined}
    >
      {!target ? (
        <p className="text-[14px] text-fg-2">Choose a target on the map first. Only the failures that fit it are listed.</p>
      ) : (
        <div className="flex flex-col gap-5">
          <fieldset>
            <legend id={groupId} className="sr-only">Failure type for {target}</legend>
            <div className="grid border-t border-line md:grid-cols-2">
              {fits.map((f, i) => {
                const no = whyNot(f, target);
                const on = failure === f.key;
                return (
                  <label
                    key={f.key}
                    className={cx(
                      "relative flex min-h-11 gap-3 border-b border-line py-3 md:px-3",
                      i % 2 === 0 && "md:border-r md:pl-0",
                      no ? "cursor-not-allowed" : "cursor-pointer hover:bg-bg/40",
                    )}
                  >
                    <input
                      type="radio"
                      name={groupId}
                      value={f.key}
                      checked={on}
                      disabled={!!no}
                      onChange={() => onFailure(f.key)}
                      className="peer sr-only"
                    />
                    <span
                      aria-hidden="true"
                      className={cx(
                        "mt-0.5 grid size-[18px] shrink-0 place-items-center rounded-[2px] border peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-fg",
                        on ? "border-fg bg-fg" : "border-fg-2",
                        no && "border-line",
                      )}
                    >
                      {on && <span className="size-2 bg-bg" />}
                    </span>
                    <span className={cx("flex min-w-0 flex-col gap-0.5", no && "opacity-60")}>
                      <FailureName k={f.key} failure={f} className="text-[14px] font-medium text-fg" />
                      <span className="text-[12px] leading-snug text-fg-2">{withCode(clean(f.description))}</span>
                      <span className="font-mono text-[11px] text-fg-2">
                        expected recovery {fmtS(f.expected_recovery_s)}
                        {f.degrade_only ? ", degrades only" : ", full outage of the target"}
                      </span>
                      {no && (
                        <span className="mt-0.5 flex items-start gap-1.5 text-[12px] text-fg">
                          <LockSimple size={13} aria-hidden="true" className="mt-0.5 shrink-0" />
                          {no}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>
          {anyBlocked && prot && (
            <Notice tone="neutral">
              {target} is protected by config/thresholds.yaml: {clean(prot.reason)}. Allowed here:{" "}
              {prot.allowed_failure_types.map(failureLabel).join(", ")}.
            </Notice>
          )}

          {chosen && !whyNot(chosen, target) && (
            <section className="border-t border-line pt-4">
              <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
                <h4 className="label-condensed text-[13px] text-fg">Parameters for {failureLabel(chosen.key).toLowerCase()}</h4>
                <button type="button" onClick={onReset} className="min-h-11 text-[13px] text-fg-2 underline-offset-2 hover:text-fg hover:underline">
                  Reset to defaults
                </button>
              </div>
              <div className="grid gap-x-8 gap-y-5 md:grid-cols-2">
                {chosen.params.map((p) => (
                  <ParamControl key={p.name} p={p} value={params[p.name]} onChange={(v) => onParam(p.name, v)} failure={chosen} />
                ))}
              </div>
              <p className="mt-4 text-[12px] text-fg-2">Bounds come from the failure catalog. The planner clamps them again at launch.</p>
            </section>
          )}
        </div>
      )}
    </Instrument>
  );
}

function ParamControl({ p, value, onChange, failure }: { p: CatalogParam; value: number | boolean | undefined; onChange: (v: number | boolean) => void; failure: CatalogFailure }) {
  const id = useId();
  const descId = useId();
  const unit = paramUnit(p.name, failure);
  if (p.type === "bool") {
    const on = Boolean(value ?? p.default);
    return (
      <label htmlFor={id} className="flex min-h-11 cursor-pointer items-start gap-3">
        <input id={id} type="checkbox" checked={on} onChange={(e) => onChange(e.target.checked)} aria-describedby={p.desc ? descId : undefined} className="mt-1 size-[18px] shrink-0 accent-fg" />
        <span className="flex flex-col">
          <span className="text-[14px] text-fg">{paramLabel(p.name)}</span>
          {p.desc && <span id={descId} className="text-[12px] leading-snug text-fg-2">{clean(p.desc)}. Default {p.default ? "on" : "off"}.</span>}
        </span>
      </label>
    );
  }
  const v = Number(value ?? p.default);
  const lo = p.lo ?? 0;
  const hi = p.hi ?? 100;
  return (
    <div className="flex flex-col">
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="text-[14px] text-fg">{paramLabel(p.name)}</label>
        <output htmlFor={id} className="font-mono tabular text-[15px] text-fg">
          {v}
          {unit && <span className="ml-1 text-[12px] text-fg-2">{unit}</span>}
        </output>
      </div>
      <input
        id={id}
        type="range"
        min={lo}
        max={hi}
        step={stepFor(p)}
        value={v}
        onChange={(e) => onChange(Number(e.target.value))}
        aria-describedby={descId}
        aria-valuetext={`${v}${unit ? ` ${unit}` : ""}`}
        className="h-11 w-full cursor-pointer accent-fg"
      />
      <div id={descId} className="-mt-1.5 flex justify-between gap-2 font-mono text-[11px] text-fg-2">
        <span>{lo}{unit && ` ${unit}`}</span>
        <span>default {String(p.default)}{unit && ` ${unit}`}</span>
        <span>{hi}{unit && ` ${unit}`}</span>
      </div>
      {p.desc && <p className="mt-1 text-[12px] text-fg-2">{clean(p.desc)}</p>}
    </div>
  );
}

/** Left column, manual mode: what is chosen so far and the hand-off. */
export function ManualSummary({
  catalog, target, failure, params, origin, prefillProblem, onContinue,
}: {
  catalog: Catalog | null;
  target: string | null;
  failure: string | null;
  params: Params;
  origin: string | null;
  prefillProblem: string | null;
  onContinue: () => void;
}) {
  const reasonId = useId();
  const f = catalog?.failures.find((x) => x.key === failure) ?? null;
  const blocked = target && f ? whyNot(f, target) : null;
  const missing = !target ? "Choose a target on the map." : !f ? "Choose a failure for it." : blocked ? blocked : null;
  return (
    <div className="flex flex-col gap-4">
      {origin && (
        <Notice tone="model">
          Set up from the Scout's prediction: {sentence(origin)} <span className="text-fg-2">Predicted, unconfirmed.</span>
        </Notice>
      )}
      {prefillProblem && <Notice tone="neutral">{prefillProblem}</Notice>}
      <div className="flex flex-col gap-2">
        <GroupLabel>Your test</GroupLabel>
        <dl className="grid grid-cols-[5.5rem_1fr] gap-x-3 gap-y-2 border-t border-line pt-3 text-[14px]">
          <dt className="text-fg-2">Target</dt>
          <dd className={target ? "font-mono text-fg" : "text-fg-2"}>{target ?? "not chosen"}</dd>
          <dt className="text-fg-2">Failure</dt>
          <dd className={f ? "text-fg" : "text-fg-2"}>{f ? <FailureName k={f.key} failure={f} /> : "not chosen"}</dd>
          <dt className="text-fg-2">Settings</dt>
          <dd>{f ? <ParamLedger params={params} failure={f} /> : <span className="text-fg-2">after the failure</span>}</dd>
        </dl>
      </div>
      <div className="flex flex-col gap-2">
        <div>
          <Button onClick={onContinue} disabled={!!missing} aria-describedby={reasonId}>
            Continue to Break
          </Button>
        </div>
        <p id={reasonId} className="text-[12px] leading-snug text-fg-2">
          {missing ?? "Recorded as planned_by operator. Faultline writes the hypothesis from the map at launch. Nothing has been injected."}
        </p>
      </div>
    </div>
  );
}
