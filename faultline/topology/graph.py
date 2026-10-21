"""Assemble the Topology: nodes + edges -> criticality ranking + findings."""
from __future__ import annotations

from collections import deque
from pathlib import Path

from faultline.schemas import Criticality, Edge, ServiceNode, Topology
from faultline.topology.compose import documented_edges, load_compose, load_knowledge, service_nodes, static_edges


# ---------------------------------------------------------------- criticality
def _reverse_adjacency(edges: list[Edge]) -> dict[str, set[str]]:
    """dst -> {src}: who depends on dst (all edge kinds)."""
    rev: dict[str, set[str]] = {}
    for e in edges:
        if e.src != e.dst:
            rev.setdefault(e.dst, set()).add(e.src)
    return rev


def _transitive(rev: dict[str, set[str]], name: str) -> list[str]:
    seen: set[str] = set()
    q = deque([name])
    while q:
        cur = q.popleft()
        for d in rev.get(cur, ()):
            if d != name and d not in seen:
                seen.add(d)
                q.append(d)
    return sorted(seen)


def criticality(nodes: dict[str, ServiceNode], edges: list[Edge]) -> list[Criticality]:
    rev = _reverse_adjacency(edges)
    datastores = [n for n in nodes.values() if n.role == "datastore" or n.stateful]
    out: list[Criticality] = []
    for name, node in nodes.items():
        dependents = _transitive(rev, name)
        fan_in = len(rev.get(name, ()))
        score = len(dependents) * 1.0
        reasons: list[str] = []
        if dependents:
            n = len(dependents)
            reasons.append(f"{n} service{'s' if n != 1 else ''} depend{'' if n != 1 else 's'} on it transitively")
        else:
            reasons.append("nothing depends on it")
        if node.stateful:
            score += 2.0
            reasons.append("holds state (sole datastore)" if node.role == "datastore" and len(datastores) == 1 else "holds state")
        if node.entrypoint:
            score += 1.0
            reasons.append("reachable from outside the sandbox")
        if node.role == "datastore":
            score += 1.0
        if node.health_port is None and node.role != "ui":
            score += 0.5
            reasons.append("no application health endpoint")
        reasons.append("single instance")
        out.append(Criticality(service=name, fan_in=fan_in, transitive_dependents=dependents, score=round(score, 2), reasons=reasons))
    out.sort(key=lambda c: (-c.score, c.service))
    return out


# ---------------------------------------------------------------- findings
def _structural_findings(nodes: dict[str, ServiceNode], edges: list[Edge]) -> list[str]:
    findings: list[str] = []
    for name in nodes:
        declared = {e.src for e in edges if e.dst == name and e.kind == "depends_on"}
        via_env = {e.src for e in edges if e.dst == name and e.kind == "env"}
        if not declared and via_env:
            n = len(via_env)
            findings.append(f"{name}: not referenced by any depends_on; {n} service{'s' if n != 1 else ''} reach it only via environment variables")
    for name, node in nodes.items():
        if node.role == "app" and node.health_port is None and node.chaos_relevant:
            findings.append(f"{name}: no healthcheck with an application port in compose")
        if node.role == "datastore" and node.chaos_relevant and node.engine is None:
            findings.append(f"{name}: datastore engine not identified from its image, the knowledge file or a "
                            f"connection URI; only generic faults apply (no engine ping, no write flood)")
    return findings


# ---------------------------------------------------------------- build
def build(
    compose_path: str,
    project: str,
    nginx_conf_path: str | None = None,
    knowledge_path: str | None = None,
    live: bool = True,
    audit=None,
) -> Topology:
    knowledge = load_knowledge(knowledge_path)
    compose = load_compose(compose_path)
    if audit is not None:  # ingestion validation + cleaning, recorded in reports/data_quality.json
        from faultline.topology.quality import check_references, clean_compose

        compose = clean_compose(compose, compose_path, audit)
    nodes = service_nodes(compose, knowledge)
    if audit is not None:
        check_references(compose, nodes, audit)

    nginx_text: str | None = None
    if nginx_conf_path and Path(nginx_conf_path).is_file():
        nginx_text = Path(nginx_conf_path).read_text()

    edges = static_edges(compose, nodes, nginx_text) + documented_edges(nodes, knowledge)
    # de-duplicate across the two sources on (src, dst, kind)
    seen: set[tuple[str, str, str]] = set()
    edges = [e for e in edges if not ((e.src, e.dst, e.kind) in seen or seen.add((e.src, e.dst, e.kind)))]
    if audit is not None:
        from faultline.topology.quality import check_edges

        edges = check_edges(edges, nodes, audit)

    findings: list[str] = []
    if nginx_conf_path and nginx_text is None:
        findings.append(f"nginx config {nginx_conf_path} not found; proxy edges skipped")
    if live:
        from faultline.topology.live import enrich  # imported lazily so docker is optional for static builds

        findings.extend(enrich(nodes, project))
    findings.extend(_structural_findings(nodes, edges))

    return Topology(project=project, services=nodes, edges=edges, criticality=criticality(nodes, edges), findings=findings)


# ---------------------------------------------------------------- operations
def start_order(topo: Topology) -> list[str]:
    """Datastores and app services, every service after the services it depends on, for a restart.

    Depth is the longest chain of dependencies below a service; a cycle (two services that each claim
    the other) is cut where it closes instead of recursing forever. Ties sort by name, so the order is
    stable from run to run."""
    names = [n for n, s in topo.services.items() if s.chaos_relevant and s.role in ("datastore", "app")]
    deps: dict[str, set[str]] = {n: set() for n in names}
    for e in topo.edges:
        if e.src in deps and e.dst in deps and e.src != e.dst:
            deps[e.src].add(e.dst)
    depth: dict[str, int] = {}

    def walk(n: str, path: frozenset[str]) -> int:
        if n in depth:
            return depth[n]
        below = [walk(d, path | {n}) + 1 for d in deps[n] if d not in path]
        depth[n] = max(below, default=0)
        return depth[n]

    for n in names:
        walk(n, frozenset())
    return sorted(names, key=lambda n: (topo.services[n].role != "datastore", depth[n], n))


# ---------------------------------------------------------------- prompt context
def system_summary(topo: Topology | None) -> str:
    """One line naming the stack's tiers, for LLM prompts: `nginx -> api -> auth/orders -> db (postgres)`.

    Derived from the topology so a prompt never carries a hardcoded description of one application. The
    entry tier is what the proxies forward to; datastores carry their engine when it is known."""
    if topo is None or not topo.services:
        return "a docker-compose deployment"
    live = {n: s for n, s in topo.services.items() if s.chaos_relevant}
    proxies = sorted(n for n, s in live.items() if s.role == "proxy")
    entry = sorted({e.dst for e in topo.edges if e.src in proxies and live.get(e.dst) and live[e.dst].role == "app"})
    apps = sorted(n for n, s in live.items() if s.role == "app" and n not in entry)
    stores = sorted(f"{n} ({s.engine})" if s.engine else n for n, s in live.items() if s.role == "datastore")
    tiers = [t for t in ("/".join(proxies), "/".join(entry), "/".join(apps), "/".join(stores)) if t]
    return " -> ".join(tiers) or "a docker-compose deployment"


# ---------------------------------------------------------------- console
def summary_lines(topo: Topology) -> list[str]:
    rows = []
    for i, c in enumerate(topo.criticality, 1):
        node = topo.services[c.service]
        rows.append((
            str(i), c.service, node.role, f"{c.score:.1f}", str(c.fan_in), str(len(c.transitive_dependents)),
            "yes" if node.chaos_relevant else "no", node.container or "-", node.state or "-", node.health or "-",
        ))
    header = ("#", "service", "role", "score", "fan-in", "deps", "target", "container", "state", "health")
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    lines = [f"topology for project '{topo.project}': {len(topo.services)} services, {len(topo.edges)} edges",
             fmt.format(*header), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*r) for r in rows]
    return lines
