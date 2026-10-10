from pathlib import Path

import pytest

from faultline.config import ROOT, Settings, load_settings


def test_env_overrides_and_absolute_paths(monkeypatch):
    # The project .env is read too; explicit env vars take precedence over it.
    monkeypatch.setenv("DITTO_URL", "http://ditto.test:9999")
    monkeypatch.setenv("FAULTLINE_COMPOSE_PROJECT", "unit-proj")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("FAULTLINE_LLM", "false")
    monkeypatch.setenv("LLM_MODEL", "claude-test")
    # The legacy OLLAMA_MODEL alias is applied after LLM_MODEL in ENV_MAP, so the project .env's
    # OLLAMA_MODEL would silently win; blank it out (empty values are skipped) to isolate the test.
    monkeypatch.setenv("OLLAMA_MODEL", "")
    s = load_settings()
    assert s.ditto_url == "http://ditto.test:9999"
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


@pytest.mark.xfail(reason="faultline/config.py ENV_MAP applies the legacy OLLAMA_MODEL/OLLAMA_URL aliases after "
                          "LLM_MODEL/LLM_BASE_URL, so a .env OLLAMA_MODEL overrides an explicit LLM_MODEL env var",
                   strict=False)
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
