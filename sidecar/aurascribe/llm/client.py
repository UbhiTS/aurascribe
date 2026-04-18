"""Provider-agnostic LLM client — all chat-completions calls route through here.

The `openai` package is a soft dep — imported lazily so the sidecar boots even
when the `[llm]` extra isn't installed. `chat()` raises ImportError in that
case, which callers translate to 503.

The Python `openai` SDK speaks OpenAI-compatible HTTP, which covers LM Studio,
Ollama's OpenAI shim, OpenAI itself, OpenRouter, Gemini's OpenAI-compat
endpoint, and most commercial gateways. Swap providers by pointing
`llm_base_url` / `llm_api_key` / `llm_model` at the new target in Settings.
"""
from __future__ import annotations

import logging

from aurascribe.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

log = logging.getLogger("aurascribe.llm")

_client = None


def get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    return _client


class LLMUnavailableError(Exception):
    pass


async def chat(
    prompt: str,
    system: str = "",
    model: str | None = None,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> str:
    """Single-turn chat. Raises LLMUnavailableError if the provider is unreachable.

    `model` defaults to the configured `llm_model` (see config.py).
    `max_tokens` caps the response length — callers producing long
    structured output (e.g. daily briefs aggregating many meetings) should
    raise this; otherwise the response gets truncated mid-JSON.
    """
    if model is None:
        model = LLM_MODEL
    client = get_client()
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        content = (choice.message.content or "").strip()
        # Empty output usually means the model hit max_tokens before producing
        # any content (reasoning models burn the whole budget on internal
        # thinking) or the input exceeded the actual context window. Surface
        # finish_reason + usage so the operator can tell which one it is.
        if not content:
            usage = getattr(response, "usage", None)
            log.warning(
                "LLM returned empty content. model=%s finish_reason=%s "
                "usage=prompt:%s completion:%s total:%s max_tokens=%s. "
                "Likely fixes: raise llm_context_tokens in Settings, or use "
                "a model that doesn't burn the whole output budget on reasoning.",
                model,
                getattr(choice, "finish_reason", "?"),
                getattr(usage, "prompt_tokens", "?") if usage else "?",
                getattr(usage, "completion_tokens", "?") if usage else "?",
                getattr(usage, "total_tokens", "?") if usage else "?",
                max_tokens,
            )
        return content
    except Exception as e:
        msg = str(e).lower()
        if "connect" in msg or "connection" in msg or "refused" in msg:
            raise LLMUnavailableError(f"LLM provider not reachable at {LLM_BASE_URL}") from e
        raise


async def get_available_models() -> list[str]:
    """List models the configured provider reports as available."""
    try:
        client = get_client()
        models = await client.models.list()
        return [m.id for m in models.data]
    except Exception:
        return []
