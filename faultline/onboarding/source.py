"""Resolve "a repo" to files on disk, then find the few files onboarding actually reads.

Accepts a local path, a GitHub URL, or `owner/name`. Remote repos are shallow-cloned (blobless)
into a cache directory so a second run is cheap and offline.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

GITHUB_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
SHORTHAND_RE = re.compile(r"^([\w.-]+)/([\w.-]+)$")
SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")

# Ordered: the first match wins. Deployment-specific files beat a root compose because projects
# like Ditto keep a toy compose at the root and the real one under deployment/.
COMPOSE_CANDIDATES = (
    "deployment/docker/docker-compose.yml", "deployment/docker/docker-compose.yaml",
    "deploy/docker-compose.yml", "deploy/docker-compose.yaml",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "docker/docker-compose.yml", "deployment/docker-compose.yml", "examples/docker-compose.yml",
)
README_CANDIDATES = ("README.md", "readme.md", "README.rst", "README.txt", "README", "docs/README.md")

# A human onboarding to an unfamiliar repo reads the architecture docs, not just the README.
# Score filenames so the overview page beats a per-service page beats a design-decision record.
DOC_SCORES = (
    (re.compile(r"architecture[-_]?overview", re.I), 100),
    (re.compile(r"(^|/)architecture\.(md|rst)$", re.I), 90),
    (re.compile(r"architecture[-_]services?[-_]", re.I), 70),
    (re.compile(r"architecture", re.I), 50),
    (re.compile(r"(^|/)(design|concepts?|overview)\.(md|rst)$", re.I), 45),
    (re.compile(r"how[-_]it[-_]works|getting[-_]started", re.I), 30),
)
DOC_PENALTY = re.compile(r"adr|decision[-_]record|changelog|release[-_]note|migration|contribut|upgrade", re.I)
# In a monorepo, `providers/x/docs/architecture.rst` documents one library, not the stack the
# compose file deploys. Demoted, not excluded: a repo whose only architecture doc lives under
# packages/ should still get it, just never ahead of a top-level one.
DOC_COMPONENT = re.compile(
    r"(^|/)(providers?|packages?|plugins?|extensions?|clients?|sdk|sdks|libs?|libraries"
    r"|modules?|contrib|integrations?|addons?|examples?|samples?)/", re.I)
DOC_EXT = (".md", ".rst", ".txt")
COMPOSE_GLOB = re.compile(r"(^|/)[\w.-]*compose(\.[\w-]+)?\.ya?ml$", re.I)
SKIP_DIRS = {".git", ".cache", ".tox", ".mypy_cache", "node_modules", "vendor", "target", "build", "dist",
             ".venv", "venv", "__pycache__", "test", "tests", "site-packages", "_site", "third_party"}
COMPOSE_MAX_DEPTH = 6       # path components, file included: deep enough for .examples/a/b/c/compose.yaml

# Compose files that exist for developers, CI or illustration rather than to deploy the product.
# They are *demoted*, never excluded -- some repositories ship nothing else, and a demoted file is
# still better than no file. The ranking below only falls back to them when nothing better exists.
COMPOSE_DEMOTE = re.compile(
    r"(^|/)\.(devcontainer|github|gitlab|circleci|codespaces|idea|vscode)(/|$)"
    r"|(^|[/._-])(test|tests|testing|e2e|integration|fixture|fixtures|benchmark|bench|perf"
    r"|ci|dev|devel|development|local|override|sample|samples|example|examples|template|templates"
    r"|demo|playground|scratch|workstation)([/._-]|$)",
    re.I)


class SourceError(RuntimeError):
    pass


@dataclass
class RepoSource:
    reference: str
    slug: str
    root: Path
    compose_path: Path
    readme_path: Path | None = None
    nginx_conf_path: Path | None = None
    doc_paths: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def slugify(text: str) -> str:
    return SLUG_CLEAN_RE.sub("-", text.strip().lower()).strip("-") or "repo"


def parse_reference(reference: str) -> tuple[str, str]:
    """-> (kind, value) where kind is 'local' or 'git'."""
    ref = reference.strip()
    if not ref:
        raise SourceError("empty repository reference")
    p = Path(ref).expanduser()
    if p.exists():
        return "local", str(p.resolve())
    m = GITHUB_URL_RE.match(ref)
    if m:
        return "git", f"https://github.com/{m.group(1)}/{m.group(2)}.git"
    if ref.startswith(("git@", "ssh://", "http://", "https://")):
        return "git", ref
    m = SHORTHAND_RE.match(ref)
    if m:
        return "git", f"https://github.com/{m.group(1)}/{m.group(2)}.git"
    raise SourceError(f"{reference!r} is neither an existing path nor a recognisable git repository")


def _slug_for(kind: str, value: str) -> str:
    name = Path(value).name
    if kind == "git":
        name = re.sub(r"\.git$", "", name)
    return slugify(name)


def clone(url: str, dest: Path, ref: str | None = None, log=print) -> None:
    """Shallow, blobless clone. Reuses an existing checkout instead of re-downloading."""
    if shutil.which("git") is None:
        raise SourceError("git is required to fetch a remote repository")
    if (dest / ".git").is_dir():
        log(f"reusing cached checkout at {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1", "--filter=blob:none", "--quiet"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(dest)]
    log(f"cloning {url} ...")
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        raise SourceError(f"git clone failed: {(done.stderr or done.stdout).strip()[:400]}")


def _first_existing(root: Path, names) -> Path | None:
    for rel in names:
        p = root / rel
        if p.is_file():
            return p
    return None


def compose_services(path: Path) -> dict | None:
    """The `services` mapping of a candidate compose file, or None if the file is not usable.

    "Not usable" means the file cannot be parsed, is not a YAML mapping, or declares `services`
    as something other than a mapping -- all of which make the downstream parsers raise. Catching
    it here turns three different tracebacks into one actionable error message. A mapping with no
    `services` key at all is accepted as an empty stack: unhelpful, but not malformed.
    """
    try:
        data = yaml.safe_load(path.read_text(errors="replace"))
    except (OSError, yaml.YAMLError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    services = data.get("services")
    if services is None:
        return {}
    return services if isinstance(services, dict) else None


def _walk_files(root: Path, max_depth: int):
    """Yield (path, relative-parts) under root, pruning SKIP_DIRS instead of walking into them."""
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str):
        rel_dir = Path(dirpath).relative_to(root)
        depth = 0 if rel_dir == Path(".") else len(rel_dir.parts)
        dirnames[:] = [] if depth >= max_depth - 1 else [d for d in dirnames if d not in SKIP_DIRS]
        if depth and any(part in SKIP_DIRS for part in rel_dir.parts):
            continue
        for name in filenames:
            yield Path(dirpath) / name, (() if depth == 0 else rel_dir.parts) + (name,)


def find_compose(root: Path, explicit: str | None = None) -> Path:
    """The compose file that most plausibly deploys the product, or a SourceError saying why not.

    The ordered candidate list wins when it matches. Otherwise every compose-looking file in the
    tree is ranked: a real deployment beats a devcontainer/test/example stack, a stack with more
    services beats a stub, and a shallow path beats a deep one. Files that are not parseable
    compose files are dropped before ranking, so a YAML fixture never wins by being shallow.
    """
    if explicit:
        p = (root / explicit) if not Path(explicit).is_absolute() else Path(explicit)
        if not p.is_file():
            raise SourceError(f"compose file {p} does not exist")
        if compose_services(p) is None:
            raise SourceError(f"compose file {p} is not a usable docker-compose file "
                              f"(unparseable YAML, or `services` is not a mapping)")
        return p
    for rel in COMPOSE_CANDIDATES:
        p = root / rel
        if p.is_file() and compose_services(p) is not None:
            return p

    ranked: list[tuple[tuple, Path]] = []
    skipped = 0
    for p, parts in _walk_files(root, COMPOSE_MAX_DEPTH):
        key = "/".join(parts)
        if not COMPOSE_GLOB.search(key):
            continue
        services = compose_services(p)
        if services is None:
            skipped += 1
            continue
        ranked.append(((bool(COMPOSE_DEMOTE.search(key)), -len(services), len(parts), len(p.name), key), p))
    if not ranked:
        detail = f"; skipped {skipped} unparseable candidate(s)" if skipped else ""
        raise SourceError(f"no docker-compose file found under {root}{detail}")
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def find_readme(root: Path, compose_path: Path) -> Path | None:
    """The repository root README if there is one; otherwise a README beside the compose file.

    The root README describes the product, a deployment-local one usually only how to start it,
    and the product description is what documents the inter-service relationships.
    """
    local = _first_existing(compose_path.parent, README_CANDIDATES)
    root_readme = _first_existing(root, README_CANDIDATES)
    if local and root_readme and local != root_readme:
        # the deployment-local README is usually about running it, the root one about the product
        return root_readme
    return root_readme or local


def find_docs(root: Path, limit: int = 6) -> list[Path]:
    """Architecture-ish documentation shipped in the repo, best first. Generic, not repo-specific."""
    scored: list[tuple[bool, int, int, Path]] = []
    for p, parts in _walk_files(root, 8):
        if p.suffix.lower() not in DOC_EXT:
            continue
        rel = Path(*parts)
        key = "/".join(parts)
        score = max((pts for rx, pts in DOC_SCORES if rx.search(key)), default=0)
        if score == 0 or DOC_PENALTY.search(key):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 200 or size > 120_000:
            continue
        scored.append((bool(DOC_COMPONENT.search(key)), -score, len(rel.parts), p))
    scored.sort()
    return [p for *_rank, p in scored[:limit]]


def find_nginx_conf(compose_dir: Path) -> Path | None:
    for name in ("nginx.conf", "nginx/nginx.conf", "conf/nginx.conf", "default.conf"):
        p = compose_dir / name
        if p.is_file():
            return p
    return None


def resolve(reference: str, cache_dir: Path, compose: str | None = None, ref: str | None = None,
            log=print) -> RepoSource:
    kind, value = parse_reference(reference)
    notes: list[str] = []
    if kind == "local":
        root = Path(value)
        slug = _slug_for(kind, value)
    else:
        slug = _slug_for(kind, value)
        root = cache_dir / slug
        clone(value, root, ref, log=log)
    if not root.is_dir():
        raise SourceError(f"{root} is not a directory")

    compose_path = find_compose(root, compose)
    rel_compose = compose_path.relative_to(root).as_posix() if compose_path.is_relative_to(root) else str(compose_path)
    if not compose and rel_compose not in COMPOSE_CANDIDATES:
        # It came from the ranked walk rather than a conventional location, so say so: picking the
        # wrong stack is the one mistake that invalidates everything downstream.
        where = "a developer/test/example path" if COMPOSE_DEMOTE.search(rel_compose) else "an unconventional path"
        notes.append(f"compose file auto-detected at {rel_compose} ({where}); "
                     f"pass --compose if this is not the stack you want to test")
    readme_path = find_readme(root, compose_path)
    nginx_path = find_nginx_conf(compose_path.parent)
    docs = find_docs(root)
    if readme_path is None:
        notes.append("no README found; the model has only the compose file to work from")
    if nginx_path is None:
        notes.append("no nginx config next to the compose file; proxy edges will not be parsed")
    return RepoSource(reference=reference, slug=slug, root=root, compose_path=compose_path,
                      readme_path=readme_path, nginx_conf_path=nginx_path, doc_paths=docs, notes=notes)
