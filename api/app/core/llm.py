"""LLM access: any OpenAI-compatible endpoint (OpenAI, Azure, Ollama, vLLM, LiteLLM)."""

from __future__ import annotations

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


async def ainvoke(prompt: Any, **kwargs: Any) -> str:
    """Invoke the configured chat model and return plain text."""
    resp = await build_llm().ainvoke(prompt, **kwargs)
    content = getattr(resp, "content", resp)
    if isinstance(content, list):  # some providers return content blocks
        content = "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return (content or "").strip()