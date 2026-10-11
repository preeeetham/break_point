import type { AgentKey, State } from "./types";

export type StageId = "load" | "map" | "predict" | "baseline" | "plan" | "break" | "score" | "reconcile";

export interface StageDef {
  id: StageId;
  n: number;
  name: string;
  agents: AgentKey[];
  agentNames: string;
  verb: string;
  line: string;
}

export const STAGES: StageDef[] = [
  { id: "load", n: 1, name: "Load", agents: ["onboarding"], agentNames: "Onboarding", verb: "Read the repo",
    line: "Reads an unfamiliar repo's compose file and docs, and writes down the dependencies compose leaves unsaid." },
  { id: "map", n: 2, name: "Map", agents: ["topology"], agentNames: "Topology", verb: "Draw the system",
    line: "Builds the dependency graph from five sources and ranks every service by how much breaks if it fails." },
  { id: "predict", n: 3, name: "Predict", agents: ["scout"], agentNames: "Weakness Scout", verb: "Guess where it breaks",
    line: "Seven code checks predict weaknesses before anything is injected. Every one is labelled a guess." },
  { id: "baseline", n: 4, name: "Baseline", agents: ["baseline"], agentNames: "Probes", verb: "Measure calm",
    line: "A synthetic client measures steady state. The abort limits are derived from it." },
  { id: "plan", n: 5, name: "Plan", agents: ["planner"], agentNames: "Planner", verb: "Choose the test",
    line: "Code ranks every service and failure pair. The model may pick one of the top five and write a hypothesis." },
  { id: "break", n: 6, name: "Break", agents: ["injector", "guardian"], agentNames: "Injector, Safety monitor, Guardian", verb: "Crash it safely",
    line: "One fault, checked every second against four limits, with two independent kill switches." },
  { id: "score", n: 7, name: "Score", agents: ["risk", "fix", "scorecard"], agentNames: "Risk, Fix, Scorecard", verb: "Grade and advise",
    line: "Code scores severity and grades each service. Fixes are advisory: nothing is ever applied." },
  { id: "reconcile", n: 8, name: "Reconcile", agents: ["reconcile"], agentNames: "Reconcile", verb: "Check the guesses",
    line: "Scores every prediction against what the experiments actually found." },
];

export const STAGE_INDEX: Record<StageId, number> = Object.fromEntries(STAGES.map((s, i) => [s.id, i])) as Record<StageId, number>;

export type StageStatus = "todo" | "working" | "done" | "error";

export function stageStatus(id: StageId, s: State | null): StageStatus {
  if (!s) return "todo";
  const def = STAGES[STAGE_INDEX[id]];
  const agentStates = def.agents.map((a) => s.agents?.[a]?.state);
  if (id === "break" && s.run.phase !== "idle") return "working";
  if (agentStates.includes("working")) return "working";
  if (s.job?.status === "running") {
    const k = s.job.kind;
    const map: Partial<Record<string, StageId>> = { onboard: "load", topology: "map", scout: "predict", baseline: "baseline", run: "break", analyze: "score", reconcile: "reconcile" };
    if (map[k] === id) return "working";
  }
  if (agentStates.includes("error") && !done(id, s)) return "error";
  return done(id, s) ? "done" : "todo";
}

function done(id: StageId, s: State): boolean {
  switch (id) {
    case "load": return !!s.onboarding;
    case "map": return !!s.topology;
    case "predict": return !!s.weaknesses;
    case "baseline": return !!s.baseline;
    case "plan": return s.experiments.length > 0 || s.agents.planner?.state === "done";
    case "break": return s.experiments.length > 0;
    case "score": return !!s.scorecard;
    case "reconcile": return !!s.reconciliation;
  }
}

export const AGENT_LEDGER: { name: string; stage: StageId | null; code: string; model: string }[] = [
  { name: "Onboarding", stage: "load", code: "Rejects edges to services that do not exist, derives the health port itself, refuses any role that would exempt a product service from testing.", model: "Proposes documented edges, roles and references." },
  { name: "Topology", stage: "map", code: "Five parsers and docker inspect build the map and the criticality ranking.", model: "Nothing. No model involved." },
  { name: "Weakness Scout", stage: "predict", code: "Seven checks decide what is a weakness and how severe it is.", model: "One sentence per finding, and at most two pattern notes that must cite existing findings." },
  { name: "Planner", stage: "plan", code: "Ranks every candidate, filters by policy, clamps every parameter.", model: "Picks one of the top five and writes a hypothesis. Cannot raise a limit or choose an unlisted target." },
  { name: "Injector", stage: "break", code: "Injects, observes every second, rolls back in a finally block, writes the report.", model: "Nothing. No model involved." },
  { name: "Safety monitor", stage: "break", code: "Four limits from config, checked every second. A watchdog fires even if the main loop hangs.", model: "Nothing. The model never sees its inputs or outputs." },
  { name: "Guardian", stage: "break", code: "A separate process with its own Docker client restores containers on the deadline or on STOP.", model: "Nothing. No model involved." },
  { name: "Risk", stage: "score", code: "Computes the 0 to 10 severity and its level.", model: "Rewrites two prose fields. Cannot move the number." },
  { name: "Fix", stage: "score", code: "18 Ditto-specific rules propose remediations. Nothing is ever applied.", model: "At most two extras, tagged unverified, confidence capped at 0.6." },
  { name: "Scorecard", stage: "score", code: "100 minus the mean penalty per service, graded A to F, worst first.", model: "Nothing. No model involved." },
  { name: "Reconcile", stage: "reconcile", code: "Arithmetic over predictions and experiment reports: confirmed, cleared or untested.", model: "Nothing. No model involved." },
  { name: "Narrator", stage: null, code: "A templated play-by-play of every experiment, in the feed.", model: "One optional closing paragraph, off by default." },
];
