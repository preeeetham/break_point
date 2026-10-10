"""Enrich compose-derived service nodes with what Docker actually reports for the running project."""
from __future__ import annotations

import re

import docker.errors

from faultline.chaos.docker_ctl import DockerController
from faultline.schemas import ServiceNode

HEX12_RE = re.compile(r"^[0-9a-f]{12}$")


def _mem_mb(hostcfg: dict) -> int | None:
    mem = hostcfg.get("Memory") or 0
    return int(round(mem / (1024 * 1024))) if mem > 0 else None


def enrich(nodes: dict[str, ServiceNode], project: str) -> list[str]:
    findings: list[str] = []
    try:
        containers = DockerController(project).containers()
    except docker.errors.DockerException as exc:
        return [f"live enrichment skipped: {exc}"]
    except Exception as exc:  # pragma: no cover - e.g. socket/permission errors raised as OSError
        return [f"live enrichment skipped: {type(exc).__name__}: {exc}"]

    for name, node in nodes.items():
        c = containers.get(name)
        if c is None:
            findings.append(f"service {name} declared in compose but has no container in project {project}")
            continue
        try:
            c.reload()
        except docker.errors.DockerException:
            pass
        attrs = c.attrs or {}
        state = attrs.get("State") or {}
        hostcfg = attrs.get("HostConfig") or {}
        node.container = c.name
        node.state = c.status or state.get("Status")
        node.health = (state.get("Health") or {}).get("Status")
        node.restart_policy = (hostcfg.get("RestartPolicy") or {}).get("Name") or None
        if node.mem_limit_mb is None:
            node.mem_limit_mb = _mem_mb(hostcfg)
        cid = c.id or ""
        for net in ((attrs.get("NetworkSettings") or {}).get("Networks") or {}).values():
            for alias in (net or {}).get("Aliases") or []:
                if not alias or HEX12_RE.match(alias) or cid.startswith(alias) or alias == c.name:
                    continue
                if alias != name and alias not in node.aliases:
                    node.aliases.append(alias)

    for svc, c in sorted(containers.items()):
        if svc not in nodes:
            findings.append(f"container {c.name} running but not declared in compose")
    return findings
