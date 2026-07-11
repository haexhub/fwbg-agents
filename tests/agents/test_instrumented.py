"""Tests for the instrumented LLM wrapper (Plan 006 Step 4)."""

from __future__ import annotations

import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from fwbg_agents.agents.instrumented import run_instrumented


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from fwbg_agents import run_events
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    run_events._seq_cache.clear()
    return settings.data_dir


async def test_run_instrumented_writes_transcript_and_events(data_dir):
    agent = Agent(TestModel(), system_prompt="you are a bot")

    @agent.tool_plain
    async def search_web(query: str) -> list[dict]:
        """Search the web."""
        return [{"url": "http://x", "title": "t"}]

    result = await run_instrumented(agent, "go", agent_run_id=42)
    assert result.output is not None

    # Transcript file exists, is parseable JSON, and carries the system prompt +
    # tool call so the LLM session can be reconstructed after the run.
    tpath = data_dir / "agent-runs" / "42" / "transcript_001.json"
    assert tpath.exists()
    transcript = json.loads(tpath.read_text())
    assert isinstance(transcript, list)
    blob = json.dumps(transcript)
    assert "you are a bot" in blob
    assert "search_web" in blob

    # events.jsonl carries the live tool call + result + round-done markers.
    epath = data_dir / "agent-runs" / "42" / "events.jsonl"
    assert epath.exists()
    types = [json.loads(line)["type"] for line in epath.read_text().splitlines() if line.strip()]
    assert "llm_tool_call" in types
    assert "llm_tool_result" in types
    assert "llm_round_done" in types


async def test_run_instrumented_round_idx_names_transcript(data_dir):
    agent = Agent(TestModel(), system_prompt="s")
    await run_instrumented(agent, "go", agent_run_id=7, round_idx=3)
    assert (data_dir / "agent-runs" / "7" / "transcript_003.json").exists()
