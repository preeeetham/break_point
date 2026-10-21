import { useMemo, useRef, useState } from "react";
import { CaretRight } from "@phosphor-icons/react";
import { Instrument, Notice, Station } from "../components/Station";
import { Button, cx } from "../components/ui";
import { useConnection, useEvents, useFState } from "../lib/store";
import { useWorkflow } from "../lib/workflow";
import { AGENT_LEDGER } from "../lib/stages";
import { relTime, timecode } from "../lib/format";
import { Switch, Working } from "./parts/load-shared";
import { repoRel, repoRoot, stripExc, useStationJob } from "./parts/load-util";
import { Transcript } from "./parts/load-transcript";
import { useReplayPlayback } from "./parts/load-replay";
import { KnowledgeResult, Verdict } from "./parts/load-result";
import { NextSteps } from "./parts/load-target";

const OUT_DIR = ".cache/onboarding/knowledge"; // never config/: that would overwrite the hand-written ground truth
const TRUTH = "config/ditto_knowledge.yaml";
const DITTO = /(^|[/:])eclipse-ditto\/ditto(\.git)?\/?$/i;

const PICKS: { value: string; label: string; title: string }[] = [
  { value: "eclipse-ditto/ditto", label: "eclipse-ditto/ditto", title: "Eclipse Ditto, the stack Faultline tests. Has a hand-written file to grade against." },
  { value: ".", label: "this repo (.)", title: "Faultline's own compose file, read from disk" },
  { value: "dockersamples/example-voting-app", label: "dockersamples/example-voting-app", title: "A small demo app the agent has never seen" },
];

const LEDGER = AGENT_LEDGER.find((a) => a.name === "Onboarding")!;

function failFix(error: string): string {
  if (/no docker-compose file found|compose file .* (does not exist|is not a usable)/i.test(error)) return "Open Advanced, point the compose path at the file you run, then load again.";
  if (/neither an existing path nor a recognisable git repository/i.test(error)) return "Use a GitHub URL, owner/name such as eclipse-ditto/ditto, or a path that exists on the server's disk.";
  if (/git clone failed/i.test(error)) return "Check the name and that the repository is public, or clone it yourself and pass the local path.";
  if (/git is required/i.test(error)) return "Install git on the machine running Faultline, or pass a local path.";
  if (/already running/i.test(error)) return "Wait for that job to finish, then load again.";
  if (/replay/i.test(error)) return "Start the server with faultline dashboard --port 8777.";
  return "Read the last transcript line for the step that failed, fix it, then load again.";
}

function commandLine(body: { repo: string; compare_to: string | null; no_llm: boolean; compose: string | null }): string {
  return [
    "faultline onboard", body.repo, "--out", OUT_DIR,
    body.compare_to ? `--compare-to ${body.compare_to}` : "",
    body.compose ? `--compose ${body.compose}` : "",
    body.no_llm ? "--no-llm" : "",
  ].filter(Boolean).join(" ");
}

export function LoadStation() {
  const s = useFState();
  const events = useEvents();
  const replay = useConnection() === "replay";
  const { go } = useWorkflow();
  const job = useStationJob("onboard");
  const pb = useReplayPlayback();

  const [repo, setRepo] = useState("eclipse-ditto/ditto");
  const [useModel, setUseModel] = useState(true);
  // null until the reader touches it: a compose-file failure opens it by itself
  const [advancedChoice, setAdvanced] = useState<boolean | null>(null);
  const [compose, setCompose] = useState("");
  const [command, setCommand] = useState<{ repo: string; line: string } | null>(null);
  const [inputError, setInputError] = useState<string | null>(null);
  const repoInput = useRef<HTMLInputElement>(null);

  const root = repoRoot(s?.target ?? null);
  const llmReady = !!s?.llm.available && !!s?.llm.enabled;
  const j = job.job;
  const advanced = advancedChoice ?? (!!job.failed && /compose file/i.test(job.failed));

  // the live transcript: this job's onboarding lines plus the system lines that bracket it
  const lines = useMemo(() => {
    if (replay) return pb.lines;
    if (j) {
      return events.filter(
        (e) => e.ts >= j.started_at - 0.5 && (j.ended_at == null || e.ts <= j.ended_at + 1) &&
          (e.source === "onboarding" || (e.source === "system" && e.text.includes(j.label))),
      );
    }
    // the last job was something else: show the last onboarding run still in the buffer
    const ob = events.filter((e) => e.source === "onboarding");
    const start = ob.map((e) => e.text.startsWith("Onboarding ")).lastIndexOf(true);
    return start >= 0 ? ob.slice(start) : ob;
  }, [replay, pb.lines, j, events]);

  const running = replay ? pb.playing : job.busy;
  const result = replay ? pb.result : running || job.failed ? null : s?.onboarding ?? null;
  const report = result?.report ?? null;
  const t0 = replay ? pb.lines[0]?.ts ?? null : j?.started_at ?? null;

  function load() {
    const r = repo.trim();
    if (!r) {
      setInputError("Enter a GitHub URL, owner/name or a local path.");
      repoInput.current?.focus();
      return;
    }
    setInputError(null);
    if (replay) {
      pb.play();
      return;
    }
    const body = { repo: r, out: OUT_DIR, compare_to: DITTO.test(r) ? TRUTH : null, no_llm: !useModel, compose: compose.trim() || null };
    setCommand({ repo: r, line: commandLine(body) });
    job.start(body);
  }

  const transcriptMeta = replay
    ? "recorded run, played back"
    : running
      ? "live"
      : j
        ? `${j.status}${j.ended_at ? `, ${timecode(j.ended_at - j.started_at).replace("t+", "")}` : ""}`
        : lines.length
          ? "last run"
          : undefined;

  const controls = (
    <form noValidate onSubmit={(e) => { e.preventDefault(); load(); }} className="flex flex-col gap-5">
      <div className="flex flex-col gap-1.5">
        <label htmlFor="load-repo" className="label-condensed text-[14px] text-fg">Repository</label>
        <input
          ref={repoInput}
          id="load-repo"
          name="repo"
          value={repo}
          placeholder="owner/name, a GitHub URL or a path…"
          onChange={(e) => setRepo(e.target.value)}
          spellCheck={false}
          autoCapitalize="off"
          autoComplete="off"
          aria-invalid={inputError ? true : undefined}
          aria-describedby={inputError ? "load-repo-hint load-repo-error" : "load-repo-hint"}
          className="min-h-11 w-full rounded-[2px] border border-line bg-sheet px-3 font-mono text-[14px] text-fg placeholder:text-fg-2 focus-visible:border-fg aria-invalid:border-fail"
        />
        <p id="load-repo-hint" className="text-[12px] leading-snug text-fg-2">A GitHub URL, owner/name, or a path on the server's disk.</p>
        {inputError && <p id="load-repo-error" className="text-[12px] text-fail" role="alert">{inputError}</p>}
        <div role="group" aria-label="Quick picks" className="mt-1 flex flex-wrap gap-1.5">
          {PICKS.map((p) => {
            const on = repo.trim() === p.value;
            return (
              <button
                key={p.value}
                type="button"
                aria-pressed={on}
                onClick={() => setRepo(p.value)}
                title={p.title}
                className={cx(
                  "min-h-11 rounded-[2px] border px-2.5 text-left font-mono text-[12px] leading-tight transition-colors",
                  on ? "border-fg text-fg" : "border-line text-fg-2 hover:border-fg-2 hover:text-fg",
                )}
              >
                {p.label}
              </button>
            );
          })}
        </div>
      </div>

      <Switch
        id="load-model"
        checked={useModel}
        onChange={setUseModel}
        label="Use the model"
        tone="model"
        hint={
          llmReady
            ? useModel
              ? <>One bounded call to <span className="font-mono">{s?.llm.model}</span> for the edges compose cannot express.</>
              : "Structural edges only. No model call is made."
            : `The model is unavailable${s?.llm.error ? ` (${s.llm.error})` : ""}, so loading writes structural edges only.`
        }
      />

      <div className="flex flex-col gap-2">
        <button
          type="button"
          aria-expanded={advanced}
          aria-controls="load-advanced"
          onClick={() => setAdvanced(!advanced)}
          className="inline-flex min-h-11 items-center gap-2 self-start text-[14px] text-fg-2 hover:text-fg"
        >
          <CaretRight size={14} aria-hidden="true" className={cx("transition-transform duration-150 motion-reduce:transition-none", advanced && "rotate-90")} />
          Advanced: compose path
        </button>
        {advanced && (
          <div id="load-advanced" className="flex flex-col gap-1.5 border-l border-line pl-4">
            <label htmlFor="load-compose" className="label-condensed text-[14px] text-fg">Compose path</label>
            <input
              id="load-compose"
              name="compose"
              value={compose}
              onChange={(e) => setCompose(e.target.value)}
              placeholder="deployment/docker/docker-compose.yml…"
              spellCheck={false}
              autoCapitalize="off"
              autoComplete="off"
              aria-describedby="load-compose-hint"
              className="min-h-11 w-full rounded-[2px] border border-line bg-sheet px-3 font-mono text-[13px] text-fg placeholder:text-fg-2/70 focus-visible:border-fg"
            />
            <p id="load-compose-hint" className="text-[12px] leading-snug text-fg-2">Only when auto-detection picks the wrong file. Relative to the repository root. Leave empty to auto-detect.</p>
          </div>
        )}
      </div>

      <div>
        <Button type="submit" disabled={running || !!job.other}>Load repository</Button>
      </div>

      {replay && !running && !result && (
        <Notice>Replay: the server is not reachable. Load repository plays back a recorded onboarding of eclipse-ditto/ditto. Nothing is cloned and no model is called.</Notice>
      )}
      {running && (
        <Working>
          {replay ? "Playing back the recorded run: " : "Loading repository: "}
          {useModel || replay ? "cloning, reading docs, asking the model what compose leaves unsaid…" : "cloning, reading compose and docs…"}
        </Working>
      )}
      {job.other && <Notice>{job.other.label} is running. Load repository is available when it finishes.</Notice>}
      {job.refusal && (
        <Notice tone="fail">
          Refused: {stripExc(job.refusal)}
          <span className="mt-1 block text-fg">{failFix(job.refusal)}</span>
        </Notice>
      )}
      {!running && job.failed && !replay && (
        <Notice tone="fail" live={job.mine}>
          Load failed: {stripExc(job.failed)}
          <span className="mt-1 block text-fg">{failFix(job.failed)}</span>
        </Notice>
      )}
      {pb.error && <Notice tone="fail">{pb.error}</Notice>}
      {report && !running && (
        <Notice tone="pass">
          {replay ? "Recorded run played back. " : "Repository loaded. "}
          {report.services.length} services, {report.structural_edges} structural edges, {report.knowledge.documented_edges.length} documented edges kept.
        </Notice>
      )}
      {!report && s?.target && (
        <p className="text-[12px] leading-snug text-fg-2">
          Testing now: <span className="break-all font-mono text-fg">{repoRel(s.target.compose_file, root)}</span>
        </p>
      )}
    </form>
  );

  const instrument = (
    <div className="flex flex-col gap-6">
      <Instrument title="Transcript" meta={transcriptMeta}>
        <Transcript
          lines={lines}
          t0={t0}
          running={running}
          command={!replay && command && j?.label === `Onboard ${command.repo.slice(0, 60)}` ? command.line : null}
          empty={
            replay
              ? "Load repository plays back the recorded run here, line by line."
              : result
                ? `This run's lines are no longer in this page's event buffer. ${result.repo ?? report?.source ?? "The repository"} was loaded ${(s && result.at ? relTime(result.at, s.now) : "") || "earlier"}; its result is below. Load again to watch the agent work.`
                : "No repository loaded. Paste a GitHub URL, owner/name or a local path, then load it."
          }
        />
      </Instrument>
      {result?.comparison && <Verdict c={result.comparison} truthRel={TRUTH} />}
      {report && <KnowledgeResult report={report} outputRel={repoRel(report.output_path, root)} recorded={replay} />}
    </div>
  );

  return (
    <Station
      id="load"
      headline="Give it a repo it has never seen"
      lede="The Onboarding agent clones the repository, reads its compose file, README and architecture docs, then makes one bounded model call for the dependencies compose cannot express. Code throws out anything it cannot verify against the files."
      code={LEDGER.code}
      model={LEDGER.model}
      controls={controls}
      instrument={instrument}
      below={report && !running ? <NextSteps report={report} target={s?.target ?? null} root={root} recorded={replay} onDone={() => go("map")} /> : undefined}
    />
  );
}
