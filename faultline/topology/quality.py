"""Ingestion-side data validation and cleaning: the fifth data-quality guarantee.

Everything Faultline writes is schema-validated on the way OUT. This module validates what comes IN:
the raw docker-compose mapping before `compose.service_nodes()` turns it into ServiceNodes, and the
edges before they become the topology graph. Six deterministic checks, no LLM:

  null         missing / empty required fields -> documented default, or the record is excluded
  consistency  env hostnames and depends_on names must resolve to a declared service or alias;
               every edge must connect two services that exist
  conformity   raw shapes and types (service names, ports, environment, depends_on, healthcheck)
  range        ports 1..65535, memory limit > 0, healthcheck interval/timeout within sane bounds
  cleaning     whitespace trimmed, duplicate env keys / depends_on entries / edges removed,
               relative build/env_file paths resolved
  time         same_window(): a configurable tolerance for correlating timestamps across clocks

Every action is recorded as a DataQualityIssue; nothing is rewritten silently, and nothing is
invented: a check may drop or flag a dependency, it never adds one.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from faultline.schemas import DataQualityIssue, DataQualityReport, Edge, ServiceNode
from faultline.topology.compose import HOST_KEY_RE, SECRET_KEY_RE, _extract_hosts, parse_mem_limit

SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")  # compose service-name grammar
DURATION_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")
HEALTH_INTERVAL_DEFAULT_S = 30.0       # Docker's own default when a healthcheck omits `interval`
HEALTH_INTERVAL_MAX_S = 3600.0
EXTERNAL_HOST_RE = re.compile(r"^(localhost|\d{1,3}(\.\d{1,3}){3}|.*\..*)$")  # IPs, FQDNs: outside the stack


class Audit:
    def __init__(self) -> None:
        self.issues: list[DataQualityIssue] = []

    def add(self, check: str, action: str, record: str, detail: str, field: str = "") -> None:
        self.issues.append(DataQualityIssue(check=check, action=action, record=record, field=field, detail=detail))  # type: ignore[arg-type]


def parse_duration_s(raw: Any) -> float | None:
    """Compose duration ("30s", "1m30s", "500ms", 10) -> seconds; None when it does not conform."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    m = DURATION_RE.match(str(raw).strip()) if raw is not None else None
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def _port_numbers(entry: Any) -> list[str]:
    """The host/container port tokens of one compose `ports` entry (short or long syntax)."""
    if isinstance(entry, dict):
        return [str(entry[k]) for k in ("target", "published") if entry.get(k) is not None]
    s = str(entry).split("/", 1)[0]          # drop /tcp
    parts = s.split(":")
    return [p for p in parts[-2:] if p] if len(parts) > 1 else [s]


# ---------------------------------------------------------------- checks 1, 3, 4, 5 on the raw mapping
def clean_compose(compose: dict, compose_path: str | Path | None, audit: Audit) -> dict:
    """Validate and clean the raw compose mapping. Returns a cleaned deep copy; the input is untouched."""
    out = copy.deepcopy(compose)
    services = out.get("services")
    if services is None or not isinstance(services, dict):
        audit.add("conformity", "defaulted", "<compose>", f"`services` is {type(services).__name__}, expected a mapping; treated as empty", "services")
        out["services"] = {}
        return out
    base = Path(compose_path).resolve().parent if compose_path else None
    cleaned: dict[str, Any] = {}
    for raw_name, svc in services.items():
        name = str(raw_name)
        # 5 cleaning: stray whitespace in a name is cleaning; casing is left alone (compose names are case sensitive)
        if name != name.strip():
            audit.add("cleaning", "normalized", name.strip(), f"service name {raw_name!r} trimmed to {name.strip()!r}", "name")
            name = name.strip()
        # 3 conformity: a name compose itself would reject cannot be a real service
        if not name or not SERVICE_NAME_RE.match(name):
            audit.add("conformity", "excluded", name or "<empty>", "service name does not match compose's [a-zA-Z0-9][a-zA-Z0-9_.-]*", "name")
            continue
        if name in cleaned:
            audit.add("cleaning", "deduplicated", name, "service declared twice after trimming; first declaration kept", "name")
            continue
        # 1 null: an empty service block
        if svc is None:
            audit.add("null", "defaulted", name, "service block is empty; treated as {}", "service")
            svc = {}
        if not isinstance(svc, dict):
            audit.add("conformity", "excluded", name, f"service block is {type(svc).__name__}, expected a mapping", "service")
            continue
        if not svc.get("image") and not svc.get("build"):
            audit.add("null", "flagged", name, "no `image` and no `build`: compose cannot start this service; image recorded as empty", "image")
        # environment: conformity + dedupe of repeated keys (compose keeps the last one)
        env = svc.get("environment")
        if env is not None and not isinstance(env, (list, dict)):
            audit.add("conformity", "excluded", name, f"`environment` is {type(env).__name__}, expected list or mapping; ignored", "environment")
            svc.pop("environment")
        elif isinstance(env, list):
            seen: dict[str, int] = {}
            for i, item in enumerate(env):
                key = str(item).partition("=")[0].strip() if item is not None else ""
                if key in seen:
                    audit.add("cleaning", "deduplicated", name, f"env {key} declared more than once; the last value wins, as in compose", "environment")
                seen[key] = i
            svc["environment"] = [env[i] for i in sorted(seen.values()) if env[i] is not None]
        # depends_on: conformity + dedupe
        dep = svc.get("depends_on")
        if dep is not None and not isinstance(dep, (list, dict)):
            audit.add("conformity", "excluded", name, f"`depends_on` is {type(dep).__name__}, expected list or mapping; ignored", "depends_on")
            svc.pop("depends_on")
        elif isinstance(dep, list):
            uniq = list(dict.fromkeys(str(d).strip() for d in dep if d is not None))
            if len(uniq) != len(dep):
                audit.add("cleaning", "deduplicated", name, f"depends_on had {len(dep) - len(uniq)} repeated or empty entries", "depends_on")
            svc["depends_on"] = uniq
        # ports: conformity (numeric) + range (1..65535)
        good_ports = []
        for entry in svc.get("ports") or []:
            if "${" in str(entry):  # interpolated at deploy time; the value is not known here
                good_ports.append(entry)
                continue
            nums = _port_numbers(entry)
            bad = [p for p in nums if not re.fullmatch(r"\d+(-\d+)?", p)]
            if bad:
                audit.add("conformity", "flagged", name, f"port {entry!r} is not numeric", "ports")
            elif any(not 1 <= int(x) <= 65535 for p in nums for x in p.split("-")):
                audit.add("range", "excluded", name, f"port {entry!r} is outside 1..65535; dropped", "ports")
                continue
            good_ports.append(entry)
        if svc.get("ports"):
            svc["ports"] = good_ports
        # memory: range
        limits = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        for raw_mem, where in ((limits.get("memory"), "deploy.resources.limits.memory"), (svc.get("mem_limit"), "mem_limit")):
            if raw_mem is None:
                continue
            mb = parse_mem_limit(raw_mem)
            if mb is None or mb <= 0:
                audit.add("range", "flagged", name, f"memory limit {raw_mem!r} is not a positive size; treated as no limit", where)
        # healthcheck: null / conformity / range
        hc = svc.get("healthcheck")
        if hc is not None:
            if not isinstance(hc, dict):
                audit.add("conformity", "excluded", name, f"`healthcheck` is {type(hc).__name__}, expected a mapping; ignored", "healthcheck")
                svc.pop("healthcheck")
            elif not hc.get("disable"):
                if not hc.get("test"):
                    audit.add("null", "flagged", name, "healthcheck has no `test`; Docker would inherit the image's, if any", "healthcheck.test")
                for key in ("interval", "timeout", "start_period"):
                    if key not in hc:
                        if key == "interval":
                            audit.add("null", "defaulted", name, f"healthcheck interval missing; Docker default {HEALTH_INTERVAL_DEFAULT_S:g}s applies", "healthcheck.interval")
                        continue
                    secs = parse_duration_s(hc[key])
                    if secs is None:
                        audit.add("conformity", "flagged", name, f"healthcheck {key} {hc[key]!r} is not a compose duration", f"healthcheck.{key}")
                    elif key in ("interval", "timeout") and not 0 < secs <= HEALTH_INTERVAL_MAX_S:
                        audit.add("range", "flagged", name, f"healthcheck {key} {hc[key]!r} = {secs:g}s is outside (0, {HEALTH_INTERVAL_MAX_S:g}]s", f"healthcheck.{key}")
                retries = hc.get("retries")
                if retries is not None and (not isinstance(retries, int) or retries < 0):
                    audit.add("range", "flagged", name, f"healthcheck retries {retries!r} is not a non-negative integer", "healthcheck.retries")
        # cleaning: relative paths resolved (recorded only; topology does not consume them)
        if base is not None:
            build = svc.get("build")
            ctx = build.get("context") if isinstance(build, dict) else build
            for raw_path, field in ((ctx, "build"), *((p, "env_file") for p in ([svc["env_file"]] if isinstance(svc.get("env_file"), str) else svc.get("env_file") or []))):
                if isinstance(raw_path, str) and raw_path and not Path(raw_path).is_absolute() and "://" not in raw_path:
                    resolved = (base / raw_path).resolve()
                    note = "" if resolved.exists() else " (does not exist)"
                    audit.add("cleaning", "normalized", name, f"{field} path {raw_path!r} resolved to {resolved}{note}", field)
        cleaned[name] = svc
    out["services"] = cleaned
    return out


# ---------------------------------------------------------------- check 2: references and edges
def check_references(compose: dict, nodes: dict[str, ServiceNode], audit: Audit) -> None:
    known = set(nodes) | {a for n in nodes.values() for a in n.aliases}
    for name, svc in (compose.get("services") or {}).items():
        svc = svc or {}
        deps = svc.get("depends_on") or []
        for dep in (deps.keys() if isinstance(deps, dict) else deps):
            if str(dep) not in nodes:
                audit.add("consistency", "excluded", name, f"depends_on {dep!r} names no declared service; no edge created", "depends_on")
        env = svc.get("environment") or {}
        items = env.items() if isinstance(env, dict) else (str(i).partition("=")[::2] for i in env)
        for key, value in items:
            key, value = str(key), "" if value is None else str(value)
            if SECRET_KEY_RE.search(key) or not value or not HOST_KEY_RE.search(key):
                continue
            for host in _extract_hosts(value):
                if host in known or "$" in host:
                    continue
                if EXTERNAL_HOST_RE.match(host):
                    continue  # an IP, localhost or a dotted name: outside this compose project, not a defect
                audit.add("consistency", "flagged", name, f"env {key} points at host {host!r}, which is no service or alias in this compose file (dangling reference)", key)


def check_edges(edges: list[Edge], nodes: dict[str, ServiceNode], audit: Audit) -> list[Edge]:
    kept: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    for e in edges:
        rec = f"{e.src}->{e.dst} ({e.kind})"
        if e.src not in nodes or e.dst not in nodes:
            audit.add("consistency", "excluded", rec, "edge endpoint is not a declared service", "edge")
            continue
        if e.src == e.dst:
            audit.add("consistency", "excluded", rec, "self edge", "edge")
            continue
        key = (e.src, e.dst, e.kind)
        if key in seen:
            audit.add("cleaning", "deduplicated", rec, "duplicate edge", "edge")
            continue
        seen.add(key)
        kept.append(e)
    return kept


# ---------------------------------------------------------------- check 6: time lenience
def same_window(t_a: float | None, t_b: float | None, tolerance_s: float) -> bool | None:
    """True when two timestamps from different clocks/samplers are too close to order.

    Callers must report such a pair as unordered ("same interval"), never as A-before-B: the
    tolerance widens the honest "we cannot tell" band, it never manufactures precision.
    None when either timestamp is missing.
    """
    if t_a is None or t_b is None:
        return None
    return abs(float(t_a) - float(t_b)) <= max(0.0, float(tolerance_s))


def order(t_a: float | None, t_b: float | None, tolerance_s: float) -> str:
    """"before" / "after" only when the gap exceeds the tolerance; otherwise "same_window" (or "unknown")."""
    w = same_window(t_a, t_b, tolerance_s)
    if w is None:
        return "unknown"
    if w:
        return "same_window"
    return "before" if t_a < t_b else "after"  # type: ignore[operator]


def report(source: str, audit: Audit, time_lenience_s: float, services: int, edges: int) -> DataQualityReport:
    counts: dict[str, int] = {}
    for i in audit.issues:
        counts[i.check] = counts.get(i.check, 0) + 1
    return DataQualityReport(source=source, time_lenience_s=time_lenience_s, services_seen=services,
                             edges_kept=edges, counts=counts, issues=audit.issues)
