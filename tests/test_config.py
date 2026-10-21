from pathlib import Path

import pytest

from faultline.config import ROOT, Settings, load_settings


def test_env_overrides_and_absolute_paths(monkeypatch):
    # The project .env is read too; explicit env vars take precedence over it.
    monkeypatch.setenv("FAULTLINE_TARGET", "")
    monkeypatch.setenv("FAULTLINE_COMPOSE_FILE", "tests/fixtures/shop/docker-compose.yml")
    monkeypatch.setenv("FAULTLINE_NGINX_CONF", "tests/fixtures/shop/nginx/nginx.conf")
    monkeypatch.setenv("FAULTLINE_KNOWLEDGE_FILE", "tests/fixtures/shop/shop_knowledge.yaml")
    monkeypatch.setenv("TARGET_URL", "http://shop.test:9999")
    monkeypatch.setenv("TARGET_USER", "probe")
    monkeypatch.setenv("FAULTLINE_COMPOSE_PROJECT", "unit-proj")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FAULTLINE_LLM", "false")
    monkeypatch.setenv("LLM_MODEL", "claude-test")
    # The legacy OLLAMA_MODEL alias is applied after LLM_MODEL in ENV_MAP, so the project .env's
    # OLLAMA_MODEL would silently win; blank it out (empty values are skipped) to isolate the test.
    monkeypatch.setenv("OLLAMA_MODEL", "")
    s = load_settings()
    assert s.target_url == "http://shop.test:9999" and s.target_user == "probe"
    assert s.compose_project == "unit-proj"
    assert s.compose_network == "unit-proj_default"
    assert s.llm_provider == "anthropic"
    assert s.llm_enabled is False
    assert s.llm_model == "claude-test"
    for attr in ("compose_file", "nginx_conf", "reports_dir", "thresholds_file", "knowledge_file"):
        p = Path(getattr(s, attr))
        assert p.is_absolute(), attr
        assert ROOT in p.parents, attr
    assert s.thresholds_file == str(ROOT / "config" / "thresholds.yaml")
    assert s.reports == Path(s.reports_dir)
    # the Faultline-owned override sits next to whatever compose file the target uses
    assert s.remediation_override_file == str(ROOT / "tests" / "fixtures" / "shop" / "docker-compose.override.yml")


def _target(tmp_path, monkeypatch, name="shop", target_yaml="", files=()):
    import faultline.config as cfg

    tdir = tmp_path / "targets" / name
    tdir.mkdir(parents=True)
    (tdir / "target.yaml").write_text(target_yaml)
    for f in files:
        (tdir / f).write_text("{}\n")
    monkeypatch.setattr(cfg, "TARGETS_DIR", tmp_path / "targets")
    for key in ("FAULTLINE_COMPOSE_FILE", "FAULTLINE_COMPOSE_PROJECT", "FAULTLINE_KNOWLEDGE_FILE",
                "FAULTLINE_PROBE_FILE", "FAULTLINE_RULES_FILE", "TARGET_URL", "FAULTLINE_APP_NAME"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("FAULTLINE_TARGET", name)
    return tdir


def test_target_yaml_layers_between_settings_and_env(tmp_path, monkeypatch):
    tdir = _target(tmp_path, monkeypatch, target_yaml=(
        "app_name: The Shop\ncompose_file: deploy/docker-compose.yml\ncompose_project: shop\n"
        "target_url: http://127.0.0.1:8181\nprobe_rps: 2\nthresholds_file: thresholds.yaml\n"),
        files=("shop_knowledge.yaml", "probe.yaml", "rules.yaml"))
    s = load_settings()
    assert s.target == "shop" and s.target_error == "" and s.target_dir == tdir
    assert s.app_name == "The Shop" and s.app_label == "The Shop"
    # relative paths in target.yaml are relative to the target directory, not the repository
    assert s.compose_file == str(tdir / "deploy" / "docker-compose.yml")
    assert s.thresholds_file == str(tdir / "thresholds.yaml")
    assert s.remediation_override_file == str(tdir / "deploy" / "docker-compose.override.yml")
    # conventional files are picked up without being named
    assert (s.knowledge_file, s.probe_file, s.rules_file) == (
        str(tdir / "shop_knowledge.yaml"), str(tdir / "probe.yaml"), str(tdir / "rules.yaml"))
    assert (s.compose_project, s.target_url, s.probe_rps) == ("shop", "http://127.0.0.1:8181", 2)
    # the environment still wins over the target
    monkeypatch.setenv("TARGET_URL", "http://elsewhere:1")
    assert load_settings().target_url == "http://elsewhere:1"


def test_an_onboarded_knowledge_file_is_found_under_the_repository_s_name(tmp_path, monkeypatch):
    tdir = _target(tmp_path, monkeypatch, name="myapp", files=("acme-shop_knowledge.yaml",))
    assert load_settings().knowledge_file == str(tdir / "acme-shop_knowledge.yaml")
    (tdir / "other_knowledge.yaml").write_text("{}\n")          # two candidates: pick none rather than guess
    assert load_settings().knowledge_file == ""


def test_missing_target_directory_is_reported_not_raised(tmp_path, monkeypatch):
    _target(tmp_path, monkeypatch, name="present")
    monkeypatch.setenv("FAULTLINE_TARGET", "absent")
    s = load_settings()
    assert s.target == "absent" and s.target_dir is None and s.compose_file == ""
    assert "targets/absent/ does not exist" in s.target_error and "available: present" in s.target_error


def test_no_target_leaves_paths_empty_rather_than_pointing_at_the_repo_root(monkeypatch):
    for key in ("FAULTLINE_TARGET", "FAULTLINE_COMPOSE_FILE", "FAULTLINE_NGINX_CONF", "FAULTLINE_KNOWLEDGE_FILE",
                "FAULTLINE_PROBE_FILE", "FAULTLINE_RULES_FILE"):
        monkeypatch.setenv(key, "")
    s = load_settings()
    assert (s.compose_file, s.nginx_conf, s.knowledge_file, s.probe_file, s.rules_file) == ("", "", "", "", "")
    assert s.remediation_override_file == "" and s.app_label == "the application under test"


def test_typed_env_parsing_and_network_override(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LLM", "yes")
    monkeypatch.setenv("FAULTLINE_NETWORK", "custom_net")
    s = load_settings()
    assert s.llm_enabled is True
    assert s.compose_network == "custom_net"


def test_settings_yaml_applies_known_keys_only(tmp_path, monkeypatch):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text("probe_rps: 9.5\ndashboard_port: 9000\nunknown_key: 1\nreports_dir: /abs/reports\n")
    monkeypatch.delenv("FAULTLINE_REPORTS_DIR", raising=False)
    s = load_settings(str(cfg))
    assert s.probe_rps == 9.5 and s.dashboard_port == 9000
    assert not hasattr(s, "unknown_key")
    assert s.reports_dir == "/abs/reports"  # absolute paths are kept as-is


def test_explicit_llm_model_beats_dotenv_ollama_alias(tmp_path, monkeypatch):
    import faultline.config as cfg

    (tmp_path / ".env").write_text("OLLAMA_MODEL=legacy-model\n")
    monkeypatch.setattr(cfg, "ROOT", tmp_path)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "explicit-model")
    assert load_settings().llm_model == "explicit-model"


def test_defaults_sane():
    s = Settings()
    assert s.compose_network == "faultline_default"
    assert s.llm_provider == "ollama" and s.llm_enabled is True
