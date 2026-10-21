from __future__ import annotations

import asyncio

import pytest

from app.routers.chat import EventBuffer, _source_question, _StreamedReply
from app.schemas import SourceRequirement, SourceType


def test_event_buffer_preserves_emission_order() -> None:
    buf = EventBuffer()
    buf("a", {"i": 1})
    buf("b", {"i": 2})
    buf("c", {"i": 3})
    assert [e["type"] for e in buf.drain()] == ["a", "b", "c"]


def test_drain_empties_the_buffer() -> None:
    buf = EventBuffer()
    buf("a", {})
    assert len(buf.drain()) == 1
    assert buf.drain() == []


def test_emit_alias_is_callable() -> None:
    buf = EventBuffer()
    buf.emit("x", {"k": "v"})
    assert buf.drain()[0]["data"] == {"k": "v"}


async def test_flush_sends_in_order() -> None:
    sent: list[tuple[str, dict]] = []

    async def send(event: str, data: dict) -> None:
        sent.append((event, data))

    buf = EventBuffer()
    for i in range(5):
        buf(f"e{i}", {"i": i})
    await buf.flush(send)
    assert [e for e, _ in sent] == [f"e{i}" for i in range(5)]


async def test_flush_never_loses_events_added_before_the_send() -> None:
    """Regression: fire-and-forget tasks reordered events across turns."""
    sent: list[str] = []

    async def send(event: str, data: dict) -> None:
        await asyncio.sleep(0)
        sent.append(event)

    buf = EventBuffer()
    buf("early", {})
    pump = asyncio.create_task(buf.pump_forever(send))
    for i in range(3):
        buf(f"during{i}", {})
        await asyncio.sleep(0.06)
    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump
    await buf.flush(send)

    assert sent[0] == "early"
    assert all(f"during{i}" in sent for i in range(3))


async def test_pump_terminates_and_flushes_on_cancel() -> None:
    sent: list[str] = []

    async def send(event: str, data: dict) -> None:
        sent.append(event)

    buf = EventBuffer()
    buf("before", {})
    pump = asyncio.create_task(buf.pump_forever(send, interval=0.01))
    await asyncio.sleep(0.05)
    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump
    assert "before" in sent


# --------------------------------------------------------------------------- #
# _source_question: the "I expect:" prefill must carry evidence, never defaults
# --------------------------------------------------------------------------- #

_REQ = SourceRequirement(key="db:raw", tables=["raw.orders"])


def test_source_question_prefers_declared_engine_hint() -> None:
    q = _source_question(
        _REQ.model_copy(update={"engine_hint": SourceType.CLICKHOUSE}),
        1,
        1,
        raw_code="USE postgres; -- misleading comment",
    )
    assert q.options[0].prefill["type"] == "clickhouse"


def test_source_question_falls_back_to_a_single_engine_word() -> None:
    q = _source_question(_REQ, 1, 1, raw_code="import clickhouse_connect\nget_client(host='h')")
    assert q.options[0].prefill["type"] == "clickhouse"


def test_source_question_silence_prefills_no_type_at_all() -> None:
    """No engine evidence anywhere: nothing fabricated, certainly not postgres."""
    q = _source_question(_REQ, 1, 1, raw_code="SELECT * FROM raw.orders")
    assert "type" not in q.options[0].prefill


def test_source_question_ambiguous_words_prefill_nothing() -> None:
    q = _source_question(_REQ, 1, 1, raw_code="moved data postgres -> clickhouse last week")
    assert "type" not in q.options[0].prefill


# --------------------------------------------------------------------------- #
# streamed replies: message.start once, then deltas, all under one id
# --------------------------------------------------------------------------- #


def test_streamed_reply_opens_then_streams_deltas_under_one_id() -> None:
    buf = EventBuffer()
    stream = _StreamedReply(buf)
    stream("first ")
    stream("chunk")
    events = buf.drain()
    assert events[0]["type"] == "message.start"
    assert events[0]["data"]["role"] == "assistant"
    assert events[0]["data"]["id"] == stream.id
    assert events[1]["type"] == "message.delta"
    assert events[1]["data"] == {"id": stream.id, "text": "first "}
    assert events[2]["data"] == {"id": stream.id, "text": "chunk"}


def test_streamed_reply_before_first_delta_emits_nothing() -> None:
    stream = _StreamedReply(emit=lambda *_: None)
    assert stream.opened is False


def test_streamed_reply_start_emitted_only_for_the_first_delta() -> None:
    buf = EventBuffer()
    stream = _StreamedReply(buf)
    stream("a")
    buf.drain()
    stream("b")
    types = [e["type"] for e in buf.drain()]
    assert "message.start" not in types


# --------------------------------------------------------------------------- #
# thoughts: the agent narrates what it is doing at the slow points
# --------------------------------------------------------------------------- #


def _spec(name: str = "s", host: str | None = None):
    from app.schemas import ConnectionSpec

    return ConnectionSpec.model_validate({"type": "postgres", "name": name, "host": host})


def test_introspect_all_announces_each_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import orchestrator as orch

    monkeypatch.setattr(
        orch,
        "introspect_source",
        lambda _spec, max_tables=40: {"ok": True, "tables": [{"name": "orders"}]},
    )
    buf = EventBuffer()
    orch.introspect_all({"c1": _spec(name="prod-db", host="prod-db.internal")}, emit=buf)
    texts = [e["data"].get("text") for e in buf.drain() if e["type"] == "thought"]
    assert "Connecting to prod-db.internal…" in texts
    assert "Read prod-db — 1 tables found." in texts


# --------------------------------------------------------------------------- #
# report streaming: on_delta reaches the model layer
# --------------------------------------------------------------------------- #


async def test_build_report_streams_llm_tokens_to_on_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.tools import report as report_mod
    from app.schemas import EtlAnalysis, RunResult, RunStatus

    captured: dict = {}

    async def fake_astream(_prompt, max_tokens=None, on_delta=None, **_kw):
        captured["on_delta"] = on_delta
        chunks = ["All good — ", "the pipeline is healthy."]
        if on_delta:
            for c in chunks:
                on_delta(c)
        return "".join(chunks)

    monkeypatch.setattr(report_mod, "llm_available", lambda: True)
    monkeypatch.setattr(report_mod, "astream", fake_astream)
    run_obj = RunResult(id="r1", status=RunStatus.COMPLETED)
    analysis = EtlAnalysis(language="sql")
    got: list[str] = []
    text = await report_mod.build_report(run_obj, analysis, on_delta=got.append)
    assert captured["on_delta"] == got.append
    assert got == ["All good — ", "the pipeline is healthy."]
    assert text == "All good — the pipeline is healthy."


async def test_build_report_fallback_leaves_stream_unopened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No LLM configured: the deterministic narrative is returned, no deltas."""
    from app.agent.tools import report as report_mod
    from app.schemas import RunResult, RunStatus

    seen: list[str] = []
    monkeypatch.setattr(report_mod, "llm_available", lambda: False)
    run_obj = RunResult(id="r1", status=RunStatus.COMPLETED)
    text = await report_mod.build_report(run_obj, None, on_delta=seen.append)
    assert "no quality" in text.lower() or "verdict" in text.lower()
    assert seen == []
