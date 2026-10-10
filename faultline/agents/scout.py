"""Weakness Scout agent. Predicts weaknesses from the static picture, before an experiment proves them.

Every other agent in Faultline is empirical: the Risk agent scores an experiment that already ran,
the Fix agent recommends off a finding that was already observed. That is correct chaos engineering,
but it leaves nothing to say about a service nobody has gotten around to breaking yet -- which matters
when the experiment budget is a hackathon afternoon, and matters more once Faultline is pointed at an
unfamiliar repo where you will never test every service.

The project's own README documents `health_check_masked_failure`, grounded in Ditto issue #1865: a
service answered /ready = 200 while the API through it was failing. That was found empirically. But a
service whose compose healthcheck only curls `/alive` -- never its own dependencies -- is visible in
the compose file before anything is broken. A careful read of the config predicts that exact class of
problem. That is what this agent does.

Two rules hold this module together:

  1. The checks are 100% deterministic. Every one of them is a fact read straight out of the compose
     file, the declared healthcheck config, or the Topology agent's own output. The LLM NEVER decides
     whether a weakness exists -- a hallucinated "no health check" on a service that has one would be
     indistinguishable from a real finding, which is not acceptable in a severity-ranked report. The
     model only writes the prose (see `_llm_pass`), the same role it already plays rewriting the Risk
     agent's prose fields.
  2. A prediction is labelled as one, everywhere, until an experiment confirms or clears it
     (`faultline scout --reconcile`). `PREDICTED_LABEL` rides on the report and on every weakness,
     the same discipline the Fix agent applies to its "[LLM suggestion, unverified]" extras. A
     predicted severity must never be mistaken for the Risk agent's measured severity.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..chaos.catalog import CATALOG, applicable
from ..planning.llm import LLMClient
from ..planning.planner import Policy
from ..reporting.store import ReportStore
from ..schemas import (
    PREDICTED_LABEL,
    PredictedWeakness,
    ServiceNode,
    SuggestedExperiment,
    SystemicPattern,
    Topology,
    WeaknessReport,
)
from ..topology.compose import load_compose, load_knowledge, parse_env

MAX_SYSTEMIC_PATTERNS = 2
MAX_PROSE_CHARS = 600
LLM_EXTRA_LABEL = "[LLM suggestion, unverified]"

# A service is "critical" enough to hold to the stricter structural standards. The floor is the
# Topology agent's own score scale (transitive dependents + 2 stateful + 1 entrypoint + 1 datastore
# + 0.5 no health endpoint), so 2.0 is roughly "two services break if this one does".
CRITICAL_SCORE_FLOOR = 2.0
# ... and "top tier" is relative to the most critical service in THIS deployment, so the redundancy
# check stays meaningful on a target whose scores are nothing like Ditto's.
TOP_TIER_FRACTION = 0.4

# Intrinsic weight per category, before the deployment's own criticality is applied. Ordered by how
# reliably the gap turns into a user-visible incident; `shallow_health_check` leads because Faultline
# has already proven that one empirically on this very stack (Ditto #1865).
CATEGORY_WEIGHT: dict[str, float] = {
    "shallow_health_check": 2.5,
    "no_redundancy": 2.25,
    "missing_health_check": 2.0,
    "no_rate_limiting": 1.5,
    "no_retry_or_backoff": 1.5,
    "weak_restart_policy": 1.5,
    "no_resource_limits": 1.0,
    "port_beyond_ingress": 0.5,
}

# Which catalog failure types can bear on each category at all. A crash is a fast, clean error, so it
# says nothing about missing retry/backoff; an unbounded cgroup is only exercised by something that
# actually loads the container. Without this gate the generic `api_impact` code would let any
# experiment that touched the service confirm any prediction about it.
CONFIRMING_FAILURE_TYPES: dict[str, list[str]] = {
    # The health-masking categories carry specific finding codes already, so any outage that makes
    # the dependency unavailable is a fair test; left open deliberately.
    "shallow_health_check": [],
    "missing_health_check": [],
    "port_beyond_ingress": [],
    "no_redundancy": ["container_crash", "container_stop", "container_pause", "network_partition",
                      "network_delay", "network_loss"],
    "no_resource_limits": ["cpu_stress", "db_flood", "traffic_spike", "disk_pressure"],
    "weak_restart_policy": ["container_crash", "container_stop"],
    "no_retry_or_backoff": ["network_delay", "network_loss", "network_partition", "container_pause",
                            "cpu_stress", "db_flood"],
    "no_rate_limiting": ["traffic_spike"],
}

# Which live finding codes would confirm each prediction. The reconcile agent matches on these, and
# ignores info-severity matches -- `api_impact` fires on every experiment, including the benign case.
CONFIRMING_CODES: dict[str, list[str]] = {
    "shallow_health_check": ["health_check_masked_failure", "dependency_blind_readiness"],
    "missing_health_check": ["no_automated_remediation", "health_check_masked_failure"],
    "no_redundancy": ["api_impact", "recovery_failed", "manual_restart_required", "slow_recovery",
                      "cascade_beyond_hypothesis"],
    "no_resource_limits": ["api_impact", "slow_recovery", "relapse_after_recovery"],
    "weak_restart_policy": ["recovery_failed", "manual_restart_required", "no_automated_remediation"],
    "no_retry_or_backoff": ["api_impact", "recovery_failed", "manual_restart_required",
                            "relapse_after_recovery", "cascade_beyond_hypothesis"],
    "port_beyond_ingress": ["cascade_beyond_hypothesis"],
    "no_rate_limiting": ["api_impact"],
}

# A healthcheck matching this and nothing dependency-shaped is structurally liveness-only: it proves
# the process is up, not that the service can do its job.
LIVENESS_ONLY_RE = re.compile(r"/alive|/live\b|/livez|/ping\b|\bpgrep\b|\bps -|nc -z|/dev/tcp", re.I)
# Evidence that a healthcheck looks past the process at something it depends on.
READINESS_RE = re.compile(r"/ready|/readyz|/status/health|/health\b|subsystem", re.I)
# Tokens that suggest somebody thought about retries, timeouts or circuit breaking on a dependency.
RESILIENCE_RE = re.compile(
    r"retry|retries|backoff|back-off|circuit|failover|fail-over|timeout|timeoutms|"
    r"serverselection|maxpool|pool[_-]?size|replicaset|read[_-]?preference",
    re.I,
)
RATE_LIMIT_RE = re.compile(r"limit_req|limit_conn|rate[_-]?limit|ratelimit|throttl", re.I)


# ---------------------------------------------------------------- inputs
@dataclass
class ScoutContext:
    """Everything the deterministic checks read. Nothing here comes from a model."""
    topology: Topology
    compose: dict
    knowledge: dict
    policy: Policy
    compose_path: str
    knowledge_path: Optional[str] = None
    nginx_text: str = ""
    notes: list[str] = field(default_factory=list)

    def raw(self, service: str) -> dict:
        return (self.compose.get("services") or {}).get(service) or {}

    def node(self, service: str) -> ServiceNode:
        return self.topology.services[service]

    def candidates(self) -> list[str]:
        """Services worth analysing: present in both compose and topology, and chaos-relevant.

        The observability stack in Ditto's compose file is tagged `other` in the knowledge file and
        is not part of the system under test; flagging its missing limits would be noise.
        """
        services = self.compose.get("services") or {}
        return [n for n in self.topology.relevant_services() if n in services]

    def max_criticality(self) -> float:
        return max((c.score for c in self.topology.criticality), default=0.0) or 1.0

    def is_critical(self, service: str) -> bool:
        node = self.node(service)
        return (self.topology.criticality_of(service) >= CRITICAL_SCORE_FLOOR
                or node.stateful or node.entrypoint)

    def is_top_tier(self, service: str) -> bool:
        return self.topology.criticality_of(service) >= TOP_TIER_FRACTION * self.max_criticality()

    def is_protected(self, service: str) -> bool:
        return service in self.policy.protected


@dataclass
class _Raw:
    """A deterministic check's output, before scoring and before any prose."""
    category: str
    services: list[str]
    title: str
    evidence: str
    experiment_prefs: list[tuple[str, str]]   # (failure_type, target_service), best first
    experiment_reason: str = ""
    # Scales the category's intrinsic weight when only part of the gap is present -- a service that
    # sets a memory limit but no CPU limit is half-covered, and should not score as if it set neither.
    weight_scale: float = 1.0


def healthcheck_text(raw: dict) -> Optional[str]:
    """The healthcheck command as one string, or None when there is effectively no healthcheck."""
    hc = raw.get("healthcheck")
    if not isinstance(hc, dict) or hc.get("disable"):
        return None
    test = hc.get("test")
    if test is None:
        return None
    text = " ".join(str(t) for t in test) if isinstance(test, (list, tuple)) else str(test)
    text = text.strip()
    return text or None


def _published_ports(raw: dict) -> list[str]:
    out: list[str] = []
    for p in raw.get("ports") or []:
        if isinstance(p, dict):
            target, published = p.get("target"), p.get("published")
            host_ip = p.get("host_ip")
            out.append(f"{host_ip + ':' if host_ip else ''}{published}:{target}")
        else:
            out.append(str(p))
    return out


def _replicas(raw: dict) -> Optional[int]:
    deploy = raw.get("deploy") or {}
    for key, src in (("replicas", deploy), ("scale", raw)):
        val = src.get(key) if isinstance(src, dict) else None
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
    return None


def _limits(raw: dict) -> tuple[Optional[str], Optional[str]]:
    """(memory, cpu) limit as written in the compose file, or None where unset."""
    limits = (((raw.get("deploy") or {}).get("resources") or {}).get("limits") or {})
    mem = limits.get("memory") or raw.get("mem_limit")
    cpu = limits.get("cpus") or raw.get("cpus") or raw.get("cpu_quota")
    return (str(mem) if mem is not None else None, str(cpu) if cpu is not None else None)


def _ingress(ctx: ScoutContext) -> Optional[str]:
    """The declared entrypoint: the proxy that publishes a port, else the only service that does."""
    publishing = [n for n in (ctx.compose.get("services") or {}) if _published_ports(ctx.raw(n))]
    proxies = [n for n in publishing if n in ctx.topology.services and ctx.node(n).role == "proxy"]
    if proxies:
        return proxies[0]
    return publishing[0] if len(publishing) == 1 else None


# ---------------------------------------------------------------- the deterministic checks
# Every check reads the compose file or topology.json and nothing else. Each returns `_Raw` records
# whose `evidence` quotes the actual field, or names precisely what is absent -- never a summary.

def check_health_checks(ctx: ScoutContext) -> list[_Raw]:
    """Missing healthcheck on a critical service, or one that is structurally liveness-only.

    The second case is the `health_check_masked_failure` predictor. It only applies to a service that
    HAS dependencies: a service with none cannot have a dependency-aware health check, so Ditto's
    mongodb (whose `mongosh ping` genuinely exercises its own storage engine) is correctly left alone.
    """
    out: list[_Raw] = []
    for name in ctx.candidates():
        raw, node = ctx.raw(name), ctx.node(name)
        text = healthcheck_text(raw)
        deps = [d for d in ctx.topology.dependencies(name) if d in ctx.topology.services]

        if text is None:
            if not ctx.is_critical(name):
                continue
            disabled = isinstance(raw.get("healthcheck"), dict) and raw["healthcheck"].get("disable")
            what = "`healthcheck: {disable: true}`" if disabled else "no `healthcheck:` block"
            out.append(_Raw(
                category="missing_health_check",
                services=[name],
                title=f"{name} declares no health check",
                evidence=(f"compose service `{name}`: {what}; criticality "
                          f"{ctx.topology.criticality_of(name):g} with "
                          f"{len(ctx.topology.transitive_dependents(name))} transitive dependent(s). "
                          f"Docker `restart:` only reacts to a process exit, so a container that is "
                          f"running but wedged is invisible to the platform."),
                experiment_prefs=[("container_pause", name), ("container_stop", name),
                                  ("network_delay", name)],
                experiment_reason=(f"freezing {name} leaves the container 'running'; with no health "
                                   f"check nothing in the platform notices"),
            ))
            continue

        if not deps:
            continue
        shallow = bool(LIVENESS_ONLY_RE.search(text))
        # Does the command look past its own process -- at a readiness/subsystem endpoint, or at a
        # dependency by name or network alias?
        dep_names = {d for d in deps} | {a for d in deps for a in ctx.node(d).aliases}
        mentions_dep = any(re.search(rf"\b{re.escape(d)}\b", text) for d in dep_names)
        if not shallow or READINESS_RE.search(text) or mentions_dep:
            continue
        out.append(_Raw(
            category="shallow_health_check",
            services=[name],
            title=f"{name}'s health check is liveness-only and cannot see its dependencies",
            evidence=(f"compose service `{name}`: healthcheck test `{text}` -- a bare liveness probe. "
                      f"It never touches {', '.join(sorted(deps))}, which {name} needs to serve a "
                      f"request, so the check can report healthy while the API through it fails. "
                      f"This is the shape of Ditto issue #1865, which Faultline has already observed "
                      f"empirically as `health_check_masked_failure`."),
            experiment_prefs=[(ft, dep) for dep in _dep_order(ctx, deps)
                              for ft in ("container_stop", "network_partition", "network_delay",
                                         "container_pause", "network_loss")],
            experiment_reason=(f"break what {name} depends on, then check whether {name}'s own health "
                               f"endpoint ever admits it"),
        ))
    return out


def _dep_order(ctx: ScoutContext, deps: list[str]) -> list[str]:
    """Dependencies worth breaking first: the datastore, then by criticality."""
    return sorted(deps, key=lambda d: (ctx.node(d).role != "datastore",
                                       -ctx.topology.criticality_of(d), d))


def check_redundancy(ctx: ScoutContext) -> list[_Raw]:
    """A top-tier service running as exactly one instance.

    This is the check that should rediscover, from the compose file alone, the two remediations the
    Fix agent's knowledge library already recommends after an experiment proves them: run MongoDB as
    a replica set, and run two `policies` instances.
    """
    out: list[_Raw] = []
    for name in ctx.candidates():
        if not ctx.is_top_tier(name):
            continue
        raw, node = ctx.raw(name), ctx.node(name)
        n = _replicas(raw)
        if n is not None and n > 1:
            continue
        declared = f"`deploy.replicas: {n}`" if n is not None else "no `deploy.replicas` and no `scale:`"
        dependents = ctx.topology.transitive_dependents(name)
        state = " It also holds the only copy of its state." if node.stateful else ""
        out.append(_Raw(
            category="no_redundancy",
            services=[name],
            title=f"{name} is a single instance with no redundancy",
            evidence=(f"compose service `{name}`: {declared}, so exactly one container serves it, "
                      f"while {len(dependents)} service(s) depend on it transitively "
                      f"({', '.join(dependents) or 'none'}). Criticality "
                      f"{ctx.topology.criticality_of(name):g}, the "
                      f"{'highest' if ctx.topology.criticality[0].service == name else 'top tier'} in "
                      f"this deployment.{state}"),
            experiment_prefs=[("container_stop", name), ("container_crash", name),
                              ("network_partition", name), ("container_pause", name),
                              ("network_delay", name)],
            experiment_reason=f"take the single instance away and measure what survives",
        ))
    return out


def check_resource_limits(ctx: ScoutContext) -> list[_Raw]:
    """A critical service with no memory and/or no CPU limit.

    Not academic: `cpu_stress`, `db_flood` and `traffic_spike` exist in Faultline's own catalog
    specifically to exercise this, and an unbounded container competes with every other container on
    the host rather than being throttled inside its own cgroup.
    """
    out: list[_Raw] = []
    for name in ctx.candidates():
        if not ctx.is_critical(name):
            continue
        raw = ctx.raw(name)
        mem, cpu = _limits(raw)
        if mem and cpu:
            continue
        missing = [k for k, v in (("memory", mem), ("CPU", cpu)) if not v]
        have = ", ".join(f"{k} limit `{v}`" for k, v in (("memory", mem), ("cpus", cpu)) if v)
        prefs = [("cpu_stress", name)] if not cpu else []
        prefs += [("db_flood", name)] if ctx.node(name).role == "datastore" else []
        prefs += [("traffic_spike", name), ("disk_pressure", name), ("cpu_stress", name)]
        out.append(_Raw(
            category="no_resource_limits",
            services=[name],
            title=f"{name} has no {' or '.join(missing)} limit",
            evidence=(f"compose service `{name}`: no "
                      f"{' and no '.join('`deploy.resources.limits.' + ('memory`' if m == 'memory' else 'cpus`') for m in missing)}"
                      + (f" (it does set {have})" if have else " and no `mem_limit`/`cpus` shorthand")
                      + f". Nothing bounds its share of the host, so a {'CPU' if not cpu else 'memory'} "
                      f"spike inside it degrades every other container on the same machine."),
            experiment_reason=f"load {name} and watch whether the cgroup contains it",
            experiment_prefs=prefs,
            weight_scale=1.0 if len(missing) == 2 else 0.5,
        ))
    return out


def check_restart_policy(ctx: ScoutContext) -> list[_Raw]:
    """`restart:` unset or `no` on a service the dependency graph marks critical.

    On Ditto's own compose file this check is expected to stay SILENT: every service declares
    `restart: always`, which is part of why its crash recovery works cleanly. A check that fires on
    everything is a broken check.
    """
    out: list[_Raw] = []
    for name in ctx.candidates():
        if not ctx.is_critical(name):
            continue
        raw = ctx.raw(name)
        declared = raw.get("restart")
        policy = str(declared).strip() if declared is not None else ""
        if policy and policy not in ("no", "none", "false"):
            continue
        what = f"`restart: {policy}`" if policy else "no `restart:` policy at all"
        out.append(_Raw(
            category="weak_restart_policy",
            services=[name],
            title=f"{name} will not be restarted after a crash",
            evidence=(f"compose service `{name}`: {what}, so Docker leaves it dead after the process "
                      f"exits; {len(ctx.topology.transitive_dependents(name))} service(s) depend on "
                      f"it transitively. Compare `restart: always`, which is what makes crash "
                      f"recovery work on the rest of this stack."),
            experiment_prefs=[("container_crash", name), ("container_stop", name)],
            experiment_reason=f"kill {name} and see whether anything brings it back",
        ))
    return out


def check_retry_backoff(ctx: ScoutContext) -> list[_Raw]:
    """No evidence of retry/backoff/circuit-breaking on a hard dependency edge.

    One finding per dependency (not per dependent), because the gap belongs to the edge: four Ditto
    services reaching MongoDB through a bare `MONGO_DB_HOSTNAME=mongodb` is one story, not four.
    """
    out: list[_Raw] = []
    knowledge_text = json.dumps(ctx.knowledge, default=str)
    for crit in ctx.topology.criticality:
        dst = crit.service
        if dst not in ctx.topology.services or len(crit.transitive_dependents) < 2:
            continue
        dst_names = {dst} | set(ctx.node(dst).aliases)
        bare: list[str] = []
        quoted: list[str] = []
        for src in ctx.candidates():
            if dst not in ctx.topology.dependencies(src):
                continue
            env = parse_env(ctx.raw(src).get("environment"))
            # Only env entries that actually name this dependency can speak to resilience on THIS edge.
            touching = {k: v for k, v in env.items()
                        if any(re.search(rf"\b{re.escape(d)}\b", f"{k}={v}") for d in dst_names)}
            if any(RESILIENCE_RE.search(f"{k}={v}") for k, v in touching.items()):
                continue
            if RESILIENCE_RE.search(knowledge_text) and re.search(
                    rf"\b{re.escape(dst)}\b.{{0,120}}(retry|backoff|circuit|timeout)", knowledge_text, re.I | re.S):
                continue
            bare.append(src)
            # Being fair to the target: note settings that DO show resilience thinking but are not on
            # this edge (Ditto tunes the Pekko failure detector globally, which says nothing about Mongo).
            elsewhere = sorted({k for k, v in env.items()
                                if RESILIENCE_RE.search(f"{k}={v}") and k not in touching})
            aside = (f" (sets {', '.join(elsewhere[:2])} elsewhere, but nothing on this edge)"
                     if elsewhere else "")
            quoted.append(f"`{src}`: " + (", ".join(f"{k}={v}" for k, v in sorted(touching.items()))
                                          or f"nothing naming {dst}") + aside)
        if len(bare) < 2:
            continue
        out.append(_Raw(
            category="no_retry_or_backoff",
            services=bare,
            title=f"{len(bare)} services reach {dst} with no declared retry, timeout or backoff",
            evidence=(f"{', '.join(bare)} depend on `{dst}`, and the only configuration naming it is "
                      f"{'; '.join(quoted)} -- no retry, backoff, circuit-breaker or timeout setting "
                      f"on any of those edges, and nothing in the knowledge file documents one. A "
                      f"stall on `{dst}` therefore propagates as a hang rather than a fast, "
                      f"retryable error."),
            experiment_prefs=[("network_delay", dst), ("network_loss", dst), ("container_pause", dst),
                              ("cpu_stress", dst), ("container_stop", dst)],
            experiment_reason=(f"add latency to {dst} and watch whether callers retry, time out, or "
                               f"just block"),
        ))
    return out


def check_published_ports(ctx: ScoutContext) -> list[_Raw]:
    """A service publishing a port that is not the declared ingress.

    This is a blast-radius observation, not a security audit: the project's own compliance language
    treats "all ports bound to 127.0.0.1, single ingress" as the right shape for a sandboxed target,
    so an extra published port is extra surface that chaos can arrive through.
    """
    ingress = _ingress(ctx)
    if ingress is None:
        ctx.notes.append("no single declared ingress found in the compose file; "
                         "published-port check reported every publishing service")
    out: list[_Raw] = []
    for name in sorted(ctx.compose.get("services") or {}):
        if name == ingress or name not in ctx.topology.services:
            continue
        ports = _published_ports(ctx.raw(name))
        if not ports:
            continue
        loopback = all(p.startswith("127.0.0.1:") or p.startswith("localhost:") for p in ports)
        in_sut = ctx.node(name).chaos_relevant
        bind = ("all bound to 127.0.0.1, so the sandbox boundary itself holds"
                if loopback else "NOT bound to 127.0.0.1: reachable from outside the host")
        scope = ("" if in_sut else " This service is tagged outside the system under test, so the "
                                   "extra surface is deliberate rather than an oversight.")
        out.append(_Raw(
            category="port_beyond_ingress",
            services=[name],
            title=f"{name} publishes a port of its own besides the {ingress or 'declared'} ingress",
            evidence=(f"compose service `{name}`: `ports: {ports}` while `{ingress}` is the declared "
                      f"entrypoint -- {bind}. Traffic can reach `{name}` without passing the proxy, "
                      f"so proxy-level protections do not apply to it.{scope}"),
            experiment_prefs=[("traffic_spike", name), ("network_partition", name),
                              ("network_delay", name)],
            experiment_reason=f"drive load straight at {name}, bypassing {ingress or 'the proxy'}",
        ))
    return out


def check_rate_limiting(ctx: ScoutContext) -> list[_Raw]:
    """No rate limiting or backpressure evident on the public entrypoint.

    Predicts vulnerability to the `traffic_spike` failure type specifically.
    """
    ingress = _ingress(ctx)
    if ingress is None or ingress not in ctx.topology.services:
        return []
    raw = ctx.raw(ingress)
    env_text = " ".join(f"{k}={v}" for k, v in parse_env(raw.get("environment")).items())
    if RATE_LIMIT_RE.search(ctx.nginx_text) or RATE_LIMIT_RE.search(env_text):
        return []
    where = ("its mounted config" if ctx.nginx_text else
             "its environment (and no proxy config was available to read)")
    if not ctx.nginx_text:
        ctx.notes.append(f"no proxy config text was available for `{ingress}`; the rate-limit check "
                         f"could only read its compose environment")
    downstream = ctx.topology.dependencies(ingress)
    return [_Raw(
        category="no_rate_limiting",
        services=[ingress],
        title=f"{ingress} is the public entrypoint and applies no rate limiting or backpressure",
        evidence=(f"`{ingress}` is the only declared ingress, and neither `limit_req`, `limit_conn` "
                  f"nor any rate-limit/throttle setting appears in {where}. Every request that "
                  f"arrives is forwarded to {', '.join(downstream) or 'the backend'}, so a burst is "
                  f"absorbed by the slowest service behind it instead of being shed at the edge with "
                  f"a 429."),
        experiment_prefs=[("traffic_spike", ingress)] + [("traffic_spike", d) for d in downstream],
        experiment_reason=f"spike traffic at {ingress} and see whether anything sheds load",
    )]


CHECKS: tuple[tuple[str, Callable[[ScoutContext], list[_Raw]]], ...] = (
    ("health_checks", check_health_checks),
    ("redundancy", check_redundancy),
    ("resource_limits", check_resource_limits),
    ("restart_policy", check_restart_policy),
    ("retry_backoff", check_retry_backoff),
    ("published_ports", check_published_ports),
    ("rate_limiting", check_rate_limiting),
)


# ---------------------------------------------------------------- severity scoring
# Deterministic, in the same additive-factor spirit as the Risk agent's real severity: the terms are
# named, each is bounded, and the sum is clamped to 10. The number is a PREDICTION -- it says how bad
# this gap would be if an experiment confirmed it, not how bad anything measured was.
def _level(severity: float) -> str:
    if severity < 3:
        return "low"
    if severity < 5.5:
        return "medium"
    if severity < 8:
        return "high"
    return "critical"


def score(raw: _Raw, ctx: ScoutContext, per_service_failures: dict[str, int]) -> dict[str, float]:
    topo = ctx.topology
    max_crit = ctx.max_criticality()
    n_services = max(len(topo.relevant_services()), 1)
    worst = max(raw.services, key=lambda s: topo.criticality_of(s)) if raw.services else ""
    dependents = max((len(topo.transitive_dependents(s)) for s in raw.services), default=0)
    # How many OTHER categories the worst-affected service also fails: a service with no health
    # check, no limits and no redundancy is a worse bet than one that fails a single check.
    compounding = max(per_service_failures.get(s, 1) for s in raw.services) - 1 if raw.services else 0
    nodes = [ctx.node(s) for s in raw.services if s in topo.services]
    return {
        "criticality": round(3.5 * min(topo.criticality_of(worst) / max_crit, 1.0), 3),
        "category": round(CATEGORY_WEIGHT.get(raw.category, 1.0) * raw.weight_scale, 3),
        "blast": round(2.0 * min(dependents / n_services, 1.0), 3),
        "compounding": round(0.5 * min(compounding, 3), 3),
        "stateful": 1.0 if any(n.stateful for n in nodes) else 0.0,
        # The operator already marked it too dangerous for a full outage, which is itself a statement
        # about how little margin this service has.
        "protected": 0.5 if any(ctx.is_protected(s) for s in raw.services) else 0.0,
    }


# ---------------------------------------------------------------- suggested experiment
def resolve_experiment(raw: _Raw, ctx: ScoutContext) -> SuggestedExperiment:
    """Pick the first preference that is a real catalog type, applies to the target's role, and is
    allowed by the safety policy. A prediction that nominates an experiment the policy forbids is
    useless to the planner, so the blocked-everything case says so instead of lying."""
    blocked: list[str] = []
    for ft_key, target in raw.experiment_prefs:
        ft = CATALOG.get(ft_key)
        if ft is None or target not in ctx.topology.services:
            continue
        if not applicable(ft, ctx.node(target)):
            continue
        ok, why = ctx.policy.allows(target, ft_key)
        if not ok:
            if why:
                blocked.append(why)
            continue
        return SuggestedExperiment(failure_type=ft_key, target_service=target,
                                   reason=raw.experiment_reason, policy_note="")
    first = raw.experiment_prefs[0] if raw.experiment_prefs else ("container_stop", raw.services[0])
    targets = [t for _, t in raw.experiment_prefs if t in ctx.topology.services]
    if targets and not any(ctx.node(t).chaos_relevant for t in targets):
        note = (f"`{targets[0]}` is tagged outside the system under test (role "
                f"{ctx.node(targets[0]).role}), so Faultline will not inject into it; this prediction "
                f"is reported for completeness and cannot be confirmed by an experiment")
    elif blocked:
        note = (blocked[0] + " -- run it with --allow-protected to confirm this prediction, or treat "
                "it as untestable under the current safety policy")
    else:
        note = ("no catalog failure type applies to this target's role, so this prediction cannot be "
                "confirmed by an experiment as configured")
    return SuggestedExperiment(failure_type=first[0], target_service=first[1],
                               reason=raw.experiment_reason, policy_note=note)


# ---------------------------------------------------------------- templated prose
TEMPLATES: dict[str, str] = {
    "shallow_health_check": ("Nothing would page anyone. {svc} would keep answering its health probe "
                             "with 200 while requests through it failed, so no orchestrator, restart "
                             "policy or alert that keys off container health would react at all."),
    "missing_health_check": ("The platform has no way to tell that {svc} is sick. Docker's restart "
                             "policy only reacts to a process exit, so a hung or wedged {svc} stays "
                             "'running' forever and traffic keeps being routed to it."),
    "no_redundancy": ("One container is the whole service. Losing {svc} is a full outage of "
                      "everything behind it for as long as it takes to come back, with no second "
                      "instance to absorb the traffic in the meantime."),
    "no_resource_limits": ("{svc} can take the host down with it. Without a cgroup limit, a runaway "
                           "loop or a memory leak inside {svc} starves every other container on the "
                           "same machine instead of being throttled or OOM-killed in isolation."),
    "weak_restart_policy": ("A single crash is a permanent outage. Nothing restarts {svc}, so the "
                            "first unhandled exception or OOM kill ends the service until a human "
                            "notices and intervenes."),
    "no_retry_or_backoff": ("A slow dependency becomes a hang rather than an error. {svc} would hold "
                            "request threads waiting on something that is not coming back, so the "
                            "failure spreads upstream as exhausted capacity instead of a fast 5xx."),
    "port_beyond_ingress": ("Traffic can reach {svc} without passing the proxy, so whatever the "
                            "proxy does for the rest of the stack -- auth, timeouts, rate limiting -- "
                            "does not protect this path."),
    "no_rate_limiting": ("A burst is absorbed by the slowest thing behind the edge. With no load "
                         "shedding at {svc}, a traffic spike turns into latency and timeouts for "
                         "every client instead of a 429 for the excess."),
}


def template_prose(raw: _Raw) -> str:
    svc = raw.services[0] if len(raw.services) == 1 else ", ".join(raw.services)
    return TEMPLATES.get(raw.category, "This gap is visible in the deployment's configuration.").format(svc=svc)


# ---------------------------------------------------------------- assemble
def analyse(ctx: ScoutContext) -> tuple[list[PredictedWeakness], list[str]]:
    """Run every deterministic check and rank the results. No LLM is involved at any point here."""
    raws: list[_Raw] = []
    checks_run: list[str] = []
    for name, fn in CHECKS:
        checks_run.append(name)
        raws.extend(fn(ctx))

    per_service: dict[str, int] = {}
    for r in raws:
        for s in r.services:
            per_service[s] = per_service.get(s, 0) + 1

    out: list[PredictedWeakness] = []
    for r in raws:
        factors = score(r, ctx, per_service)
        severity = round(min(max(sum(factors.values()), 0.0), 10.0), 2)
        out.append(PredictedWeakness(
            weakness_id=f"{r.category}:{'+'.join(r.services)}",
            category=r.category,  # type: ignore[arg-type]
            services=list(r.services),
            title=r.title,
            evidence=r.evidence,
            predicted_severity=severity,
            predicted_level=_level(severity),  # type: ignore[arg-type]
            factors=factors,
            suggested_experiment=resolve_experiment(r, ctx),
            confirming_finding_codes=list(CONFIRMING_CODES.get(r.category, [])),
            confirming_failure_types=list(CONFIRMING_FAILURE_TYPES.get(r.category, [])),
            why_it_matters=template_prose(r),
            generated_by="rules",
        ))
    out.sort(key=lambda w: (-w.predicted_severity, w.category, w.weakness_id))
    return out, checks_run


# ---------------------------------------------------------------- the LLM's narrow role
# "LLM proposes, code disposes", exactly as in the planner, risk and fix agents. The model NEVER
# decides whether a weakness exists -- the list it receives is final and it cannot add to, remove
# from or re-rank it. It gets two jobs: write the plain-English "why this matters" line for each
# deterministic finding, and optionally point out that several of them are the same story. If it is
# unreachable, slow, or answers something that does not validate, every deterministic finding still
# ships with its templated prose.
# Two separate, small calls instead of one large one. A 20-weakness report sent whole is ~14k
# characters and, with the reply, exceeds the per-minute token budget of the free tiers these models
# are usually served from -- and an all-or-nothing call means one 429 costs every explanation. Prose
# is therefore requested in small chunks and the systemic-pattern narrative in one tiny call of its
# own, so each failure costs only its own piece and the rest still get model prose.
LLM_CHUNK_SIZE = 7
EVIDENCE_EXCERPT_CHARS = 180
# Generous enough that a reasoning model can think before it emits the object. Too small and the
# reply is truncated mid-JSON, which some providers reject outright rather than returning the
# fragment -- that looks like a hard failure but is really a budget that was set too low.
PROSE_MAX_TOKENS = 900
PATTERN_MAX_TOKENS = 700

PROSE_SYSTEM = (
    "You are an SRE reviewing the STATIC configuration of a docker-compose deployment. A "
    "deterministic analyser has already decided which weaknesses exist; that list is final and you "
    "cannot add to it, remove from it, re-rank it or re-score it. For each weakness id, write ONE "
    "plain-English sentence saying why it matters to a user of the system: concrete, no hedging, and "
    "do not just restate the evidence. Nothing has been measured -- these are predictions from "
    "configuration, so never claim anything was observed. "
    'Reply with JSON only: {"explanations": {"<weakness_id>": "<one sentence>"}}.'
)

PATTERN_SYSTEM = (
    "You are an SRE reviewing predicted weaknesses in a docker-compose deployment, all of them "
    "derived deterministically from static configuration -- nothing has been measured. Identify at "
    "most two SYSTEMIC patterns where several of the listed weaknesses are really one story (for "
    "example several critical services sharing the same unlimited-resource gap and the same "
    "traffic-spike exposure). Each pattern must cite at least two weakness ids from the list and may "
    "cite no id that is not in it. Do not invent a weakness that is not listed. "
    'Reply with JSON only: {"systemic_patterns": [{"title": str, "weakness_ids": [str], '
    '"narrative": str}]}.'
)


def _prose_payload(weaknesses: list[PredictedWeakness], ctx: ScoutContext) -> str:
    def excerpt(text: str) -> str:
        return text if len(text) <= EVIDENCE_EXCERPT_CHARS else text[: EVIDENCE_EXCERPT_CHARS - 1] + "\u2026"

    return json.dumps({
        "project": ctx.topology.project,
        "note": "predicted from static configuration; nothing has been injected or measured",
        "weaknesses": [{"weakness_id": w.weakness_id, "category": w.category, "services": w.services,
                        "dependents": sorted({d for s in w.services
                                              for d in ctx.topology.transitive_dependents(s)}),
                        "evidence": excerpt(w.evidence),
                        "predicted_severity": w.predicted_severity,
                        "would_be_tested_by": f"{w.suggested_experiment.failure_type} on "
                                              f"{w.suggested_experiment.target_service}"}
                       for w in weaknesses],
    }, default=str)


def _pattern_payload(weaknesses: list[PredictedWeakness], ctx: ScoutContext) -> str:
    return json.dumps({
        "project": ctx.topology.project,
        "weaknesses": [{"weakness_id": w.weakness_id, "category": w.category, "services": w.services,
                        "predicted_severity": w.predicted_severity,
                        "would_be_tested_by": w.suggested_experiment.failure_type}
                       for w in weaknesses],
    }, default=str)


def _apply_prose(data: dict, known: dict[str, PredictedWeakness]) -> int:
    """Accept only well-formed prose for ids the deterministic checks actually produced."""
    replaced = 0
    explanations = data.get("explanations")
    if not isinstance(explanations, dict):
        return 0
    for wid, text in explanations.items():
        w = known.get(str(wid))
        if w is None or not isinstance(text, str):
            continue
        text = text.strip()
        if 0 < len(text) <= MAX_PROSE_CHARS:
            w.why_it_matters = text
            w.generated_by = "llm+rules"
            replaced += 1
    return replaced


def _collect_patterns(data: dict, known: dict[str, PredictedWeakness]) -> list[SystemicPattern]:
    patterns: list[SystemicPattern] = []
    for item in (data.get("systemic_patterns") or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        narrative = str(item.get("narrative") or "").strip()
        ids = [str(i) for i in (item.get("weakness_ids") or []) if str(i) in known]
        # A pattern citing fewer than two of the deterministic findings is the model inventing one.
        if not title or not narrative or len(ids) < 2 or len(narrative) > MAX_PROSE_CHARS:
            continue
        patterns.append(SystemicPattern(title=title[:160], weakness_ids=ids,
                                        narrative=f"{LLM_EXTRA_LABEL} {narrative}"))
        if len(patterns) >= MAX_SYSTEMIC_PATTERNS:
            break
    return patterns


def _llm_pass(
    llm: Optional[LLMClient], weaknesses: list[PredictedWeakness], ctx: ScoutContext,
) -> tuple[bool, list[SystemicPattern], list[str]]:
    """Returns (any_prose_replaced, patterns, notes). Never raises; never alters the finding list."""
    notes: list[str] = []
    if llm is None or not llm.available():
        why = (llm.last_error or "disabled") if llm is not None else "no client"
        notes.append(f"LLM unavailable ({why}); templated prose used for all "
                     f"{len(weaknesses)} prediction(s)")
        return False, [], notes
    if not weaknesses:
        return False, [], notes

    known = {w.weakness_id: w for w in weaknesses}
    replaced = 0
    failures = 0
    chunks = [weaknesses[i : i + LLM_CHUNK_SIZE] for i in range(0, len(weaknesses), LLM_CHUNK_SIZE)]
    for chunk in chunks:
        data = llm.chat_json(PROSE_SYSTEM, _prose_payload(chunk, ctx), max_tokens=PROSE_MAX_TOKENS)
        if not data:
            failures += 1
            continue
        replaced += _apply_prose(data, known)
    if failures:
        notes.append(f"LLM ({llm.describe()}) failed on {failures} of {len(chunks)} prose batch(es) "
                     f"({llm.last_error or 'invalid JSON'}); those predictions kept their templated text")
    if replaced < len(weaknesses) and not failures:
        notes.append(f"LLM wrote prose for {replaced}/{len(weaknesses)} prediction(s); "
                     f"the rest kept their templated text")

    patterns: list[SystemicPattern] = []
    data = llm.chat_json(PATTERN_SYSTEM, _pattern_payload(weaknesses, ctx), max_tokens=PATTERN_MAX_TOKENS)
    if data:
        patterns = _collect_patterns(data, known)
    else:
        notes.append(f"LLM ({llm.describe()}) gave no usable systemic-pattern answer "
                     f"({llm.last_error or 'invalid JSON'}); the deterministic predictions stand alone")
    return replaced > 0, patterns, notes


# ---------------------------------------------------------------- markdown
def render_markdown(report: WeaknessReport) -> str:
    lines = [
        "# Predicted weaknesses — static analysis",
        "",
        f"**Project:** `{report.project}`  **Compose:** `{report.compose_file}`  "
        f"**Generated by:** {report.generated_by}  ",
        f"**{report.label}** — every severity below is PREDICTED from configuration. "
        f"Nothing here was measured: no fault has been injected. These numbers are not, and must not "
        f"be read as, the Risk agent's post-hoc measured severities. "
        f"Run `faultline scout --reconcile` after some experiments to see which held.",
        "",
        f"{len(report.weaknesses)} prediction(s) from {len(report.checks_run)} deterministic check(s): "
        f"{', '.join(report.checks_run)}.",
        "",
    ]
    if not report.weaknesses:
        lines.append("No predicted weaknesses: every deterministic check passed on this deployment.")
    for i, w in enumerate(report.weaknesses, 1):
        se = w.suggested_experiment
        lines += [
            f"## {i}. {w.title}",
            "",
            f"`{w.category}` · **{w.predicted_level.upper()}** · predicted severity "
            f"**{w.predicted_severity:.1f}/10** _(predicted, not measured)_ · "
            f"services: {', '.join(f'`{s}`' for s in w.services)}",
            "",
            f"**Why it matters.** {w.why_it_matters}",
            "",
            f"**Structural evidence.** {w.evidence}",
            "",
            f"**Would be tested by.** `{se.failure_type}` on `{se.target_service}`"
            + (f" — {se.reason}" if se.reason else ""),
        ]
        if se.policy_note:
            lines.append(f"> Policy: {se.policy_note}")
        lines += [
            "",
            f"**Confirmed if a live experiment raises.** "
            f"{', '.join(f'`{c}`' for c in w.confirming_finding_codes) or 'n/a'}",
            "",
            f"**Score.** " + " + ".join(f"{k} {v:g}" for k, v in w.factors.items() if v)
            + f" = {w.predicted_severity:g}",
            "",
        ]
    if report.systemic_patterns:
        lines += ["## Systemic patterns", "",
                  "_LLM narrative over the deterministic predictions above. Additive commentary, not "
                  "findings of their own; no pattern can introduce a weakness the checks did not "
                  "find._", ""]
        for p in report.systemic_patterns:
            lines += [f"### {p.title}", "",
                      f"Covers: {', '.join(f'`{i}`' for i in p.weakness_ids)}", "",
                      p.narrative, ""]
    if report.notes:
        lines += ["## Notes", "", *[f"- {n}" for n in report.notes], ""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- entry points
def build_context(settings, topology: Topology, nginx_text: str = "") -> ScoutContext:
    from ..safety.thresholds import load_policy

    knowledge_path = settings.knowledge_file if Path(settings.knowledge_file).is_file() else None
    if nginx_text == "" and Path(settings.nginx_conf).is_file():
        nginx_text = Path(settings.nginx_conf).read_text()
    return ScoutContext(
        topology=topology,
        compose=load_compose(settings.compose_file),
        knowledge=load_knowledge(knowledge_path),
        policy=Policy(load_policy(settings.thresholds_file)),
        compose_path=str(settings.compose_file),
        knowledge_path=knowledge_path,
        nginx_text=nginx_text,
    )


def scout(ctx: ScoutContext, llm: Optional[LLMClient] = None) -> WeaknessReport:
    weaknesses, checks_run = analyse(ctx)
    used_llm, patterns, llm_notes = _llm_pass(llm, weaknesses, ctx)
    notes = list(ctx.notes) + llm_notes
    if ctx.knowledge_path is None:
        notes.append("no knowledge file was readable; checks that consult documented knowledge "
                     "(retry/backoff evidence) ran on compose and topology alone")
    return WeaknessReport(
        project=ctx.topology.project,
        compose_file=ctx.compose_path,
        knowledge_file=ctx.knowledge_path,
        weaknesses=weaknesses,
        systemic_patterns=patterns,
        checks_run=checks_run,
        notes=notes,
        generated_by="llm+rules" if (used_llm or patterns) else "rules",
    )


def run(settings, store: ReportStore, llm: Optional[LLMClient] = None,
        log: Callable[[str], None] = print) -> WeaknessReport:
    """Read topology.json + the compose/knowledge files, write reports/weaknesses.json and .md."""
    topology = store.topology()
    if topology is None:
        raise FileNotFoundError(f"{store.root / 'topology.json'} not found; run `faultline topology` first")
    if not Path(settings.compose_file).is_file():
        raise FileNotFoundError(f"compose file {settings.compose_file} not found")

    report = scout(build_context(settings, topology), llm)
    store.write_weaknesses(report)
    (store.root / "weaknesses.md").write_text(render_markdown(report))
    for w in report.weaknesses:
        se = w.suggested_experiment
        log(f"[scout] {w.predicted_level.upper():<8} {w.predicted_severity:>5.1f}/10 (predicted)  "
            f"{w.category:<22} {','.join(w.services):<28} -> would be tested by "
            f"{se.failure_type} on {se.target_service}")
    for p in report.systemic_patterns:
        log(f"[scout] systemic pattern: {p.title} ({len(p.weakness_ids)} predictions)")
    for n in report.notes:
        log(f"[scout] note: {n}")
    log(f"[scout] {len(report.weaknesses)} predicted weakness(es) from {len(report.checks_run)} "
        f"deterministic check(s) ({report.generated_by}); all {report.label} "
        f"-> weaknesses.json, weaknesses.md")
    return report
