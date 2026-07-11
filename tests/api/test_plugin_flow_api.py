"""Tests for the M5b plugin-flow endpoints.

POST /strategies/{id}/author-plugin         → 202 + AgentRun envelope
POST /plugins/{id}/evaluate                 → 202 + AgentRun envelope
GET  /plugins/{id}/verification-runs        → newest-first list

Background-task entry points are monkeypatched so the assertions focus on the
endpoint's validation + AgentRun bookkeeping. End-to-end execution is covered
by scripts/m5_smoke.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
    VerificationRun,
)


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
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
        yield client, session, settings

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


# ---------------------------------------------------------------------------
# POST /strategies/{id}/author-plugin
# ---------------------------------------------------------------------------


async def test_post_author_plugin_returns_202_with_agent_run(client_with_db, monkeypatch):
    client, session, settings = client_with_db

    now = datetime.now(UTC)
    parent = Strategy(
        slug="parent_v1",
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    # Pre-seed the sidecar so the precondition passes.
    it_dir = settings.data_dir / "strategies" / "parent_v1" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "add_indicator_request.json").write_text(
        json.dumps(
            {
                "kind": "add_indicator",
                "confidence": 0.7,
                "reasoning": "x",
                "phase": "indicators",
                "capability": "rolling close-price mean",
                "category": "indicator",
            }
        )
    )

    captured: list[tuple] = []

    async def fake_author_bg(strategy_id: int, agent_run_id: int) -> None:
        captured.append((strategy_id, agent_run_id))

    from fwbg_agents.api import plugins as plugins_api

    monkeypatch.setattr(plugins_api, "_run_author_background", fake_author_bg)

    r = await client.post(f"/strategies/{parent.id}/author-plugin")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "scheduled"
    assert body["strategy_id"] == parent.id

    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == body["agent_run_id"]))
    ).scalar_one()
    assert ar.agent_name == "plugin_author_flow"
    assert ar.status == AgentRunStatus.PENDING.value
    assert ar.strategy_id == parent.id
    assert ar.input_artifact_path.endswith("add_indicator_request.json")

    assert captured == [(parent.id, body["agent_run_id"])]


async def test_post_author_plugin_422_when_no_sidecar(client_with_db):
    client, session, _ = client_with_db
    now = datetime.now(UTC)
    s = Strategy(
        slug="no-sidecar",
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)

    r = await client.post(f"/strategies/{s.id}/author-plugin")
    assert r.status_code == 422, r.text
    assert "add_indicator_request" in r.json()["detail"]


async def test_post_author_plugin_422_when_wrong_state(client_with_db):
    client, session, _ = client_with_db
    now = datetime.now(UTC)
    s = Strategy(
        slug="wrong-state",
        current_state=StrategyState.PROPOSED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)

    r = await client.post(f"/strategies/{s.id}/author-plugin")
    assert r.status_code == 422, r.text
    assert "BACKTESTED" in r.json()["detail"]


async def test_post_author_plugin_404_when_strategy_missing(client_with_db):
    client, _, _ = client_with_db
    r = await client.post("/strategies/99999/author-plugin")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /plugins/{id}/evaluate
# ---------------------------------------------------------------------------


async def test_post_plugin_evaluate_returns_202(client_with_db, monkeypatch):
    client, session, _ = client_with_db
    now = datetime.now(UTC)
    plugin = Plugin(
        slug="indi-1",
        current_state=PluginState.AUTHORED.value,
        kind="indicator",
        contract_path="data/plugins/indi-1/v1/contract.yaml",
        spec_path="data/plugins/indi-1/v1/spec.md",
        created_at=now,
        updated_at=now,
    )
    session.add(plugin)
    await session.commit()
    await session.refresh(plugin)

    captured: list[tuple] = []

    async def fake_eval_bg(plugin_id: int, agent_run_id: int) -> None:
        captured.append((plugin_id, agent_run_id))

    from fwbg_agents.api import plugins as plugins_api

    monkeypatch.setattr(plugins_api, "_run_evaluator_background", fake_eval_bg)

    r = await client.post(f"/plugins/{plugin.id}/evaluate")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["plugin_id"] == plugin.id

    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == body["agent_run_id"]))
    ).scalar_one()
    assert ar.agent_name == "plugin_evaluator_flow"
    assert ar.plugin_id == plugin.id
    assert ar.status == AgentRunStatus.PENDING.value

    assert captured == [(plugin.id, body["agent_run_id"])]


async def test_post_plugin_evaluate_422_when_not_authored(client_with_db):
    client, session, _ = client_with_db
    now = datetime.now(UTC)
    plugin = Plugin(
        slug="specified-only",
        current_state=PluginState.SPECIFIED.value,
        kind="indicator",
        created_at=now,
        updated_at=now,
    )
    session.add(plugin)
    await session.commit()
    await session.refresh(plugin)

    r = await client.post(f"/plugins/{plugin.id}/evaluate")
    assert r.status_code == 422, r.text
    assert "AUTHORED" in r.json()["detail"]


async def test_post_plugin_evaluate_404_when_missing(client_with_db):
    client, _, _ = client_with_db
    r = await client.post("/plugins/99999/evaluate")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /plugins/{id}/verification-runs
# ---------------------------------------------------------------------------


async def test_get_verification_runs_returns_newest_first(client_with_db):
    client, session, _ = client_with_db
    now = datetime.now(UTC)
    plugin = Plugin(
        slug="vlist",
        current_state=PluginState.AUTHORED.value,
        kind="indicator",
        created_at=now,
        updated_at=now,
    )
    session.add(plugin)
    await session.commit()
    await session.refresh(plugin)

    # Three runs: passed, failed, failed (in chronological order — oldest first).
    for i, status in enumerate(("passed", "failed", "failed")):
        ts = now + timedelta(seconds=i)
        session.add(
            VerificationRun(
                plugin_id=plugin.id,
                status=status,
                scenarios_run=2,
                scenarios_passed=2 if status == "passed" else 0,
                started_at=ts,
                ended_at=ts,
                created_at=ts,
            )
        )
    await session.commit()

    r = await client.get(f"/plugins/{plugin.id}/verification-runs")
    assert r.status_code == 200, r.text
    runs = r.json()["verification_runs"]
    assert len(runs) == 3
    # Newest-first: last-inserted (failed) at index 0; oldest (passed) at end
    assert runs[0]["status"] == "failed"
    assert runs[-1]["status"] == "passed"


async def test_get_verification_runs_404_when_plugin_missing(client_with_db):
    client, _, _ = client_with_db
    r = await client.get("/plugins/99999/verification-runs")
    assert r.status_code == 404
