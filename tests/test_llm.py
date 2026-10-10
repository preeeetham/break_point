import pytest
import requests

from faultline.planning.llm import LLMClient, OllamaClient


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _ollama_tags(*models):
    return FakeResponse({"models": [{"name": m} for m in models]})


def test_openai_without_key_is_unavailable():
    c = LLMClient(provider="openai", api_key="")
    assert c.available() is False
    assert "KEY" in c.last_error.upper()
    assert c.chat_json("s", "u") is None
    assert c.describe() == "openai:gpt-4o-mini"


def test_unknown_provider_falls_back_to_ollama_and_disabled_is_unavailable():
    assert LLMClient(provider="bogus").provider == "ollama"
    c = LLMClient(provider="ollama", enabled=False)
    assert c.available() is False


def test_ollama_chat_json_parses_fenced_json_with_think_tags(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen["get"] = url
        return _ollama_tags("phi4-mini:latest", "llama3")

    def fake_post(url, **kw):
        seen["post"] = (url, kw)
        content = '<think>let me reason</think>\nSure:\n```json\n{"choice": 1, "parameters": {"duration_s": 20}}\n```'
        return FakeResponse({"message": {"role": "assistant", "content": content}})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    c = LLMClient(provider="ollama", model="phi4-mini", base_url="http://ollama.test:11434/")
    assert c.available() is True
    assert c.chat_json("sys", "user") == {"choice": 1, "parameters": {"duration_s": 20}}
    assert seen["get"] == "http://ollama.test:11434/api/tags"
    assert seen["post"][0] == "http://ollama.test:11434/api/chat"
    assert seen["post"][1]["json"]["format"] == "json"
    assert seen["post"][1]["json"]["model"] == "phi4-mini"


def test_ollama_model_missing_from_tags(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: _ollama_tags("llama3"))
    c = LLMClient(provider="ollama", model="phi4-mini")
    assert c.available() is False
    assert "phi4-mini" in c.last_error


def test_chat_json_non_json_reply_returns_none(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: _ollama_tags("phi4-mini"))
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse({"message": {"content": "I cannot help with that."}}))
    c = LLMClient(provider="ollama", model="phi4-mini")
    assert c.chat_json("s", "u") is None
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse({"message": {"content": "{not: valid json}"}}))
    assert c.chat_json("s", "u") is None
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse({"message": {"content": "[1, 2]"}}))
    assert c.chat_json("s", "u") is None


def test_connection_error_never_raises(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, **kw: _ollama_tags("phi4-mini"))

    def boom(url, **kw):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", boom)
    c = OllamaClient("http://127.0.0.1:1", "phi4-mini")
    assert c.chat_json("s", "u") is None
    assert c.complete("s", "u") is None
    assert "refused" in c.last_error


def test_probe_connection_error_marks_unavailable(monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError("no ollama")

    monkeypatch.setattr(requests, "get", boom)
    c = LLMClient(provider="ollama")
    assert c.available() is False and "no ollama" in c.last_error


def test_anthropic_request_shape(monkeypatch):
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured.update(kw)
        return FakeResponse({"content": [{"type": "text", "text": '{"ok": true}'}, {"type": "tool_use"}]})

    monkeypatch.setattr(requests, "post", fake_post)
    c = LLMClient(provider="anthropic", api_key="sk-test", model="claude-x")
    assert c.available() is True
    assert c.chat_json("sys", "user") == {"ok": True}
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert "anthropic-version" in captured["headers"]
    body = captured["json"]
    assert body["system"] == "sys" and body["model"] == "claude-x"
    assert body["messages"][0]["role"] == "user" and "JSON" in body["messages"][0]["content"]


def test_openai_request_shape_and_http_error(monkeypatch):
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured.update(kw)
        return FakeResponse({"choices": [{"message": {"content": '{"a": 1}'}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    c = LLMClient(provider="openai", api_key="k", base_url="https://groq.test/openai/v1")
    assert c.chat_json("s", "u") == {"a": 1}
    assert captured["url"] == "https://groq.test/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["json"]["response_format"] == {"type": "json_object"}
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse({}, status=500))
    assert c.chat_json("s", "u") is None
    assert "500" in c.last_error


def test_complete_strips_think_tags(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse(
        {"choices": [{"message": {"content": "<think>hmm</think>  plain answer "}}]}))
    c = LLMClient(provider="openai", api_key="k")
    assert c.complete("s", "u") == "plain answer"
