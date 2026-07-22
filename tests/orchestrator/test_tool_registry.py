"""Tests for orchestrator.tool_registry — the MCP bridge's live-closure registry.

Register/get/unregister semantics, double-register (second run's `with`
overwrites, first's teardown must not clobber it), and the per-run lock
serializing concurrent invocations against a shared (non-thread-safe)
resource.
"""

from __future__ import annotations

import asyncio

import pytest

from fwbg_agents.orchestrator import tool_registry


def test_registered_tool_is_reachable_by_name():
    def echo(x: int) -> int:
        return x

    with tool_registry.registered(1, {"echo": echo}):
        fn = tool_registry.get(1, "echo")
        assert fn is echo
        assert fn(5) == 5
        assert tool_registry.get_lock(1) is not None


def test_unregistered_run_or_tool_returns_none():
    assert tool_registry.get(999, "echo") is None
    assert tool_registry.get_lock(999) is None


def test_teardown_on_normal_exit():
    def noop() -> None:
        return None

    with tool_registry.registered(2, {"noop": noop}):
        assert tool_registry.get(2, "noop") is noop
    assert tool_registry.get(2, "noop") is None
    assert tool_registry.get_lock(2) is None


def test_teardown_on_exception():
    def noop() -> None:
        return None

    with pytest.raises(RuntimeError), tool_registry.registered(3, {"noop": noop}):
        assert tool_registry.get(3, "noop") is noop
        raise RuntimeError("boom")
    assert tool_registry.get(3, "noop") is None
    assert tool_registry.get_lock(3) is None


async def test_teardown_on_cancellation():
    def noop() -> None:
        return None

    async def body():
        with tool_registry.registered(4, {"noop": noop}):
            await asyncio.sleep(10)

    task = asyncio.ensure_future(body())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tool_registry.get(4, "noop") is None
    assert tool_registry.get_lock(4) is None


def test_double_register_second_wins_while_active():
    def a():
        return "a"

    def b():
        return "b"

    with tool_registry.registered(5, {"t": a}):
        with tool_registry.registered(5, {"t": b}):
            assert tool_registry.get(5, "t") is b
        # Inner block's teardown removes the run entirely, even though the
        # outer `with` is technically still "active" — matches run_registry's
        # dict-keyed-by-id idiom (no reference counting).
        assert tool_registry.get(5, "t") is None


async def test_lock_serializes_concurrent_invocations():
    """Two concurrent calls against a slow fake closure must not overlap —
    the registry's lock is what a caller (internal_tools.py) is expected to
    hold around each invoke()."""
    order: list[str] = []

    async def slow_closure(tag: str) -> str:
        order.append(f"{tag}-start")
        await asyncio.sleep(0.05)
        order.append(f"{tag}-end")
        return tag

    with tool_registry.registered(6, {"slow": slow_closure}):
        lock = tool_registry.get_lock(6)
        fn = tool_registry.get(6, "slow")

        async def call(tag: str) -> str:
            async with lock:
                return await fn(tag)

        results = await asyncio.gather(call("first"), call("second"))

    assert results == ["first", "second"]
    # Serialized: one call's start+end must be adjacent, never interleaved.
    assert order == ["first-start", "first-end", "second-start", "second-end"]
