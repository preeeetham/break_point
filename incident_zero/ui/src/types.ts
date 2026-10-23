// Mirrors incident_zero/models.py and the server's JSON.

export type Phase =
  | "detected" | "investigating" | "diagnosing" | "auditing" | "planning" | "awaiting_approval"
  | "executing" | "verifying" | "reporting"
  | "resolved" | "dry_run_complete" | "escalated" | "rejected" | "failed";

export const TERMINAL: Phase[] = ["resolved", "dry_run_complete", "escalated", "rejected", "failed"];

export interface Alert {
  id: string; rule: string; service: string; signal: string;
  value: number | null; threshold: number | null; baseline: number | null; unit: string;
  query: string; first_seen: string; last_seen: string; active: boolean;
}

export interface Claim { text: string; evidence_id: string; quote: string }
export interface RootCause { component: string; category: string; cause: string; confidence: number; claims: Claim[] }
export interface Diagnosis {
  summary: string; root_causes: RootCause[]; explained_alerts: string[]; produced_by: "llm" | "rules"; created_at: string;
}
export interface AuditResult {
  passed: boolean; problems: string[]; unexplained_alerts: string[]; checked_claims: number; created_at: string;
}
export interface ProposedAction {
  action: string; parameters: Record<string, unknown>; reason: string; dry_run: boolean;
  preview: string | null; reversible: boolean;
}
export interface Plan {
  actions: ProposedAction[]; change_hash: string; produced_by: string; no_action_reason: string; created_at: string;
}
export interface Approval {
  required: boolean; decision: "pending" | "approved" | "rejected" | "sent_back" | "timed_out" | "not_requested";
  approver: string | null; timestamp: string | null; note: string; change_hash: string | null;
  requested_at: string | null; expires_at: string | null;
}
export interface ActionResult {
  action: string; parameters: Record<string, unknown>; execution_id: string | null; ok: boolean; dry_run: boolean;
  message: string; at: string;
}
export interface Incident {
  id: string; title: string; phase: Phase; created_at: string; updated_at: string; resolved_at: string | null;
  alerts: Alert[]; services: string[]; rounds: number; investigation_notes: string[]; human_notes: string[];
  diagnosis: Diagnosis | null; audits: AuditResult[]; plan: Plan | null; approval: Approval;
  execution: { attempted: boolean; results: ActionResult[]; rollback_performed: boolean;
               rollback_results: ActionResult[]; notes: string };
  verification: { checked: boolean; recovered: boolean | null; signals_checked: string[]; notes: string;
                  samples: { at: string; firing: string[] }[] };
  prevention: string[]; limitations: string[]; escalation_reason: string; report_summary: string; llm: string;
}
export interface IncidentSummary {
  id: string; title: string; phase: Phase; created_at: string; updated_at: string; services: string[];
  alerts: number; active_alerts: number; root_cause: string | null; confidence: number | null; approval: string;
}
export interface Evidence {
  id: string; source: string; tool: string; args: Record<string, unknown>; summary: string; content: string;
  data: unknown; created_at: string;
}
export interface Event {
  seq: number; ts: string; incident_id: string | null; agent: string; kind: string; title: string;
  detail: Record<string, unknown> | null; evidence_id: string | null;
}
export interface State {
  dry_run: boolean; frozen: boolean; eval_mode: boolean; llm: string | null; llm_reason: string;
  endpoints: { prometheus: string; jaeger: string; opensearch: string; action_api: string; notes: string[] };
  open: number; awaiting_approval: number; total: number; last_event: number; project: string;
  approval_timeout_s: number;
}
export interface AgentRow {
  agent: string; state: "idle" | "working"; incident_id: string | null; doing: string; since: string; runs: number;
  recent: Event[];
}
export interface RED { rps: number; error_ratio: number; p95_ms?: number | null; p95?: number | null }
export interface ServiceRow {
  now: RED | null; baseline: RED | null; callees: string[];
  container: { state: string; running: boolean; restart_count: number; oom_killed: boolean; health: string | null;
               uptime_s: number | null } | null;
}
export interface Services { at: number; error: string | null; services: Record<string, ServiceRow> }
export interface Scenario { id: string; title: string; split: string; faults: string[] }
export interface PracticeRun {
  id: string; scenario_id: string; title: string; split: string; started_at: number; deadline: number;
  status: "active" | "reverted"; notes: string[]; ended_at?: number;
  truth?: { components: string[]; categories: string[]; actions: string[] };
  score: { points: number; max: number; incidents: string[]; detail: Record<string, unknown> } | null;
}
