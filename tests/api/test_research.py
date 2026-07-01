"""Tests for the M4 research endpoints.

POST /research/brief             → schedules Researcher+Translator
POST /strategies/{id}/reiterate  → 422 if not BACKTESTED, 409 if no sidecar
GET  /hypotheses                 → lists strategies with hypothesis_path

Background-task entry points are monkeypatched so we don't hit a real LLM
or real Tavily. The endpoint code (validation + AgentRun bookkeeping)
is what's under test here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)


@pytest_asyncio.fixture
async def research_client(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/research.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    session = Session()

    async def _override_get_session():
        yield session

    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, session, settings, tmp_path

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def test_post_research_brief_schedules_and_returns_202(research_client, monkeypatch):
    client, session, *_ = research_client

    captured: list[tuple] = []

    async def fake_run_research(input, agent_run_id):
        captured.append((input.asset_class, agent_run_id))

    from fwbg_agents.api import research as research_api

    monkeypatch.setattr(research_api, "_run_research_background", fake_run_research)

    # Stub fwbg asset-registry — no live server needed.
    class _FakeFwbgClient:
        def __init__(self, base_url): pass
        async def get_asset_classes(self): return ["FOREX", "INDEX", "COMMODITY", "CRYPTO"]
        async def aclose(self): pass

    monkeypatch.setattr(research_api, "FwbgClient", _FakeFwbgClient)

    r = await client.post(
        "/research/brief",
        json={"asset_class": "FOREX", "strategy_family_hint": "ORB"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert "agent_run_id" in body
    assert body["status"] == "scheduled"

    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == body["agent_run_id"]))
    ).scalar_one()
    assert ar.agent_name == "research_flow"
    assert ar.status == AgentRunStatus.PENDING.value

    assert captured == [("FOREX", body["agent_run_id"])]


async def test_post_reiterate_returns_422_when_parent_not_backtested(research_client):
    client, session, *_ = research_client

    now = datetime.now(UTC)
    parent = Strategy(
        slug="orb__forex__001",
        current_state=StrategyState.PROPOSED.value,
        iteration_count=1,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    r = await client.post(f"/strategies/{parent.id}/reiterate")
    assert r.status_code == 422, r.text
    assert "BACKTESTED" in r.json().get("detail", "")


async def test_post_reiterate_returns_409_when_sidecar_missing(research_client):
    client, session, *_ = research_client

    now = datetime.now(UTC)
    parent = Strategy(
        slug="orb__forex__001",
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=1,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    r = await client.post(f"/strategies/{parent.id}/reiterate")
    assert r.status_code == 409, r.text
    assert "analyst_recommendation" in r.json().get("detail", "")


async def test_post_reiterate_unknown_parent_404(research_client):
    client, *_ = research_client
    r = await client.post("/strategies/99999/reiterate")
    assert r.status_code == 404


async def test_post_reiterate_happy_path_schedules_and_returns_202(research_client, monkeypatch):
    client, session, settings, _ = research_client

    now = datetime.now(UTC)
    parent = Strategy(
        slug="orb__forex__001",
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=1,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    # Stage sidecar so the precondition passes.
    it_dir = settings.data_dir / "strategies" / parent.slug / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "analyst_recommendation.json").write_text(
        json.dumps({"kind": "tune_params", "param": "atr_period", "new_range": [10, 14]})
    )

    captured: list[tuple] = []

    async def fake_run_reiterate(parent_id, agent_run_id):
        captured.append((parent_id, agent_run_id))

    from fwbg_agents.api import research as research_api

    monkeypatch.setattr(research_api, "_run_reiterate_background", fake_run_reiterate)

    r = await client.post(f"/strategies/{parent.id}/reiterate")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["parent_strategy_id"] == parent.id

    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == body["agent_run_id"]))
    ).scalar_one()
    assert ar.agent_name == "reiterate"
    assert ar.strategy_id == parent.id
    assert captured == [(parent.id, body["agent_run_id"])]


async def test_get_hypotheses_lists_strategies_with_hypothesis_path(research_client):
    client, session, *_ = research_client

    now = datetime.now(UTC)
    with_path = Strategy(
        slug="orb__forex__001",
        current_state=StrategyState.PROPOSED.value,
        iteration_count=1,
        asset_class="FOREX",
        strategy_family="ORB",
        hypothesis_path="/some/where/hypothesis.json",
        created_at=now,
        updated_at=now,
    )
    without_path = Strategy(
        slug="manual_v1",
        current_state=StrategyState.PROPOSED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add_all([with_path, without_path])
    await session.commit()

    r = await client.get("/hypotheses")
    assert r.status_code == 200, r.text
    body = r.json()
    slugs = [h["slug"] for h in body["hypotheses"]]
    assert "orb__forex__001" in slugs
    assert "manual_v1" not in slugs


async def test_get_hypotheses_empty_when_none(research_client):
    client, *_ = research_client
    r = await client.get("/hypotheses")
    assert r.status_code == 200
    assert r.json() == {"hypotheses": []}
