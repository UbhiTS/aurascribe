"""Provider-agnostic LLM client — all chat-completions calls route through here.

The `openai` package is a soft dep — imported lazily so the sidecar boots even
when the `[llm]` extra isn't installed. `chat()` raises ImportError in that
case, which callers translate to 503.

The Python `openai` SDK speaks OpenAI-compatible HTTP, which covers LM Studio,
Ollama's OpenAI shim, OpenAI itself, OpenRouter, Gemini's OpenAI-compat
endpoint, and most commercial gateways. Swap providers by pointing
`llm_base_url` / `llm_api_key` / `llm_model` at the new target in Settings.

Stability contract:

  * **Every call has a timeout.** Default 60s per call, override via
    `timeout=` kwarg. Without this, a wedged provider freezes the UI
    forever (summarize → "Processing…" spinner that never ends).
  * **Truncation is a typed error, not silent data loss.** If the
    provider returns `finish_reason='length'` the response was cut off
    mid-generation — parsing it as JSON gives garbage. `LLMTruncatedError`
    lets callers either retry with a bigger budget or degrade gracefully.
  * **Transient errors retry with backoff.** Connection refused / reset /
    timeout during a call → up to 2 retries at 1s, 2s before surfacing.
    Non-transient errors (4xx, auth, parse) raise immediately.
"""
from __future__ import annotations

import asyncio
import logging

from aurascribe.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

log = logging.getLogger("aurascribe.llm")

_client = None

# Default per-call timeout. 60s is generous for small completions (title,
# bucket) but correct for medium summaries on a local model. Callers
# producing larger output (daily brief) bump this explicitly.
_DEFAULT_TIMEOUT_SEC = 60.0

# Retry schedule for transient errors — connection refused / read timeout
# / reset. Short enough the user doesn't wait forever; long enough to
# ride out a provider blip.
_RETRY_DELAYS = (1.0, 2.0)


def get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    return _client


class LLMUnavailableError(Exception):
    """Provider isn't reachable — connection refused, DNS failure, or
    repeated timeouts after retry exhaustion. Callers should degrade
    gracefully (skip the LLM step, keep the transcript, log and move on)."""
    pass


class LLMTruncatedError(Exception):
    """Provider returned `finish_reason='length'` — the response was cut
    off at `max_tokens`. The content is still attached as `.content` for
    callers that want to salvage it, but structured-JSON callers must
    treat this as a failure (parsing truncated JSON gives garbage).

    Fix: raise `llm_context_tokens` in Settings, pass a larger
    `max_tokens=`, or swap to a model with a bigger budget.
    """
    def __init__(self, message: str, *, content: str = "") -> None:
        super().__init__(message)
        self.content = content


def _is_transient(exc: BaseException) -> bool:
    """Classify openai/httpx errors as transient-vs-not. Transient errors
    retry with backoff; others surface immediately so we don't hide
    misconfiguration behind a long stall."""
    msg = str(exc).lower()
    # Connection-level transience — provider is up-and-down, worth retrying.
    if any(s in msg for s in ("connect", "connection", "refused", "reset",
                              "timeout", "timed out", "read timeout")):
        return True
    # Explicit httpx/openai timeout classes.
    name = type(exc).__name__.lower()
    if "timeout" in name or "connecterror" in name:
        return True
    return False


async def chat(
    prompt: str,
    system: str = "",
    model: str | None = None,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.3,
    timeout: float = _DEFAULT_TIMEOUT_SEC,
) -> str:
    """Single-turn chat. Raises:

    - `LLMUnavailableError` if the provider is unreachable (after retries).
    - `LLMTruncatedError` if the response was cut off at `max_tokens`.

    `model` defaults to the configured `llm_model` (see config.py).
    `timeout` is per-attempt; total wall-clock before failure is roughly
    `timeout × (retries + 1) + sum(_RETRY_DELAYS)`.
    """
    if model is None:
        model = LLM_MODEL
    client = get_client()
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_exc: BaseException | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                timeout=timeout,
            )
            break  # success path continues below
        except asyncio.TimeoutError as e:
            last_exc = e
            log.warning("LLM call timed out after %.0fs (attempt %d)", timeout, attempt + 1)
            if attempt < len(_RETRY_DELAYS):
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                continue
            raise LLMUnavailableError(
                f"LLM call timed out after {timeout:.0f}s × {attempt + 1} attempts"
            ) from e
        except Exception as e:
            if _is_transient(e) and attempt < len(_RETRY_DELAYS):
                last_exc = e
                log.warning("LLM transient error (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                continue
            if _is_transient(e):
                raise LLMUnavailableError(
                    f"LLM provider not reachable at {LLM_BASE_URL}: {e}"
                ) from e
            raise
    else:  # pragma: no cover — loop always breaks or raises
        raise LLMUnavailableError("LLM retries exhausted") from last_exc

    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    finish_reason = getattr(choice, "finish_reason", None)

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
            finish_reason or "?",
            getattr(usage, "prompt_tokens", "?") if usage else "?",
            getattr(usage, "completion_tokens", "?") if usage else "?",
            getattr(usage, "total_tokens", "?") if usage else "?",
            max_tokens,
        )

    # Truncation is distinct from empty output: we got *some* content but
    # the model was cut off. JSON callers will fail to parse this. Raise
    # a typed error so they can log a useful message ("Your model hit the
    # output budget — raise max_tokens or llm_context_tokens") instead of
    # a generic JSONDecodeError.
    if finish_reason == "length":
        raise LLMTruncatedError(
            f"LLM response truncated at max_tokens={max_tokens}. "
            f"Raise llm_context_tokens / max_tokens or use a larger model.",
            content=content,
        )

    return content


async def get_available_models() -> list[str]:
    """List models the configured provider reports as available."""
    try:
        client = get_client()
        # Short timeout — this powers a settings dropdown, not a real
        # request. A slow provider shouldn't stall the settings page.
        models = await asyncio.wait_for(client.models.list(), timeout=10.0)
        return [m.id for m in models.data]
    except Exception:
        return []
