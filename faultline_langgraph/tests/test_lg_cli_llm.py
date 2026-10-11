"""The opt-in CLI's `graph` command and the chat-model factory (test 10). No network: sockets are blocked."""
from __future__ import annotations

import socket
import sys

import pytest
import requests

from faultline.config import Settings
from faultline_langgraph import llm as lg_llm
from faultline_langgraph.cli import main


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_graph_command_prints_mermaid(monkeypatch, tmp_path, capsys, no_network):
    # load_settings() honours the environment; point the blackboard at tmp so ReportStore's mkdirs stay out of the repo
    monkeypatch.setenv("FAULTLINE_REPORTS_DIR", str(tmp_path / "reports"))
    assert main(["graph"]) == 0
    out = capsys.readouterr().out
    assert out.lstrip().startswith(("graph", "---", "%%"))
    for node in ("rank", "agent", "tools", "validate", "fallback", "__start__", "__end__"):
        assert node in out, node


def test_disabled_llm_gives_no_model():
    handle = lg_llm.chat_model_from_settings(Settings(llm_enabled=False))
    assert handle.model is None and handle.reason == "LLM disabled"
    assert handle.label == "langgraph:ollama:phi4-mini"


def test_enabled_false_argument_overrides_settings():
    s = Settings(llm_provider="openai", llm_api_key="sk-test", llm_enabled=True)
    handle = lg_llm.chat_model_from_settings(s, enabled=False)
    assert handle.model is None and handle.reason == "LLM disabled"


def test_openai_without_key_is_unavailable(no_network):
    handle = lg_llm.chat_model_from_settings(Settings(llm_provider="openai", llm_api_key=""))
    assert handle.model is None and handle.reason == "LLM_API_KEY not set"


def test_openai_model_constructed_without_network(no_network):
    from langchain_openai import ChatOpenAI

    s = Settings(llm_provider="openai", llm_model="gpt-test", llm_api_key="sk-test",
                 llm_base_url="http://127.0.0.1:9/v1", llm_timeout_s=5)
    handle = lg_llm.chat_model_from_settings(s, max_tokens=321)
    assert isinstance(handle.model, ChatOpenAI), handle.reason
    assert handle.label == "langgraph:openai:gpt-test" and handle.reason == ""
    m = handle.model
    assert m.model_name == "gpt-test" and m.openai_api_base == "http://127.0.0.1:9/v1"
    assert m.openai_api_key.get_secret_value() == "sk-test"
    assert m.max_tokens == 321 and m.temperature == lg_llm.TEMPERATURE and m.max_retries == lg_llm.RETRIES


def test_anthropic_model_constructed_without_network(no_network):
    from langchain_anthropic import ChatAnthropic

    s = Settings(llm_provider="anthropic", llm_api_key="sk-ant-test", llm_base_url="http://127.0.0.1:9")
    handle = lg_llm.chat_model_from_settings(s)
    assert isinstance(handle.model, ChatAnthropic), handle.reason
    assert handle.label.startswith("langgraph:anthropic:")


def test_ollama_unreachable_gives_reason(monkeypatch, no_network):
    def down(*a, **k):
        raise requests.ConnectionError("ollama is down")

    monkeypatch.setattr(requests, "get", down)
    handle = lg_llm.chat_model_from_settings(Settings(llm_provider="ollama", llm_base_url="http://127.0.0.1:9"))
    assert handle.model is None and "ollama is down" in handle.reason


def test_missing_provider_package_gives_install_hint(monkeypatch, no_network):
    monkeypatch.setitem(sys.modules, "langchain_openai", None)  # makes `import langchain_openai` raise ImportError
    handle = lg_llm.chat_model_from_settings(Settings(llm_provider="openai", llm_api_key="sk-test"))
    assert handle.model is None and "pip install -e '.[langgraph]'" in handle.reason
