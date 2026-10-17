from __future__ import annotations

import asyncio

import pytest

from app.routers.chat import EventBuffer


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