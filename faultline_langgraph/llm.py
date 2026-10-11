"""LangChain chat models from the SAME settings the original LLMClient reads.

There is deliberately no second configuration scheme: LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL / LLM_API_KEY /
FAULTLINE_LLM resolve through faultline.config and faultline.planning.llm.client_from_settings exactly as for
the original agents (same provider defaults, same availability probe), and only the transport differs:

  ollama     langchain_ollama.ChatOllama
  openai     langchain_openai.ChatOpenAI   (any OpenAI-compatible endpoint: OpenAI, Groq, OpenRouter, ...)
  anthropic  langchain_anthropic.ChatAnthropic

Provider packages are imported lazily, so only the one in use has to be installed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from faultline.planning.llm import client_from_settings

TEMPERATURE = 0.2  # same as the original LLMClient
# An agent makes several calls per plan, so rate limits (429, e.g. Groq's free tier) are expected rather than
# exceptional; the provider SDKs back off and honour Retry-After. The original single-shot client never retries.
RETRIES = 3


@dataclass
class ModelHandle:
    model: Any | None  # a langchain BaseChatModel, or None when the LLM is disabled/unavailable
    label: str         # "langgraph:<provider>:<model>", recorded in planner notes
    reason: str = ""   # why model is None


def chat_model_from_settings(settings, enabled: bool = True, max_tokens: int = 700) -> ModelHandle:
    client = client_from_settings(settings)
    client.enabled = client.enabled and enabled
    label = f"langgraph:{client.describe()}"
    if not client.enabled:
        return ModelHandle(None, label, "LLM disabled")
    if not client.available():  # the original probe: ollama model pulled / API key present
        return ModelHandle(None, label, client.last_error or "LLM not available")
    try:
        return ModelHandle(_build(client, max_tokens), label)
    except ImportError as exc:
        return ModelHandle(None, label, f"{exc.name or exc} not installed; pip install -e '.[langgraph]'")


def _build(c, max_tokens: int):
    if c.provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=c.model, base_url=c.base_url, temperature=TEMPERATURE, num_predict=max_tokens,
                          client_kwargs={"timeout": c.timeout})
    if c.provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=c.model, base_url=c.base_url, api_key=c.api_key, temperature=TEMPERATURE,
                          max_tokens=max_tokens, timeout=c.timeout, max_retries=RETRIES)
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=c.model, base_url=c.base_url, api_key=c.api_key, temperature=TEMPERATURE,
                         max_tokens=max_tokens, timeout=c.timeout, max_retries=RETRIES)
