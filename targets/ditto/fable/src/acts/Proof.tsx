import type { MouseEvent, ReactNode } from "react";
import type { ExperimentSummary, ScoreRow, State } from "../lib/types";
import type { StageId } from "../lib/stages";
import { useConnection, useFState } from "../lib/store";
import { stageHref, useWorkflow, type Intent } from "../lib/workflow";
import { failureLabel, fmtS, timecode } from "../lib/format";
import { AiMark, Container } from "../components/ui";
import { abortExplain, tidy } from "../stations/parts/score-text";
import { didRun, isAutoStop, isModelExtra, plural, rankFindings, stopAt } from "../stations/parts/score-rollup";

const GRADE_ORDER = ["F", "D", "C", "B", "A"];

/** "Container crash" reads "container crash" mid sentence; "CPU stress" keeps its capitals. */
function inline(label: string): string {
  return /^[A-Z][a-z]/.test(label) ? label.charAt(0).toLowerCase() + label.slice(1) : label;
}

function words(list: string[]): string {
  if (list.length < 2) return list.join("");
  return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

const startMs = (e: ExperimentSummary) => Date.parse(e.started_at) || 0;

function gradeSpread(rows: ScoreRow[]): string {
  const counts = new Map<string, number>();
  for (const r of rows) {
    const g = r.score == null || !/^[A-F]$/.test(r.grade) ? "untested" : r.grade;
    counts.set(g, (counts.get(g) ?? 0) + 1);
  }
  return [...GRADE_ORDER, "untested"]
    .filter((g) => counts.has(g))
    .map((g) => `${counts.get(g)} ${g}`)
    .join(", ");
}

interface Link {
  stage: StageId;
  label: string;
  intent?: Intent;
}

interface Row {
  key: string;
  title: string;
  value: string;
  unit: string;
  body: ReactNode;
  links: Link[];
}

function rows(s: State): Row[] {
  const exps = [...s.experiments].sort((a, b) => startMs(a) - startMs(b));

  const ran = exps.filter(didRun);

  // 1 the map, then the planner: the same pick as the Map station, the first testable service by criticality
  const top = s.topology?.criticality.find((c) => s.topology?.services[c.service]?.chaos_relevant) ?? s.topology?.criticality[0];
  const picks = exps.filter((e) => e.planned_by !== "operator");
  const byModel = picks.some((e) => e.planned_by === "llm");
  const operator = exps.length - picks.length;
  const pickText = words(picks.slice(0, 3).map((e) => `${inline(failureLabel(e.failure_type))} on ${e.target_service}`));
  const aimed = !!top && picks.some((e) => e.target_service === top.service);
  const mark = byModel && <> <span className="inline-block align-[1px]"><AiMark /></span></>;
  const more = picks.length > 3 ? `, plus ${picks.length - 3} more` : "";
  const others = operator > 0 ? ` An operator chose the other ${operator}.` : "";
  const planner = (
    <>
      {top ? <>The map ranked {top.service} first, criticality {top.score.toFixed(1)}</> : <>No map has been drawn yet</>}
      {picks.length === 0 && <>. An operator chose every test in this run.</>}
      {picks.length > 0 && aimed && <>, and the planner{mark} went after it: {pickText}{more}.{others}</>}
      {picks.length > 0 && !aimed && <>. The planner{mark} picked {pickText}{more}.{others}</>}
    </>
  );

  // 2 failure types
  const types = [...new Set(ran.map((e) => e.failure_type))];
  const typeLabels = types.map((k) => inline(failureLabel(k)));

  // 3 ranked findings and grades
  const findings = rankFindings(exps).length;
  const overall = s.scorecard?.overall_score;
  const spread = gradeSpread(s.scorecard?.ranking ?? []);

  // 4 the first automatic stop
  const stop = exps.find(isAutoStop);
  const at = stop ? stopAt(stop) : null;
  // the safety monitor logs a safety_abort with its numbers; a watchdog-only stop logs just watchdog_fired
  const watchdogOnly = !!stop?.watchdog_fired && !stop.timeline?.some((t) => t.event === "safety_abort");
  const why = stop && !watchdogOnly ? tidy(abortExplain(stop).text) : null;

  // 5 fixes
  const recs = exps.flatMap((e) => e.fix?.recommendations ?? []);
  const extras = recs.filter(isModelExtra).length;

  return [
    {
      key: "plan",
      title: "The planner chose from the map",
      value: top ? top.score.toFixed(1) : "n/a",
      unit: top ? `criticality, ${top.service}` : "criticality",
      body: planner,
      links: [{ stage: "map", label: "Open Map" }, { stage: "plan", label: "Open Plan" }],
    },
    {
      key: "types",
      title: "Different ways to break it",
      value: String(types.length),
      unit: types.length === 1 ? "failure type" : "failure types",
      body: <>{plural(ran.length, "experiment")} ran {types.length === 1 ? "one failure type" : `${types.length} distinct failure types`}: {words(typeLabels)}.</>,
      links: [{ stage: "score", label: "Open Score" }],
    },
    {
      key: "findings",
      title: "Findings, ranked worst first",
      value: String(findings),
      unit: findings === 1 ? "finding above info" : "findings above info",
      body: (
        <>
          Overall score {overall != null ? overall.toFixed(1) : "n/a"} out of 100
          {spread ? <>; grades {spread}.</> : "."}
        </>
      ),
      links: [{ stage: "score", label: "Open Score" }],
    },
    stop
      ? {
          key: "stop",
          title: "The kill switch fired on its own",
          value: at != null ? timecode(at) : "n/a",
          unit: why ? "safety abort" : "watchdog stop",
          body: (
            <>
              {why ? (
                <>
                  The safety monitor rolled back {inline(failureLabel(stop.failure_type))} on {stop.target_service}
                  {at != null ? ` at ${timecode(at)}` : ""}: {why}.
                </>
              ) : (
                <>
                  The watchdog hit the hard deadline on {inline(failureLabel(stop.failure_type))} against {stop.target_service}
                  {at != null ? ` at ${timecode(at)}` : ""} and forced a rollback.
                </>
              )}
              {stop.time_to_recover_s != null && ` The API recovered in ${fmtS(stop.time_to_recover_s, 1)}.`}
            </>
          ),
          links: [{ stage: "score", label: `Inspect ${stop.experiment_id}`, intent: { kind: "inspect-experiment", experimentId: stop.experiment_id } }],
        }
      : {
          key: "stop",
          title: "No automatic stop was needed",
          value: "0",
          unit: "automatic stops",
          body: <>Every experiment stayed inside the limits and ran its full duration.</>,
          links: [{ stage: "score", label: "Open Score" }],
        },
    {
      key: "fixes",
      title: "Fixes, advisory only",
      value: String(recs.length),
      unit: recs.length === 1 ? "recommendation" : "recommendations",
      body: (
        <>
          {recs.length} {recs.length === 1 ? "recommendation" : "recommendations"} across {exps.length} {exps.length === 1 ? "experiment" : "experiments"}
          {extras > 0 && <>, {extras} of them model suggestions marked unverified</>}. Advisory only, nothing applied.
        </>
      ),
      links: [{ stage: "score", label: "Open Score" }],
    },
  ];
}

/** F5: what this run proved, computed live. Hidden when no experiment has run. */
export function Proof() {
  const s = useFState();
  const { go } = useWorkflow();
  const conn = useConnection();
  if (!s || !s.experiments?.length) return null;

  const follow = (l: Link) => (ev: MouseEvent<HTMLAnchorElement>) => {
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button !== 0) return;
    ev.preventDefault();
    go(l.stage, l.intent ?? null);
  };

  return (
    <section id="the-proof" aria-labelledby="proof-title" className="border-t border-line">
      <Container className="py-20 md:py-28">
        <h2 id="proof-title" className="display max-w-[22ch] text-balance text-[clamp(2rem,4.2vw,3.5rem)] text-fg">
          What this run proved.
        </h2>
        <p className="mt-5 max-w-[56ch] text-[17px] leading-relaxed text-fg-2">
          {conn === "replay"
            ? "Five readings from a recorded run, shown because the server is not reachable."
            : "Five readings from the last run, computed from the live state."}{" "}
          Each one opens the station that holds the evidence.
        </p>

        <ol className="mt-12 divide-y divide-line border-y border-line border-t-[3px] border-t-fg">
          {rows(s).map((r, i) => (
            <li
              key={r.key}
              className="grid grid-cols-[2rem_minmax(0,1fr)] gap-x-4 gap-y-4 py-6 md:grid-cols-[3rem_minmax(0,1fr)_minmax(12rem,16rem)] md:gap-x-8"
            >
              <span aria-hidden="true" className="pt-0.5 font-mono tabular text-[22px] leading-none text-fg">{i + 1}</span>
              <div className="min-w-0">
                <h3 className="display text-[19px] text-fg">{r.title}</h3>
                <p className="mt-2 max-w-[60ch] text-[15px] leading-snug text-fg-2">{r.body}</p>
                <p className="mt-2 flex flex-wrap gap-x-5">
                  {r.links.map((l) => (
                    <a
                      key={l.label}
                      href={stageHref(l.stage)}
                      onClick={follow(l)}
                      className="inline-flex min-h-11 items-center text-[14px] text-fg underline decoration-line decoration-1 underline-offset-4 hover:decoration-fg"
                    >
                      {l.label}
                    </a>
                  ))}
                </p>
              </div>
              <p className="col-start-2 font-mono leading-none text-fg tabular md:col-start-3 md:text-right">
                <span className="block text-[clamp(2rem,3.4vw,2.75rem)] tracking-tight">{r.value}</span>
                <span className="mt-2 block text-[12px] text-fg-2">{r.unit}</span>
              </p>
            </li>
          ))}
        </ol>
      </Container>
    </section>
  );
}
