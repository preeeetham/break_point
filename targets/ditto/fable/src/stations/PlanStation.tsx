import { useEffect, useState } from "react";
import { Instrument, Station } from "../components/Station";
import { Button } from "../components/ui";
import { AGENT_LEDGER } from "../lib/stages";
import { useCatalog, useFState } from "../lib/store";
import { useWorkflow, type Intent } from "../lib/workflow";
import { failureLabel } from "../lib/format";
import { autoRequest, manualRequest, saveDraft, useDraft, usePlanRequest, type DraftMode } from "../lib/draft";
import type { Plan } from "../lib/types";
import { ModeSwitch } from "./parts/plan-mode";
import { CandidateRanking, PickPanel, PlannerControls } from "./parts/plan-auto";
import { FailurePicker, ManualSummary, TargetMap } from "./parts/plan-manual";
import { effectiveParams, lockReasons, whyNot, type Params } from "./parts/plan-helpers";

const LEDGER = AGENT_LEDGER.find((a) => a.name === "Planner")!;

export function PlanStation() {
  const s = useFState();
  const catalog = useCatalog();
  const draft = useDraft();
  const req = usePlanRequest();
  const { intent, consumeIntent, go } = useWorkflow();

  const fromDraft = draft?.mode === "manual" ? draft.request : null;
  const [mode, setMode] = useState<DraftMode>(draft?.mode ?? "auto");
  // follow the server's model status until the operator flips the switch
  const [llmChoice, setLlm] = useState<boolean | null>(req.status !== "idle" ? req.llm : null);
  const llm = llmChoice ?? (!!s?.llm.available && !!s?.llm.enabled);
  const [target, setTarget] = useState<string | null>(fromDraft?.target ?? null);
  const [failure, setFailure] = useState<string | null>(fromDraft?.failure ?? null);
  const [overrides, setOverrides] = useState<Params>(fromDraft?.params ?? {});
  const [origin, setOrigin] = useState<string | null>(draft?.mode === "manual" ? draft.summary.origin : null);

  // The Scout hands over a target and a failure to test. Prefill from it while rendering (so the first
  // paint already shows it), then consume it so a later visit starts clean.
  const [taken, setTaken] = useState<Intent>(null);
  if (intent?.kind === "plan-manual" && intent !== taken) {
    setTaken(intent);
    setMode("manual");
    setTarget(intent.target);
    setFailure(intent.failure);
    setOverrides({});
    setOrigin(intent.reason ?? `${failureLabel(intent.failure).toLowerCase()} on ${intent.target}.`);
  }
  useEffect(() => {
    if (intent?.kind === "plan-manual") consumeIntent();
  }, [intent, consumeIntent]);

  const locks = s?.topology && catalog ? lockReasons(s.topology, catalog) : {};
  const chosen = catalog?.failures.find((f) => f.key === failure) ?? null;
  const params = effectiveParams(chosen, overrides);

  // If the Scout suggested something the policy or the catalog will not allow, say so instead of failing at launch.
  let prefillProblem: string | null = null;
  if (origin && target && catalog) {
    if (locks[target]) prefillProblem = `${target} cannot be tested: ${locks[target]}. Choose another target.`;
    else if (failure && chosen && !chosen.applies_to.includes(target) && !chosen.policy_blocked_on.includes(target)) prefillProblem = `${failureLabel(chosen.key)} does not apply to ${target}. Choose another failure.`;
    else if (failure && chosen && whyNot(chosen, target)) prefillProblem = `${whyNot(chosen, target)} Choose another failure.`;
    else if (failure && !chosen) prefillProblem = `The catalog has no failure called ${failure}. Choose one from the list.`;
  }

  const pickTarget = (svc: string) => {
    if (locks[svc]) return;
    setTarget(svc);
    const f = catalog?.failures.find((x) => x.key === failure);
    if (f && (!f.applies_to.includes(svc) || whyNot(f, svc))) {
      setFailure(null);
      setOverrides({});
    }
  };

  const continueAuto = (plan: Plan) => {
    const pick = plan.pick;
    saveDraft({
      mode: "auto",
      request: autoRequest(llm),
      pick,
      summary: {
        target: pick?.target_service ?? null,
        failure: pick?.failure_type ?? null,
        params: pick?.parameters ?? {},
        plannedBy: pick?.planned_by ?? null,
        hypothesis: pick?.hypothesis.statement ?? null,
        expected: pick?.hypothesis.expected_affected ?? [],
        origin: null,
      },
    });
    go("break");
  };

  const continueManual = () => {
    if (!target || !failure) return;
    saveDraft({
      mode: "manual",
      request: manualRequest(target, failure, params),
      pick: null,
      summary: { target, failure, params, plannedBy: "operator", hypothesis: null, expected: [], origin },
    });
    go("break");
  };

  return (
    <Station
      id="plan"
      headline="Code ranks. The model picks from five."
      lede={
        <p>
          Code scores every service and failure pair by criticality, with a bonus for pairs never tested. Only the top five go to the
          model, which may pick one and write a hypothesis you can prove wrong. Nothing is injected here.
        </p>
      }
      code={LEDGER.code}
      model={LEDGER.model}
      controls={
        <>
          <ModeSwitch value={mode} onChange={setMode} />
          {mode === "auto" ? (
            <PlannerControls llm={llm} setLlm={setLlm} onContinue={continueAuto} />
          ) : (
            <ManualSummary catalog={catalog} target={target} failure={failure} params={params} origin={origin} prefillProblem={prefillProblem} onContinue={continueManual} />
          )}
        </>
      }
      instrument={
        mode === "auto" ? (
          <PickPanel />
        ) : !s?.topology ? (
          <Instrument title="Choose the target">
            <div className="flex flex-col items-start gap-3">
              <p className="text-[14px] text-fg-2">No map yet. Map the system to choose a target on it.</p>
              <Button variant="ghost" onClick={() => go("map")}>Go to Map</Button>
            </div>
          </Instrument>
        ) : (
          <TargetMap topology={s.topology} locks={locks} target={target} onTarget={pickTarget} />
        )
      }
      below={
        mode === "auto" ? (
          <CandidateRanking />
        ) : catalog ? (
          <FailurePicker
            catalog={catalog}
            target={target}
            failure={failure}
            onFailure={(k) => {
              setFailure(k);
              setOverrides({});
            }}
            params={params}
            onParam={(name, v) => setOverrides((o) => ({ ...o, [name]: v }))}
            onReset={() => setOverrides({})}
          />
        ) : (
          <Instrument title="Choose the failure">
            <p className="text-[14px] text-fg-2">Loading the failure catalog from the server.</p>
          </Instrument>
        )
      }
    />
  );
}
