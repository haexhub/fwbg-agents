"""Tavily web-search client + quota tracking (M4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import AgentRun, AgentRunStatus, LlmCall
from fwbg_agents.tools.search import SearchResult, SearchUnavailableError, TavilyClient
from fwbg_agents.tools.search.tavily import get_quota_usage


def _mock_transport(payload, status=200):
    async def handler(request):
        return httpx.Response(status, json=payload)

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


async def _new_agent_run(session):
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="researcher",
        status=AgentRunStatus.RUNNING.value,
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)
    return ar


@pytest.mark.asyncio
async def test_search_parses_results():
    payload = {
        "results": [
            {"url": "https://x", "title": "X", "content": "snippet", "score": 0.9},
            {"url": "https://y", "title": "Y", "content": "other", "score": 0.5},
        ]
    }
    http = httpx.AsyncClient(transport=_mock_transport(payload))
    client = TavilyClient(api_key="k", http=http)
    results = await client.search("query")
    assert results == [
        SearchResult(url="https://x", title="X", content_snippet="snippet", score=0.9),
        SearchResult(url="https://y", title="Y", content_snippet="other", score=0.5),
    ]


@pytest.mark.asyncio
async def test_search_raises_when_api_key_missing():
    client = TavilyClient(api_key=None)
    with pytest.raises(SearchUnavailableError):
        await client.search("q")


@pytest.mark.asyncio
async def test_search_raises_on_http_error():
    http = httpx.AsyncClient(transport=_mock_transport({"error": "rate-limited"}, status=429))
    client = TavilyClient(api_key="k", http=http)
    with pytest.raises(httpx.HTTPStatusError):
        await client.search("q")


@pytest.mark.asyncio
async def test_search_logs_llm_call_for_quota(db):
    ar = await _new_agent_run(db)
    http = httpx.AsyncClient(transport=_mock_transport({"results": []}))
    client = TavilyClient(api_key="k", http=http)
    await client.search("q", session=db, agent_run_id=ar.id)

    rows = (
        await db.execute(select(LlmCall).where(LlmCall.model == "tavily-search"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].agent_run_id == ar.id
    assert rows[0].latency_ms is not None and rows[0].latency_ms >= 0


@pytest.mark.asyncio
async def test_search_does_not_log_without_session_and_agent_run(db):
    http = httpx.AsyncClient(transport=_mock_transport({"results": []}))
    client = TavilyClient(api_key="k", http=http)
    await client.search("q")
    rows = (
        await db.execute(select(LlmCall).where(LlmCall.model == "tavily-search"))
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_search_omits_results_with_missing_fields():
    payload = {
        "results": [
            {"url": "https://x", "title": "X", "content": "good", "score": 0.9},
            {"url": "https://y"},  # missing fields — must be dropped, not crash
        ]
    }
    http = httpx.AsyncClient(transport=_mock_transport(payload))
    client = TavilyClient(api_key="k", http=http)
    results = await client.search("q")
    assert [r.url for r in results] == ["https://x"]


@pytest.mark.asyncio
async def test_get_quota_usage_counts_recent_rows(db):
    ar = await _new_agent_run(db)
    now = datetime.now(UTC)
    for _i in range(3):
        db.add(
            LlmCall(
                agent_run_id=ar.id,
                model="tavily-search",
                input_tokens=0,
                output_tokens=0,
                created_at=now,
            )
        )
    await db.commit()
    assert await get_quota_usage(db) == 3


@pytest.mark.asyncio
async def test_get_quota_usage_excludes_rows_outside_window(db):
    ar = await _new_agent_run(db)
    now = datetime.now(UTC)

    fresh = LlmCall(
        agent_run_id=ar.id, model="tavily-search",
        input_tokens=0, output_tokens=0, created_at=now,
    )
    stale = LlmCall(
        agent_run_id=ar.id, model="tavily-search",
        input_tokens=0, output_tokens=0, created_at=now,
    )
    db.add_all([fresh, stale])
    await db.commit()
    await db.refresh(stale)

    # back-date the stale row past the 30-day window
    await db.execute(
        update(LlmCall)
        .where(LlmCall.id == stale.id)
        .values(created_at=now - timedelta(days=45))
    )
    await db.commit()

    assert await get_quota_usage(db, window_days=30) == 1


@pytest.mark.asyncio
async def test_get_quota_usage_excludes_other_models(db):
    ar = await _new_agent_run(db)
    now = datetime.now(UTC)
    db.add(
        LlmCall(agent_run_id=ar.id, model="claude-opus-4-7",
                input_tokens=10, output_tokens=20, created_at=now)
    )
    db.add(
        LlmCall(agent_run_id=ar.id, model="tavily-search",
                input_tokens=0, output_tokens=0, created_at=now)
    )
    await db.commit()
    assert await get_quota_usage(db) == 1
