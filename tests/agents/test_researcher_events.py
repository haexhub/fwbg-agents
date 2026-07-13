"""Researcher timeline events land in events.jsonl (Plan 006 Step 5).

Drives a real Researcher.run with a non-streaming FunctionModel that calls
search_web then emits a hypothesis, and asserts the persisted timeline carries
the researcher-specific events the dashboard renders (search query, result URLs,
hypothesis) plus the generic lifecycle + LLM events.
"""

from __future__ import annotations

import json

import pytest_asyncio
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.researcher import Researcher, ResearcherInput
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import AgentRun
from fwbg_agents.tools.search.base import SearchResult

_HYP_ARGS = {
    "title": "ORB on FOREX majors",
    "asset_class": "FOREX",
    "strategy_family": "ORB",
    "edge_mechanism": "London-open range breakouts ride early-session liquidity momentum",
    "hypothesis": "Opening range breakouts on EURUSD M15 produce a momentum edge.",
    "expected_edge_explanation": "Early London liquidity creates session persistence.",
    "key_indicators": ["opening_range", "atr"],
    "tags": ["orb", "intraday", "forex_majors"],
    "sources": [
        {
            "url": "https://example.com/orb",
            "title": "ORB note",
            "why_relevant": "documents the London-open ORB effect",
        }
    ],
    "suggested_universe": [
        {"scope": "asset_class", "value": "FOREX", "rationale": "majors"},
    ],
    "differentiates_from": [],
}

_AVAILABLE_PLUGINS = {
    "indicators": [{"name": "opening_range", "description": "Opening range levels"}],
    "preprocessing": [],
    "feature_selection": [],
    "data_loading": [],
    "extra_filters": [],
    "exit_strategies": [],
    "models": [],
}


def _model() -> FunctionModel:
    """First model turn calls search_web; second emits the hypothesis."""

    def handler(messages, info: AgentInfo) -> ModelResponse:
        searched = any(
            getattr(p, "part_kind", None) == "tool-return"
            for m in messages
            for p in getattr(m, "parts", [])
        )
        if not searched:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="search_web_tool", args={"query": "orb forex"})]
            )
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=_HYP_ARGS)])

    return FunctionModel(handler)


class _FakeSearch:
    async def search(self, query, *, session=None, agent_run_id=None):
        return [
            SearchResult(
                url="https://example.com/orb",
                title="ORB note",
                content_snippet="London open ORB edge",
                score=0.9,
            )
        ]


@pytest_asyncio.fixture
async def session(tmp_path, monkeypatch):
    from fwbg_agents import run_events
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    run_events._seq_cache.clear()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def test_researcher_emits_timeline_events(session, tmp_path):
    researcher = Researcher(
        session,
        model=_model(),
        search_client=_FakeSearch(),
        available_plugins=_AVAILABLE_PLUGINS,
    )
    await researcher.run(ResearcherInput(asset_class="FOREX"))

    ar = (await session.execute(select(AgentRun))).scalar_one()
    events_file = tmp_path / "data" / "agent-runs" / str(ar.id) / "events.jsonl"
    assert events_file.exists()
    events = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
    types = [e["type"] for e in events]

    assert "agent_run_started" in types
    assert "research_search" in types
    assert "research_results" in types
    assert "llm_tool_call" in types
    assert "hypothesis_ready" in types
    assert "llm_round_done" in types
    assert "agent_run_done" in types

    search_ev = next(e for e in events if e["type"] == "research_search")
    assert search_ev["query"] == "orb forex"
    hyp_ev = next(e for e in events if e["type"] == "hypothesis_ready")
    assert hyp_ev["strategy_family"] == "ORB"
    assert hyp_ev["asset_class"] == "FOREX"
