"""Resolve "a repo" to files on disk, then find the few files onboarding actually reads.

Accepts a local path, a GitHub URL, or `owner/name`. Remote repos are shallow-cloned (blobless)
into a cache directory so a second run is cheap and offline.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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
DOC_EXT = (".md", ".rst", ".txt")
COMPOSE_GLOB = re.compile(r"(^|/)(docker-)?compose(\.[\w-]+)?\.ya?ml$", re.I)
SKIP_DIRS = {".git", ".cache", ".tox", ".mypy_cache", "node_modules", "vendor", "target", "build", "dist",
             ".venv", "venv", "__pycache__", "test", "tests", "site-packages", "_site", "third_party"}


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


def find_compose(root: Path, explicit: str | None = None) -> Path:
    if explicit:
        p = (root / explicit) if not Path(explicit).is_absolute() else Path(explicit)
        if not p.is_file():
            raise SourceError(f"compose file {p} does not exist")
        return p
    found = _first_existing(root, COMPOSE_CANDIDATES)
    if found:
        return found
    # fall back to a bounded walk, preferring the shallowest match
    hits: list[Path] = []
    for p in root.rglob("*.y*ml"):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if len(rel.parts) > 4:
            continue
        if COMPOSE_GLOB.search(rel.as_posix()):
            hits.append(p)
    if not hits:
        raise SourceError(f"no docker-compose file found under {root}")
    hits.sort(key=lambda p: (len(p.relative_to(root).parts), len(p.name)))
    return hits[0]


def find_readme(root: Path, compose_path: Path) -> Path | None:
    """Prefer a README next to the compose file; otherwise the repository root README."""
    local = _first_existing(compose_path.parent, README_CANDIDATES)
    root_readme = _first_existing(root, README_CANDIDATES)
    if local and root_readme and local != root_readme:
        # the deployment-local README is usually about running it, the root one about the product
        return root_readme
    return root_readme or local


def find_docs(root: Path, limit: int = 6) -> list[Path]:
    """Architecture-ish documentation shipped in the repo, best first. Generic, not repo-specific."""
    scored: list[tuple[int, int, Path]] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in DOC_EXT:
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or len(rel.parts) > 8:
            continue
        key = rel.as_posix()
        score = max((pts for rx, pts in DOC_SCORES if rx.search(key)), default=0)
        if score == 0 or DOC_PENALTY.search(key):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 200 or size > 120_000:
            continue
        scored.append((-score, len(rel.parts), p))
    scored.sort()
    return [p for _s, _d, p in scored[:limit]]


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
    readme_path = find_readme(root, compose_path)
    nginx_path = find_nginx_conf(compose_path.parent)
    docs = find_docs(root)
    if readme_path is None:
        notes.append("no README found; the model has only the compose file to work from")
    if nginx_path is None:
        notes.append("no nginx config next to the compose file; proxy edges will not be parsed")
    return RepoSource(reference=reference, slug=slug, root=root, compose_path=compose_path,
                      readme_path=readme_path, nginx_conf_path=nginx_path, doc_paths=docs, notes=notes)
