import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { STAGE_INDEX, STAGES, type StageId } from "./stages";

/** A hand-off from one station to another, e.g. the Scout asking the Planner to set up a specific test. */
export type Intent =
  | { kind: "plan-manual"; target: string; failure: string; reason?: string }
  | { kind: "inspect-experiment"; experimentId: string }
  | null;

interface Workflow {
  stage: StageId;
  intent: Intent;
  go: (stage: StageId, intent?: Intent) => void;
  next: () => void;
  prev: () => void;
  consumeIntent: () => Intent;
}

const Ctx = createContext<Workflow | null>(null);
const PREFIX = "#workflow/";

// The next station mounts only after the old one has animated out, so focus is handed over on mount.
let pendingFocus: StageId | null = null;
export function takePendingFocus(id: StageId): boolean {
  if (pendingFocus !== id) return false;
  pendingFocus = null;
  return true;
}

// explicit offset: scrollIntoView would honour html's scroll-padding (kept for focus) and stop short
export function scrollToWorkflow() {
  const el = document.getElementById("workflow");
  if (el) window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY });
}

export function stageHref(id: StageId): string {
  return `${PREFIX}${id}`;
}

function fromHash(): StageId | null {
  const h = window.location.hash;
  if (!h.startsWith(PREFIX)) return null;
  const id = h.slice(PREFIX.length) as StageId;
  return id in STAGE_INDEX ? id : null;
}

export function WorkflowProvider({ children }: { children: ReactNode }) {
  const [stage, setStage] = useState<StageId>(() => fromHash() ?? "load");
  const [intent, setIntent] = useState<Intent>(null);

  useEffect(() => {
    const onHash = () => {
      const id = fromHash();
      if (!id) return;
      setStage(id);
      pendingFocus = id;
      requestAnimationFrame(scrollToWorkflow);
    };
    window.addEventListener("hashchange", onHash);
    if (fromHash()) {
      requestAnimationFrame(scrollToWorkflow);
    }
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = useCallback((id: StageId, next: Intent = null) => {
    setStage(id);
    setIntent(next);
    history.replaceState(null, "", `${PREFIX}${id}`);
    pendingFocus = id;
    scrollToWorkflow();
    // same station: nothing remounts, so focus now
    requestAnimationFrame(() => {
      if (pendingFocus === id && document.getElementById(`station-${id}-title`)) {
        pendingFocus = null;
        document.getElementById(`station-${id}-title`)?.focus({ preventScroll: true });
      }
    });
  }, []);

  const value = useMemo<Workflow>(() => {
    const i = STAGE_INDEX[stage];
    return {
      stage,
      intent,
      go,
      next: () => i < STAGES.length - 1 && go(STAGES[i + 1].id),
      prev: () => i > 0 && go(STAGES[i - 1].id),
      consumeIntent: () => {
        const cur = intent;
        setIntent(null);
        return cur;
      },
    };
  }, [stage, intent, go]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkflow(): Workflow {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWorkflow outside WorkflowProvider");
  return v;
}
