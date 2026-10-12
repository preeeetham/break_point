"""The Faultline-owned compose override: the only file a human-approved fix ever writes.

The base compose file(s) of the target stack are never edited. Every config change lives in one override
file (settings.remediation_override_file), layered last with `-f`, so removing a fix is restoring that one
file and recreating the touched services. This module owns that file:

    read_text / load / render     the file on disk <-> the override dict, rendered deterministically
    diff                          the unified diff a human approves, verbatim
    backup / restore              exact-bytes snapshot and restore (incl. "the file did not exist")
    live_project / compose_files  which -f files the RUNNING project was started from, override last
    base_config                   the merged base config (read-only `docker compose config`), no override
    recreate_cmd / recreate       `docker compose ... up -d --no-deps --force-recreate <services>`

Nothing here decides WHAT to change; that is faultline/remediation/catalog.py.
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

# Compose ignores top-level `x-` keys, so the list of applied fixes can travel inside the override itself.
META_KEY = "x-faultline"
APPLIED_KEY = "applied_fixes"
# Written in place of a backup when there was no override file to back up: restore deletes the override.
ABSENT_MARKER = b"# faultline: override did not exist before this change; restore deletes it\n"
HEADER = (
    "# Faultline-owned compose override. Written only by Faultline after a human approved a fix.\n"
    "# Layered last on top of the stack's own compose files; the base compose files are never edited.\n"
    "# Do not edit by hand: every change here is backed up and restored byte-for-byte on rollback.\n"
)
LABEL_PROJECT = "com.docker.compose.project"
LABEL_CONFIG_FILES = "com.docker.compose.project.config_files"
LABEL_WORKING_DIR = "com.docker.compose.project.working_dir"
LABEL_ENV_FILE = "com.docker.compose.project.environment_file"


# ---------------------------------------------------------------- YAML loading
class _ComposeLoader(yaml.SafeLoader):
    """safe_load plus the compose-only merge tags (`!reset`, `!override`), read as their plain value.

    Only used for the static fallback; the real merged config comes from `docker compose config`."""


def _plain(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!reset", _plain)
_ComposeLoader.add_constructor("!override", _plain)


def load_yaml_text(text: str) -> dict:
    data = yaml.load(text, Loader=_ComposeLoader) if text.strip() else None  # noqa: S506 - SafeLoader subclass
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("compose file does not contain a mapping")
    return data


# ---------------------------------------------------------------- the override file
def read_text(path: str | Path) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def load(path: str | Path) -> dict:
    return load_yaml_text(read_text(path))


def applied_fixes(override: dict) -> list[str]:
    meta = override.get(META_KEY) or {}
    return [str(x) for x in (meta.get(APPLIED_KEY) or [])]


def with_applied(override: dict, fix_id: str) -> dict:
    """A copy of `override` whose metadata lists `fix_id` (once, in application order)."""
    out = copy.deepcopy(override)
    meta = dict(out.get(META_KEY) or {})
    applied = [str(x) for x in (meta.get(APPLIED_KEY) or [])]
    if fix_id not in applied:
        applied.append(fix_id)
    meta[APPLIED_KEY] = applied
    out[META_KEY] = meta
    return out


def _is_empty(override: dict) -> bool:
    return not any(v for k, v in override.items() if k != META_KEY)


class _Dumper(yaml.SafeDumper):
    """Block style everywhere and indented sequences, so the same dict always renders the same bytes."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):  # noqa: D401
        return super().increase_indent(flow, False)


def render(override: dict) -> str:
    """Deterministic YAML for the override (sorted keys, block style, fixed width), or "" when it is empty.

    An empty override renders as "" on purpose: an override file with no services is not a valid compose
    file, so the apply engine removes the file instead of writing an empty one.
    """
    if not override or _is_empty(override):
        return ""
    applied = applied_fixes(override)
    lines = [HEADER.rstrip("\n")]
    lines.append("# Applied fixes (in order): " + (", ".join(applied) if applied else "none recorded"))
    body = yaml.dump(override, Dumper=_Dumper, sort_keys=True, default_flow_style=False, allow_unicode=True,
                     width=4096, indent=2)
    return "\n".join(lines) + "\n" + body


def diff(before_text: str, after_text: str, path_label: str) -> str:
    """Unified diff of the override file: the exact text a human approves (and change_hash pins)."""
    out = difflib.unified_diff(
        before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
        fromfile=f"a/{path_label}" if before_text else "/dev/null",
        tofile=f"b/{path_label}" if after_text else "/dev/null",
        n=3,
    )
    lines = []
    for line in out:
        lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(lines)


def backup(path: str | Path, backup_dir: str | Path, label: str) -> Path:
    """Snapshot the override's exact bytes to backup_dir/<label>.yml (or an absent-marker if it did not exist)."""
    src, dest_dir = Path(path), Path(backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in label) or "override"
    dest = dest_dir / f"{safe}.yml"
    if src.is_file():
        shutil.copyfile(src, dest)
    else:
        dest.write_bytes(ABSENT_MARKER)
    return dest


def is_absent_marker(backup_path: str | Path) -> bool:
    p = Path(backup_path)
    return p.is_file() and p.read_bytes() == ABSENT_MARKER


def write_atomic(path: str | Path, text: str) -> None:
    """Write via a temp file in the same directory + os.replace, so a reader never sees half a file."""
    write_bytes_atomic(path, text.encode("utf-8"))


def write_bytes_atomic(path: str | Path, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def restore(path: str | Path, backup_path: str | Path) -> None:
    """Put the override back exactly as it was: same bytes, or no file at all if it did not exist."""
    b = Path(backup_path)
    if not b.is_file():
        raise FileNotFoundError(f"backup {b} not found; refusing to guess what the override looked like")
    data = b.read_bytes()
    if data == ABSENT_MARKER:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        return
    write_bytes_atomic(path, data)


# ---------------------------------------------------------------- the live project
def _client(docker: Any = None) -> Any:
    """A docker SDK client from a DockerController, a DockerClient, or the environment. None if unreachable."""
    if docker is not None:
        return getattr(docker, "client", docker)
    try:
        import docker as docker_sdk

        cli = docker_sdk.from_env(timeout=5)
        cli.ping()
        return cli
    except Exception:
        return None


def live_project(settings: Any, docker: Any = None) -> Optional[dict]:
    """How the running project was started, from its compose labels, or None if Docker/the project is unavailable.

    {"config_files": [...], "working_dir": str|None, "environment_file": str|None}
    """
    cli = _client(docker)
    if cli is None:
        return None
    try:
        containers = cli.containers.list(all=True, filters={"label": f"{LABEL_PROJECT}={settings.compose_project}"})
    except Exception:
        return None
    for c in sorted(containers, key=lambda c: getattr(c, "name", "")):
        labels = getattr(c, "labels", None) or {}
        raw = labels.get(LABEL_CONFIG_FILES) or ""
        files = [f.strip() for f in raw.split(",") if f.strip()]
        if files:
            return {"config_files": files, "working_dir": labels.get(LABEL_WORKING_DIR) or None,
                    "environment_file": labels.get(LABEL_ENV_FILE) or None}
    return None


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def compose_files(settings: Any, docker: Any = None, live: Optional[dict] = None) -> list[str]:
    """The -f files for the live project, override always last.

    Prefers the running stack's own label (com.docker.compose.project.config_files), because the project
    may have been started from different files than settings.compose_file. A previous recreate leaves the
    override in that label, so it is removed from the base list before being appended once at the end.
    """
    info = live if live is not None else live_project(settings, docker)
    override = str(settings.remediation_override_file)
    base = list(info["config_files"]) if info else [str(settings.compose_file)]
    base = [f for f in base if not _same_path(f, override)]
    return base + [override]


def base_files(settings: Any, docker: Any = None, live: Optional[dict] = None) -> list[str]:
    """compose_files() without the override: what the stack looks like with no Faultline fix applied."""
    return compose_files(settings, docker, live)[:-1]


def _project_args(settings: Any, info: Optional[dict]) -> list[str]:
    args = ["-p", str(settings.compose_project)]
    if info and info.get("working_dir"):
        args += ["--project-directory", str(info["working_dir"])]
    if info and info.get("environment_file") and Path(info["environment_file"]).is_file():
        args += ["--env-file", str(info["environment_file"])]
    return args


def compose_env(settings: Any) -> dict[str, str]:
    """Extra variables the live stack's compose files need (settings.compose_env, "K=V,K2=V2"), e.g.
    DITTO_LOCAL_DIR, which the stack's own launcher script exports and Faultline otherwise lacks."""
    out: dict[str, str] = {}
    for item in str(getattr(settings, "compose_env", "") or "").split(","):
        k, sep, v = item.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def base_config(settings: Any, docker: Any = None, runner: Callable[..., Any] = subprocess.run,
                timeout: float = 60) -> dict:
    """The merged base compose config of the live project, WITHOUT the Faultline override.

    Runs the read-only `docker compose ... config --format json` over the live project's own files. Variable
    interpolation is tried first; if a required variable is missing from this environment (e.g. one only
    exported by the stack's launcher script), it retries with --no-interpolate, which is enough for every
    key the catalog reads. Falls back to the yaml of settings.compose_file when Docker or the labels are
    unavailable (tests, a stopped stack).
    """
    info = live_project(settings, docker)
    if info:
        cmd = ["docker", "compose", *_project_args(settings, info)]
        for f in base_files(settings, live=info):
            cmd += ["-f", f]
        cmd += ["config", "--format", "json"]
        for extra in ([], ["--no-interpolate"]):
            try:
                res = runner(cmd + extra, capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, **compose_env(settings)})
            except Exception:
                break
            if getattr(res, "returncode", 1) == 0:
                try:
                    data = json.loads(res.stdout or "")
                except ValueError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("services"), dict):
                    return data
    p = Path(settings.compose_file)
    return load_yaml_text(p.read_text(encoding="utf-8")) if p.is_file() else {}


# ---------------------------------------------------------------- recreate
def recreate_cmd(settings: Any, services: list[str], files: list[str], project_directory: Optional[str] = None,
                 env_file: Optional[str] = None) -> list[str]:
    """`docker compose -p <project> [--project-directory d] [--env-file e] -f f1 ... up -d --no-deps --force-recreate <svcs>`."""
    cmd = ["docker", "compose", "-p", str(settings.compose_project)]
    if project_directory:
        cmd += ["--project-directory", str(project_directory)]
    if env_file:
        cmd += ["--env-file", str(env_file)]
    for f in files:
        cmd += ["-f", str(f)]
    cmd += ["up", "-d", "--no-deps", "--force-recreate", *services]
    return cmd


def recreate(settings: Any, services: list[str], runner: Callable[..., Any] = subprocess.run, docker: Any = None,
             timeout: float = 300, env: Optional[dict[str, str]] = None) -> tuple[bool, str]:
    """Recreate `services` from the live project's files plus the override. Never raises: (ok, output).

    If the override file does not exist (e.g. it was just restored to "absent"), it is left out of the -f
    list, which is exactly the pre-fix configuration. `env` is added to os.environ for the compose call, for
    variables the stack's own launcher exports (compose refuses `${VAR:?}` references when they are unset).
    """
    if not services:
        return False, "no services to recreate"
    try:
        info = live_project(settings, docker)
        files = [f for f in compose_files(settings, live=info)
                 if not _same_path(f, str(settings.remediation_override_file)) or Path(f).is_file()]
        env_file = info.get("environment_file") if info else None
        if env_file and not Path(env_file).is_file():
            env_file = None
        cmd = recreate_cmd(settings, list(services), files,
                           project_directory=(info or {}).get("working_dir"), env_file=env_file)
        res = runner(cmd, capture_output=True, text=True, timeout=timeout,
                     env={**os.environ, **compose_env(settings), **(env or {})})
        out = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).strip()
        rc = getattr(res, "returncode", 1)
        return rc == 0, out or f"exit code {rc}"
    except subprocess.TimeoutExpired as exc:
        return False, f"timed out after {timeout:.0f}s: {exc}"
    except Exception as exc:  # never raise: the caller decides what a failed recreate means (rollback)
        return False, f"{type(exc).__name__}: {exc}"
