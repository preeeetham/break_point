import { useEffect, useRef } from "react";
import { motion } from "motion/react";
import { CircleNotch, CheckCircle, WarningCircle, Stop, Play } from "@phosphor-icons/react";
import { STAGES, stageStatus } from "../lib/stages";
import { actions, isRunActive, jobRunning, useConnection, useFState } from "../lib/store";
import { useWorkflow } from "../lib/workflow";
import { announce } from "../lib/a11y";
import { pad, timecode } from "../lib/format";
import { MarkerGlyph } from "./Marker";
import { useStageLink } from "./Station";
import { cx } from "./ui";

export function CameraHeader() {
  return (
    <div className="sticky top-0 z-40 border-b border-line bg-bg/95 backdrop-blur-[2px]">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-5 gap-y-1 px-4 py-1.5 md:px-8 md:py-2.5">
        <a href="#top" className="flex min-h-11 items-center gap-2 text-fg" aria-label="Faultline, back to the explainer">
          <MarkerGlyph size={18} />
          <span className="display text-[17px]">Faultline</span>
        </a>
        <TestClock />
        <div className="ml-auto flex items-center gap-2">
          <Pills />
          <KillSwitch />
        </div>
      </div>
      <Rail />
    </div>
  );
}

function TestClock() {
  const s = useFState();
  if (!s || !isRunActive(s)) return null;
  const n = s.experiments.length + 1;
  return (
    <p className="order-last w-full font-mono tabular text-[12px] text-fg md:order-none md:w-auto" aria-live="off">
      <span className="mr-2 inline-block size-2 bg-marker align-middle" aria-hidden="true" />
      Test {pad(n, 3)} · {s.run.phase} · {timecode(s.run.elapsed_s)}
      {s.run.deadline_in_s != null && <span className="text-fg-2"> · {Math.max(0, Math.ceil(s.run.deadline_in_s))} s left</span>}
    </p>
  );
}

function Pills() {
  const s = useFState();
  const conn = useConnection();
  const guardianFresh = !!s?.guardian && s.now - s.guardian.at < 15;
  return (
    <ul className="flex items-center gap-1.5 font-mono text-[11px]">
      <Pill tone={conn === "live" ? "pass" : conn === "replay" ? "warn" : "muted"} label={conn === "live" ? "live" : conn === "replay" ? "replay" : "connecting"} title={conn === "replay" ? "The Faultline server is not reachable. Showing a recorded snapshot. Actions are disabled except Stop." : undefined} />
      {s && <Pill wide tone={s.llm.available && s.llm.enabled ? "model" : "muted"} label={s.llm.available && s.llm.enabled ? s.llm.model.split("/").pop() ?? s.llm.model : "rules only"} title={`${s.llm.provider}: ${s.llm.error || "available"}`} />}
      {s && <Pill wide tone={guardianFresh ? "pass" : "muted"} label={guardianFresh ? "guardian on" : "guardian off"} title={guardianFresh ? "The independent Guardian process is heartbeating." : "Start it with: faultline guardian"} />}
      {s && <Pill wide tone={s.session.armed ? "pass" : "muted"} label={s.session.armed ? `probes ${s.session.probe.samples}` : "probes off"} />}
    </ul>
  );
}

function Pill({ tone, label, title, wide = false }: { tone: "pass" | "warn" | "muted" | "model"; label: string; title?: string; wide?: boolean }) {
  const ink = { pass: "text-pass", warn: "text-fg", muted: "text-fg-2", model: "text-model" }[tone];
  const dot = { pass: "bg-pass", warn: "bg-marker", muted: "bg-fg-2", model: "bg-model" }[tone];
  return (
    <li title={title} className={cx("items-center gap-1.5 rounded-[2px] border border-line px-2 py-1", wide ? "hidden lg:inline-flex" : "inline-flex", ink)}>
      <span aria-hidden="true" className={cx("size-1.5", dot)} />
      {label}
    </li>
  );
}

const STOPPED = "Stopped everything. The fault rolls back within about a second.";
const STOP_FAILED = "Stop did not reach the server. Run faultline stop in a terminal, or touch reports/STOP.";

function stopAll() {
  return actions.stop().then((r) => announce(r.ok ? STOPPED : `${STOP_FAILED} (${r.error ?? "no response"})`));
}

function KillSwitch() {
  const s = useFState();
  const active = isRunActive(s) || jobRunning(s);
  const latched = !!s?.stop_requested;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (!active) return;
      stopAll();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);

  if (latched) {
    return (
      <button type="button" onClick={() => actions.resume().then((r) => announce(r.ok ? "Resumed. Launches are allowed again." : r.error ?? "Resume failed"))} className="inline-flex min-h-11 items-center gap-2 rounded-[2px] border border-fail px-3 text-[13px] font-medium text-fail">
        <Play size={14} weight="fill" aria-hidden="true" /> <span className="hidden sm:inline">Stop latched.</span> Resume
      </button>
    );
  }
  return (
    <button
      type="button"
      onClick={stopAll}
      aria-keyshortcuts="Escape"
      title={active ? "Stop everything (Esc)" : "Stop everything. Nothing is running right now."}
      className={cx("inline-flex min-h-11 items-center gap-2 rounded-[2px] px-3 text-[13px] font-semibold", active ? "bg-fail text-on-fail" : "border border-line text-fg-2 hover:text-fg")}
    >
      <Stop size={14} weight="fill" aria-hidden="true" /> Stop everything
      <kbd className="hidden font-mono text-[10px] opacity-70 sm:inline">Esc</kbd>
    </button>
  );
}

function Rail() {
  const s = useFState();
  const { stage } = useWorkflow();
  const link = useStageLink();
  const list = useRef<HTMLOListElement>(null);
  const activeIndex = STAGES.findIndex((d) => d.id === stage);

  // scroll only the rail sideways; scrollIntoView would also drag the page down to the header
  useEffect(() => {
    const ol = list.current;
    const li = ol?.querySelector<HTMLElement>('[aria-current="step"]')?.parentElement;
    if (!ol || !li) return;
    const left = li.offsetLeft, right = left + li.offsetWidth;
    if (left < ol.scrollLeft) ol.scrollLeft = left;
    else if (right > ol.scrollLeft + ol.clientWidth) ol.scrollLeft = right - ol.clientWidth;
  }, [stage]);

  return (
    <nav aria-label="Workflow stations" className="mx-auto max-w-[1400px] px-4 md:px-8">
      <ol ref={list} className="scroll-x-snap relative flex overflow-x-auto">
        {STAGES.map((d, i) => {
          const st = stageStatus(d.id, s);
          const current = d.id === stage;
          return (
            <li key={d.id} className="relative min-w-[6.5rem] flex-1 snap-start md:min-w-[7.5rem]">
              <a
                {...link(d.id)}
                aria-current={current ? "step" : undefined}
                className={cx("group flex min-h-12 w-full flex-col items-start justify-center gap-0.5 px-2 pb-2 pt-1.5 text-left md:min-h-14", current ? "text-fg" : "text-fg-2 hover:text-fg")}
              >
                <span className="flex items-center gap-1.5 font-mono text-[11px] tabular">
                  {String(d.n).padStart(2, "0")}
                  <RailGlyph status={st} />
                </span>
                <span className="label-condensed text-[14px]">{d.name}</span>
              </a>
              {current && (
                <motion.span layoutId="rail-current" aria-hidden="true" className="absolute inset-x-2 bottom-0 h-[3px] bg-marker" transition={{ type: "spring", stiffness: 420, damping: 36 }} />
              )}
              {i < activeIndex && !current && <span aria-hidden="true" className="absolute inset-x-2 bottom-0 h-px bg-fg-2/60" />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function RailGlyph({ status }: { status: ReturnType<typeof stageStatus> }) {
  if (status === "working") return <CircleNotch size={12} role="img" aria-label="working" className="animate-spin text-fg motion-reduce:animate-none" />;
  if (status === "done") return <CheckCircle size={12} weight="fill" role="img" aria-label="done" className="text-pass" />;
  if (status === "error") return <WarningCircle size={12} weight="fill" role="img" aria-label="failed" className="text-fail" />;
  return <span className="sr-only">not run yet</span>;
}
