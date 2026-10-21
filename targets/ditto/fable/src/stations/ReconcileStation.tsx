import { useMemo, useState } from "react";
import { ArrowRight, CircleNotch } from "@phosphor-icons/react";
import { Notice, Station } from "../components/Station";
import { Button } from "../components/ui";
import { actions, isRunActive, jobRunning, useCatalog, useFState } from "../lib/store";
import { useWorkflow } from "../lib/workflow";
import type { PredictedWeakness, State } from "../lib/types";
import { ReconcileTally } from "./parts/reconcile-tally";
import { ReconcileLoop } from "./parts/reconcile-loop";
import { ReconcileOutcomes } from "./parts/reconcile-outcomes";
import { newestFirst, tidy } from "./parts/score-text";

export function ReconcileStation() {
  const s = useFState();
  const catalog = useCatalog();
  const { go } = useWorkflow();
  const rec = s?.reconciliation ?? null;
  const weaknesses = useMemo(() => new Map<string, PredictedWeakness>((s?.weaknesses?.weaknesses ?? []).map((w) => [w.weakness_id, w])), [s?.weaknesses]);
  const experiments = useMemo(() => newestFirst(s?.experiments ?? []), [s?.experiments]);
  const now = s?.now ?? 0;

  const open = (eid: string) => go("score", { kind: "inspect-experiment", experimentId: eid });
  const plan = (target: string, failure: string, reason: string) => go("plan", { kind: "plan-manual", target, failure, reason });

  return (
    <Station
      id="reconcile"
      headline="Which predictions held up"
      lede={
        <p>
          Reconcile goes back over the Weakness Scout's predictions once experiments have run and says which were right, which were
          wrong and which nobody tested. Arithmetic only.
        </p>
      }
      code="A prediction is confirmed only when an experiment that bore on it raised a finding the Scout nominated, above info severity. An aborted run never clears one."
      controls={<ReconcileControls s={s} />}
      instrument={<ReconcileTally rec={rec} now={now} predicted={s?.weaknesses ? s.weaknesses.weaknesses.length : null} experiments={experiments.length} />}
      below={
        rec ? (
          <div className="flex flex-col gap-6">
            <ReconcileLoop rec={rec} weaknesses={weaknesses} experiments={experiments} onOpen={open} />
            <ReconcileOutcomes rec={rec} weaknesses={weaknesses} experiments={experiments} catalog={catalog} onOpen={open} onPlan={plan} />
          </div>
        ) : undefined
      }
    />
  );
}

const NO_SCOUT = /scout|predict|weakness/i;

function ReconcileControls({ s }: { s: State | null }) {
  const { go } = useWorkflow();
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const running = jobRunning(s, "reconcile");
  const otherBusy = !running && (jobRunning(s) || isRunActive(s));
  const mine = s?.job && jobId && s.job.id === jobId ? s.job : null;
  const status = mine?.status;
  const rec = s?.reconciliation ?? null;
  const nPred = s?.weaknesses?.weaknesses.length ?? null;
  const nExp = s?.experiments.length ?? 0;
  const failMsg = status === "failed" ? mine?.error ?? "" : err ?? "";
  const scoutMissing = (s && !s.weaknesses) || (!!failMsg && NO_SCOUT.test(failMsg));

  const start = async () => {
    setErr(null);
    const r = await actions.job("reconcile", {});
    if (!r.ok) {
      setErr(r.status === 409 ? "Another job is already running. Wait for it to finish, then reconcile again." : r.error ?? "Reconcile did not start.");
      return;
    }
    const j = (r.data as { job?: { id?: string } } | undefined)?.job;
    setJobId(j?.id ?? null);
  };

  return (
    <div className="flex flex-col gap-4">
      <div>
        <Button onClick={start} disabled={!s || running || otherBusy} aria-describedby="reconcile-help">
          {running && <CircleNotch size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}
          Reconcile predictions
        </Button>
      </div>
      <p id="reconcile-help" className="max-w-[46ch] text-[13px] leading-snug text-fg-2">
        {nPred != null
          ? `Compares ${nPred} predictions with ${nExp} experiment ${nExp === 1 ? "report" : "reports"}. No model, nothing injected, done in a second.`
          : "Compares the Scout's predictions with every experiment report. No model, nothing injected."}
      </p>

      {running && <Notice>Reconciling predictions against {nExp} experiment {nExp === 1 ? "report" : "reports"}…</Notice>}
      {status === "done" && rec && (
        <Notice tone="pass">
          Reconciled. {rec.predicted_total} predicted, {rec.confirmed} confirmed, {rec.cleared} cleared, {rec.untested} untested.
          {rec.untested > 0 && " The untested are what to break next."}
        </Notice>
      )}
      {scoutMissing ? (
        <div className="flex flex-col items-start gap-3">
          <Notice tone={failMsg ? "fail" : "neutral"}>No predictions on disk. Predict weaknesses, then reconcile.</Notice>
          <Button variant="ghost" onClick={() => go("predict")}>
            Go to Predict <ArrowRight size={16} aria-hidden="true" />
          </Button>
        </div>
      ) : (
        failMsg && <Notice tone="fail">{tidy(failMsg)}</Notice>
      )}
      {otherBusy && !err && <Notice>Another job is running{s?.job?.label ? `: ${tidy(s.job.label)}` : ""}. Reconcile when it finishes.</Notice>}
    </div>
  );
}
