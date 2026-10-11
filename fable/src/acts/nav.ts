import { useCallback } from "react";
import { useWorkflow } from "../lib/workflow";
import type { StageId } from "../lib/stages";

/** Open a station in the dark act. go() scrolls the workflow to the top of the viewport and hands focus to the station. */
export function useOpenStation() {
  const { go } = useWorkflow();
  return useCallback((id: StageId) => go(id), [go]);
}

export function useStartWalkthrough() {
  const open = useOpenStation();
  return useCallback(() => open("load"), [open]);
}
