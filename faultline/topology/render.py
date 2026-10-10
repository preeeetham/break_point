"""Render a Topology as Mermaid and Markdown."""
from __future__ import annotations

import re

from faultline.schemas import Topology

DEFAULT_CLUSTER_ALIAS = "ditto-cluster"


def _mid(name: str) -> str:
    return "svc_" + re.sub(r"[^A-Za-z0-9_]", "_", name)


def _shape(name: str, role: str) -> str:
    label = name.replace('"', "'")
    if role == "datastore":
        return f'{_mid(name)}[("{label}")]'
    if role == "proxy":
        return f'{_mid(name)}(["{label}"])'
    if role == "ui":
        return f'{_mid(name)}[/"{label}"/]'
    return f'{_mid(name)}["{label}"]'


def mermaid(topo: Topology, cluster_alias: str = DEFAULT_CLUSTER_ALIAS) -> str:
    lines = ["graph LR"]
    in_cluster = [n for n, s in topo.services.items() if cluster_alias in s.aliases]
    outside = [n for n in topo.services if n not in in_cluster]
    if in_cluster:
        lines.append(f'  subgraph cluster["{cluster_alias} (Pekko cluster)"]')
        lines += [f"    {_shape(n, topo.services[n].role)}" for n in in_cluster]
        lines.append("  end")
    lines += [f"  {_shape(n, topo.services[n].role)}" for n in outside]

    grouped: dict[tuple[str, str], list[str]] = {}
    for e in topo.edges:
        kinds = grouped.setdefault((e.src, e.dst), [])
        if e.kind not in kinds:
            kinds.append(e.kind)
    for (src, dst), kinds in grouped.items():
        arrow = "-.->" if kinds == ["depends_on"] else "-->"
        lines.append(f"  {_mid(src)} {arrow}|{'+'.join(kinds)}| {_mid(dst)}")

    for n, s in topo.services.items():
        if s.role == "datastore":
            lines.append(f"  style {_mid(n)} stroke-width:3px")
        if not s.chaos_relevant:
            lines.append(f"  style {_mid(n)} stroke-dasharray: 4 3,opacity:0.7")
    return "\n".join(lines)


def _cell(v) -> str:
    if v is None:
        return "-"
    return str(v).replace("|", "\\|").replace("\n", " ")


def markdown(topo: Topology, cluster_alias: str = DEFAULT_CLUSTER_ALIAS) -> str:
    out: list[str] = []
    out.append(f"# Faultline topology — project `{topo.project}`")
    out.append("")
    out.append(f"Generated {topo.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')} · {len(topo.services)} services · {len(topo.edges)} edges")
    out.append("")
    out.append("```mermaid")
    out.append(mermaid(topo, cluster_alias))
    out.append("```")
    out.append("")

    out.append("## Services")
    out.append("")
    out.append("| service | role | mem limit (MB) | health port | container | state | health | restart policy | chaos target |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for n, s in topo.services.items():
        out.append("| " + " | ".join(_cell(x) for x in (
            f"`{n}`", s.role, s.mem_limit_mb, s.health_port, s.container, s.state, s.health, s.restart_policy,
            "yes" if s.chaos_relevant else "no",
        )) + " |")
    out.append("")

    out.append("## Edges (src depends on dst)")
    out.append("")
    out.append("| src | dst | kind | evidence |")
    out.append("|---|---|---|---|")
    for e in sorted(topo.edges, key=lambda e: (e.src, e.dst, e.kind)):
        out.append(f"| `{e.src}` | `{e.dst}` | {e.kind} | {_cell(e.evidence)} |")
    out.append("")

    out.append("## Criticality ranking")
    out.append("")
    out.append("| rank | service | score | fan-in | transitive dependents | reasons |")
    out.append("|---|---|---|---|---|---|")
    for i, c in enumerate(topo.criticality, 1):
        deps = ", ".join(c.transitive_dependents) or "-"
        out.append(f"| {i} | `{c.service}` | {c.score:.1f} | {c.fan_in} | {_cell(deps)} | {_cell('; '.join(c.reasons))} |")
    out.append("")

    out.append("## Findings")
    out.append("")
    if topo.findings:
        out += [f"- {f}" for f in topo.findings]
    else:
        out.append("- none")
    out.append("")
    return "\n".join(out)
