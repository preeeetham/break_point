"""Turn an experiment's findings into "Can I fix this?" proposals. The LLM proposes, the code disposes.

Code does everything that matters:
  * which fixes are candidates (only code-defined catalog fixes, faultline/remediation/catalog.py),
  * which services they touch (CatalogFix.targets),
  * the exact change (CatalogFix.build against the base compose and the current override),
  * the verbatim preview and its sha256 hash, the expected metrics, the rollback text.

An optional single LLM call may rank the shortlist and reword three prose fields (problem, expected
improvement, risks) in plainer language. Every part of its answer is validated; anything off the shortlist,
too long, empty, or that looks like a diff or YAML is discarded and the template is kept. The `change` and
`rollback` fields and the preview are never touched by the model. With FAULTLINE_LLM=false (or no model
reachable) the feature works the same, entirely from templates.

Nothing here applies anything: a proposal is written as `pending` and waits for a human (CLI, dashboard or
email) to approve the exact hash it was shown. Applying is faultline/remediation/apply.py's job.
"""
from __future__ import annotations

import copy
import json
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from faultline.remediation import proposals as P
from faultline.reporting.store import ReportStore
from faultline.schemas import (
    ExperimentReport,
    ExpectedMetric,
    Finding,
    FixProposal,
    ProposalAuditEntry,
    ProposalExplanation,
    Topology,
)

# Proposals that already cover a (experiment, fix, services) triple: still in flight, or done and verified.
# A rejected / expired / rolled-back / verification_failed one does not block proposing it again.
BLOCKING_STATUSES = frozenset({"pending", "approved", "applying", "applied", "verified"})
LLM_FIELDS = ("problem", "expected_improvement", "risks")
LLM_MAX_CHARS = 600
_DIFF_PREFIXES = ("+", "-", "@@", "services:", "```")

# Plain-words meaning of each finding code. Same wording as the dashboard's FINDING_HELP
# (faultline/dashboard/static/copy.js), with the dashes taken out.
FINDING_PLAIN = {
    "health_check_masked_failure": "The platform reported this service as ready while real requests were failing, so an "
                                   "orchestrator would have kept routing traffic to it. This mirrors Eclipse Ditto issue #1865.",
    "dependency_blind_readiness": "Dependents stayed 'ready' while their dependency was gone, because readiness only "
                                  "reflects the service's own process.",
    "no_automated_remediation": "Nothing reacted to the outage. Docker's restart policy only fires when a container exits, "
                                "never when it is merely unhealthy.",
    "manual_restart_required": "The system did not heal by itself; Faultline had to restart containers. In production this "
                               "is a page at 3am.",
    "recovery_failed": "The system never came back within the recovery timeout.",
    "relapse_after_recovery": "It looked recovered, then degraded again.",
    "data_loss_suspected": "A write the API had already acknowledged was missing afterwards. This is the most serious "
                           "class of finding.",
    "cascade_beyond_hypothesis": "The failure spread further than predicted. The blast radius is larger than the "
                                 "dependency map suggested.",
    "slow_recovery": "Recovery took substantially longer than the hypothesis predicted.",
    "safety_abort": "A safety limit was crossed and the experiment was rolled back early, by design.",
    "api_impact": "Real API clients were affected by the fault.",
}

# Metrics Faultline can compare before/after a fix, and which direction is better (used for wording only;
# the apply engine owns the actual comparison).
METRIC_LABELS = {
    "time_to_recover_s": ("time to recover", "s"),
    "error_rate_during": ("API error rate during the fault", ""),
    "p95_during_ms": ("p95 latency during the fault", "ms"),
    "service_score": ("resilience score", "/100"),
}


def _plain(text: Any) -> str:
    """No em/en dashes in anything a human reads (house style), and collapse whitespace."""
    s = str(text or "").replace("—", ", ").replace("–", "-")
    return " ".join(s.split()).replace(" ,", ",").replace(",,", ",")


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _num(x: Optional[float], unit: str = "") -> str:
    if x is None:
        return "n/a"
    v = (f"{x:.4f}" if abs(x) < 1 else f"{x:.1f}").rstrip("0").rstrip(".")
    return f"{v} {unit}".strip() if unit and not unit.startswith("/") else f"{v}{unit}"


# ---------------------------------------------------------------------- candidates
@dataclass
class Candidate:
    finding: Finding
    fix: Any                       # catalog.CatalogFix
    change: Any                    # catalog.Change
    services: list[str]
    preview: str                   # verbatim, code-generated; hashed as-is
    requires_allow_protected: bool = False
    motivated_by: str = ""         # title of the Fix agent recommendation that motivated it, if any
    path_label: str = ""

    @property
    def id(self) -> str:
        """Stable shortlist id shown to the LLM: catalog fix id plus target services."""
        return f"{self.fix.id}:{'+'.join(self.services)}"


def _load_yaml(path: str) -> dict:
    """yaml.safe_load resolves anchors, aliases and `<<` merges, so the result is the effective config."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        from faultline.remediation.override import load_yaml_text   # same loader the apply engine falls back to
        return load_yaml_text(p.read_text(encoding="utf-8"))
    except ImportError:
        data = yaml.safe_load(p.read_text()) or {}
        return data if isinstance(data, dict) else {}


def base_compose(settings) -> dict:
    """The configuration really running: override.base_config (merged live `docker compose config`, read-only)
    when Agent A's module provides it, else the yaml of settings.compose_file."""
    try:
        from faultline.remediation import override
        fn = getattr(override, "base_config", None)
        if fn is not None:
            cfg = fn(settings)
            if isinstance(cfg, dict) and cfg:
                return cfg
    except Exception:
        pass
    return _load_yaml(settings.compose_file)


def _current_override(settings) -> tuple[dict, str]:
    path = settings.remediation_override_file
    try:
        from faultline.remediation import override
        text = override.read_text(path) or ""
        data = override.load(path) or {}
    except ImportError:
        text = Path(path).read_text() if Path(path).is_file() else ""
        data = yaml.safe_load(text) or {} if text else {}
    return (data if isinstance(data, dict) else {}), text


def _path_label(settings) -> str:
    p = Path(settings.remediation_override_file)
    try:
        return str(p.relative_to(Path(settings.compose_file).resolve().parent.parent))
    except ValueError:
        return p.name


def _topology(settings, store: ReportStore) -> Optional[Topology]:
    topo = store.topology()
    if topo is not None:
        return topo
    try:
        from faultline.topology.graph import build
        nginx = settings.nginx_conf if Path(settings.nginx_conf).is_file() else None
        return build(settings.compose_file, settings.compose_project, nginx, settings.knowledge_file, live=False)
    except Exception:
        return None


def _requires_allow_protected(settings, fix: Any, services: list[str]) -> bool:
    """CatalogFix.requires_allow_protected(services, protected): the policy's protected list (thresholds.yaml)."""
    fn = getattr(fix, "requires_allow_protected", None)
    if not callable(fn):
        return False
    try:
        from faultline.remediation.catalog import protected_services
        thresholds = getattr(settings, "thresholds_file", None)
        protected = protected_services(thresholds_file=thresholds) if thresholds and Path(thresholds).is_file() else None
        return bool(fn(services, protected))
    except Exception:
        return bool(fn(services))


def _fix_agent_titles(store: ReportStore, eid: str) -> list[str]:
    rec = store.fix(eid)
    return [r.title for r in rec.recommendations] if rec else []


def _motivating_title(titles: list[str], finding: Finding, services: list[str], fix: Any) -> str:
    """Which Fix-agent recommendation (reports/fixes/<id>.json) led here: matched through the knowledge rules
    by finding code first, then by target service. Empty when the Fix agent said nothing relevant."""
    if not titles:
        return ""
    present = set(titles)
    for attr in ("recommendation_titles", "knowledge_titles"):
        for t in getattr(fix, attr, None) or ():
            if t in present:
                return t
    try:
        from faultline.agents.knowledge import RULES
    except Exception:
        RULES = []
    by_code = [r["recommendation"]["title"] for r in RULES
               if finding.code in (r["match"].get("finding_codes") or ()) and r["recommendation"]["title"] in present]
    if by_code:
        return by_code[0]
    by_target = [r["recommendation"]["title"] for r in RULES
                 if set(services) & set(r["match"].get("targets") or ()) and r["recommendation"]["title"] in present]
    if by_target:
        return by_target[0]
    for t in titles:
        low = t.lower()
        if any(s.lower() in low for s in services):
            return t
    return ""


def _blocked(store: ReportStore, eid: str, fix_id: str, services: list[str]) -> Optional[FixProposal]:
    want = sorted(services)
    for p in store.proposals():
        if (p.experiment_id == eid and p.catalog_fix_id == fix_id and sorted(p.target_services) == want
                and p.status in BLOCKING_STATUSES):
            return p
    return None


def candidates(settings, store: ReportStore, experiment_id: str, include_test: bool = False,
               base: Optional[dict] = None, log: Callable[[str], None] = lambda m: None) -> list[Candidate]:
    """Code-selected fix candidates for one experiment. Raises KeyError if the experiment does not exist."""
    from faultline.remediation import catalog

    report = store.experiment(experiment_id)
    if report is None:
        raise KeyError(f"no experiment {experiment_id}")
    topology = _topology(settings, store)
    base_cfg = base if base is not None else base_compose(settings)
    current, current_text = _current_override(settings)
    label = _path_label(settings)
    titles = _fix_agent_titles(store, experiment_id)

    out: list[Candidate] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for finding in report.findings:
        if finding.severity == "info":
            continue
        for fix in catalog.for_finding(finding.code, include_test=include_test):
            try:
                wanted = list(fix.targets(report, topology, finding, base_compose=base_cfg) or [])
            except Exception as exc:
                log(f"[propose] {fix.id}: targets() failed ({type(exc).__name__}: {exc}); skipped")
                continue
            if not wanted or (fix.id, tuple(sorted(wanted))) in seen:
                continue
            seen.add((fix.id, tuple(sorted(wanted))))
            try:
                change = fix.build(copy.deepcopy(base_cfg), copy.deepcopy(current), list(wanted), topology)
            except Exception as exc:
                log(f"[propose] {fix.id}: build() failed ({type(exc).__name__}: {exc}); skipped")
                continue
            if change is None:
                log(f"[propose] {fix.id} on {', '.join(wanted)}: nothing to change (already in place or not applicable)")
                continue
            # The services the change really touches (build may skip some of those asked for). The apply engine
            # rebuilds from exactly this list, which reproduces the same override and so the same preview.
            services = list(getattr(change, "services", None) or wanted)
            key = (fix.id, tuple(sorted(services)))
            if key != (fix.id, tuple(sorted(wanted))):
                if key in seen:
                    continue
                seen.add(key)
            existing = _blocked(store, experiment_id, fix.id, services)
            if existing is not None:
                log(f"[propose] {fix.id} on {', '.join(services)}: already proposed as {existing.proposal_id} "
                    f"({existing.status}); not proposing again")
                continue
            preview = catalog.preview(change, current_text, label)
            out.append(Candidate(
                finding=finding, fix=fix, change=change, services=services, preview=preview,
                requires_allow_protected=_requires_allow_protected(settings, fix, services),
                motivated_by=_motivating_title(titles, finding, services, fix), path_label=label,
            ))
    return out


# ---------------------------------------------------------------------- templates (no LLM)
def _service_score(store: ReportStore, service: str) -> Optional[float]:
    """The experiment target's scorecard score: the same number the apply engine compares after the re-run."""
    sc = store.scorecard()
    if sc is None:
        return None
    return next((r.score for r in sc.ranking if r.service == service and r.score is not None), None)


LOWER_IS_BETTER = ("time_to_recover_s", "error_rate_during", "p95_during_ms")
# The catalog's Change.expected names -> the metric names the apply engine compares (apply.METRICS).
CATALOG_METRIC_ALIASES = {
    "time_to_recover_s": "time_to_recover_s",
    "during_success_rate": "error_rate_during",
    "error_rate_during": "error_rate_during",
    "error_rate": "error_rate_during",
    "p95_ms": "p95_during_ms",
    "during_p95_ms": "p95_during_ms",
    "p95_during_ms": "p95_during_ms",
    "service_score": "service_score",
}


def _metric_bound(metric: str, before: Optional[float], hint: str) -> str:
    """Turn the catalog's expectation into a stated bound. Code decides the number, never the model."""
    h = _plain(hint)
    low = h.lower()
    if before is None:
        return h or "no worse"
    if not h or low.startswith("no worse") or low in ("same", "unchanged"):
        op = ">=" if metric == "service_score" else "<="
        bound = f"{op} {_num(before)} (no worse)"
        return bound + h[len("no worse"):] if low.startswith("no worse") and len(h) > len("no worse") else bound
    if low in ("lower", "decrease", "down", "better") and metric in LOWER_IS_BETTER:
        return f"< {_num(before)}"
    if low in ("higher", "increase", "up", "better") and metric == "service_score":
        return f"> {_num(before)}"
    return h


def _expected_hints(change: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in getattr(change, "expected", None) or []:
        try:
            metric, bound = item
        except (TypeError, ValueError):
            continue
        out.append((str(metric), str(bound)))
    return out


def expected_metrics(report: ExperimentReport, change: Any, services: list[str],
                     store: Optional[ReportStore] = None) -> list[ExpectedMetric]:
    """Before values from the report (and the target's scorecard score), with the catalog's expectation as a bound.
    The four standard metrics use the apply engine's names so its before/after comparison lines up."""
    sr = report.during.success_rate
    before = {
        "time_to_recover_s": report.recovery.time_to_recover_s,
        "error_rate_during": None if sr is None else round(1.0 - sr, 4),
        "p95_during_ms": report.during.p95_ms,
        "service_score": _service_score(store, report.target.get("service", "")) if store is not None else None,
    }
    hints: dict[str, str] = {}
    extras: list[tuple[str, str]] = []
    for name, bound in _expected_hints(change):
        canon = CATALOG_METRIC_ALIASES.get(name)
        if canon:
            hints.setdefault(canon, bound)
        else:
            extras.append((name, bound))
    out = []
    for metric in ("time_to_recover_s", "error_rate_during", "p95_during_ms", "service_score"):
        if before[metric] is None and metric not in hints:
            continue
        out.append(ExpectedMetric(metric=metric, before=before[metric],
                                  expected_after=_metric_bound(metric, before[metric], hints.get(metric, ""))))
    # What else the catalog promises (e.g. "finding:health_check_masked_failure" -> "absent ..."). For a finding,
    # `before` is how many times the report raised it, so "absent" has a concrete starting point.
    codes = [f.code for f in report.findings]
    for name, bound in extras:
        b = float(codes.count(name.split(":", 1)[1])) if name.startswith("finding:") else None
        out.append(ExpectedMetric(metric=name, before=b, expected_after=_plain(bound) or "no worse"))
    return out


def _evidence(report: ExperimentReport, finding: Finding) -> str:
    target = report.target.get("service", "?")
    bits = [f"In experiment {report.experiment_id} ({report.failure_type} on {target}), Faultline observed: "
            f"{_plain(finding.title)}. Evidence: {_plain(finding.evidence)}."]
    b, d = report.baseline, report.during
    bits.append(f"During the fault API success was {_pct(d.success_rate)} (baseline {_pct(b.success_rate)}) and "
                f"p95 latency was {_num(d.p95_ms, 'ms')} (baseline {_num(b.p95_ms, 'ms')}).")
    rec = report.recovery
    if rec.recovered and rec.time_to_recover_s is not None:
        how = " and needed manual restarts" if rec.remediation_actions else ""
        bits.append(f"It took {_num(rec.time_to_recover_s, 's')} to recover{how}.")
    elif not rec.recovered:
        bits.append("It did not recover within the timeout.")
    if report.affected_services_observed:
        bits.append(f"Affected services: {', '.join(report.affected_services_observed)}.")
    return " ".join(bits)


def _metric_label(metric: str) -> tuple[str, str]:
    if metric in METRIC_LABELS:
        return METRIC_LABELS[metric]
    kind, _, what = metric.partition(":")
    if kind == "finding" and what:
        return f"finding {what}", ""
    if kind == "docker_health" and what:
        return f"Docker health of {what}", ""
    return metric.replace("_", " "), ""


def _before_text(m: ExpectedMetric) -> str:
    if m.metric.startswith("finding:"):
        if m.before is None:
            return "n/a"
        n = int(m.before)
        return "not raised" if n == 0 else f"raised {n} time{'s' if n != 1 else ''}"
    if m.metric == "error_rate_during":
        return _pct(m.before)
    return _num(m.before, _metric_label(m.metric)[1])


def _expected_text(metrics: list[ExpectedMetric]) -> str:
    parts = []
    for m in metrics:
        label = _metric_label(m.metric)[0]
        now = "" if m.before is None else f"now {_before_text(m)}, "
        parts.append(f"{label}: {now}expected after the fix {m.expected_after}")
    return "; ".join(parts)


def template_explanation(report: ExperimentReport, c: Candidate, metrics: list[ExpectedMetric]) -> ProposalExplanation:
    finding, change = c.finding, c.change
    meaning = FINDING_PLAIN.get(finding.code, "")
    problem = (f"{meaning} " if meaning else "") + _evidence(report, finding)
    if c.motivated_by:
        problem += f" The Fix agent recommended: {_plain(c.motivated_by)}."

    summary = _plain(getattr(change, "summary", "") or "")
    runtime = bool(getattr(change, "runtime_only", False) or getattr(c.fix, "runtime_only", False))
    if runtime:
        change_text = (f"{summary} Exact change (shown verbatim below): the runtime command "
                       f"'{_plain(getattr(change, 'action', ''))}' for {', '.join(c.services)}. No configuration file "
                       "is written. What runs is exactly the text under 'Exact change' and nothing else; its sha256 "
                       "hash is pinned in this proposal.")
    else:
        change_text = (f"{summary} Exact change (shown verbatim below): a unified diff of the Faultline-owned compose "
                       f"override ({c.path_label}) for {', '.join(c.services)}, after which those containers are "
                       "recreated; the base compose files are never edited. What is applied is exactly the text "
                       "under 'Exact change' and nothing else; its sha256 hash is pinned in this proposal.")

    expected = _expected_text(metrics)
    expected_improvement = ((f"Against this experiment's numbers: {expected}. " if expected else "")
                            + "Faultline re-runs the same experiment after applying and reports before and after; "
                              "if the stack is unhealthy or the result is worse, it rolls the change back.")

    risks = _plain(getattr(change, "risks", "") or "") or "No specific risk recorded for this fix."
    if c.requires_allow_protected:
        try:
            from faultline.remediation.catalog import protected_services
            prot = sorted(set(c.services) & protected_services()) or c.services
        except Exception:
            prot = c.services
        risks += (f" Touches protected service {', '.join(prot)}; applying it needs --allow-protected "
                  "(the data store holds every twin and policy).")

    rollback = _plain(getattr(change, "rollback", "") or "")
    if runtime:
        rollback = (rollback + " The override file is not touched by this action.").strip()
    else:
        rollback = (rollback + " Faultline backs up the override first and restores it byte for byte if the "
                    "stack is unhealthy after applying, if the re-run is worse, or on request.").strip()
    return ProposalExplanation(problem=_plain(problem), change=_plain(change_text),
                               expected_improvement=_plain(expected_improvement), risks=_plain(risks),
                               rollback=_plain(rollback), generated_by="template")


# ---------------------------------------------------------------------- optional LLM step
LLM_SYSTEM = (
    "You review proposed fixes for an Eclipse Ditto docker-compose deployment after a chaos experiment. "
    "The fixes and their exact changes are fixed by code; you cannot add, change or invent fixes. "
    "You may (a) rank the given candidate ids from most to least valuable, and (b) reword the 'problem', "
    "'expected_improvement' and 'risks' texts of a candidate in plain language for an operator, keeping every "
    "number and fact. Never write a diff, YAML, code or commands. Each text at most 500 characters. "
    'Reply with JSON only: {"ranking": ["<id>", ...], "rewrites": {"<id>": {"problem": str, '
    '"expected_improvement": str, "risks": str}}}'
)


def _looks_like_change(text: str) -> bool:
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith(_DIFF_PREFIXES):
            return True
    return False


def valid_llm_text(value: Any) -> Optional[str]:
    """A reworded field is accepted only if it is a non-empty string of at most 600 chars with no diff/YAML."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or len(s) > LLM_MAX_CHARS or _looks_like_change(s):
        return None
    return _plain(s)


@dataclass
class LLMOutcome:
    order: list[str]                                      # final order of candidate ids
    ranked_by_llm: bool = False
    rewrites: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def apply_llm_answer(data: Any, ids: list[str]) -> LLMOutcome:
    """Strictly validate one LLM answer against the code-built shortlist `ids`."""
    out = LLMOutcome(order=list(ids))
    if not isinstance(data, dict):
        out.notes.append("LLM answer was not a JSON object; template kept")
        return out
    shortlist = set(ids)

    ranking = data.get("ranking")
    if isinstance(ranking, list):
        valid: list[str] = []
        for r in ranking:
            if not isinstance(r, str) or r not in shortlist:
                out.notes.append(f"LLM named {str(r)[:60]!r}, which is not on the code-built shortlist; discarded")
                continue
            if r not in valid:
                valid.append(r)
        if valid:
            out.order = valid + [i for i in ids if i not in valid]
            out.ranked_by_llm = True
    elif ranking is not None:
        out.notes.append("LLM ranking was not a list; code order kept")

    rewrites = data.get("rewrites")
    if isinstance(rewrites, dict):
        for cid, fields in rewrites.items():
            if cid not in shortlist:
                out.notes.append(f"LLM rewrote {str(cid)[:60]!r}, which is not on the shortlist; discarded")
                continue
            if not isinstance(fields, dict):
                continue
            kept: dict[str, str] = {}
            for name, value in fields.items():
                if name not in LLM_FIELDS:
                    if name in ("change", "rollback", "change_preview", "diff"):
                        out.notes.append(f"LLM tried to write {name!r} for {cid}; only code writes it, discarded")
                    continue
                text = valid_llm_text(value)
                if text is None:
                    out.notes.append(f"LLM {name} for {cid} rejected (empty, over {LLM_MAX_CHARS} chars, or "
                                     "contained a diff/YAML); template kept")
                    continue
                kept[name] = text
            if kept:
                out.rewrites[cid] = kept
    return out


def _llm_payload(report: ExperimentReport, cands: list[Candidate], expl: dict[str, ProposalExplanation]) -> str:
    return json.dumps({
        "experiment": {"id": report.experiment_id, "target": report.target.get("service"),
                       "failure_type": report.failure_type,
                       "success_rate_during": report.during.success_rate,
                       "time_to_recover_s": report.recovery.time_to_recover_s},
        "candidates": [{"id": c.id, "fix": c.fix.id, "title": getattr(c.fix, "title", c.fix.id),
                        "finding": c.finding.code, "services": c.services,
                        "problem": expl[c.id].problem, "expected_improvement": expl[c.id].expected_improvement,
                        "risks": expl[c.id].risks} for c in cands],
    }, default=str)


def _ask_llm(llm: Any, report: ExperimentReport, cands: list[Candidate],
             expl: dict[str, ProposalExplanation]) -> LLMOutcome:
    ids = [c.id for c in cands]
    try:
        data = llm.chat_json(LLM_SYSTEM, _llm_payload(report, cands, expl), max_tokens=1500)
    except Exception as exc:
        return LLMOutcome(order=ids, notes=[f"LLM call failed ({exc}); template kept"])
    if not data:
        return LLMOutcome(order=ids, notes=["LLM gave no usable answer; template kept"])
    return apply_llm_answer(data, ids)


def _llm_usable(llm: Any) -> bool:
    if llm is None or getattr(llm, "enabled", True) is False:
        return False
    try:
        return bool(llm.available())
    except Exception:
        return False


# ---------------------------------------------------------------------- build + persist
def build_proposals(settings, store: ReportStore, experiment_id: str, llm: Any = None, include_test: bool = False,
                    log: Callable[[str], None] = print, base: Optional[dict] = None) -> list[FixProposal]:
    """candidates -> templates -> (optional, validated) LLM ranking/rewording -> pending FixProposals on disk."""
    report = store.experiment(experiment_id)
    if report is None:
        raise KeyError(f"no experiment {experiment_id}")
    cands = candidates(settings, store, experiment_id, include_test=include_test, base=base, log=log)
    if not cands:
        log(f"[propose] {experiment_id}: no catalog fix applies (or all are already proposed); nothing to ask")
        return []

    metrics = {c.id: expected_metrics(report, c.change, c.services, store) for c in cands}
    expl = {c.id: template_explanation(report, c, metrics[c.id]) for c in cands}
    outcome = LLMOutcome(order=[c.id for c in cands])
    if _llm_usable(llm):
        outcome = _ask_llm(llm, report, cands, expl)
        for n in outcome.notes:
            log(f"[propose] {n}")
        for cid, fields in outcome.rewrites.items():
            expl[cid] = expl[cid].model_copy(update={**fields, "generated_by": "llm"})

    by_id = {c.id: c for c in cands}
    out: list[FixProposal] = []
    for rank, cid in enumerate(outcome.order, 1):
        c = by_id[cid]
        note = "proposed by faultline"
        if c.motivated_by:
            note += f"; motivated by Fix agent recommendation: {c.motivated_by}"
        if outcome.notes:
            note += "; LLM: " + " | ".join(outcome.notes)
        p = FixProposal(
            proposal_id=P.new_proposal_id(), experiment_id=experiment_id, finding_code=c.finding.code,
            catalog_fix_id=c.fix.id, target_services=c.services, change_preview=c.preview,
            change_hash=P.change_hash(c.preview), explanation=expl[cid], expected_metrics=metrics[cid],
            requires_allow_protected=c.requires_allow_protected, rank=rank,
            ranked_by="llm" if outcome.ranked_by_llm else "code", status="pending",
            expires_at=P.expires_at(settings.fix_proposal_timeout_s),
            audit=[ProposalAuditEntry(actor="faultline", from_status=None, to_status="pending", note=note)],
        )
        store.write_proposal(p)
        md = store.root / "proposals" / f"{p.proposal_id}.md"
        md.write_text(render_markdown(p))
        out.append(p)
        log(f"[propose] {experiment_id}: #{rank} {p.proposal_id} {c.fix.id} on {', '.join(c.services)} "
            f"(hash {p.change_hash[:12]}, {p.explanation.generated_by}, expires "
            f"{p.expires_at.astimezone(timezone.utc):%H:%M} UTC) -> {md.name}")
    return out


# ---------------------------------------------------------------------- rendering
def _expires_in(p: FixProposal, now: Optional[datetime] = None) -> str:
    secs = (p.expires_at - (now or datetime.now(timezone.utc))).total_seconds()
    if secs <= 0:
        return "expired"
    return f"in {int(secs // 60)} min" if secs >= 60 else f"in {int(secs)} s"


def _metric_lines(p: FixProposal) -> list[str]:
    out = []
    for m in p.expected_metrics:
        out.append(f"{m.metric} ({_metric_label(m.metric)[0]}): before {_before_text(m)}, "
                   f"expected after {m.expected_after}")
    return out


def _wrap(text: str, indent: str = "   ") -> str:
    return textwrap.fill(text, width=100, initial_indent=indent, subsequent_indent=indent, break_on_hyphens=False,
                         break_long_words=False)


def render_text(p: FixProposal) -> str:
    """The full human-readable "Can I fix this?" request (CLI and email)."""
    e = p.explanation
    lines = [
        f"Can I fix this?  Proposal {p.proposal_id}  [{p.status}]",
        f"Experiment {p.experiment_id}, finding {p.finding_code}, fix '{p.catalog_fix_id}' on "
        f"{', '.join(p.target_services)} (rank {p.rank}, ranked by {p.ranked_by}, text by {e.generated_by})",
        "",
        "1. What is wrong", _wrap(e.problem), "",
        "2. Exactly what I will change", _wrap(e.change), "",
        "   ----- exact change (verbatim, generated by code) -----",
        p.change_preview.rstrip("\n"),
        "   ----- end of change -----", "",
        "3. Expected improvement", _wrap(e.expected_improvement),
        *[f"   - {m}" for m in _metric_lines(p)], "",
        "4. Risks", _wrap(e.risks), "",
        "5. Rollback", _wrap(e.rollback), "",
        f"Change hash (sha256): {p.change_hash}",
        f"Expires: {p.expires_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC ({_expires_in(p)}); "
        "unanswered proposals expire and are never applied.",
    ]
    if p.requires_allow_protected:
        lines.append("Protected: this touches a protected service; approving from the CLI needs --allow-protected.")
    if p.status == "pending":
        lines += [
            "",
            f"Approve:  faultline approve {p.proposal_id} --hash {p.change_hash[:12]}"
            + (" --allow-protected" if p.requires_allow_protected else ""),
            f"          (or run `faultline approve {p.proposal_id}` and type the first 8 characters: {p.change_hash[:8]})",
            f"Reject:   faultline reject {p.proposal_id} --reason \"...\"",
        ]
    elif p.verification is not None:
        lines += ["", f"Verification: {p.verification.outcome}. {p.verification.reason}".rstrip()]
    return "\n".join(lines) + "\n"


def render_markdown(p: FixProposal) -> str:
    e = p.explanation
    metrics = _metric_lines(p)
    lines = [
        f"# Can I fix this? `{p.proposal_id}`",
        "",
        f"**Experiment:** `{p.experiment_id}`  **Finding:** `{p.finding_code}`  **Fix:** `{p.catalog_fix_id}` "
        f"on {', '.join(f'`{s}`' for s in p.target_services)}  **Status:** {p.status}  ",
        f"**Rank:** {p.rank} (by {p.ranked_by})  **Text:** {e.generated_by}",
        "",
        "## 1. What is wrong", "", e.problem, "",
        "## 2. Exactly what will change", "", e.change, "",
        "```diff", p.change_preview.rstrip("\n"), "```", "",
        "## 3. Expected improvement", "", e.expected_improvement, "",
        *[f"- {m}" for m in metrics], *([""] if metrics else []),
        "## 4. Risks", "", e.risks, "",
        "## 5. Rollback", "", e.rollback, "",
        f"**Change hash (sha256):** `{p.change_hash}`  ",
        f"**Expires:** {p.expires_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC. Unanswered proposals expire "
        "and are never applied.",
        "",
        f"Approve: `faultline approve {p.proposal_id} --hash {p.change_hash[:12]}`"
        + (" `--allow-protected`" if p.requires_allow_protected else "") + "  ",
        f"Reject: `faultline reject {p.proposal_id} --reason \"...\"`",
    ]
    return "\n".join(lines).rstrip() + "\n"
