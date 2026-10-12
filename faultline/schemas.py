"""Shared contracts between the agents. Every JSON file in reports/ validates against one of these."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- topology
class Edge(BaseModel):
    src: str
    dst: str
    kind: Literal["depends_on", "env", "proxy", "documented", "observed"]
    evidence: str = ""


class ServiceNode(BaseModel):
    name: str
    image: str = ""
    role: Literal["datastore", "app", "proxy", "ui", "other"] = "app"
    stateful: bool = False
    entrypoint: bool = False
    health_port: Optional[int] = None
    mem_limit_mb: Optional[int] = None
    aliases: list[str] = []
    chaos_relevant: bool = True
    container: Optional[str] = None
    state: Optional[str] = None
    health: Optional[str] = None
    restart_policy: Optional[str] = None


class Criticality(BaseModel):
    service: str
    fan_in: int
    transitive_dependents: list[str]
    score: float
    reasons: list[str]


class Topology(BaseModel):
    project: str
    generated_at: datetime = Field(default_factory=utcnow)
    services: dict[str, ServiceNode]
    edges: list[Edge]
    criticality: list[Criticality]
    findings: list[str] = []

    def direct_dependents(self, name: str) -> list[str]:
        return sorted({e.src for e in self.edges if e.dst == name})

    def dependencies(self, name: str) -> list[str]:
        return sorted({e.dst for e in self.edges if e.src == name})

    def transitive_dependents(self, name: str) -> list[str]:
        seen: set[str] = set()
        stack = [name]
        while stack:
            cur = stack.pop()
            for d in self.direct_dependents(cur):
                if d not in seen and d != name:
                    seen.add(d)
                    stack.append(d)
        return sorted(seen)

    def criticality_of(self, name: str) -> float:
        for c in self.criticality:
            if c.service == name:
                return c.score
        return 0.0

    def relevant_services(self) -> list[str]:
        return [n for n, s in self.services.items() if s.chaos_relevant]


# ---------------------------------------------------------------- observation
class WindowStats(BaseModel):
    sample_count: int = 0
    success_rate: Optional[float] = None
    p50_ms: Optional[float] = None
    p95_ms: Optional[float] = None
    p99_ms: Optional[float] = None
    by_operation: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}


class Thresholds(BaseModel):
    abort_error_rate_above: float
    abort_p95_ms_above: float
    trailing_samples: int
    non_target_health_failures_in_a_row: int
    max_experiment_seconds: int
    watchdog_grace_seconds: int
    recovery_success_rate_at_least: float
    recovery_p95_ms_below: float
    recovery_consecutive_checks: int
    recovery_timeout_seconds: int
    baseline_p95_ms: Optional[float] = None


class Baseline(BaseModel):
    captured_at: datetime
    duration_s: float
    ditto_url: str
    api: WindowStats
    health_availability: dict[str, Optional[float]]
    thresholds: Thresholds
    notes: list[str] = []


class HealthSummary(BaseModel):
    service: str
    ticks: int
    alive_ok_fraction: Optional[float] = None
    ready_ok_fraction: Optional[float] = None
    docker_unhealthy_seen: bool = False
    container_down_seen: bool = False


# ---------------------------------------------------------------- planning
class Hypothesis(BaseModel):
    statement: str
    expected_affected: list[str] = []
    expected_max_error_rate: float = 1.0
    expected_recovery_s: float = 30.0


class ExperimentSpec(BaseModel):
    experiment_id: str
    target_service: str
    container: str
    failure_type: str
    parameters: dict[str, Any] = {}
    rationale: str
    hypothesis: Hypothesis
    criticality_score: float
    planned_by: Literal["llm", "deterministic", "operator"]
    planner_notes: list[str] = []
    reproduce: list[str] = []
    # Set by the policy when this candidate would otherwise have needed `--allow-protected`. The run
    # path must obtain an admin decision before injecting; `--allow-protected` clears it instead.
    requires_approval: bool = False
    approval_reason: str = ""


# ---------------------------------------------------------------- experiment report
class TimelineEvent(BaseModel):
    at: datetime
    t_plus_s: float
    event: str
    detail: str = ""


class Finding(BaseModel):
    code: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    title: str
    evidence: str


class SafetyOutcome(BaseModel):
    aborted_early: bool = False
    abort_reason: Optional[str] = None
    abort_detail: dict[str, Any] = {}
    watchdog_fired: bool = False
    operator_stop: bool = False
    thresholds: Thresholds


class RecoveryOutcome(BaseModel):
    recovered: bool
    recovered_at: Optional[datetime] = None
    time_to_recover_s: Optional[float] = None
    outage_duration_s: Optional[float] = None
    auto_restarted_by_docker: Optional[bool] = None
    remediation_actions: list[str] = []
    data_integrity_ok: Optional[bool] = None
    data_integrity_detail: str = ""


class HypothesisResult(BaseModel):
    held: Optional[bool] = None
    observed_max_error_rate: Optional[float] = None
    observed_recovery_s: Optional[float] = None
    unexpected_affected: list[str] = []
    notes: str = ""


# ---------------------------------------------------------------- cascade attribution
# How the failure moved through the dependency graph, hop by hop. Observational only: computed from
# the health-probe and API-probe samples the injector already takes, matched against topology.json's
# real edges by deterministic rules. No LLM decides a path or a mechanism. Timing is reported as an
# interval (the failure began somewhere between the last good observation and the first bad one), so
# two services whose intervals overlap are "same_interval": the data cannot say which went first.
CASCADE_MECHANISMS = (
    "injected_fault",          # the origin: Faultline did this on purpose
    "process_down",            # container not running (exited / missing)
    "process_paused",          # container frozen (SIGSTOP)
    "liveness_failed",         # /alive not 200 (or mongosh ping failed)
    "readiness_failed",        # /alive 200 but /ready not 200: subsystem-level failure
    "dependency_timeout",      # requests through it timed out (client timeout or 504)
    "dependency_unreachable",  # connection refused / reset, or 502 from the proxy
    "dependency_error",        # it answered, with 5xx (e.g. 503 when a dependency is gone)
    "auth_failure",            # 401 / 403 where the baseline had none
    "elevated_error_rate",     # failures with no dominant kind, or a minority of requests failing
    "latency_degraded",        # no errors, but p95 well above baseline
)
PATH_CONFIDENCE = (
    "origin",          # the injected target itself
    "ordered",         # exactly one graph path fits, and every hop's onset is strictly after its upstream's
    "same_interval",   # one path fits, but at least one hop's onset overlaps its upstream's: direction unclear
    "ambiguous",       # several graph paths fit the timing; candidates listed, most likely named by a stated rule
    "no_graph_path",   # affected, but topology.json has no dependency route to the origin
)


class HealthTransition(BaseModel):
    """One change of a service's health state, as the health probe saw it (only changes are kept)."""
    service: str
    t_plus_s: float                     # seconds after injection (negative = before)
    state: Literal["healthy", "process_down", "process_paused", "liveness_failed", "readiness_failed"]
    detail: str = ""                    # e.g. "alive=200 ready=503 docker=healthy"


class OperationSignal(BaseModel):
    """What one synthetic API operation saw during the window. `route` is the services it passes
    through (faultline.observe.probes.OP_ROUTES), filtered to services present in the topology."""
    op: str
    route: list[str]
    requests: int
    failures: int
    first_failure_s: Optional[float] = None
    last_ok_before_failure_s: Optional[float] = None
    error_kinds: dict[str, int] = {}    # "timeout", "connection_error", "503", ...
    mechanism: Optional[str] = None     # one of CASCADE_MECHANISMS, None if the op never failed


class CascadeHop(BaseModel):
    service: str
    onset_earliest_s: Optional[float]   # failure began no earlier than this (t+ injection)
    onset_latest_s: Optional[float]     # ...and no later than this
    onset_source: Literal["injection", "health_probe", "api_probe"]
    phase: Literal["during_fault", "after_rollback"] = "during_fault"
    mechanism: str                      # one of CASCADE_MECHANISMS
    mechanism_evidence: str
    masked_by_health_check: bool = False  # health stayed green the whole window while traffic through it failed
    upstream: Optional[str] = None      # the dependency it most likely failed through (None for the origin)
    edge_kinds: list[str] = []          # evidence kinds of the topology edge service -> upstream
    lag_s: Optional[float] = None       # onset_latest(service) - onset_latest(upstream); None if not computable
    path: list[str] = []                # most likely path, this service first, origin last
    candidate_paths: list[list[str]] = []  # every graph path that fits the timing (incl. `path`)
    path_confidence: str                # one of PATH_CONFIDENCE
    hops_from_origin: Optional[int] = None
    beyond_hypothesis: bool = False
    note: str = ""


class CascadeEdge(BaseModel):
    """For the dashboard: one dependency edge the failure travelled along (src depends on dst,
    matching Edge's direction), labelled with how the dependent failed."""
    src: str
    dst: str
    mechanism: str
    confidence: str                     # PATH_CONFIDENCE of the hop that used this edge
    lag_s: Optional[float] = None


class CascadeAttribution(BaseModel):
    method: str = "timing+topology v1"
    origin: str
    health_interval_s: float            # health-probe polling interval: the timing resolution of health onsets
    window_start_s: float
    window_end_s: float
    hops: list[CascadeHop] = []         # origin first, then by onset
    edges: list[CascadeEdge] = []
    transitions: list[HealthTransition] = []
    operations: list[OperationSignal] = []
    hops_beyond_hypothesis: int = 0     # max hops_from_origin over hops with beyond_hypothesis
    beyond_hypothesis: list[str] = []
    unattributed: list[str] = []        # affected services with no graph path to the origin
    summary: str = ""                   # deterministic one-paragraph account; Narrator may rephrase, never re-derive
    notes: list[str] = []


class ExperimentReport(BaseModel):
    schema_version: str = "1.0"
    experiment_id: str
    started_at: datetime
    ended_at: datetime
    target: dict[str, str]
    failure_type: str
    parameters: dict[str, Any]
    rationale: str
    hypothesis: Hypothesis
    hypothesis_result: HypothesisResult
    criticality_score: float
    planned_by: str
    baseline: WindowStats
    during: WindowStats
    after: WindowStats
    health: list[HealthSummary]
    recovery: RecoveryOutcome
    safety: SafetyOutcome
    affected_services_observed: list[str]
    findings: list[Finding]
    timeline: list[TimelineEvent]
    resources: dict[str, Any] = {}
    reproduce: list[str] = []
    raw_logs_ref: str = ""
    status: Literal["completed", "aborted", "failed", "skipped"]
    error: Optional[str] = None
    # Absent on reports written before cascade attribution existed, and on skipped runs.
    cascade: Optional[CascadeAttribution] = None
    # An admin decision attached to this experiment: the pre-injection gate that cleared it, or the
    # post-incident choice of which fix they intend to apply. A decision log entry, never an action.
    approval: Optional["ApprovalDecision"] = None


# ---------------------------------------------------------------- downstream agents
class RiskAssessment(BaseModel):
    experiment_id: str
    assessed_at: datetime = Field(default_factory=utcnow)
    target_service: str
    failure_type: str
    severity: float
    level: Literal["low", "medium", "high", "critical"]
    blast_radius: int
    user_impact: float
    recovery_s: Optional[float]
    factors: dict[str, float]
    findings: list[Finding]
    summary: str
    production_implication: str
    generated_by: Literal["llm+rules", "rules"]


class Recommendation(BaseModel):
    title: str
    category: Literal["configuration", "architecture", "observability", "operations", "code"]
    effort: Literal["S", "M", "L"]
    confidence: float
    rationale: str
    steps: list[str]
    verify: list[str]
    references: list[str] = []


class FixRecommendation(BaseModel):
    experiment_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    target_service: str
    failure_type: str
    risk_level: str
    recommendations: list[Recommendation]
    advisory_only: bool = True
    generated_by: Literal["llm+rules", "rules"]


class FixState(BaseModel):
    """A finding's fix state on the scorecard, linking to reports/proposals/<proposal_id>.json."""
    proposal_id: str
    experiment_id: str
    finding_code: str
    catalog_fix_id: str
    state: Literal["proposed", "approved", "applying", "applied", "verified", "verification_failed",
                   "rejected", "rolled_back", "expired"]
    reason: str = ""


class ServiceScore(BaseModel):
    service: str
    score: Optional[float]
    grade: str
    experiments: int
    worst_success_rate: Optional[float] = None
    slowest_recovery_s: Optional[float] = None
    findings: int = 0
    notes: list[str] = []
    fixes: list[FixState] = []                # human-approved fix proposals for this service's findings


class Scorecard(BaseModel):
    generated_at: datetime = Field(default_factory=utcnow)
    project: str
    overall_score: Optional[float]
    ranking: list[ServiceScore]
    experiments_total: int
    summary: str


# ---------------------------------------------------------------- repo onboarding
# What `config/<slug>_knowledge.yaml` carries: facts about a target application that a compose
# file cannot express. The fields here are exactly the ones `faultline/topology/compose.py` and
# `render.py` read — nothing is invented, because an unread field is dead weight in a config file.
ROLES = ("datastore", "app", "proxy", "ui", "other")


class DocumentedEdge(BaseModel):
    """`src` needs `dst` to work, asserted by documentation rather than by the deployment files."""
    src: str
    dst: str
    evidence: str = "documented"

    @field_validator("src", "dst")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("service name must not be empty")
        return v


class RepoKnowledge(BaseModel):
    """The knowledge file. Consumed by topology.compose.service_nodes/documented_edges."""
    health_port: Optional[int] = None
    cluster_alias: Optional[str] = None
    roles: dict[str, str] = Field(default_factory=dict)
    documented_edges: list[DocumentedEdge] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)

    @field_validator("health_port")
    @classmethod
    def _port_range(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (1 <= v <= 65535):
            raise ValueError(f"health_port {v} is not a TCP port")
        return v

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, v: dict[str, str]) -> dict[str, str]:
        bad = {k: r for k, r in v.items() if r not in ROLES}
        if bad:
            raise ValueError(f"unknown role(s) {bad}; allowed: {ROLES}")
        return v


class OnboardingReport(BaseModel):
    """What `faultline onboard` did, so the operator can judge whether to trust the output."""
    slug: str
    source: str
    resolved_path: str
    compose_path: str
    readme_path: Optional[str] = None
    nginx_conf_path: Optional[str] = None
    services: list[str] = Field(default_factory=list)
    structural_edges: int = 0
    generated_by: Literal["llm", "structural-only"] = "structural-only"
    model: str = ""
    knowledge: RepoKnowledge = Field(default_factory=RepoKnowledge)
    rejected: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    output_path: str = ""


# ---------------------------------------------------------------- weakness scout
# Everything below is PREDICTED from static configuration, never measured. The label is carried on
# every record so that no consumer can mistake a prediction for one of the Risk agent's post-hoc,
# empirically-measured severities. The discipline mirrors the Fix agent's "[LLM suggestion,
# unverified]" tagging: a prediction stays unconfirmed until an experiment confirms or clears it.
PREDICTED_LABEL = "[predicted, unconfirmed]"

WEAKNESS_CATEGORIES = (
    "shallow_health_check",
    "missing_health_check",
    "no_redundancy",
    "no_resource_limits",
    "weak_restart_policy",
    "no_retry_or_backoff",
    "port_beyond_ingress",
    "no_rate_limiting",
)


class SuggestedExperiment(BaseModel):
    """Which EXISTING catalog failure type would actually test this prediction, and against whom.

    `target_service` is not always the weak service: the way to test a liveness-only health check on
    `things` is to break what `things` depends on and watch whether its /ready notices.
    """
    failure_type: str
    target_service: str
    reason: str = ""
    policy_note: str = ""


class PredictedWeakness(BaseModel):
    weakness_id: str
    category: Literal[
        "shallow_health_check", "missing_health_check", "no_redundancy", "no_resource_limits",
        "weak_restart_policy", "no_retry_or_backoff", "port_beyond_ingress", "no_rate_limiting",
    ]
    services: list[str]
    title: str
    evidence: str
    predicted_severity: float
    predicted_level: Literal["low", "medium", "high", "critical"]
    factors: dict[str, float]
    suggested_experiment: SuggestedExperiment
    confirming_finding_codes: list[str]
    # Which catalog failure types could speak to this prediction at all. Empty = any. This exists
    # because `api_impact` fires on every experiment that moves the error rate, so without it a
    # container crash would "confirm" a missing CPU limit, and the headline predicted-vs-confirmed
    # number would inflate itself on experiments that have nothing to do with the prediction.
    confirming_failure_types: list[str] = []
    why_it_matters: str
    status: Literal["predicted", "confirmed", "cleared"] = "predicted"
    label: str = PREDICTED_LABEL
    generated_by: Literal["rules", "llm+rules"] = "rules"


class SystemicPattern(BaseModel):
    """Additive LLM narrative over several deterministic findings. Never a finding of its own."""
    title: str
    weakness_ids: list[str]
    narrative: str
    generated_by: Literal["llm"] = "llm"


class WeaknessReport(BaseModel):
    schema_version: str = "1.0"
    generated_at: datetime = Field(default_factory=utcnow)
    project: str
    compose_file: str
    knowledge_file: Optional[str] = None
    label: str = PREDICTED_LABEL
    weaknesses: list[PredictedWeakness] = []
    systemic_patterns: list[SystemicPattern] = []
    checks_run: list[str] = []
    notes: list[str] = []
    generated_by: Literal["rules", "llm+rules"] = "rules"

    def by_id(self, weakness_id: str) -> Optional[PredictedWeakness]:
        return next((w for w in self.weaknesses if w.weakness_id == weakness_id), None)


class WeaknessOutcome(BaseModel):
    """One prediction, judged against the experiments that have since run."""
    weakness_id: str
    category: str
    services: list[str]
    predicted_severity: float
    predicted_level: str
    state: Literal["confirmed", "cleared", "untested"]
    experiments: list[str] = []
    confirming_findings: list[str] = []
    detail: str = ""


class WeaknessReconciliation(BaseModel):
    """Closing the loop: predicted-vs-confirmed at whole-system level, the same hypothesis-vs-observed
    discipline the injector applies per experiment via `hypothesis_result.held`."""
    generated_at: datetime = Field(default_factory=utcnow)
    project: str
    predicted_at: Optional[datetime] = None
    predicted_total: int = 0
    confirmed: int = 0
    cleared: int = 0
    untested: int = 0
    experiments_considered: int = 0
    outcomes: list[WeaknessOutcome] = []
    summary: str = ""


# ---------------------------------------------------------------- approvals
# The human-in-the-loop gate. Two scenarios, one mechanism: pause, email the admin a fixed set of
# replies, wait for one of them or for the deadline. The vocabulary is closed on purpose — an admin
# answering from a phone should never have to improvise a syntax, and the parser should never have to
# guess. Anything outside it is not an answer and is never treated as a default action.
APPROVAL_SCENARIOS = ("pre_injection", "problem_occurred")

# First line of the reply body, matched case-insensitively. Keys are what the admin types.
PRE_INJECTION_REPLIES = {
    "APPROVE": "approve",
    "APPROVE REDUCED": "approve_reduced",
    "SKIP": "skip",
    "STOP RUN": "stop_run",
}
PROBLEM_REPLIES = {
    "FIX <n>": "fix",
    "WAIT": "wait",
    "CONTINUE": "continue",
}
# Safe defaults when nobody answers. Scenario A does not do the risky thing unattended; scenario B
# does not pause a run over an unanswered post-hoc note, because WAIT must always be a deliberate
# human choice and never a timeout artefact.
TIMEOUT_DEFAULTS = {"pre_injection": "skip", "problem_occurred": "continue"}


class ApprovalDecision(BaseModel):
    """How one pending decision ended. `answered` separates a human's answer from a timeout default."""
    answer: Literal["approve", "approve_reduced", "skip", "stop_run", "fix", "wait", "continue", "reject"]
    answered: bool
    reason: str
    decided_at: datetime = Field(default_factory=utcnow)
    fix_index: Optional[int] = None          # 1-based, as listed in the email; only for `fix`
    fix_title: str = ""
    raw_first_line: str = ""                 # what the admin actually typed, for the audit trail
    clarifications_sent: int = 0             # malformed replies that got the options sent back
    parameters_before: dict[str, Any] = {}   # only for `approve_reduced`
    parameters_after: dict[str, Any] = {}
    parameter_notes: list[str] = []


class ApprovalRequest(BaseModel):
    """`reports/PENDING_APPROVAL.json` while open, then `reports/approvals/<approval_id>.json` for good.

    Same spirit as the `ACTIVE_EXPERIMENT.json` lock: one small file that says what is waiting, since
    when, and until when, so the Guardian and the control room can see a human is being waited on
    without a second status channel. It carries the recipient and the correlation token; it never
    carries the SMTP or IMAP credentials used to send it.
    """
    approval_id: str
    scenario: Literal["pre_injection", "problem_occurred", "fix_proposal"]
    experiment_id: str
    token: str                               # correlation token, carried in the subject line
    message_id: str = ""                     # Message-ID of the email we sent, matched by In-Reply-To
    subject: str = ""
    to: str = ""
    target_service: str = ""
    failure_type: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    deadline: datetime
    timeout_s: float
    options: list[str] = []                  # the fixed vocabulary offered, verbatim
    default_on_timeout: str
    sent: bool = False
    send_error: str = ""                      # transport failure, no credentials, no recipient
    resends: int = 0
    status: Literal["pending", "answered", "timed_out", "failed"] = "pending"
    decision: Optional[ApprovalDecision] = None


# ---------------------------------------------------------------- human-approved fixes (remediation)
ProposalStatus = Literal["pending", "approved", "rejected", "expired", "applying", "applied",
                         "verified", "verification_failed", "rolled_back"]


class ProposalExplanation(BaseModel):
    """The five things a human must read before approving. Templated by code; the LLM may only reword it,
    and it must quote the real change_preview, never write its own diff."""
    problem: str
    change: str
    expected_improvement: str
    risks: str
    rollback: str
    generated_by: Literal["template", "llm"] = "template"


class ExpectedMetric(BaseModel):
    metric: str                               # e.g. "time_to_recover_s", "during_success_rate", "service_score"
    before: Optional[float] = None
    expected_after: str                       # stated as a direction or bound, e.g. "<= 26.5" or "no worse"


class ProposalAuditEntry(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    actor: str                                # "faultline", "cli:<user>", "dashboard", "email:<addr>"
    from_status: Optional[str] = None
    to_status: str
    note: str = ""


class MetricComparison(BaseModel):
    metric: str
    before: Optional[float] = None
    after: Optional[float] = None
    better_is: Literal["lower", "higher"]
    verdict: Literal["better", "same", "worse", "unknown"]


class FixVerification(BaseModel):
    """Before/after of re-running the same experiment once the fix was applied."""
    rerun_experiment_id: Optional[str] = None
    comparisons: list[MetricComparison] = []
    new_findings: list[str] = []              # finding codes in the re-run that the original did not have
    targeted_metric: str = ""
    outcome: Literal["verified", "verification_failed", "not_run"] = "not_run"
    reason: str = ""


class FixProposal(BaseModel):
    """`reports/proposals/<proposal_id>.json`: one "Can I fix this?" request and everything that happened to it.

    Only a code-defined catalog fix can ever be proposed or applied (faultline/remediation/catalog.py).
    The change is built deterministically, shown verbatim in change_preview, and pinned by change_hash:
    an approval must quote the hash it was shown, so a diff that changed after review cannot be applied.
    Expired, rejected or unanswered proposals are never applied; nothing is ever auto-approved.
    """
    proposal_id: str
    experiment_id: str
    finding_code: str
    catalog_fix_id: str
    target_services: list[str]
    change_preview: str                       # the exact override diff or runtime action, generated by code
    change_hash: str                          # sha256 of change_preview
    explanation: ProposalExplanation
    expected_metrics: list[ExpectedMetric] = []
    requires_allow_protected: bool = False    # touches a protected service (MongoDB)
    rank: int = 0                             # order among this experiment's proposals (code or LLM ranked)
    ranked_by: Literal["code", "llm"] = "code"
    status: ProposalStatus = "pending"
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    applied_at: Optional[datetime] = None
    override_backup: Optional[str] = None     # path of the override snapshot taken before applying
    verification: Optional[FixVerification] = None
    audit: list[ProposalAuditEntry] = []


ExperimentReport.model_rebuild()
