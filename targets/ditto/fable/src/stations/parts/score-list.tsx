// The experiment log: a vertical tab list, newest first. Arrow keys, Home and End move the selection.
import { useRef, type KeyboardEvent } from "react";
import type { ExperimentSummary } from "../../lib/types";
import { failureLabel, relTime } from "../../lib/format";
import { cx } from "../../components/ui";
import { HeldMark, RiskMark, StatusMark } from "./score-marks";

interface Props {
  experiments: ExperimentSummary[];
  selected: string;
  onSelect: (id: string, byPointer: boolean) => void;
  now: number;
}

export function ExperimentList({ experiments, selected, onSelect, now }: Props) {
  const refs = useRef(new Map<string, HTMLButtonElement>());

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = experiments.findIndex((x) => x.experiment_id === selected);
    const last = experiments.length - 1;
    let j: number;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") j = Math.min(last, i + 1);
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") j = Math.max(0, i - 1);
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = last;
    else return;
    e.preventDefault();
    const id = experiments[j].experiment_id;
    onSelect(id, false);
    refs.current.get(id)?.focus();
  };

  return (
    <div role="tablist" aria-orientation="vertical" aria-label="Experiments, newest first" onKeyDown={onKey} className="flex flex-col divide-y divide-line">
      {experiments.map((e) => {
        const id = e.experiment_id;
        const on = id === selected;
        return (
          <button
            key={id}
            ref={(el) => {
              if (el) refs.current.set(id, el);
              else refs.current.delete(id);
            }}
            type="button"
            role="tab"
            id={`exp-tab-${id}`}
            aria-selected={on}
            aria-controls="exp-detail"
            tabIndex={on ? 0 : -1}
            onClick={() => onSelect(id, true)}
            className={cx(
              "relative grid min-h-11 w-full gap-1 px-4 py-3 text-left focus-visible:outline-offset-[-2px]",
              on ? "bg-bg" : "hover:bg-bg/50",
            )}
          >
            {on && <span aria-hidden="true" className="absolute inset-y-0 left-0 w-[3px] bg-fg" />}
            <span className="flex items-baseline justify-between gap-3">
              <span className="font-mono tabular text-[13px] text-fg">{id}</span>
              <span className="text-[12px] text-fg-2">{relTime(e.started_at, now)}</span>
            </span>
            <span className="flex flex-wrap items-baseline gap-x-2 text-[14px]">
              <span className="font-mono text-[13px] text-fg">{e.target_service}</span>
              <span className="text-fg-2">{failureLabel(e.failure_type)}</span>
            </span>
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
              <StatusMark status={e.status} />
              <RiskMark risk={e.risk} />
              <HeldMark held={e.hypothesis_held} />
            </span>
          </button>
        );
      })}
    </div>
  );
}
