"""Tests for the per-run timeline event module (Plan 006 Step 1)."""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def run_events(tmp_path, monkeypatch):
    """Point the event store at a tmp data dir and reset the seq cache."""
    from fwbg_agents import run_events as re
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    re._seq_cache.clear()
    return re


def test_emit_appends_jsonl_line(run_events):
    run_events.emit_run_event(1, "research_search", query="orb forex")

    path = run_events.run_dir(1) / "events.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    evt = json.loads(lines[0])
    assert evt["seq"] == 0
    assert evt["type"] == "research_search"
    assert evt["query"] == "orb forex"
    assert "ts" in evt


def test_read_run_events_roundtrip_in_seq_order(run_events):
    run_events.emit_run_event(7, "agent_run_started", agent_name="researcher")
    run_events.emit_run_event(7, "research_search", query="q1")

    got = run_events.read_run_events(7)
    assert [e["seq"] for e in got] == [0, 1]
    assert [e["type"] for e in got] == ["agent_run_started", "research_search"]
    assert got[1]["query"] == "q1"


def test_read_run_events_missing_run_returns_empty(run_events):
    assert run_events.read_run_events(999) == []


async def test_emit_broadcasts_on_sse_bus(run_events):
    from fwbg_agents import events as event_bus

    agen = event_bus.subscribe()
    # Prime the generator so it registers its queue before we emit.
    task = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)

    run_events.emit_run_event(3, "llm_tool_call", tool_name="search_web")

    evt = await asyncio.wait_for(task, timeout=1.0)
    assert evt["type"] == "llm_tool_call"
    assert evt["agent_run_id"] == 3
    assert evt["tool_name"] == "search_web"
    assert "seq" in evt
    await agen.aclose()
