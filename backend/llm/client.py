"""
LM Studio client — all LLM interactions go through here.
"""
from openai import AsyncOpenAI
from backend.config import LM_STUDIO_URL, LM_STUDIO_API_KEY

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=LM_STUDIO_URL,
            api_key=LM_STUDIO_API_KEY,
        )
    return _client


class LLMUnavailableError(Exception):
    pass


async def chat(prompt: str, system: str = "", model: str = "local-model") -> str:
    """Single-turn chat. Raises LLMUnavailableError if LM Studio is unreachable."""
    import httpx
    client = get_client()
    messages = []
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
    except (httpx.ConnectError, httpx.TimeoutException, Exception) as e:
        if "connect" in str(e).lower() or "connection" in str(e).lower():
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
