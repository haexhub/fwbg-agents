"""Researcher agent tests (M4).

The Researcher is the second LLM consumer in fwbg-agents (after Analyst).
We exercise the agent via pydantic-ai's FunctionModel which replays canned
tool calls + a final structured ResearcherHypothesis. No real LLM, no real
Tavily.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.researcher import (
    Researcher,
    ResearcherFailed,
    ResearcherInput,
)
from fwbg_agents.orchestrator.hypotheses import ResearcherHypothesis
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Strategy,
    StrategyState,
    StrategyTag,
)
from fwbg_agents.tools.web_search import TavilyClient


def _hyp_args(**over):
    base = dict(
        title="Mean-reversion on FOREX majors during London open",
        asset_class="FOREX",
        strategy_family="RSI_meanrev",
        hypothesis="During the London session, EUR/USD mean-reverts after 1-bar momentum spikes filtered by RSI extremes.",
        expected_edge_explanation="Liquidity-driven overreactions in the first 30 minutes of London revert as US algos take over.",
        key_indicators=["rsi", "atr", "session_clock"],
        tags=["mean_reversion", "intraday", "forex_majors", "session_filter"],
        sources=[
            {"url": "https://example.com/a", "title": "Mean reversion in FX",
             "why_relevant": "documents the London-open effect on EUR/USD"},
        ],
        differentiates_from=[],
    )
    base.update(over)
    return base


def _final_only_handler(hyp_args):
    """FunctionModel handler that immediately emits the final hypothesis."""

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("final_result", hyp_args)])

    return handler


def _lookup_then_final_handler(lookup_args, hyp_args):
    """First turn: call lookup_prior_art. Second turn: emit final hypothesis."""

    def handler(messages: list[ModelRequest], _info: AgentInfo) -> ModelResponse:
        seen_tool_return = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "lookup_prior_art_tool"
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if not seen_tool_return:
            return ModelResponse(
                parts=[ToolCallPart("lookup_prior_art_tool", lookup_args)]
            )
        return ModelResponse(parts=[ToolCallPart("final_result", hyp_args)])

    return handler


def _mock_transport(payload):
    async def handler(_req):
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed_prior_strategy(session, slug, family, asset_class, tags):
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=StrategyState.ABANDONED.value,
        asset_class=asset_class,
        strategy_family=family,
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.flush()
    for t in tags:
        session.add(StrategyTag(strategy_id=s.id, tag=t))
    await session.commit()


@pytest.mark.asyncio
async def test_happy_path_no_prior_art(db):
    model = FunctionModel(_final_only_handler(_hyp_args()))
    researcher = Researcher(db, model=model, tavily=None)
    result = await researcher.run(
        ResearcherInput(asset_class="FOREX", strategy_family_hint="RSI_meanrev")
    )

    assert isinstance(result, ResearcherHypothesis)
    assert result.strategy_family == "RSI_meanrev"

    runs = (await db.execute(select(AgentRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == AgentRunStatus.DONE.value
    assert runs[0].agent_name == "researcher"

    calls = (await db.execute(select(LlmCall))).scalars().all()
    assert any(c.model != "tavily-search" for c in calls)  # at least one LLM call recorded


@pytest.mark.asyncio
async def test_hypothesis_rejected_when_prior_art_and_no_differentiates_from(db):
    await _seed_prior_strategy(
        db, "rsimeanrev__forex__001", "RSI_meanrev", "FOREX",
        ["mean_reversion", "intraday", "forex_majors"],
    )

    model = FunctionModel(
        _lookup_then_final_handler(
            {"strategy_family": "RSI_meanrev", "asset_class": "FOREX",
             "tags": ["mean_reversion", "intraday", "forex_majors"]},
            _hyp_args(),  # differentiates_from=[]
        )
    )
    researcher = Researcher(db, model=model, tavily=None)
    with pytest.raises(ResearcherFailed):
        await researcher.run(
            ResearcherInput(asset_class="FOREX", strategy_family_hint="RSI_meanrev")
        )

    runs = (await db.execute(select(AgentRun))).scalars().all()
    assert runs[0].status == AgentRunStatus.FAILED.value
    assert "differentiates_from" in (runs[0].error or "")


@pytest.mark.asyncio
async def test_hypothesis_accepted_when_differentiates_from_covers_prior_art(db):
    await _seed_prior_strategy(
        db, "rsimeanrev__forex__001", "RSI_meanrev", "FOREX",
        ["mean_reversion", "intraday", "forex_majors"],
    )

    model = FunctionModel(
        _lookup_then_final_handler(
            {"strategy_family": "RSI_meanrev", "asset_class": "FOREX",
             "tags": ["mean_reversion", "intraday", "forex_majors"]},
            _hyp_args(differentiates_from=["rsimeanrev__forex__001"]),
        )
    )
    researcher = Researcher(db, model=model, tavily=None)
    result = await researcher.run(
        ResearcherInput(asset_class="FOREX", strategy_family_hint="RSI_meanrev")
    )
    assert "rsimeanrev__forex__001" in result.differentiates_from


@pytest.mark.asyncio
async def test_search_web_with_tavily_unset_returns_empty(db):
    """When TAVILY_API_KEY is unset, the search_web tool returns [] rather than crashing."""

    def handler(messages, _info: AgentInfo) -> ModelResponse:
        seen_search = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "search_web_tool"
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if not seen_search:
            return ModelResponse(parts=[ToolCallPart("search_web_tool", {"query": "RSI FX"})])
        return ModelResponse(parts=[ToolCallPart("final_result", _hyp_args())])

    model = FunctionModel(handler)
    researcher = Researcher(db, model=model, tavily=None)
    result = await researcher.run(
        ResearcherInput(asset_class="FOREX", strategy_family_hint="RSI_meanrev")
    )
    assert isinstance(result, ResearcherHypothesis)


@pytest.mark.asyncio
async def test_search_web_with_tavily_set_logs_tavily_quota(db):
    """Verify Tavily quota row is logged when the tool actually fires."""
    tavily_payload = {
        "results": [{"url": "https://x", "title": "X", "content": "snippet", "score": 0.9}]
    }
    tavily = TavilyClient(
        api_key="k",
        http=httpx.AsyncClient(transport=_mock_transport(tavily_payload)),
    )

    def handler(messages, _info: AgentInfo) -> ModelResponse:
        seen_search = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "search_web_tool"
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if not seen_search:
            return ModelResponse(parts=[ToolCallPart("search_web_tool", {"query": "RSI FX"})])
        return ModelResponse(parts=[ToolCallPart("final_result", _hyp_args())])

    researcher = Researcher(db, model=FunctionModel(handler), tavily=tavily)
    await researcher.run(ResearcherInput(asset_class="FOREX"))

    tavily_rows = (
        await db.execute(select(LlmCall).where(LlmCall.model == "tavily-search"))
    ).scalars().all()
    assert len(tavily_rows) == 1
