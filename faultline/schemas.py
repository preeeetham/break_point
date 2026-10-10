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


class ServiceScore(BaseModel):
    service: str
    score: Optional[float]
    grade: str
    experiments: int
    worst_success_rate: Optional[float] = None
    slowest_recovery_s: Optional[float] = None
    findings: int = 0
    notes: list[str] = []


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
