"""Static topology extraction from docker-compose + nginx.

The point of this module is to see dependencies that `depends_on` does not declare. In the Ditto
compose file nothing formally depends on mongodb, yet every Ditto service reaches it through
`MONGO_DB_HOSTNAME=mongodb`. So besides `depends_on` we scan environment variables for hostnames
that resolve to other services, parse nginx `proxy_pass` targets, and layer documented knowledge
about the Pekko cluster on top.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from faultline.schemas import Edge, ServiceNode

HOST_KEY_RE = re.compile(r"(_HOSTNAME|_HOST|_URI|_URL|_ADDR|_ADDRESS|_ENDPOINT|_SEED_NODES)$")
SECRET_KEY_RE = re.compile(r"PASSWORD|SECRET|TOKEN", re.IGNORECASE)
HEALTH_PORT_RE = re.compile(r":(\d+)/")
PROXY_PASS_RE = re.compile(r"proxy_pass\s+https?://([A-Za-z0-9_.-]+)")
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
HEX12_RE = re.compile(r"^[0-9a-f]{12}$")
EVIDENCE_VALUE_MAX = 60


# ---------------------------------------------------------------- loading / small parsers
def load_compose(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: compose file does not contain a mapping")
    return data


def load_knowledge(path: str | Path | None) -> dict:
    if not path or not Path(path).is_file():
        return {}
    return yaml.safe_load(Path(path).read_text()) or {}


def parse_env(raw: Any) -> dict[str, str]:
    """Normalise compose `environment` (list of "K=V" / "K" or a mapping) into {str: str}."""
    out: dict[str, str] = {}
    if not raw:
        return out
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = "" if v is None else str(v)
        return out
    for item in raw:
        if item is None:
            continue
        if isinstance(item, dict):  # odd but seen in the wild
            out.update(parse_env(item))
            continue
        s = str(item)
        k, sep, v = s.partition("=")
        out[k.strip()] = v.strip() if sep else ""
    return out


def parse_mem_limit(raw: Any) -> int | None:
    """'256m' / '1g' / '512M' / '1.5gb' / raw bytes -> MB (rounded)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(round(raw / (1024 * 1024))) if raw > 0 else None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgGtT]?)(?:i?[bB])?\s*$", str(raw))
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).lower()
    factor = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[unit]
    mb = num * factor / (1024**2)
    return int(round(mb)) if mb > 0 else None


def parse_health_port(healthcheck: Any) -> int | None:
    if not isinstance(healthcheck, dict):
        return None
    test = healthcheck.get("test")
    if test is None or healthcheck.get("disable"):
        return None
    text = " ".join(str(t) for t in test) if isinstance(test, (list, tuple)) else str(test)
    m = HEALTH_PORT_RE.search(text)
    return int(m.group(1)) if m else None


def parse_aliases(networks: Any) -> list[str]:
    """`networks` may be a list of names (no aliases) or a mapping {net: {aliases: [...]}}."""
    if not isinstance(networks, dict):
        return []
    out: list[str] = []
    for _net, cfg in networks.items():
        if isinstance(cfg, dict):
            for a in cfg.get("aliases") or []:
                if a and str(a) not in out:
                    out.append(str(a))
    return out


def _guess_role(name: str, image: str) -> str:
    hay = f"{name} {image}".lower()
    if re.search(r"mongo|postgres|redis|mysql|mariadb|cassandra", hay):
        return "datastore"
    if re.search(r"nginx|traefik|haproxy|envoy", image.lower()) or name.lower() == "nginx":
        return "proxy"
    if "swagger" in hay or name.lower().endswith("-ui") or name.lower().endswith("_ui"):
        return "ui"
    return "app"


def _mounts_named_volume(svc: dict, top_volumes: Iterable[str]) -> bool:
    top = set(top_volumes)
    for v in svc.get("volumes") or []:
        if isinstance(v, dict):
            if v.get("type") == "volume":
                return True
            src = str(v.get("source") or "")
        else:
            src = str(v).split(":", 1)[0]
        if not src:
            continue
        if src in top:
            return True
        if not (src.startswith(("/", ".", "~", "$")) or re.match(r"^[A-Za-z]:\\", src)):
            return True  # bare name that is not a path -> named volume
    return False


# ---------------------------------------------------------------- nodes
def service_nodes(compose: dict, knowledge: dict | None = None) -> dict[str, ServiceNode]:
    knowledge = knowledge or {}
    role_overrides = {str(k): str(v) for k, v in (knowledge.get("roles") or {}).items()}
    default_health_port = knowledge.get("health_port")
    top_volumes = list((compose.get("volumes") or {}).keys())
    nodes: dict[str, ServiceNode] = {}
    for name, svc in (compose.get("services") or {}).items():
        svc = svc or {}
        image = str(svc.get("image") or "")
        role = role_overrides.get(name) or _guess_role(name, image)
        if role not in ("datastore", "app", "proxy", "ui", "other"):
            role = "app"
        mem = parse_mem_limit((((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}).get("memory"))
        if mem is None:
            mem = parse_mem_limit(svc.get("mem_limit"))
        health_port = parse_health_port(svc.get("healthcheck"))
        if health_port is None and default_health_port and role == "app" and "ditto-" in image:
            health_port = int(default_health_port)
        aliases = parse_aliases(svc.get("networks"))
        nodes[name] = ServiceNode(
            name=name,
            image=image,
            role=role,  # type: ignore[arg-type]
            stateful=(role == "datastore") or _mounts_named_volume(svc, top_volumes),
            entrypoint=bool(svc.get("ports")),
            health_port=health_port,
            mem_limit_mb=mem,
            aliases=aliases,
            chaos_relevant=role not in ("ui", "other"),
        )
    return nodes


# ---------------------------------------------------------------- edges
def _alias_index(nodes: dict[str, ServiceNode]) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for n in nodes.values():
        idx.setdefault(n.name, []).append(n.name)
        for a in n.aliases:
            idx.setdefault(a, [])
            if n.name not in idx[a]:
                idx[a].append(n.name)
    return idx


def _extract_hosts(value: str) -> list[str]:
    """Pull hostname candidates out of an env value: bare hosts, host:port, URIs, comma lists."""
    hosts: list[str] = []
    for token in re.split(r"[,\s;]+", value.strip()):
        if not token:
            continue
        if URI_RE.match(token):
            token = URI_RE.sub("", token, count=1)
        token = token.split("/", 1)[0]  # drop path
        token = token.rsplit("@", 1)[-1]  # drop user:pass@
        token = token.split("?", 1)[0]
        if token.startswith("["):  # [ipv6]:port
            token = token[1:].split("]", 1)[0]
        else:
            token = token.split(":", 1)[0]  # drop port
        token = token.strip()
        if token and token not in hosts:
            hosts.append(token)
    return hosts


def _dedupe(edges: Iterable[Edge]) -> list[Edge]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Edge] = []
    for e in edges:
        if e.src == e.dst:
            continue
        key = (e.src, e.dst, e.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _depends_on_names(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys()]
    return [str(x) for x in raw]


def static_edges(compose: dict, nodes: dict[str, ServiceNode], nginx_conf_text: str | None = None) -> list[Edge]:
    edges: list[Edge] = []
    services = compose.get("services") or {}
    alias_idx = _alias_index(nodes)

    # 1. depends_on (list or dict form)
    for name, svc in services.items():
        for dep in _depends_on_names((svc or {}).get("depends_on")):
            if dep in nodes:
                edges.append(Edge(src=name, dst=dep, kind="depends_on", evidence="depends_on"))

    # 2. environment scan: hostnames that resolve to another service
    for name, svc in services.items():
        for key, value in parse_env((svc or {}).get("environment")).items():
            if SECRET_KEY_RE.search(key) or not value:
                continue
            key_hint = bool(HOST_KEY_RE.search(key))
            hosts = _extract_hosts(value)
            targets: list[str] = []
            if key_hint:
                for h in hosts:
                    targets.extend(alias_idx.get(h, []))
            else:
                # without a host-ish key be strict: the whole value (or URI host) must name a service
                if value in alias_idx:
                    targets.extend(alias_idx[value])
                elif URI_RE.match(value) and len(hosts) == 1:
                    targets.extend(alias_idx.get(hosts[0], []))
            shown = value if len(value) <= EVIDENCE_VALUE_MAX else value[: EVIDENCE_VALUE_MAX - 1] + "…"
            for dst in targets:
                if dst != name:
                    edges.append(Edge(src=name, dst=dst, kind="env", evidence=f"env {key}={shown}"))

    # 3. nginx proxy_pass
    if nginx_conf_text:
        proxies = [n.name for n in nodes.values() if "nginx" in n.image.lower()] or [
            n.name for n in nodes.values() if n.role == "proxy"
        ]
        for src in proxies:
            for host in PROXY_PASS_RE.findall(nginx_conf_text):
                for dst in alias_idx.get(host, []):
                    edges.append(Edge(src=src, dst=dst, kind="proxy", evidence=f"proxy_pass → {host}"))

    return _dedupe(edges)


def documented_edges(nodes: dict[str, ServiceNode], knowledge: dict | None) -> list[Edge]:
    edges: list[Edge] = []
    for item in (knowledge or {}).get("documented_edges") or []:
        src, dst = str(item.get("src", "")), str(item.get("dst", ""))
        if src in nodes and dst in nodes:
            edges.append(Edge(src=src, dst=dst, kind="documented", evidence=str(item.get("evidence") or "documented")))
    return _dedupe(edges)
