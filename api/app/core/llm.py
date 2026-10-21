"""LLM access: any OpenAI-compatible endpoint (OpenAI, Azure, Ollama, vLLM, LiteLLM)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.core.logging import log


def llm_available() -> bool:
    return settings.llm_configured


def build_llm(temperature: float | None = None, model: str | None = None) -> Any:
    """Return a ChatOpenAI instance configured for the selected provider."""
    if not llm_available():
        raise RuntimeError(
            "No LLM configured. Set LLM_API_KEY (and optionally LLM_BASE_URL) in .env, "
            "or switch LLM_PROVIDER=ollama to use the local Ollama service."
        )

    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": model or settings.llm_model,
        "temperature": settings.llm_temperature if temperature is None else temperature,
        "api_key": settings.llm_api_key or "ollama",
        "base_url": settings.llm_base_url,
        "max_retries": settings.llm_max_retries,
        "timeout": settings.llm_timeout_seconds,
    }
    if settings.llm_provider.lower() == "ollama":
        kwargs["model"] = model or settings.llm_model
    log.info("llm ready", provider=settings.llm_provider, model=kwargs["model"])
    return ChatOpenAI(**kwargs)


def _content_text(content: Any) -> str:
    """Normalize a message/chunk payload to plain text."""
    if isinstance(content, list):  # some providers return content blocks
        content = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return content or ""


async def astream(
    prompt: Any,
    max_tokens: int | None = None,
    on_delta: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> str:
    """Stream the configured chat model, returning the full text.

    Behaves exactly like `ainvoke`, but each chunk also passes through the
    synchronous `on_delta` callback as it arrives — WS emitters use it to
    stream tokens into the chat while generation is still running.
    """
    model = build_llm()
    if max_tokens:
        model = model.bind(max_tokens=max_tokens)
    parts: list[str] = []
    async for chunk in model.astream(prompt, **kwargs):
        text = _content_text(getattr(chunk, "content", chunk))
        if not text:
            continue
        parts.append(text)
        if on_delta:
            try:
                on_delta(text)
            except Exception:  # noqa: BLE001
                log.exception("llm delta callback failed")
    return "".join(parts).strip()


async def ainvoke(prompt: Any, max_tokens: int | None = None, **kwargs: Any) -> str:
    """Invoke the configured chat model and return plain text.

    `max_tokens` bounds the generation. Always pass it for prose: a small
    local model can fall into a loop and emit thousands of tokens, which the
    deadline then aborts — wasted time either way.
    """
    return await astream(prompt, max_tokens=max_tokens, **kwargs)
