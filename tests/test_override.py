"""The Faultline-owned compose override: exact-bytes backup/restore, deterministic render/diff, recreate. No Docker."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.remediation import override as ov

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "deploy" / "docker-compose.yml"


def _settings(tmp_path: Path, project: str = "bosch-ditto") -> SimpleNamespace:
    return SimpleNamespace(compose_project=project, compose_file=str(BASE),
                           remediation_override_file=str(tmp_path / "docker-compose.override.yml"))


class _Container:
    def __init__(self, name: str, labels: dict):
        self.name, self.labels = name, labels


class _FakeDocker:
    """Stands in for a DockerController: `.client.containers.list(...)`."""

    def __init__(self, labels: dict | None):
        containers = [_Container("bosch-ditto-things-1", labels)] if labels is not None else []
        self.client = SimpleNamespace(containers=SimpleNamespace(list=lambda **kw: containers))


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


LIVE_FILES = ["/srv/ditto/docker-compose.yml", "/srv/local/compose.local.yml"]


def _labels(files: list[str], wd: str = "/srv/ditto", env_file: str = "") -> dict:
    return {ov.LABEL_PROJECT: "bosch-ditto", ov.LABEL_CONFIG_FILES: ",".join(files),
            ov.LABEL_WORKING_DIR: wd, ov.LABEL_ENV_FILE: env_file}


# ---------------------------------------------------------------- backup / restore
@pytest.mark.parametrize("data", [
    b"services:\n  things:\n    restart: always\n",
    b"# hand-edited\r\nservices:\r\n  things: {}\r\n",        # CRLF
    b"services:\n  gateway:\n    restart: \"no\"",           # no trailing newline
    "services:\n  things:\n    labels: {note: \"Grüße ✓\"}\n".encode("utf-8"),
])
def test_backup_restore_is_byte_identical(tmp_path, data):
    path, backups = tmp_path / "override.yml", tmp_path / "backups"
    path.write_bytes(data)
    b = ov.backup(path, backups, "fx-123")
    assert b == backups / "fx-123.yml" and b.read_bytes() == data
    ov.write_atomic(path, "services:\n  things:\n    restart: unless-stopped\n")
    assert path.read_bytes() != data
    ov.restore(path, b)
    assert path.read_bytes() == data


def test_backup_of_absent_override_restores_to_absent(tmp_path):
    path = tmp_path / "override.yml"
    b = ov.backup(path, tmp_path / "backups", "fx-absent")
    assert ov.is_absent_marker(b)
    ov.write_atomic(path, "services:\n  things: {}\n")
    assert path.exists()
    ov.restore(path, b)
    assert not path.exists()
    ov.restore(path, b)            # idempotent
    assert not path.exists()


def test_restore_refuses_without_backup(tmp_path):
    path = tmp_path / "override.yml"
    path.write_text("services: {}\n")
    with pytest.raises(FileNotFoundError):
        ov.restore(path, tmp_path / "missing.yml")
    assert path.read_text() == "services: {}\n"


def test_backup_label_is_sanitised(tmp_path):
    b = ov.backup(tmp_path / "nope.yml", tmp_path / "b", "../../etc/passwd")
    assert b.parent == tmp_path / "b"


def test_write_atomic_leaves_no_temp_files(tmp_path):
    path = tmp_path / "sub" / "override.yml"
    ov.write_atomic(path, "a: 1\n")
    ov.write_atomic(path, "a: 2\n")
    assert path.read_text() == "a: 2\n"
    assert [p.name for p in path.parent.iterdir()] == ["override.yml"]


def test_read_text_and_load_absent(tmp_path):
    assert ov.read_text(tmp_path / "x.yml") == ""
    assert ov.load(tmp_path / "x.yml") == {}
    (tmp_path / "empty.yml").write_text("")
    assert ov.load(tmp_path / "empty.yml") == {}


def test_load_tolerates_compose_merge_tags(tmp_path):
    p = tmp_path / "local.yml"
    p.write_text("services:\n  mongodb:\n    ports: !reset []\n  nginx:\n    ports: !override\n      - '80:80'\n")
    assert ov.load(p) == {"services": {"mongodb": {"ports": []}, "nginx": {"ports": ["80:80"]}}}


# ---------------------------------------------------------------- render / diff
def test_render_is_deterministic_and_round_trips():
    a = {"services": {"things": {"restart": "always", "healthcheck": {"test": "x", "interval": "30s"}},
                      "gateway": {"restart": "always"}}}
    b = {"services": {"gateway": {"restart": "always"},
                      "things": {"healthcheck": {"interval": "30s", "test": "x"}, "restart": "always"}}}
    assert ov.render(a) == ov.render(b)
    assert ov.load_yaml_text(ov.render(a)) == a
    text = ov.render(ov.with_applied(a, "add_or_tighten_healthcheck"))
    assert text.startswith("# Faultline-owned compose override")
    assert "# Applied fixes (in order): add_or_tighten_healthcheck\n" in text
    assert text.index("  gateway:") < text.index("  things:")


def test_render_empty_override_is_empty_string():
    assert ov.render({}) == ""
    assert ov.render({ov.META_KEY: {ov.APPLIED_KEY: ["x"]}, "services": {}}) == ""


def test_with_applied_does_not_mutate_and_dedupes():
    base = {"services": {"things": {"restart": "always"}}}
    one = ov.with_applied(base, "a")
    two = ov.with_applied(one, "a")
    assert ov.META_KEY not in base
    assert ov.applied_fixes(two) == ["a"]
    assert ov.applied_fixes(ov.with_applied(two, "b")) == ["a", "b"]


def test_diff_is_stable_and_unified():
    before = ov.render({"services": {"things": {"restart": "always"}}})
    after = ov.render({"services": {"things": {"restart": "unless-stopped"}}})
    d1 = ov.diff(before, after, "deploy/docker-compose.override.yml")
    d2 = ov.diff(before, after, "deploy/docker-compose.override.yml")
    assert d1 == d2
    assert d1.startswith("--- a/deploy/docker-compose.override.yml\n+++ b/deploy/docker-compose.override.yml\n")
    assert "-    restart: always\n" in d1 and "+    restart: unless-stopped\n" in d1
    assert ov.diff(before, before, "x") == ""


def test_diff_from_and_to_absent_file():
    after = ov.render({"services": {"things": {"restart": "always"}}})
    created = ov.diff("", after, "o.yml")
    assert created.startswith("--- /dev/null\n+++ b/o.yml\n")
    removed = ov.diff(after, "", "o.yml")
    assert removed.startswith("--- a/o.yml\n+++ /dev/null\n")


def test_diff_marks_missing_final_newline():
    d = ov.diff("a: 1\n", "a: 2", "o.yml")
    assert d.endswith("+a: 2\n\\ No newline at end of file\n")


# ---------------------------------------------------------------- live project / files
def test_compose_files_prefers_live_label_and_appends_override_last(tmp_path):
    s = _settings(tmp_path)
    files = ov.compose_files(s, docker=_FakeDocker(_labels(LIVE_FILES)))
    assert files == LIVE_FILES + [s.remediation_override_file]


def test_compose_files_does_not_repeat_override_from_previous_recreate(tmp_path):
    s = _settings(tmp_path)
    files = ov.compose_files(s, docker=_FakeDocker(_labels(LIVE_FILES + [s.remediation_override_file])))
    assert files == LIVE_FILES + [s.remediation_override_file]


def test_compose_files_falls_back_to_settings_without_live_project(tmp_path):
    s = _settings(tmp_path)
    assert ov.compose_files(s, docker=_FakeDocker(None)) == [str(BASE), s.remediation_override_file]
    assert ov.base_files(s, docker=_FakeDocker(None)) == [str(BASE)]


def test_live_project_reads_labels(tmp_path):
    info = ov.live_project(_settings(tmp_path), docker=_FakeDocker(_labels(LIVE_FILES, env_file="/srv/.env")))
    assert info == {"config_files": LIVE_FILES, "working_dir": "/srv/ditto", "environment_file": "/srv/.env"}


def test_live_project_none_when_docker_errors(tmp_path):
    def boom(**kw):
        raise RuntimeError("daemon down")
    docker = SimpleNamespace(client=SimpleNamespace(containers=SimpleNamespace(list=boom)))
    assert ov.live_project(_settings(tmp_path), docker=docker) is None


# ---------------------------------------------------------------- base config
def test_base_config_uses_compose_config_without_override(tmp_path):
    s = _settings(tmp_path)
    calls = []
    merged = {"services": {"things": {"restart": "always"}}}

    def runner(cmd, **kw):
        calls.append(cmd)
        return _Result(0, json.dumps(merged))
    assert ov.base_config(s, docker=_FakeDocker(_labels(LIVE_FILES)), runner=runner) == merged
    cmd = calls[0]
    assert cmd[:4] == ["docker", "compose", "-p", "bosch-ditto"]
    assert cmd[-3:] == ["config", "--format", "json"]
    assert s.remediation_override_file not in cmd
    assert cmd[cmd.index("--project-directory") + 1] == "/srv/ditto"


def test_base_config_retries_without_interpolation(tmp_path):
    s = _settings(tmp_path)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if "--no-interpolate" not in cmd:
            return _Result(1, "", "required variable DITTO_LOCAL_DIR is missing a value")
        return _Result(0, json.dumps({"services": {"mongodb": {}}}))
    assert ov.base_config(s, docker=_FakeDocker(_labels(LIVE_FILES)), runner=runner) == {"services": {"mongodb": {}}}
    assert len(calls) == 2 and calls[1][-1] == "--no-interpolate"


def test_base_config_falls_back_to_compose_file_yaml(tmp_path):
    s = _settings(tmp_path)

    def runner(cmd, **kw):
        raise AssertionError("must not run compose without a live project")
    cfg = ov.base_config(s, docker=_FakeDocker(None), runner=runner)
    # the shared anchor is resolved by the yaml loader
    assert cfg["services"]["things"]["healthcheck"]["test"] == "curl --fail `hostname`:7626/alive || exit 1"


# ---------------------------------------------------------------- recreate
def test_recreate_cmd_shape(tmp_path):
    s = _settings(tmp_path)
    cmd = ov.recreate_cmd(s, ["things", "gateway"], ["a.yml", "b.yml", "o.yml"])
    assert cmd == ["docker", "compose", "-p", "bosch-ditto", "-f", "a.yml", "-f", "b.yml", "-f", "o.yml",
                   "up", "-d", "--no-deps", "--force-recreate", "things", "gateway"]
    cmd = ov.recreate_cmd(s, ["things"], ["a.yml"], project_directory="/wd", env_file="/wd/.env")
    assert cmd[4:8] == ["--project-directory", "/wd", "--env-file", "/wd/.env"]


def test_recreate_failure_returns_false(tmp_path):
    s = _settings(tmp_path)
    ok, out = ov.recreate(s, ["things"], runner=lambda cmd, **kw: _Result(1, "", "boom: no such service"),
                          docker=_FakeDocker(None))
    assert ok is False and "boom" in out


def test_recreate_never_raises(tmp_path):
    s = _settings(tmp_path)

    def raising(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    ok, out = ov.recreate(s, ["things"], runner=raising, docker=_FakeDocker(None), timeout=1)
    assert ok is False and "timed out" in out

    def oserror(cmd, **kw):
        raise FileNotFoundError("docker")
    assert ov.recreate(s, ["things"], runner=oserror, docker=_FakeDocker(None))[0] is False
    assert ov.recreate(s, [], runner=oserror, docker=_FakeDocker(None))[0] is False


def test_recreate_success_uses_live_files_and_override(tmp_path):
    s = _settings(tmp_path)
    Path(s.remediation_override_file).write_text("services:\n  things: {}\n")
    env_file = tmp_path / ".env"
    env_file.write_text("X=1\n")
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env")
        return _Result(0, "Container bosch-ditto-things-1 Started")
    ok, out = ov.recreate(s, ["things"], runner=runner,
                          docker=_FakeDocker(_labels(LIVE_FILES, env_file=str(env_file))), env={"DITTO_LOCAL_DIR": "/x"})
    assert ok and "Started" in out
    cmd = seen["cmd"]
    files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-f"]
    assert files == LIVE_FILES + [s.remediation_override_file]
    assert cmd[cmd.index("--env-file") + 1] == str(env_file)
    assert seen["env"]["DITTO_LOCAL_DIR"] == "/x"


def test_recreate_leaves_out_absent_override(tmp_path):
    s = _settings(tmp_path)
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        return _Result(0)
    assert ov.recreate(s, ["things"], runner=runner, docker=_FakeDocker(_labels(LIVE_FILES)))[0]
    assert s.remediation_override_file not in seen["cmd"]
    assert "--env-file" not in seen["cmd"]     # label empty -> not passed
