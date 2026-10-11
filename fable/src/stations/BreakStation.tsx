import { Instrument, Station } from "../components/Station";
import { StripChart } from "../components/StripChart";
import { TopologyMap } from "../components/TopologyMap";
import { Feed } from "../components/Feed";
import type { MarkerState } from "../components/Marker";
import { jobRunning, useCatalog, useEvents, useFState } from "../lib/store";
import { useDraft, useLaunchMark, type Draft } from "../lib/draft";
import type { RunPhase, State, Thresholds, Topology } from "../lib/types";
import { LimitsList } from "./parts/baseline-limits";
import { LaunchControl, Preconditions, RunJobFailure, TestCard } from "./parts/break-launch";
import { useChecks } from "./parts/break-helpers";
import { BlastPanel, HealthTable, MarkerKey, PhaseSentence, RunCard, RunClock, SafetyAbort, StopEverything } from "./parts/break-run";
import { Outcome } from "./parts/break-outcome";

const FEED_SOURCES = ["injector", "probe", "system", "narrator"];

/**
 * The phase to show. The server reports idle while a run job is still planning (before the injector
 * starts) and again while it analyses (after the injector writes idle), so fill both gaps from the job.
 */
function shownPhase(s: State | null): RunPhase {
  if (!s) return "idle";
  if (s.run.phase !== "idle") return s.run.phase;
  if (jobRunning(s, "run") && s.job) {
    const newest = s.experiments[0];
    return newest && Date.parse(newest.started_at) / 1000 >= s.job.started_at - 1 ? "analyzing" : "preflight";
  }
  return "idle";
}

export function BreakStation() {
  const s = useFState();
  const draft = useDraft();
  const phase = shownPhase(s);
  const running = phase !== "idle";
  const { blocked } = useChecks(draft);

  return (
    <Station
      id="break"
      headline="One fault, four tripwires, two kill switches"
      lede={
        <p>
          The Injector applies one fault. The Safety monitor checks four limits every second and rolls back the moment one is crossed.
          The Guardian, a separate process, restores containers on its own.
        </p>
      }
      code="Injects one fault with every parameter clamped, checks four limits every second, rolls back in a finally block. The Guardian restores containers on the deadline or on STOP."
      model="Nothing. The model never sees the safety monitor's inputs or outputs."
      controls={
        running && s ? (
          <>
            <PhaseSentence phase={phase} run={s.run} />
            <StopEverything />
            <RunCard run={s.run} />
          </>
        ) : (
          <>
            <TestCard draft={draft} />
            <Preconditions draft={draft} />
            <LaunchControl draft={draft} blocked={blocked} />
            <RunJobFailure />
          </>
        )
      }
      instrument={running ? <RunningInstrument phase={phase} /> : <IdleInstrument draft={draft} />}
      below={<FeedPanel running={running} />}
    />
  );
}

function useLimits(): Thresholds | null {
  const s = useFState();
  const catalog = useCatalog();
  return catalog?.thresholds ?? s?.baseline?.thresholds ?? null;
}

function TraceInstrument({ running }: { running: boolean }) {
  const s = useFState();
  const t = useLimits();
  const points = s?.telemetry.points ?? [];
  return (
    <Instrument title="The API as users see it" meta={running ? "last 120 s, abort limits drawn" : "last 300 s, abort limits drawn"} grid>
      <StripChart
        points={points}
        bands={s?.telemetry.phases ?? []}
        now={s?.now}
        window={running ? 120 : 300}
        successFloor={t ? 1 - t.abort_error_rate_above : null}
        p95Ceiling={t?.abort_p95_ms_above ?? null}
        height={running ? 240 : 200}
        ariaLabel="API success rate and p95 latency, with the abort limits"
      />
      {!points.length && (
        <p className="mt-3 text-[13px] text-fg-2">
          {s?.session.armed ? "Armed. The first samples land here within seconds." : "No samples. Arm the probes: launch only into a flat trace."}
        </p>
      )}
    </Instrument>
  );
}

function RunningInstrument({ phase }: { phase: RunPhase }) {
  const s = useFState();
  const t = useLimits();
  if (!s) return null;
  return (
    <div className="flex flex-col gap-6">
      <SafetyAbort run={s.run} t={t} />
      <RunClock run={s.run} phase={phase} t={t} />
      <TraceInstrument running />
      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        {s.topology ? (
          <BlastPanel topology={s.topology} run={s.run} phase={phase} />
        ) : (
          <Instrument title="Blast radius">
            <p className="text-[13px] text-fg-2">No map on disk, so the blast radius cannot be drawn. The health table still reports every service.</p>
          </Instrument>
        )}
        <HealthTable run={s.run} />
      </div>
    </div>
  );
}

function IdleInstrument({ draft }: { draft: Draft | null }) {
  const s = useFState();
  const t = useLimits();
  const mark = useLaunchMark();
  const newest = s?.experiments[0] ?? null;
  const fresh = !!newest && !!mark && Date.parse(newest.started_at) / 1000 >= mark.at - 5;
  const now = s?.now ?? 0;
  return (
    <div className="flex flex-col gap-6">
      {newest && fresh && <Outcome exp={newest} fresh now={now} />}
      <TraceInstrument running={false} />
      {s?.topology ? (
        <PreviewMap topology={s.topology} draft={draft} />
      ) : (
        <Instrument title="Blast radius, predicted">
          <p className="text-[13px] text-fg-2">No map yet. Map the system to see where the fault is expected to spread.</p>
        </Instrument>
      )}
      <Instrument title="Limits in force" meta={s?.baseline ? "derived from the baseline" : "config floors"}>
        {t ? <LimitsList t={t} compact columns /> : <p className="text-[13px] text-fg-2">Loading the limits from the server.</p>}
      </Instrument>
      {newest && !fresh && <Outcome exp={newest} fresh={false} now={now} />}
    </div>
  );
}

function PreviewMap({ topology, draft }: { topology: Topology; draft: Draft | null }) {
  const target = draft?.summary.target ?? null;
  const expected = draft?.summary.expected ?? [];
  const states: Record<string, MarkerState> = {};
  for (const x of expected) states[x] = "predicted";
  if (target) states[target] = "target";
  const meta = !draft ? "no test planned" : expected.length ? "from the planner's hypothesis" : target ? "the hypothesis is written at launch" : "the planner picks at launch";
  return (
    <Instrument title="Blast radius, predicted" meta={meta}>
      <TopologyMap
        topology={topology}
        ariaLabel={`Application map. ${target ? `Target ${target}.` : "No target yet."} ${expected.length ? `Predicted to be affected: ${expected.join(", ")}.` : ""}`}
        states={states}
        selected={target}
        showScores={false}
        height={280}
      />
      <MarkerKey only={["target", "predicted"]} />
    </Instrument>
  );
}

function FeedPanel({ running }: { running: boolean }) {
  const events = useEvents();
  return (
    <Instrument title="Live feed" meta="injector, probe, system and narrator, newest first">
      <div role="region" aria-label="Live feed" tabIndex={0} className={running ? "max-h-[26rem] overflow-y-auto" : "max-h-[18rem] overflow-y-auto"}>
        <Feed
          events={events}
          sources={FEED_SOURCES}
          limit={80}
          ariaLabel="Injector, probe, system and narrator events"
          empty="The feed is quiet. Launch an experiment and every step narrates itself here."
        />
      </div>
    </Instrument>
  );
}
