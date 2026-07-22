"""Tests for POST /internal/tool-exec/{agent_run_id} — the MCP bridge endpoint.

haex-claude-proxy's bridge-server.js is the real caller in production; here
we drive the endpoint directly over HTTP (like the other api/ tests) with a
fake tool closure registered via tool_registry.
"""

from __future__ import annotations

import asyncio

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from fwbg_agents.main import app
from fwbg_agents.orchestrator import tool_registry


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_returns_503_when_feature_unconfigured(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", None)
    resp = await client.post(
        "/internal/tool-exec/1",
        json={"tool_name": "echo", "args": {}},
        headers={"X-Internal-Tool-Key": "anything"},
    )
    assert resp.status_code == 503


async def test_returns_401_on_wrong_key(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "correct-secret")
    resp = await client.post(
        "/internal/tool-exec/1",
        json={"tool_name": "echo", "args": {}},
        headers={"X-Internal-Tool-Key": "wrong-secret"},
    )
    assert resp.status_code == 401


async def test_returns_401_on_missing_key(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "correct-secret")
    resp = await client.post("/internal/tool-exec/1", json={"tool_name": "echo", "args": {}})
    assert resp.status_code == 401


async def test_returns_404_for_unknown_run(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "sekret")
    resp = await client.post(
        "/internal/tool-exec/424242",
        json={"tool_name": "echo", "args": {}},
        headers={"X-Internal-Tool-Key": "sekret"},
    )
    assert resp.status_code == 404


async def test_returns_404_for_unknown_tool_on_a_registered_run(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "sekret")

    def echo(x: int) -> int:
        return x

    with tool_registry.registered(101, {"echo": echo}):
        resp = await client.post(
            "/internal/tool-exec/101",
            json={"tool_name": "not_registered", "args": {}},
            headers={"X-Internal-Tool-Key": "sekret"},
        )
    assert resp.status_code == 404


async def test_ok_true_on_successful_sync_call(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "sekret")

    def add(a: int, b: int) -> int:
        return a + b

    with tool_registry.registered(102, {"add": add}):
        resp = await client.post(
            "/internal/tool-exec/102",
            json={"tool_name": "add", "args": {"a": 2, "b": 3}},
            headers={"X-Internal-Tool-Key": "sekret"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "result": 5}


async def test_ok_true_on_successful_async_call(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "sekret")

    async def search(query: str) -> list:
        return [{"query": query, "hit": True}]

    with tool_registry.registered(103, {"search": search}):
        resp = await client.post(
            "/internal/tool-exec/103",
            json={"tool_name": "search", "args": {"query": "rsi"}},
            headers={"X-Internal-Tool-Key": "sekret"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["result"] == [{"query": "rsi", "hit": True}]


async def test_ok_false_when_closure_raises(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "sekret")

    def boom() -> None:
        raise ValueError("bad input")

    with tool_registry.registered(104, {"boom": boom}):
        resp = await client.post(
            "/internal/tool-exec/104",
            json={"tool_name": "boom", "args": {}},
            headers={"X-Internal-Tool-Key": "sekret"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "bad input" in body["error"]


async def test_ok_false_when_closure_exceeds_timeout(client, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "sekret")
    monkeypatch.setattr(settings, "internal_tool_exec_timeout_seconds", 0.05)

    async def slow() -> str:
        await asyncio.sleep(5)
        return "too late"

    with tool_registry.registered(105, {"slow": slow}):
        resp = await client.post(
            "/internal/tool-exec/105",
            json={"tool_name": "slow", "args": {}},
            headers={"X-Internal-Tool-Key": "sekret"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False


async def test_emits_llm_tool_call_and_result_events(client, monkeypatch, tmp_path):
    from fwbg_agents import run_events
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "internal_tool_exec_key", "sekret")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    run_events._seq_cache.clear()

    def echo(x: int) -> int:
        return x

    with tool_registry.registered(106, {"echo": echo}):
        resp = await client.post(
            "/internal/tool-exec/106",
            json={"tool_name": "echo", "args": {"x": 7}},
            headers={"X-Internal-Tool-Key": "sekret"},
        )
    assert resp.status_code == 200

    events = run_events.read_run_events(106)
    types = [e["type"] for e in events]
    assert "llm_tool_call" in types
    assert "llm_tool_result" in types
    call_ev = next(e for e in events if e["type"] == "llm_tool_call")
    assert call_ev["tool_name"] == "echo"
