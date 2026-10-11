import { useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "motion/react";
import { ArrowRight, CircleNotch } from "@phosphor-icons/react";
import { Instrument, Notice, Station } from "../components/Station";
import { Button } from "../components/ui";
import { actions, isRunActive, jobRunning, useFState } from "../lib/store";
import { useWorkflow } from "../lib/workflow";
import type { Scorecard, State } from "../lib/types";
import { ExperimentList } from "./parts/score-list";
import { ExperimentDetail } from "./parts/score-detail";
import { ScorecardPanel } from "./parts/score-scorecard";
import { newestFirst, tidy } from "./parts/score-text";
import { AutoStops } from "./parts/score-safety";
import { AllFindings } from "./parts/score-findings";
import { Coverage } from "./parts/score-coverage";

export function ScoreStation() {
  const s = useFState();
  const { intent, consumeIntent } = useWorkflow();
  const reduce = useReducedMotion();
  const experiments = useMemo(() => newestFirst(s?.experiments ?? []), [s?.experiments]);
  const [picked, setPicked] = useState<string | null>(null);
  const current = experiments.find((e) => e.experiment_id === picked) ?? experiments[0] ?? null;
  const logRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<HTMLDivElement>(null);

  // A hand-off from Break or Reconcile: open that experiment (adjusted during render), then consume the
  // intent and bring the log into view.
  const asked = intent?.kind === "inspect-experiment" ? intent.experimentId : null;
  if (asked && asked !== picked) setPicked(asked);
  // consuming the intent re-runs this effect, so the pending scroll lives in a ref, cleared only on unmount
  const scrollTimer = useRef(0);
  useEffect(() => () => window.clearTimeout(scrollTimer.current), []);
  useEffect(() => {
    if (!asked) return;
    consumeIntent();
    window.clearTimeout(scrollTimer.current);
    scrollTimer.current = window.setTimeout(() => logRef.current?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" }), 280);
  }, [asked, consumeIntent, reduce]);

  const select = (id: string, byPointer: boolean) => {
    setPicked(id);
    // on one column the detail sits under the list: follow the tap down to it
    if (byPointer && window.matchMedia("(max-width: 1023px)").matches) {
      requestAnimationFrame(() => detailRef.current?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" }));
    }
  };

  // the summary panels open an experiment in the log: select it, bring it into view, focus its row.
  // With a finding index (its place in exp.findings), land on that finding inside the detail instead.
  const open = (id: string, findingIdx?: number) => {
    setPicked(id);
    // read the refs after the re-render: picking another experiment replaces the detail element
    const toLog = () => {
      const target = window.matchMedia("(max-width: 1023px)").matches ? detailRef.current : logRef.current;
      target?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
      document.getElementById(`exp-tab-${id}`)?.focus({ preventScroll: true });
    };
    // two frames: the picked experiment's detail has committed by then, even if it was already open
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (findingIdx == null) return toLog();
        const el = document.getElementById(`exp-finding-${id}-${findingIdx}`);
        if (!el) return toLog();
        el.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
        el.focus({ preventScroll: true });
      }),
    );
  };

  return (
    <Station
      id="score"
      headline="Code scores it. Nothing gets applied."
      lede={
        <p>
          The Risk agent turns each experiment into a 0 to 10 severity. The Fix agent matches its findings against Ditto-specific rules and
          never applies one. The Scorecard grades every tested service A to F, worst first.
        </p>
      }
      code="Severity from user impact, blast radius, recovery time and intervention. Ditto-specific rules propose the fixes. Each service scores 100 minus its mean penalty."
      model="Rewrite the two risk sentences and add at most two fixes, tagged unverified, confidence capped at 0.6. It cannot move a number."
      controls={<AnalyzeControls s={s} />}
      instrument={<ScorecardPanel sc={s?.scorecard ?? null} />}
      below={
        <div className="flex flex-col gap-8">
        <AutoStops experiments={experiments} onOpen={open} />
        <div ref={logRef} className="scroll-mt-32">
          <Instrument title="Experiment log" meta={experiments.length ? `${experiments.length} experiments, newest first` : undefined}>
            {!current ? (
              <EmptyLog />
            ) : (
              <div className="-m-4 grid lg:grid-cols-[21rem_minmax(0,1fr)]">
                <div className="border-b border-line lg:border-b-0 lg:border-r">
                  <div className="lg:sticky lg:top-32">
                    <ExperimentList experiments={experiments} selected={current.experiment_id} onSelect={select} now={s?.now ?? 0} />
                  </div>
                </div>
                <ExperimentDetail ref={detailRef} key={current.experiment_id} exp={current} now={s?.now ?? 0} />
              </div>
            )}
          </Instrument>
        </div>
        <AllFindings experiments={experiments} state={s} onOpen={open} />
        <Coverage experiments={experiments} scorecard={s?.scorecard ?? null} services={s?.topology?.services ?? null} onOpen={open} />
        </div>
      }
    />
  );
}

function EmptyLog() {
  const { go } = useWorkflow();
  return (
    <div className="flex flex-col items-start gap-3">
      <p className="max-w-[52ch] text-[14px] leading-relaxed text-fg-2">
        No experiment reports yet. Plan one, launch it in Break, and its risk, fixes and play-by-play land here.
      </p>
      <Button variant="ghost" onClick={() => go("break")}>
        Go to Break <ArrowRight size={16} aria-hidden="true" />
      </Button>
    </div>
  );
}

function weakest(sc: Scorecard | null) {
  const r = sc?.ranking.filter((x) => x.score != null).sort((a, b) => (a.score ?? 0) - (b.score ?? 0))[0];
  return r ? `Weakest: ${r.service}, ${r.grade}, ${r.score!.toFixed(1)}.` : "";
}

function AnalyzeControls({ s }: { s: State | null }) {
  const llmAvail = !!s?.llm.available && !!s?.llm.enabled;
  const [wantLlm, setWantLlm] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const running = jobRunning(s, "analyze");
  const otherBusy = !running && (jobRunning(s) || isRunActive(s));
  const mine = s?.job && jobId && s.job.id === jobId ? s.job : null;
  const hasExperiments = (s?.experiments.length ?? 0) > 0;
  const useLlm = llmAvail && wantLlm;

  // the Notices below are the one live channel for this job: running, done or failed
  const status = mine?.status;

  const start = async () => {
    setErr(null);
    const r = await actions.job("analyze", { llm: useLlm });
    if (!r.ok) {
      setErr(r.status === 409 ? "Another job is already running. Wait for it to finish, then run analysis again." : r.error ?? "Analysis did not start.");
      return;
    }
    const j = (r.data as { job?: { id?: string } } | undefined)?.job;
    setJobId(j?.id ?? null);
  };

  const result = mine?.result as { risks?: number; fixes?: number } | undefined;
  const overall = s?.scorecard?.overall_score;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={start} disabled={!s || running || otherBusy || !hasExperiments} aria-describedby="analyze-help">
          {running && <CircleNotch size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}
          Run analysis
        </Button>
      </div>
      <label className="flex min-h-11 cursor-pointer items-start gap-3 text-[14px] leading-snug text-fg has-[:disabled]:cursor-not-allowed">
        <input
          type="checkbox"
          className="mt-0.5 size-4 shrink-0 accent-fg"
          checked={useLlm}
          disabled={!llmAvail || running}
          onChange={(e) => setWantLlm(e.target.checked)}
        />
        <span>
          Let the model rewrite the risk prose and suggest up to two fixes
          <span className="mt-0.5 block text-[12px] text-fg-2">
            {llmAvail ? `${s!.llm.model}. Anything it writes is tagged AI.` : "No model is available, so analysis runs on rules only."}
          </span>
        </span>
      </label>
      <p id="analyze-help" className="max-w-[46ch] text-[13px] leading-snug text-fg-2">
        Scores only the experiments that have no risk and fix output yet, then rebuilds the scorecard and reconciles predictions.
        Every number comes from code either way.
      </p>

      {running && <Notice>Running analysis: risk, then fixes, then the scorecard…</Notice>}
      {status === "done" && (
        <Notice tone="pass">
          Analysis done.{" "}
          {result?.risks === 0 ? "Every experiment already had risk and fix output, so nothing new was scored. " : result?.risks != null ? `Scored ${result.risks} ${result.risks === 1 ? "experiment" : "experiments"}. ` : ""}
          {overall != null && `Overall ${overall.toFixed(1)}/100. `}
          {weakest(s?.scorecard ?? null)}
        </Notice>
      )}
      {status === "failed" && <Notice tone="fail">{tidy(mine?.error) || "Analysis failed."} Launch an experiment if none has run, then run analysis again.</Notice>}
      {err && <Notice tone="fail">{tidy(err)}</Notice>}
      {otherBusy && !err && <Notice>Another job is running{s?.job?.label ? `: ${tidy(s.job.label)}` : ""}. Run analysis when it finishes.</Notice>}
      {!hasExperiments && s && <Notice>No experiment reports on disk. Launch an experiment, then run analysis.</Notice>}
    </div>
  );
}
