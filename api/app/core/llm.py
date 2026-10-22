"""LLM access: any OpenAI-compatible endpoint (OpenAI, Azure, Ollama, vLLM, LiteLLM)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.core.logging import log

ThoughtCallback = Callable[[str], None]
DeltaCallback = Callable[[str], None]


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


def raw_async_client() -> Any:
    """The underlying openai SDK client, without LangChain's chunk rewriting.

    `_convert_delta_to_message_chunk` in langchain_openai drops
    `delta.reasoning`, so thinking tokens are only observable on the raw
    streaming chunks this client returns.
    """
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "ollama",
        timeout=float(settings.llm_timeout_seconds),
        max_retries=settings.llm_max_retries,
    )


def thinking_disabled() -> dict[str, Any]:
    """Request fields that switch a thinking model's reasoning off.

    Structured callers (JSON extraction) should spend their whole budget on the
    answer, not on a monologue. Ollama maps `reasoning_effort` to its think
    levels and accepts "none" for models without thinking too; other providers
    get nothing extra.
    """
    if settings.llm_provider.lower() == "ollama":
        return {"reasoning_effort": "none"}
    return {}


def _content_text(content: Any) -> str:
    """Normalize a message/chunk payload to plain text."""
    if isinstance(content, list):  # some providers return content blocks
        content = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return content or ""


_TAG_OPEN = "<think>"
_TAG_CLOSE = "</think>"


class _ThinkSplitter:
    """Steer an inline token stream into reasoning vs reply.

    Providers surface thinking two ways: a dedicated field on the stream
    delta (`delta.reasoning` / `delta.reasoning_content`), or thinking text
    inlined in the content wrapped in `<think>` tags. The inline path needs a
    state machine: the tags arrive split across chunk boundaries all the
    time, so text is held back until the last few characters can no longer
    be the start of the pending tag.
    """

    def __init__(
        self,
        on_thought: ThoughtCallback | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> None:
        self.on_thought = on_thought or (lambda _text: None)
        self.on_delta = on_delta or (lambda _text: None)
        # content starts as reply; reasoning only becomes the active sink once
        # an inline `<think>` tag is actually seen (providers that send reasoning
        # in a dedicated field never route through here at all)
        self.thinking = False
        self._saw_tag = False
        self._buf = ""

    @staticmethod
    def _hold_len(pending: str, tag: str) -> int:
        """Length of `pending`'s suffix that is itself a prefix of `tag`."""
        for k in range(min(len(pending), len(tag) - 1), 0, -1):
            if pending.endswith(tag[:k]):
                return k
        return 0

    def _emit(self, text: str) -> None:
        if not text:
            return
        if self.thinking:
            self.on_thought(text)
        else:
            self.on_delta(text)

    def _drain(self, final: bool) -> None:
        tag = _TAG_CLOSE if self.thinking else _TAG_OPEN
        while True:
            at = self._buf.find(tag)
            if at >= 0:
                self._emit(self._buf[:at])
                self._buf = self._buf[at + len(tag) :]
                self.thinking = not self.thinking
                self._saw_tag = True
                tag = _TAG_CLOSE if self.thinking else _TAG_OPEN
                continue
            if final:
                break
            hold = self._hold_len(self._buf, tag)
            emit, self._buf = self._buf[: len(self._buf) - hold], self._buf[len(self._buf) - hold :]
            self._emit(emit)
            break

    def add(self, text: str) -> None:
        if not text:
            return
        self._buf += text
        self._drain(final=False)

    def flush(self) -> None:
        """Emit whatever is still held to the currently active sink."""
        if not self._buf:
            return
        self._drain(final=True)


def _as_messages(prompt: Any) -> list[dict[str, Any]]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    return prompt


async def astream(
    prompt: Any,
    max_tokens: int | None = None,
    on_delta: DeltaCallback | None = None,
    on_thought: ThoughtCallback | None = None,
    **kwargs: Any,
) -> str:
    """Stream the configured chat model, returning the full reply text.

    Thinking tokens never enter the returned text: they surface only through
    the synchronous `on_thought` callback, while the reply passes through
    `on_delta` as it is generated — WS emitters use both to build the live
    "thinking" feed and the streaming bubble. Behaves like `ainvoke` apart
    from the callbacks.
    """
    parts: list[str] = []

    def content_delta(text: str) -> None:
        parts.append(text)
        if on_delta:
            on_delta(text)

    splitter = _ThinkSplitter(on_thought=on_thought, on_delta=content_delta)
    inline_thought = on_thought is not None

    body: dict[str, Any] = {
        "temperature": settings.llm_temperature,
        **kwargs,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens

    client = raw_async_client()
    try:
        stream = await client.chat.completions.create(
            model=settings.llm_model,
            messages=_as_messages(prompt),
            stream=True,
            **body,
        )
        # A thinking model can trickle reasoning for a very long time; a runner
        # that stops mid-stream never sends a terminal chunk. Both are bounded
        # here so a turn always ends and the caller can fall back.
        deadline = time.monotonic() + settings.llm_timeout_seconds
        iterator = stream.__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "llm call hit the wall clock, ending stream",
                    seconds=settings.llm_timeout_seconds,
                )
                break
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=min(float(settings.llm_stream_stall_seconds), remaining),
                )
            except StopAsyncIteration:
                break
            except TimeoutError:
                log.warning(
                    "llm stream stalled, ending stream",
                    seconds=settings.llm_stream_stall_seconds,
                )
                break
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            if delta is None:
                continue
            reasoning = getattr(delta, "reasoning", None) or getattr(
                delta, "reasoning_content", None
            )
            if reasoning and inline_thought:
                on_thought(reasoning)  # type: ignore[misc]
            content = _content_text(getattr(delta, "content", None))
            if content:
                splitter.add(content)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - cleanup must never mask the result
            log.debug("llm client close failed")
    splitter.flush()
    return "".join(parts).strip()


async def ainvoke(
    prompt: Any,
    max_tokens: int | None = None,
    on_thought: ThoughtCallback | None = None,
    on_delta: DeltaCallback | None = None,
    **kwargs: Any,
) -> str:
    """Invoke the configured chat model and return plain reply text.

    `max_tokens` bounds the generation. Always pass it for prose: a small
    local model can fall into a loop and emit thousands of tokens, which the
    deadline then aborts — wasted time either way. Thinking tokens are
    consumed and discarded, so JSON callers never see think-tag wrappers.
    """
    return await astream(
        prompt,
        max_tokens=max_tokens,
        on_thought=on_thought,
        on_delta=on_delta,
        **kwargs,
    )
