"""Datastore engines from the project's own code, for services whose compose image does not say.

The image name is the strongest signal and topology.compose already reads it. What onboarding can add,
because it has the repository on disk, is what a reviewer would check next:

  1. the service's own Dockerfile: `build: ./db` with `FROM postgres:16` is a postgres, whatever the
     service is called (and a datastore, even if its name made the role guesser say "app");
  2. the client drivers its dependents declare: if every service that reaches `store` through its
     environment ships a postgres driver and nothing else unaccounted for, `store` speaks postgres.

Code decides, from files it can quote; no model is asked. A signal that is ambiguous (two candidate
engines left) is reported as a note and the engine stays unknown -- generic faults only, never a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from break_point.schemas import DatastoreFact, Edge, ServiceNode
from break_point.topology import datastores

FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.I | re.M)
MANIFESTS = ("pom.xml", "build.gradle", "build.gradle.kts", "package.json", "requirements.txt", "requirements.in",
             "pyproject.toml", "Pipfile", "setup.py", "setup.cfg", "go.mod", "Gemfile", "Cargo.toml", "composer.json")
MAX_MANIFEST_BYTES = 256_000
DEPENDENCY_KINDS = ("env", "depends_on")


@dataclass
class Evidence:
    datastores: dict[str, DatastoreFact] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)      # services the code proves are datastores
    notes: list[str] = field(default_factory=list)


def build_paths(svc: dict[str, Any], compose_dir: Path) -> tuple[Path | None, Path | None]:
    """(build context, Dockerfile) of a compose service, or (None, None) when it uses a prebuilt image."""
    build = svc.get("build")
    if isinstance(build, str):
        ctx, dockerfile = build, "Dockerfile"
    elif isinstance(build, dict):
        ctx, dockerfile = str(build.get("context") or "."), str(build.get("dockerfile") or "Dockerfile")
    else:
        return None, None
    root = (compose_dir / ctx).resolve()
    return root, (root / dockerfile)


def dockerfile_base(path: Path | None) -> str:
    """The final stage's base image: in a multi-stage build that is what actually runs."""
    if path is None or not path.is_file():
        return ""
    found = FROM_RE.findall(path.read_text(errors="replace"))
    return found[-1] if found else ""


def manifest_engines(context: Path | None) -> dict[str, str]:
    """{engine: manifest file} for the drivers declared in a build context (itself and one level down)."""
    if context is None or not context.is_dir():
        return {}
    out: dict[str, str] = {}
    candidates = [context / m for m in MANIFESTS]
    candidates += [d / m for d in sorted(context.iterdir()) if d.is_dir() and not d.name.startswith(".") for m in MANIFESTS]
    for path in candidates:
        if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            continue
        for key in datastores.from_manifest(path.read_text(errors="replace")):
            out.setdefault(key, str(path.relative_to(context.parent)) if path.is_relative_to(context.parent) else path.name)
    return out


def detect(nodes: dict[str, ServiceNode], edges: list[Edge], compose: dict, compose_dir: Path) -> Evidence:
    ev = Evidence()
    services = compose.get("services") or {}
    contexts = {n: build_paths(services.get(n) or {}, compose_dir) for n in nodes}

    # 1. the service's own Dockerfile
    for name, node in sorted(nodes.items()):
        if node.engine is not None:
            continue
        base = dockerfile_base(contexts[name][1])
        engine = datastores.from_image(base) if base else None
        if engine is not None:
            ev.datastores[name] = DatastoreFact(engine=engine.key, evidence=f"Dockerfile FROM {base}")
            if node.role != "datastore":
                ev.roles[name] = "datastore"

    # 2. the drivers every dependent declares, minus engines its other datastores already account for
    def engine_of(name: str) -> str | None:
        fact = ev.datastores.get(name)
        return fact.engine if fact else nodes[name].engine

    is_store = {n for n, node in nodes.items() if node.role == "datastore" or n in ev.roles}
    reaches: dict[str, set[str]] = {}
    for e in edges:
        if e.kind in DEPENDENCY_KINDS and e.dst in is_store and e.src in nodes:
            reaches.setdefault(e.src, set()).add(e.dst)
    for store in sorted(n for n in is_store if engine_of(n) is None):
        dependents = sorted(src for src, dsts in reaches.items() if store in dsts)
        candidates: dict[str, str] = {}
        for dep in dependents:
            explained = {engine_of(other) for other in reaches[dep] if other != store} - {None}
            for key, where in manifest_engines(contexts[dep][0]).items():
                if key not in explained:
                    candidates.setdefault(key, f"{dep}: {where}")
        if len(candidates) == 1:
            key, where = next(iter(candidates.items()))
            ev.datastores[store] = DatastoreFact(engine=key, evidence=f"client driver declared by {where}")
        elif candidates:
            ev.notes.append(f"{store}: its dependents declare drivers for {', '.join(sorted(candidates))}; "
                            f"engine left unidentified (generic faults only)")
    return ev
