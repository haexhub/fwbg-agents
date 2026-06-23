"""Tests for the M3 runs endpoints.

POST /strategies/{id}/run     → kicks off the Runner in a background task
POST /strategies/{id}/analyze → kicks off the Analyst in a background task
GET  /agents/runs/{id}        → returns AgentRun status + paths

Tests patch the background-task entry points so we don't make real HTTP calls
to fwbg or talk to a real LLM. The endpoint code itself (input validation +
AgentRun bookkeeping) is what's under test here; Runner/Analyst behaviour is
covered by their own tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

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
async def runs_client(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/runs.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    session = Session()

    async def _override_get_session():
        yield session

    app.dependency_overrides[get_session] = _override_get_session

    # Seed strategies: one in PROPOSED for the run endpoint, one in BACKTESTED
    # with iteration_001 + fwbg_results.json for the analyze endpoint.
    now = datetime.now(UTC)
    proposed = Strategy(
        slug="prop_v1",
        current_state=StrategyState.PROPOSED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    backtested = Strategy(
        slug="back_v1",
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add_all([proposed, backtested])
    await session.commit()
    await session.refresh(proposed)
    await session.refresh(backtested)

    # iteration dir + fwbg_results.json for backtested
    it = settings.data_dir / "strategies" / "back_v1" / "iteration_001"
    it.mkdir(parents=True, exist_ok=True)
    (it / "strategy.json").write_text(json.dumps({"name": "back_v1"}))
    (it / "fwbg_results.json").write_text(json.dumps({"status": "completed", "assets": {}}))

    # And iteration dir for the proposed one (Runner expects it)
    it2 = settings.data_dir / "strategies" / "prop_v1" / "iteration_001"
    it2.mkdir(parents=True, exist_ok=True)
    (it2 / "strategy.json").write_text(json.dumps({"name": "prop_v1"}))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, session, proposed.id, backtested.id, tmp_path

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def test_post_run_creates_agent_run_and_returns_202(runs_client, monkeypatch):
    client, session, proposed_id, _, _ = runs_client

    captured = []

    async def fake_run_runner(strategy_id: int):
        captured.append(("runner", strategy_id))

    from fwbg_agents.api import runs as runs_api

    monkeypatch.setattr(runs_api, "_run_runner_background", fake_run_runner)

    r = await client.post(f"/strategies/{proposed_id}/run")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["strategy_id"] == proposed_id
    assert "agent_run_id" in body

    # AgentRun row exists in PENDING/RUNNING
    ar = (await session.execute(select(AgentRun).where(AgentRun.id == body["agent_run_id"]))).scalar_one()
    assert ar.agent_name == "runner"
    assert ar.strategy_id == proposed_id

    assert captured == [("runner", proposed_id)]


async def test_post_run_unknown_strategy_404(runs_client):
    client, _, _, _, _ = runs_client
    r = await client.post("/strategies/99999/run")
    assert r.status_code == 404


async def test_post_analyze_returns_202_when_results_present(runs_client, monkeypatch):
    client, session, _, backtested_id, _ = runs_client
    captured = []

    async def fake_run_analyst(strategy_id: int):
        captured.append(("analyst", strategy_id))

    from fwbg_agents.api import runs as runs_api

    monkeypatch.setattr(runs_api, "_run_analyst_background", fake_run_analyst)

    r = await client.post(f"/strategies/{backtested_id}/analyze")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["strategy_id"] == backtested_id
    assert captured == [("analyst", backtested_id)]


async def test_post_analyze_without_results_returns_409(runs_client):
    client, _, proposed_id, _, _ = runs_client
    r = await client.post(f"/strategies/{proposed_id}/analyze")
    assert r.status_code == 409
    assert "results" in r.json().get("detail", "").lower()


async def test_get_agent_run_returns_status(runs_client):
    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="runner",
        status=AgentRunStatus.DONE.value,
        strategy_id=1,
        input_artifact_path="/in.json",
        output_artifact_path="/out.json",
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    r = await client.get(f"/agents/runs/{ar.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == ar.id
    assert body["status"] == "done"
    assert body["agent_name"] == "runner"
    assert body["output_artifact_path"] == "/out.json"


async def test_get_agent_run_404(runs_client):
    client, _, _, _, _ = runs_client
    r = await client.get("/agents/runs/99999")
    assert r.status_code == 404
