// Mirrors docs/UI_CONTRACT.md §2 and faultline/schemas.py. Only fields the UI reads are typed strictly.

export type AgentKey =
  | "onboarding" | "topology" | "scout" | "baseline" | "planner" | "injector"
  | "guardian" | "risk" | "fix" | "scorecard" | "reconcile";

export type AgentRunState = "idle" | "working" | "done" | "error";

export interface AgentStatus {
  state: AgentRunState;
  at: number | null;
  detail: string;
}

export type EventLevel = "info" | "success" | "warn" | "error" | "chaos" | "ai";

export interface EventItem {
  seq: number;
  ts: number;
  level: EventLevel;
  source: string;
  text: string;
  data?: Record<string, unknown>;
}

export type JobKind = "topology" | "scout" | "baseline" | "run" | "analyze" | "heal" | "plan" | "onboard" | "reconcile";

export interface Job {
  id: string;
  kind: JobKind;
  label: string;
  status: "running" | "done" | "failed" | "cancelled";
  started_at: number;
  ended_at: number | null;
  error: string | null;
  result: Record<string, unknown>;
}

export type RunPhase = "idle" | "preflight" | "injecting" | "rollback" | "recovering" | "soaking" | "analyzing";

export interface ServiceHealth {
  alive: number | null;
  ready: number | null;
  status: string | null;
}

export interface Hypothesis {
  statement: string;
  expected_affected: string[];
  expected_max_error_rate: number;
  expected_recovery_s: number;
}

export interface TimelineEntry {
  t_plus_s: number;
  event: string;
  detail?: string;
  at?: string;
}

export interface RunState {
  phase: RunPhase;
  experiment_id: string | null;
  target_service: string | null;
  failure_type: string | null;
  parameters: Record<string, number | boolean> | null;
  rationale: string | null;
  hypothesis: Hypothesis | string | null;
  planned_by: "llm" | "deterministic" | "operator" | null;
  elapsed_s: number | null;
  deadline_in_s: number | null;
  max_s: number | null;
  success_rate: number | null;
  p95_ms: number | null;
  sample_count: number | null;
  consecutive_ok?: number | null;
  soak_elapsed_s?: number | null;
  health: Record<string, ServiceHealth> | null;
  timeline?: TimelineEntry[];
  safety: { abort_reason: string | null; detail: Record<string, unknown> } | null;
  blast: { expected: string[]; observed: string[] } | null;
}

export interface TelemetryPoint {
  t: number;
  success: number | null;
  p95: number | null;
  n: number;
}

export interface PhaseBand {
  from: number;
  to: number | null;
  phase: RunPhase;
  experiment_id: string | null;
}

export interface Service {
  role: string;
  stateful: boolean;
  entrypoint: boolean;
  container: string | null;
  state: string | null;
  health: string | null;
  chaos_relevant: boolean;
  criticality: number;
  mem_limit_mb?: number | null;
  image?: string;
}

export type EdgeKind = "depends_on" | "env" | "proxy" | "documented" | "observed";

export interface Edge {
  src: string;
  dst: string;
  kind: EdgeKind;
  evidence?: string;
}

export interface Criticality {
  service: string;
  score: number;
  fan_in: number;
  transitive_dependents: string[];
  reasons: string[];
}

export interface Topology {
  services: Record<string, Service>;
  edges: Edge[];
  criticality: Criticality[];
  findings: string[];
  generated_at: string;
}

export interface Thresholds {
  abort_error_rate_above: number;
  abort_p95_ms_above: number;
  trailing_samples: number;
  non_target_health_failures_in_a_row: number;
  max_experiment_seconds: number;
  watchdog_grace_seconds: number;
  recovery_success_rate_at_least: number;
  recovery_p95_ms_below: number;
  recovery_consecutive_checks: number;
  recovery_timeout_seconds: number;
  baseline_p95_ms: number | null;
}

export interface Baseline {
  captured_at: string;
  duration_s: number;
  api: { sample_count: number; success_rate: number; p50_ms: number; p95_ms: number; p99_ms: number };
  thresholds: Thresholds;
  health_availability: Record<string, number>;
  notes: string[];
}

export interface Finding {
  code: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  title: string;
  evidence: string;
}

export interface Risk {
  severity: number;
  level: "low" | "medium" | "high" | "critical";
  summary: string;
  production_implication: string;
  blast_radius: number;
  user_impact?: number;
  recovery_s?: number | null;
  factors: Record<string, number>;
  generated_by: string;
}

export interface Recommendation {
  title: string;
  category: string;
  effort: string;
  confidence: number;
  rationale: string;
  steps: string[];
  verify: string[];
  references: string[];
}

export interface ExperimentSummary {
  experiment_id: string;
  status: "completed" | "aborted" | "failed" | "skipped";
  started_at: string;
  ended_at: string | null;
  target_service: string;
  failure_type: string;
  parameters: Record<string, number | boolean>;
  rationale: string;
  planned_by: "llm" | "deterministic" | "operator";
  criticality_score: number | null;
  hypothesis: Hypothesis;
  hypothesis_held: boolean | null;
  hypothesis_notes?: string | string[];
  baseline_success_rate: number | null;
  baseline_p95_ms: number | null;
  during_success_rate: number | null;
  during_p95_ms: number | null;
  after_success_rate: number | null;
  time_to_recover_s: number | null;
  outage_duration_s: number | null;
  recovered: boolean | null;
  auto_restarted_by_docker: boolean | null;
  remediation_actions: string[];
  data_integrity_ok: boolean | null;
  data_integrity_detail?: string;
  aborted_early: boolean;
  abort_reason: string | null;
  watchdog_fired: boolean;
  operator_stop: boolean;
  affected_services_observed: string[];
  findings: Finding[];
  risk: Risk | null;
  fix: { generated_by: string; recommendations: Recommendation[] } | null;
  timeline: TimelineEntry[];
  reproduce: string[];
}

export interface ScoreRow {
  service: string;
  score: number | null;
  grade: string;
  experiments: number;
  worst_success_rate: number | null;
  slowest_recovery_s: number | null;
  findings: number;
  notes: string[];
}

export interface Scorecard {
  overall_score: number | null;
  summary: string;
  experiments_total?: number;
  ranking: ScoreRow[];
}

export type WeaknessCategory =
  | "shallow_health_check" | "missing_health_check" | "no_redundancy" | "no_resource_limits"
  | "weak_restart_policy" | "no_retry_or_backoff" | "port_beyond_ingress" | "no_rate_limiting";

export interface PredictedWeakness {
  weakness_id: string;
  category: WeaknessCategory;
  services: string[];
  title: string;
  evidence: string;
  predicted_severity: number;
  predicted_level: "low" | "medium" | "high" | "critical";
  factors: Record<string, number>;
  suggested_experiment: { failure_type: string; target_service: string; reason: string; policy_note: string } | null;
  confirming_finding_codes: string[];
  confirming_failure_types?: string[];
  why_it_matters: string;
  status: "predicted" | "confirmed" | "cleared";
  label: string;
  generated_by: string;
}

export interface WeaknessReport {
  generated_at: string;
  project: string;
  compose_file: string;
  knowledge_file: string | null;
  label: string;
  checks_run: string[];
  notes: string[];
  generated_by: string;
  weaknesses: PredictedWeakness[];
  systemic_patterns: { title: string; weakness_ids: string[]; narrative: string; generated_by: string }[];
}

export interface WeaknessOutcome {
  weakness_id: string;
  category: string;
  services: string[];
  predicted_severity: number;
  predicted_level: string;
  state: "confirmed" | "cleared" | "untested";
  experiments: string[];
  confirming_findings: string[];
  detail: string;
}

export interface Reconciliation {
  generated_at: string;
  project: string;
  predicted_at: string | null;
  predicted_total: number;
  confirmed: number;
  cleared: number;
  untested: number;
  experiments_considered: number;
  outcomes: WeaknessOutcome[];
  summary: string;
}

export interface OnboardingReport {
  slug: string;
  source: string;
  resolved_path: string;
  compose_path: string;
  readme_path: string | null;
  nginx_conf_path: string | null;
  services: string[];
  structural_edges: number;
  generated_by: "llm" | "structural-only";
  model: string;
  knowledge: {
    health_port: number | null;
    cluster_alias: string | null;
    roles: Record<string, string>;
    documented_edges: { src: string; dst: string; evidence: string }[];
    references: string[];
  };
  rejected: string[];
  notes: string[];
  output_path: string | null;
}

export interface OnboardingComparison {
  final_graph_coverage: { truth_relationships: number; covered_by_generated_or_structural: number; coverage: number; uncovered: string[] };
  edges: { generated: number; truth: number; matched: number; precision: number; recall: number; f1: number; matched_pairs: string[]; missed: string[]; extra: string[] };
  roles: { compared: number; agreed: number; accuracy: number; disagreed: unknown[] };
  health_port: { generated: number | null; truth: number | null; match: boolean };
  cluster_alias: { generated: string | null; truth: string | null; match: boolean };
}

export interface OnboardingState {
  at?: number;
  repo?: string;
  report: OnboardingReport;
  comparison: OnboardingComparison | null;
}

export interface LlmStatus {
  provider: string;
  model: string;
  available: boolean;
  enabled: boolean;
  error: string;
}

export interface Session {
  armed: boolean;
  error: string | null;
  probe: { running: boolean; rps: number; canary: string; samples: number };
  project?: string;
  ditto_url?: string;
}

export interface GuardianBeat {
  at: number;
  stop: boolean;
  active?: unknown;
  actions: unknown[];
  down: Record<string, unknown>;
}

export interface State {
  now: number;
  seq: number;
  events: EventItem[];
  session: Session;
  job: Job | null;
  run: RunState;
  telemetry: { points: TelemetryPoint[]; phases: PhaseBand[] };
  stop_requested: boolean;
  guardian: GuardianBeat | null;
  agents: Record<AgentKey, AgentStatus>;
  llm: LlmStatus;
  topology: Topology | null;
  baseline: Baseline | null;
  experiments: ExperimentSummary[];
  scorecard: Scorecard | null;
  weaknesses: WeaknessReport | null;
  reconciliation: Reconciliation | null;
  onboarding: OnboardingState | null;
  target: { compose_file: string; knowledge_file: string; project: string } | null;
}

export interface CatalogParam {
  name: string;
  default: number | boolean;
  lo?: number;
  hi?: number;
  unit?: string;
  desc?: string;
  type: "number" | "bool";
}

export interface CatalogFailure {
  key: string;
  title: string;
  slide_category: string;
  description: string;
  degrade_only: boolean;
  expected_recovery_s: number;
  requires_images: string[];
  available: boolean;
  unavailable_reason: string | null;
  params: CatalogParam[];
  applies_to: string[];
  policy_blocked_on: string[];
}

export interface Catalog {
  failures: CatalogFailure[];
  protected: Record<string, { reason: string; allowed_failure_types: string[] }>;
  thresholds: Thresholds;
}

export interface PlanCandidate {
  service: string;
  failure_type: string;
  priority: number;
  reasons: string[];
  criticality: number;
}

export interface PlanPick {
  experiment_id?: string;
  target_service: string;
  failure_type: string;
  parameters: Record<string, number | boolean>;
  rationale: string;
  hypothesis: Hypothesis;
  planned_by: "llm" | "deterministic" | "operator";
  planner_notes: string[];
  reproduce: string[];
  criticality_score?: number;
}

export interface Plan {
  candidates: PlanCandidate[];
  pick: PlanPick | null;
  llm: { used: boolean; provider: string; model: string; available: boolean; error: string };
}

export interface Narrative {
  header: string;
  timeline: string[];
  impact: string;
  recovery: string;
  hypothesis: string;
  safety: string | null;
  findings: string[];
  risk: string;
  fix: string;
  error: string | null;
  llm: string | null;
}

export interface RunRequest {
  mode: "auto" | "manual";
  target?: string;
  failure?: string;
  params?: Record<string, number | boolean>;
  experiments?: number;
  llm?: boolean;
  allow_protected?: boolean;
  analyze?: boolean;
}
