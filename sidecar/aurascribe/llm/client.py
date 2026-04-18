"""LM Studio client — all LLM calls route through here.

The `openai` package is a soft dep — imported lazily so the sidecar boots even
when the `[llm]` extra isn't installed. `chat()` raises ImportError in that
case, which callers translate to 503.
"""
from __future__ import annotations

from aurascribe.config import LM_STUDIO_API_KEY, LM_STUDIO_MODEL, LM_STUDIO_URL

_client = None


def get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(base_url=LM_STUDIO_URL, api_key=LM_STUDIO_API_KEY)
    return _client


class LLMUnavailableError(Exception):
    pass


async def chat(prompt: str, system: str = "", model: str | None = None) -> str:
    """Single-turn chat. Raises LLMUnavailableError if LM Studio is unreachable.

    `model` defaults to the `LM_STUDIO_MODEL` env var (see config.py).
    """
    if model is None:
        model = LM_STUDIO_MODEL
    client = get_client()
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            max_tokens=2048,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        msg = str(e).lower()
        if "connect" in msg or "connection" in msg or "refused" in msg:
            raise LLMUnavailableError(f"LM Studio not reachable at {LM_STUDIO_URL}") from e
        raise


async def get_available_models() -> list[str]:
    """List models currently loaded in LM Studio."""
    try:
        client = get_client()
        models = await client.models.list()
        return [m.id for m in models.data]
    except Exception:
        return []
