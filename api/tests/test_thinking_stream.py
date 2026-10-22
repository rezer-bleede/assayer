"""The live thinking feed: splitter, raw-client reasoning callbacks, WS burst."""

from __future__ import annotations

import asyncio

import pytest

from app.core.llm import _TAG_CLOSE, _TAG_OPEN, _ThinkSplitter, astream
from app.routers.chat import _ThinkingBurst

# --------------------------------------------------------------------------- #
# _ThinkSplitter: inline `<think>` tags routed thought/reply, split-tolerant
# --------------------------------------------------------------------------- #


def run_splitter(chunks: list[str]) -> tuple[list[str], list[str]]:
    thought: list[str] = []
    delta: list[str] = []
    sp = _ThinkSplitter(on_thought=thought.append, on_delta=delta.append)
    for c in chunks:
        sp.add(c)
    sp.flush()
    return thought, delta


def test_splitter_routes_closed_tags() -> None:
    thought, delta = run_splitter([f"{_TAG_OPEN}need to inspect{_TAG_CLOSE}The table looks fine."])
    assert "".join(thought) == "need to inspect"
    assert "".join(delta) == "The table looks fine."


def test_splitter_survives_tags_split_across_chunks() -> None:
    # the open tag is torn across chunk boundaries, a classic small-model case
    thought, delta = run_splitter(
        [_TAG_OPEN[:4], _TAG_OPEN[4:], "weigh 2", "x2 2 = w", "hat", _TAG_CLOSE, "It is 4."]
    )
    assert "".join(thought) == "weigh 2x2 2 = what"
    assert "".join(delta) == "It is 4."


def test_splitter_without_tags_streams_everything_as_reply() -> None:
    thought, delta = run_splitter(["just a ", "plain reply"])
    assert thought == []
    assert "".join(delta) == "just a plain reply"


def test_splitter_unclosed_tag_flushes_to_thought() -> None:
    thought, delta = run_splitter([_TAG_OPEN, "unfinished reasoning"])
    assert "".join(thought) == "unfinished reasoning"
    assert delta == []


def test_splitter_multiple_bursts_alternate() -> None:
    thought, delta = run_splitter([f"{_TAG_OPEN}a{_TAG_CLOSE}b{_TAG_OPEN}c{_TAG_CLOSE}d"])
    assert "".join(thought) == "ac"
    assert "".join(delta) == "bd"


# --------------------------------------------------------------------------- #
# astream: raw-client reasoning fields reach on_thought, reply reaches on_delta
# --------------------------------------------------------------------------- #


class _Delta:
    def __init__(self, reasoning=None, content=None):
        self.reasoning = reasoning
        self.reasoning_content = None
        self.content = content


class _Chunk:
    def __init__(self, delta):
        self.choices = [type("C", (), {"delta": delta})()]


class _Completions:
    def __init__(self, chunks, captured: dict):
        self._chunks = chunks
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.update(kwargs)

        async def gen():
            for c in self._chunks:
                yield c

        return gen()


class _FakeClient:
    def __init__(self, chunks, captured: dict):
        self.chat = type("Chat", (), {"completions": _Completions(chunks, captured)})()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, chunks: list[_Chunk]
) -> tuple[dict, _FakeClient]:
    captured: dict = {}
    client = _FakeClient(chunks, captured)
    monkeypatch.setattr("app.core.llm.raw_async_client", lambda: client)
    return captured, client


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
async def test_astream_reasoning_fields_call_on_thought(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    delta = _Delta(content="Hello.")
    setattr(delta, field, "why: model is healthy")
    captured, client = _patch_client(monkeypatch, [_Chunk(delta)])
    thought: list[str] = []
    got_delta: list[str] = []
    text = await astream("hi", on_delta=got_delta.append, on_thought=thought.append)
    assert "".join(thought) == "why: model is healthy"
    assert "".join(got_delta) == "Hello."
    assert text == "Hello."
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["stream"] is True
    assert client.closed is True


async def test_astream_inline_tags_are_split(monkeypatch: pytest.MonkeyPatch) -> None:
    captured, _ = _patch_client(
        monkeypatch,
        [
            _Chunk(_Delta(content=_TAG_OPEN)),
            _Chunk(_Delta(content="hdmm")),
            _Chunk(_Delta(content=f"{_TAG_CLOSE}4 tokens")),
        ],
    )
    thought: list[str] = []
    assert await astream("hi", on_thought=thought.append) == "4 tokens"
    assert "".join(thought) == "hdmm"


async def test_astream_without_callbacks_still_returns_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(
        monkeypatch,
        [
            _Chunk(_Delta(reasoning="private", content=f"{_TAG_OPEN}x{_TAG_CLOSE}answer")),
        ],
    )
    assert await astream("hi") == "answer"


async def test_astream_passes_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    captured, _ = _patch_client(monkeypatch, [_Chunk(_Delta(content="ok"))])
    await astream("hi", max_tokens=64)
    assert captured["max_tokens"] == 64


# --------------------------------------------------------------------------- #
# astream bounds a thinking model: a stalled or endless stream still returns
# --------------------------------------------------------------------------- #


class _HangingStream:
    """Yields one chunk, then never produces another (a wedged runner)."""

    def __init__(self, first: _Chunk) -> None:
        self._first = first
        self._done = False

    def __aiter__(self) -> _HangingStream:
        return self

    async def __anext__(self) -> _Chunk:
        if not self._done:
            self._done = True
            return self._first
        await asyncio.Event().wait()
        raise StopAsyncIteration  # pragma: no cover - the wait never returns


class _HangingCompletions:
    def __init__(self, stream: _HangingStream) -> None:
        self._stream = stream

    async def create(self, **_: object) -> _HangingStream:
        return self._stream


class _HangingClient:
    def __init__(self, stream: _HangingStream) -> None:
        self.chat = type("Chat", (), {"completions": _HangingCompletions(stream)})()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_astream_ends_on_a_stalled_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_stream_stall_seconds", 0.05)
    monkeypatch.setattr(settings, "llm_timeout_seconds", 60)
    client = _HangingClient(_HangingStream(_Chunk(_Delta(content="partial"))))
    monkeypatch.setattr("app.core.llm.raw_async_client", lambda: client)

    text = await asyncio.wait_for(astream("hi"), timeout=2)
    assert text == "partial"
    assert client.closed is True


async def test_astream_ends_at_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_stream_stall_seconds", 60)
    monkeypatch.setattr(settings, "llm_timeout_seconds", 0)
    client = _HangingClient(_HangingStream(_Chunk(_Delta(content="never read"))))
    monkeypatch.setattr("app.core.llm.raw_async_client", lambda: client)

    assert await asyncio.wait_for(astream("hi"), timeout=2) == ""
    assert client.closed is True


def test_thinking_disabled_only_for_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings
    from app.core.llm import thinking_disabled

    monkeypatch.setattr(settings, "llm_provider", "ollama")
    assert thinking_disabled() == {"reasoning_effort": "none"}
    monkeypatch.setattr(settings, "llm_provider", "openai")
    assert thinking_disabled() == {}


# --------------------------------------------------------------------------- #
# _ThinkingBurst: thought.open on the first token, deltas, close with duration
# --------------------------------------------------------------------------- #


def test_burst_opens_streams_and_closes_under_one_id() -> None:
    from app.routers.chat import EventBuffer

    buf = EventBuffer()
    burst = _ThinkingBurst(buf)
    burst("let me ")
    burst("check the scores")
    burst("resumed")
    mid = buf.drain()  # interim: the pump drains mid-burst; stays under one id
    assert [e["type"] for e in mid] == ["thought.open"] + ["thought.delta"] * 3
    assert {e["data"]["id"] for e in mid} == {burst.id}
    burst.close()
    close = buf.drain()
    assert [e["type"] for e in close] == ["thought.close"]
    assert close[0]["data"]["secs"] >= 0


def test_burst_with_no_reasoning_never_opens() -> None:
    from app.routers.chat import EventBuffer

    buf = EventBuffer()
    burst = _ThinkingBurst(buf)
    burst(" ")
    burst.close()
    assert buf.drain() == []


def test_burst_close_is_idempotent() -> None:
    from app.routers.chat import EventBuffer

    buf = EventBuffer()
    burst = _ThinkingBurst(buf)
    burst("think")
    burst.close()
    burst.close()
    types = [e["type"] for e in buf.drain()]
    assert types.count("thought.close") == 1
