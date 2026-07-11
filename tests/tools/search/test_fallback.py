"""FallbackSearchClient tests (M4b)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import AgentRun, AgentRunStatus, LlmCall
from fwbg_agents.tools.search import BraveClient, FallbackSearchClient, SearchUnavailableError
from fwbg_agents.tools.search.tavily import TavilyClient


class _StubProvider:
    def __init__(self, name, *, results=None, exc=None):
        self.name = name
        self._results = results or []
        self._exc = exc
        self.calls = 0

    async def search(self, query, *, max_results=5, session=None, agent_run_id=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._results


def _http_status_error(status=429):
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


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
async def test_fallback_uses_primary_when_healthy():
    primary = _StubProvider("primary", results=[])
    secondary = _StubProvider("secondary", results=[])
    client = FallbackSearchClient([primary, secondary])

    await client.search("q")

    assert primary.calls == 1
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_fallback_falls_back_on_search_unavailable():
    primary = _StubProvider("primary", exc=SearchUnavailableError("no key"))
    secondary = _StubProvider("secondary", results=[])
    client = FallbackSearchClient([primary, secondary])

    await client.search("q")

    assert primary.calls == 1
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_fallback_falls_back_on_http_429():
    primary = _StubProvider("primary", exc=_http_status_error(429))
    secondary = _StubProvider("secondary", results=[])
    client = FallbackSearchClient([primary, secondary])

    await client.search("q")

    assert primary.calls == 1
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_fallback_returns_empty_when_all_providers_fail():
    primary = _StubProvider("primary", exc=SearchUnavailableError("no key"))
    secondary = _StubProvider("secondary", exc=_http_status_error(429))
    client = FallbackSearchClient([primary, secondary])

    result = await client.search("q")

    assert result == []


@pytest.mark.asyncio
async def test_fallback_logs_serving_provider_for_quota(db):
    ar = await _new_agent_run(db)

    tavily = TavilyClient(api_key=None)  # raises SearchUnavailableError

    brave_payload = {
        "web": {"results": [{"url": "https://x", "title": "X", "description": "snippet"}]}
    }

    async def brave_handler(_request):
        return httpx.Response(200, json=brave_payload)

    brave = BraveClient(
        api_key="k", http=httpx.AsyncClient(transport=httpx.MockTransport(brave_handler))
    )

    client = FallbackSearchClient([tavily, brave])
    await client.search("q", session=db, agent_run_id=ar.id)

    rows = (
        (await db.execute(select(LlmCall).where(LlmCall.model == "brave-search"))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].agent_run_id == ar.id
