"""The remediation catalog: the ONLY changes Faultline can ever apply.

"LLM proposes, code disposes." An experiment finding maps to catalog entries by finding code; each entry
picks its target services from the experiment report by fixed rules (`targets`), and builds the change
deterministically from the base compose config and the current Faultline override (`build`). A model may
rank or reword proposals, never invent a fix, a target or a diff: `preview` is generated here, and that
exact text is what a human approves and what `change_hash` pins.

Config changes go into the Faultline-owned override file (faultline/remediation/override.py), never into
the base compose files. Runtime-only entries change no configuration at all.

Every entry was checked against the real Ditto 3.9.8 stack (deploy/docker-compose.yml and the live
`bosch-ditto` project, which runs the upstream Ditto compose file):
  add_or_tighten_healthcheck   /alive -> /ready on port 7626 for the five Java services (a real change)
  add_dependency_healthcheck   mongosh ping on mongodb when the base has none (the live stack has none)
  add_healthy_dependency       depends_on mongodb: service_healthy; None until mongodb has a healthcheck
  set_restart_policy           None on Ditto: every service already has `restart: always`
  set_resource_limits          a CPU limit sized to the JVM's -XX:ActiveProcessorCount (Ditto sets 2)
  restart_service_now          runtime only: `docker restart <container>`
  break_healthcheck_for_test   TEST ONLY: a healthcheck that always fails, to exercise rollback
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from faultline.remediation import override as ov
from faultline.schemas import ExperimentReport, Finding, Topology
from faultline.topology.compose import parse_env, parse_mem_limit

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_CATALOG_ENV = "FAULTLINE_TEST_CATALOG"

# Codes whose finding names the services it is about (one finding per service), as the injector writes them.
SERVICE_NAMED_CODES = frozenset({"health_check_masked_failure", "dependency_blind_readiness"})

HEALTHCHECK_TIMING_KEYS = ("interval", "timeout", "retries", "start_period", "start_interval")
# The MongoDB check Faultline's own deploy/docker-compose.yml ships. mongosh is in the official mongo >= 6
# images; verified on the live mongo 7.0.43 container (`mongosh --quiet --eval ...` printed 1).
MONGO_HEALTHCHECK = {
    "test": 'mongosh --quiet --eval "db.runCommand({ping:1}).ok" | grep -q 1',
    "interval": "15s",
    "timeout": "5s",
    "retries": 3,
    "start_period": "20s",
}
CPU_MIN, CPU_MAX, CPU_DEFAULT = 0.5, 2.0, 1.0
ACTIVE_PROCESSORS_RE = re.compile(r"-XX:ActiveProcessorCount=(\d+)")
BROKEN_HEALTHCHECK = {"test": ["CMD-SHELL", "exit 1"], "interval": "5s", "timeout": "2s", "retries": 1,
                      "start_period": "0s"}


# ---------------------------------------------------------------- data
@dataclass
class Change:
    """One deterministic change. `override_after` is the FULL new override (runtime-only: unchanged)."""
    fix_id: str
    services: list[str]
    override_after: dict
    action: str = ""                                   # runtime-only: the exact command
    summary: str = ""                                  # one plain sentence
    expected: list[tuple[str, str]] = field(default_factory=list)   # (metric, expected_after)
    risks: str = ""
    rollback: str = ""
    runtime_only: bool = False
    notes: list[str] = field(default_factory=list)     # services considered and skipped, and why


BuildFn = Callable[[dict, dict, list[str], Topology], tuple[Optional[Change], str]]
TargetsFn = Callable[["CatalogFix", ExperimentReport, Topology, Optional[Finding], Optional[dict]], list[str]]


@dataclass(frozen=True)
class CatalogFix:
    id: str
    title: str
    addresses: tuple[str, ...]
    roles: tuple[str, ...]
    runtime_only: bool
    is_reversible: bool = True
    test_only: bool = False
    protected_ok: bool = False
    _targets: Optional[TargetsFn] = field(default=None, repr=False, compare=False)
    _build: Optional[BuildFn] = field(default=None, repr=False, compare=False)

    def targets(self, report: ExperimentReport, topology: Topology, finding: Optional[Finding] = None,
                base_compose: Optional[dict] = None) -> list[str]:
        """Services this fix would change for this finding, chosen by code, filtered to existing services
        (in `base_compose` when given, and in the topology) whose role this fix may touch."""
        raw = self._targets(self, report, topology, finding, base_compose) if self._targets else []
        base_services = _services(base_compose) if base_compose is not None else None
        out: list[str] = []
        for s in raw:
            node = topology.services.get(s)
            if node is None or node.role not in self.roles or s in out:
                continue
            if base_services is not None and s not in base_services:
                continue
            out.append(s)
        return out

    def build_with_reason(self, base_compose: dict, override: dict, services: list[str],
                          topology: Topology) -> tuple[Optional[Change], str]:
        """(change, "") or (None, why nothing needs changing / why it cannot be built)."""
        if not services:
            return None, "no target services"
        if self._build is None:
            return None, "entry has no builder"
        return self._build(base_compose or {}, override or {}, list(services), topology)

    def build(self, base_compose: dict, override: dict, services: list[str], topology: Topology) -> Optional[Change]:
        return self.build_with_reason(base_compose, override, services, topology)[0]

    def preview(self, change: Change, before_text: str, path_label: str) -> str:
        return preview(change, before_text, path_label)

    def requires_allow_protected(self, services: Iterable[str], protected: Optional[Iterable[str]] = None) -> bool:
        """True when the change touches a protected service (thresholds.yaml policy.protected, e.g. mongodb)."""
        if self.protected_ok:
            return False
        prot = set(protected) if protected is not None else protected_services()
        return bool(prot & set(services))


def preview(change: Change, before_text: str, path_label: str) -> str:
    """The verbatim text a human approves: the override diff, or the exact runtime command."""
    if change.runtime_only:
        return change.action
    return ov.diff(before_text, ov.render(change.override_after), path_label)


def protected_services(policy: Optional[dict] = None, thresholds_file: Optional[str] = None) -> set[str]:
    if policy is None:
        from faultline.safety.thresholds import load_policy

        path = thresholds_file or str(ROOT / "config" / "thresholds.yaml")
        policy = load_policy(path) if Path(path).is_file() else {}
    return set(((policy or {}).get("policy") or {}).get("protected") or {})


# ---------------------------------------------------------------- compose helpers
def _services(compose: Optional[dict]) -> dict:
    return (compose or {}).get("services") or {}


def _svc(compose: Optional[dict], name: str) -> dict:
    return _services(compose).get(name) or {}


def _ov_svc(after: dict, name: str) -> dict:
    """The override's entry for `name`, created on demand (mutates `after`)."""
    services = after.setdefault("services", {})
    return services.setdefault(name, {})


def _test_text(test: Any) -> str:
    if isinstance(test, (list, tuple)):
        return " ".join(str(t) for t in test)
    return "" if test is None else str(test)


def _has_healthcheck(svc: dict) -> bool:
    hc = svc.get("healthcheck")
    if not isinstance(hc, dict) or hc.get("disable"):
        return False
    test = hc.get("test")
    if test is None:
        return False
    if isinstance(test, (list, tuple)) and (not test or str(test[0]).upper() == "NONE"):
        return False
    return bool(_test_text(test).strip())


def _effective(base: dict, after: dict, name: str) -> dict:
    """A shallow view of service `name` with the override's top-level keys on top of the base's."""
    return {**_svc(base, name), **_svc(after, name)}


def _swap_path(test: Any, old: str, new: str) -> Any:
    """Replace the endpoint path in a healthcheck test, keeping its form (string or list). None if absent."""
    pat = re.compile(re.escape(old) + r"(?![\w/-])")
    if isinstance(test, (list, tuple)):
        if not any(pat.search(str(t)) for t in test):
            return None
        return [pat.sub(new, str(t)) for t in test]
    if test is None or not pat.search(str(test)):
        return None
    return pat.sub(new, str(test))


def _depends_mapping(raw: Any) -> dict:
    """depends_on in mapping form; list entries become {condition: service_started} (compose's default)."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): dict(v) if isinstance(v, dict) else {"condition": "service_started"} for k, v in raw.items()}
    return {str(x): {"condition": "service_started"} for x in raw}


def _container(base: dict, topology: Topology, name: str) -> str:
    explicit = _svc(base, name).get("container_name")
    if explicit:
        return str(explicit)
    node = topology.services.get(name)
    if node is not None and node.container:
        return node.container
    return f"{topology.project}-{name}-1"   # compose v2's default container name


def _datastores_via_env(topology: Topology, name: str) -> list[str]:
    """Datastores `name` reaches through an environment variable (e.g. MONGO_DB_HOSTNAME=mongodb)."""
    out = []
    for e in topology.edges:
        if e.src == name and e.kind == "env":
            node = topology.services.get(e.dst)
            if node is not None and node.role == "datastore" and e.dst not in out:
                out.append(e.dst)
    return sorted(out)


def _env_dependents(topology: Topology, datastore: str) -> list[str]:
    return sorted({e.src for e in topology.edges if e.dst == datastore and e.kind == "env"})


def _plural(items: list[str]) -> str:
    return ", ".join(items)


# ---------------------------------------------------------------- target selection
def _names_in(text: str, names: Iterable[str]) -> list[str]:
    """Services named in `text`, hyphen-aware (`things` must not match inside `things-search`)."""
    return [n for n in sorted(names, key=lambda n: (-len(n), n))
            if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)]


def _target(report: ExperimentReport) -> str:
    t = report.target or {}
    return t.get("service") or t.get("name") or ""


def _candidates(report: ExperimentReport, topology: Topology, finding: Optional[Finding]) -> list[str]:
    """Target + observed collateral, or, for per-service findings, the services the finding names."""
    target = _target(report)
    default = list(dict.fromkeys([s for s in [target, *(report.affected_services_observed or [])] if s]))
    if finding is None or finding.code not in SERVICE_NAMED_CODES:
        return default
    names = list(topology.services)
    title = finding.title or ""
    if finding.code == "dependency_blind_readiness":
        # "Dependents stayed 'ready' while <target> was down: things (100%), gateway (100%)"
        tail = title.split(": ", 1)[1] if ": " in title else ""
        named = _names_in(tail, names) if tail else [n for n in _names_in(title, names) if n != target]
    else:
        # "<service> kept reporting /ready=200 while the API was failing"
        named = _names_in(title, names)
    return sorted(named) if named else default


def _t_candidates(fix: CatalogFix, report, topology, finding, base) -> list[str]:
    return _candidates(report, topology, finding)


def _t_datastores(fix: CatalogFix, report, topology, finding, base) -> list[str]:
    out: list[str] = []
    for s in _candidates(report, topology, finding):
        node = topology.services.get(s)
        found = [s] if node is not None and node.role == "datastore" else _datastores_via_env(topology, s)
        out.extend(d for d in found if d not in out)
    return out


def _t_env_dependents(fix: CatalogFix, report, topology, finding, base) -> list[str]:
    out: list[str] = []
    for s in _candidates(report, topology, finding):
        node = topology.services.get(s)
        found = _env_dependents(topology, s) if node is not None and node.role == "datastore" else [s]
        out.extend(d for d in found if d not in out and _datastores_via_env(topology, d))
    return out


# ---------------------------------------------------------------- builders
def _b_healthcheck(base: dict, override: dict, services: list[str], topology: Topology):
    after = copy.deepcopy(override)
    changed, notes = [], []
    for s in services:
        base_hc = _svc(base, s).get("healthcheck")
        if not _has_healthcheck(_svc(base, s)):
            notes.append(f"{s}: no healthcheck in the base config; not inventing one")
            continue
        cur = (_svc(after, s).get("healthcheck") or {}).get("test")
        if cur is not None and "/ready" in _test_text(cur):
            notes.append(f"{s}: the override already checks /ready")
            continue
        if "/ready" in _test_text(base_hc.get("test")):
            notes.append(f"{s}: the base healthcheck already checks /ready")
            continue
        new_test = _swap_path(base_hc.get("test"), "/alive", "/ready")
        if new_test is None:
            notes.append(f"{s}: the base healthcheck does not call /alive; nothing this entry knows how to tighten")
            continue
        hc = {"test": new_test}
        for k in HEALTHCHECK_TIMING_KEYS:
            if k in base_hc:
                hc[k] = base_hc[k]
        _ov_svc(after, s)["healthcheck"] = hc
        changed.append(s)
    if not changed:
        return None, "; ".join(notes) or "nothing to change"
    return Change(
        fix_id="add_or_tighten_healthcheck", services=changed,
        override_after=ov.with_applied(after, "add_or_tighten_healthcheck"),
        summary=(f"Point the Docker healthcheck of {_plural(changed)} at the readiness endpoint (/ready) on the same "
                 f"port instead of the liveness endpoint (/alive), keeping the base interval, timeout, retries and "
                 f"start period."),
        expected=[("finding:health_check_masked_failure", "absent, or naming fewer services than before"),
                  ("finding:dependency_blind_readiness", "absent, or naming fewer services than before"),
                  ("during_success_rate", "no worse"), ("time_to_recover_s", "no worse")],
        risks=("On Ditto 3.9.8 /ready answers 200 'OK' on port 7626 (checked read-only on the live stack). It is "
               "stricter than /alive: it reports whether the node is a ready member of the Pekko cluster. It is NOT "
               "known to reflect MongoDB/persistence health: Ditto issue #1865 says neither /alive nor /ready "
               "considered subsystem health, and the fix (PR #1867) went into the liveness checks only, so the "
               "masked-failure finding may survive this change; the re-run decides. A stricter check can mark a "
               "service 'unhealthy' during a cluster rejoin. Docker never restarts an unhealthy container (restart "
               "policies only act on exit), so the gain is visibility (docker ps, depends_on: service_healthy), not "
               "automatic recovery. Applying recreates the listed containers; each Ditto service needs 60-120 s to "
               "start and rejoin the cluster."),
        rollback=(f"Restore the previous override file byte-for-byte from its backup and recreate {_plural(changed)}; "
                  f"their healthcheck returns to the base /alive check."),
        notes=notes,
    ), ""


def _b_dependency_healthcheck(base: dict, override: dict, services: list[str], topology: Topology):
    after = copy.deepcopy(override)
    changed, notes = [], []
    for s in services:
        if _has_healthcheck(_effective(base, after, s)):
            notes.append(f"{s}: already has a healthcheck")
            continue
        image = str(_svc(base, s).get("image") or (topology.services[s].image if s in topology.services else ""))
        if "mongo" not in image.lower():
            notes.append(f"{s}: no verified healthcheck for image '{image}'; only MongoDB's is in the catalog")
            continue
        _ov_svc(after, s)["healthcheck"] = dict(MONGO_HEALTHCHECK)
        changed.append(s)
    if not changed:
        return None, "; ".join(notes) or "nothing to change"
    return Change(
        fix_id="add_dependency_healthcheck", services=changed,
        override_after=ov.with_applied(after, "add_dependency_healthcheck"),
        summary=(f"Give {_plural(changed)} the same mongosh ping healthcheck Faultline's own compose file uses, so "
                 f"its health is visible and dependents can wait for it (prerequisite for add_healthy_dependency)."),
        expected=[("docker_health:" + changed[0], "healthy at steady state"),
                  ("during_success_rate", "no worse"), ("time_to_recover_s", "no worse")],
        risks=("Touches the protected sole datastore: applying recreates the MongoDB container, a full database "
               "outage of a few seconds (data stays in the named volume) during which every Ditto service loses its "
               "connection. The check itself changes no runtime behaviour: Docker does not restart an unhealthy "
               "container. mongosh ships in the official mongo >= 6 images (verified on the live 7.0 container)."),
        rollback=(f"Restore the previous override file byte-for-byte from its backup and recreate {_plural(changed)} "
                  f"without the healthcheck (another short database outage)."),
        notes=notes,
    ), ""


def _b_healthy_dependency(base: dict, override: dict, services: list[str], topology: Topology):
    after = copy.deepcopy(override)
    changed, notes = [], []
    for s in services:
        stores = [d for d in _datastores_via_env(topology, s) if d in _services(base)]
        if not stores:
            notes.append(f"{s}: reaches no datastore through its environment")
            continue
        merged = _depends_mapping(_svc(base, s).get("depends_on"))
        for k, v in _depends_mapping(_svc(after, s).get("depends_on")).items():
            merged[k] = {**merged.get(k, {}), **v}
        added = []
        for d in stores:
            if not _has_healthcheck(_effective(base, after, d)):
                # compose refuses `condition: service_healthy` on a service without a healthcheck
                notes.append(f"{s}: {d} has no healthcheck, so service_healthy cannot be used yet "
                             f"(add_dependency_healthcheck must be applied first)")
                continue
            if (merged.get(d) or {}).get("condition") == "service_healthy":
                notes.append(f"{s}: already waits for {d} to be healthy")
                continue
            merged[d] = {**(merged.get(d) or {}), "condition": "service_healthy"}
            added.append(d)
        if added:
            _ov_svc(after, s)["depends_on"] = merged
            changed.append(s)
    if not changed:
        return None, "; ".join(notes) or "nothing to change"
    stores = sorted({d for s in changed for d in _datastores_via_env(topology, s)})
    return Change(
        fix_id="add_healthy_dependency", services=changed,
        override_after=ov.with_applied(after, "add_healthy_dependency"),
        summary=(f"Declare the hidden environment-variable dependency of {_plural(changed)} on {_plural(stores)} as "
                 f"depends_on with condition service_healthy, keeping their existing dependencies."),
        expected=[("finding:cascade_beyond_hypothesis", "no worse"),
                  ("finding:dependency_blind_readiness", "no worse"),
                  ("time_to_recover_s", "no worse")],
        risks=("depends_on conditions only take effect when compose (re)creates or starts the service, never at "
               "runtime: if the datastore fails later, these services are not stopped, restarted or re-gated, so a "
               "re-run of a runtime fault will most likely look the same. What it changes is start-up ordering on "
               "the next `up`: these services wait for the datastore's healthcheck instead of starting into "
               "connection errors, and `up` blocks or fails if the datastore never turns healthy. Faultline recreates "
               "with --no-deps, so it does not rely on the condition being evaluated during that recreate. Applying "
               "recreates the listed Ditto containers (60-120 s each to rejoin the cluster)."),
        rollback=(f"Restore the previous override file byte-for-byte from its backup and recreate {_plural(changed)}; "
                  f"depends_on returns to the base declaration."),
        notes=notes,
    ), ""


def _b_restart_policy(base: dict, override: dict, services: list[str], topology: Topology):
    # On Ditto every service already has `restart: always`, so this returns None there. That is right, not a
    # gap: restart policies only react to a process EXIT, never to `unhealthy`, so no restart policy could
    # have fixed a hung-but-running service (the `no_automated_remediation` finding says exactly that).
    after = copy.deepcopy(override)
    changed, notes = [], []
    for s in services:
        if "restart" in _svc(after, s):
            notes.append(f"{s}: the override already sets restart: {_svc(after, s)['restart']}")
            continue
        current = _svc(base, s).get("restart")
        if current not in (None, "", "no"):
            notes.append(f"{s}: already has restart: {current} (restart policies only act on exit, never on unhealthy)")
            continue
        _ov_svc(after, s)["restart"] = "unless-stopped"
        changed.append(s)
    if not changed:
        return None, "; ".join(notes) or "nothing to change"
    return Change(
        fix_id="set_restart_policy", services=changed,
        override_after=ov.with_applied(after, "set_restart_policy"),
        summary=f"Set restart: unless-stopped on {_plural(changed)}, which had no restart policy.",
        expected=[("finding:manual_restart_required", "absent when the fault is a process exit"),
                  ("finding:recovery_failed", "absent when the fault is a process exit"),
                  ("time_to_recover_s", "no worse")],
        risks=("Docker restarts a container only when its process exits; a running but hung or unhealthy container "
               "is never restarted, so this does not help faults that leave the process up. A crash-looping service "
               "is restarted indefinitely. Applying recreates the listed containers."),
        rollback=(f"Restore the previous override file byte-for-byte from its backup and recreate {_plural(changed)} "
                  f"with no restart policy."),
        notes=notes,
    ), ""


def _desired_cpus(svc: dict) -> tuple[float, str]:
    """CPU limit matched to what the JVM was told it has (-XX:ActiveProcessorCount), else CPU_DEFAULT."""
    env = parse_env(svc.get("environment"))
    m = ACTIVE_PROCESSORS_RE.search(env.get("JAVA_TOOL_OPTIONS", ""))
    if m:
        return float(m.group(1)), f"-XX:ActiveProcessorCount={m.group(1)}"
    return CPU_DEFAULT, "default"


def _limits(svc: dict) -> dict:
    return ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}


def _b_resource_limits(base: dict, override: dict, services: list[str], topology: Topology):
    after = copy.deepcopy(override)
    changed, notes, clamps = [], [], []
    for s in services:
        base_lim, ov_lim = _limits(_svc(base, s)), _limits(_svc(after, s))
        mem_fix = None
        base_mem, ov_mem = parse_mem_limit(base_lim.get("memory")), parse_mem_limit(ov_lim.get("memory"))
        if ov_mem is not None and base_mem is not None and ov_mem < base_mem:
            mem_fix = base_lim.get("memory")   # never leave a limit below the base value
        if ov_lim.get("cpus") is not None or base_lim.get("cpus") is not None:
            if mem_fix is None:
                notes.append(f"{s}: already has a CPU limit ({ov_lim.get('cpus', base_lim.get('cpus'))}); left as is")
                continue
            cpus = None
        else:
            want, source = _desired_cpus(_svc(base, s))
            cpus = min(CPU_MAX, max(CPU_MIN, want))
            if cpus != want:
                clamps.append(f"{s} {want:g} -> {cpus:g} ({source}, bounds {CPU_MIN:g}..{CPU_MAX:g})")
        limits = (_ov_svc(after, s).setdefault("deploy", {}).setdefault("resources", {}).setdefault("limits", {}))
        if cpus is not None:
            limits["cpus"] = cpus
        if mem_fix is not None:
            limits["memory"] = mem_fix
        changed.append(s)
    if not changed:
        return None, "; ".join(notes) or "nothing to change"
    cpu_txt = ", ".join(f"{s} {_limits(_svc(after, s)).get('cpus', 'unchanged')}" for s in changed)
    summary = (f"Add a CPU limit ({cpu_txt}) matched to the processor count the JVM is configured for, leaving the "
               f"memory limits at their base values")
    summary += f"; clamped: {'; '.join(clamps)}." if clamps else "."
    return Change(
        fix_id="set_resource_limits", services=changed,
        override_after=ov.with_applied(after, "set_resource_limits"),
        summary=summary,
        expected=[("time_to_recover_s", "no worse; lower only if CPU contention from a neighbour slowed recovery"),
                  ("during_success_rate", "no worse")],
        risks=("A CPU quota isolates: a runaway or flooded service can no longer starve its neighbours. It can also "
               "slow the limited service itself: JVM start-up and Pekko cluster rejoin get slower under a quota, so "
               "time_to_recover_s may rise rather than fall; the re-run decides. Memory limits are never lowered. "
               "Applying recreates the listed containers."),
        rollback=(f"Restore the previous override file byte-for-byte from its backup and recreate {_plural(changed)} "
                  f"without the CPU limit."),
        notes=notes,
    ), ""


def _b_restart_now(base: dict, override: dict, services: list[str], topology: Topology):
    containers = [_container(base, topology, s) for s in services]
    return Change(
        fix_id="restart_service_now", services=list(services), override_after=copy.deepcopy(override),
        action="docker restart " + " ".join(containers), runtime_only=True,
        summary=f"Restart {_plural(list(services))} now, with their existing configuration.",
        expected=[("after_success_rate", "back to baseline once the restart completes")],
        risks=("A restart is a short outage of each listed service (a Ditto service needs 60-120 s to rejoin the "
               "cluster). It treats the symptom, not the cause: no configuration changes, so re-running the same "
               "experiment will most likely show the same weakness."),
        rollback="nothing to roll back; the container restarts with its existing configuration",
    ), ""


def _b_break_healthcheck(base: dict, override: dict, services: list[str], topology: Topology):
    after = copy.deepcopy(override)
    changed, notes = [], []
    for s in services:
        if (_svc(after, s).get("healthcheck") or {}) == BROKEN_HEALTHCHECK:
            notes.append(f"{s}: already broken by this test entry")
            continue
        _ov_svc(after, s)["healthcheck"] = copy.deepcopy(BROKEN_HEALTHCHECK)
        changed.append(s)
    if not changed:
        return None, "; ".join(notes) or "nothing to change"
    return Change(
        fix_id="break_healthcheck_for_test", services=changed,
        override_after=ov.with_applied(after, "break_healthcheck_for_test"),
        summary=(f"TEST ONLY: replace the healthcheck of {_plural(changed)} with one that always fails (exit 1, every "
                 f"5 s, 1 retry), to prove the unhealthy-after-apply rollback path."),
        expected=[("docker_health:" + changed[0], "unhealthy within ~10 s; this change is meant to be rolled back")],
        risks=("Deliberately marks the services unhealthy. Docker does not restart unhealthy containers, so traffic is "
               "unaffected, but anything gated on service_healthy will refuse to start. Never offered for real "
               "findings."),
        rollback=(f"Restore the previous override file byte-for-byte from its backup and recreate {_plural(changed)}."),
        notes=notes,
    ), ""


# ---------------------------------------------------------------- the catalog (priority order)
_ENTRIES: tuple[CatalogFix, ...] = (
    CatalogFix(id="add_or_tighten_healthcheck", title="Check readiness (/ready) instead of liveness (/alive)",
               addresses=("health_check_masked_failure", "dependency_blind_readiness"), roles=("app",),
               runtime_only=False, _targets=_t_candidates, _build=_b_healthcheck),
    CatalogFix(id="add_dependency_healthcheck", title="Give the datastore a healthcheck (mongosh ping)",
               addresses=("cascade_beyond_hypothesis", "dependency_blind_readiness"), roles=("datastore",),
               runtime_only=False, _targets=_t_datastores, _build=_b_dependency_healthcheck),
    CatalogFix(id="add_healthy_dependency", title="Wait for a healthy datastore before starting (depends_on)",
               addresses=("cascade_beyond_hypothesis", "dependency_blind_readiness"), roles=("app",),
               runtime_only=False, _targets=_t_env_dependents, _build=_b_healthy_dependency),
    CatalogFix(id="set_restart_policy", title="Add a restart policy where there is none",
               addresses=("manual_restart_required", "no_automated_remediation"),
               roles=("app", "datastore", "proxy"), runtime_only=False,
               _targets=_t_candidates, _build=_b_restart_policy),
    CatalogFix(id="restart_service_now", title="Restart the service now",
               addresses=("manual_restart_required", "recovery_failed", "relapse_after_recovery"),
               roles=("app", "datastore", "proxy"), runtime_only=True,
               _targets=_t_candidates, _build=_b_restart_now),
    CatalogFix(id="set_resource_limits", title="Add a CPU limit sized to the service",
               addresses=("slow_recovery", "relapse_after_recovery"), roles=("app", "datastore"),
               runtime_only=False, _targets=_t_candidates, _build=_b_resource_limits),
)
_TEST_ENTRIES: tuple[CatalogFix, ...] = (
    CatalogFix(id="break_healthcheck_for_test", title="TEST ONLY: a healthcheck that always fails",
               addresses=("health_check_masked_failure",), roles=("app",), runtime_only=False, test_only=True,
               _targets=_t_candidates, _build=_b_break_healthcheck),
)

CATALOG: dict[str, CatalogFix] = {f.id: f for f in _ENTRIES}
TEST_CATALOG: dict[str, CatalogFix] = {f.id: f for f in _TEST_ENTRIES}
FIX_ORDER: tuple[str, ...] = tuple(f.id for f in _ENTRIES + _TEST_ENTRIES)


def _test_enabled(include_test: bool) -> bool:
    return include_test or os.environ.get(TEST_CATALOG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def catalog_entries(include_test: bool = False) -> list[CatalogFix]:
    """Every applicable entry in priority order. Test-only entries need include_test or FAULTLINE_TEST_CATALOG=1."""
    return list(_ENTRIES) + (list(_TEST_ENTRIES) if _test_enabled(include_test) else [])


def get(fix_id: str, include_test: bool = False) -> Optional[CatalogFix]:
    return {f.id: f for f in catalog_entries(include_test)}.get(fix_id)


def for_finding(code: str, include_test: bool = False) -> list[CatalogFix]:
    """Catalog entries that address this finding code, in the fixed priority order."""
    return [f for f in catalog_entries(include_test) if code in f.addresses]
