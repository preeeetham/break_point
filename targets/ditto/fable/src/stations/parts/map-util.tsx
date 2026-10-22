import type { EdgeKind } from "../../lib/types";

/** The four evidence layers the Topology agent reads, styled like the map draws them. */
export const LAYERS: { kind: EdgeKind; label: string; dash?: string; width: number; strong?: boolean }[] = [
  { kind: "depends_on", label: "compose depends_on", width: 1.5 },
  { kind: "env", label: "Environment variables", dash: "5 4", width: 1.5, strong: true },
  { kind: "proxy", label: "nginx routes", dash: "1 4", width: 2 },
  { kind: "documented", label: "Documented in the repo", dash: "8 3 2 3", width: 1.25 },
];
