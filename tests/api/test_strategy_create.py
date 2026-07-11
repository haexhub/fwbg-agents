"""Tests for POST /strategies — the M3 manual-seeding endpoint.

This is the Researcher-skip path: a human (or smoke script) POSTs a strategy
config, the row is created in PROPOSED with iteration_001/strategy.json on
disk, no transition row is emitted (creation ≠ state change).
"""

from __future__ import annotations

import json

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import (
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/api_test.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False)
    session = SessionMaker()

    async def _override_get_session():
        yield session

    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, session, tmp_path

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


_BASE_BODY = {
    "slug": "demo_orb_v1",
    "asset_class": "INDEX",
    "strategy_family": "ORB",
    "strategy_json": {
        "name": "demo_orb_v1",
        "pipeline": "orb_scalping_v1",
        "params": {"window": 15, "tp": 1.5, "sl": 1.0},
    },
    "tags": ["orb", "index", "intraday"],
}


async def test_post_strategy_creates_row_in_proposed(client_with_db):
    client, session, _tmp_path = client_with_db
    r = await client.post("/strategies", json=_BASE_BODY)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "demo_orb_v1"
    assert body["current_state"] == StrategyState.PROPOSED.value
    assert body["iteration_dir"].endswith("iteration_001")

    row = (
        await session.execute(select(Strategy).where(Strategy.slug == "demo_orb_v1"))
    ).scalar_one()
    assert row.current_state == StrategyState.PROPOSED.value
    assert row.asset_class == "INDEX"
    assert row.strategy_family == "ORB"


async def test_post_strategy_writes_iteration_001_strategy_json(client_with_db):
    client, _session, tmp_path = client_with_db
    r = await client.post("/strategies", json=_BASE_BODY)
    assert r.status_code == 201, r.text

    path = tmp_path / "data" / "strategies" / "demo_orb_v1" / "iteration_001" / "strategy.json"
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["name"] == "demo_orb_v1"
    assert data["params"]["tp"] == 1.5


async def test_post_strategy_persists_tags(client_with_db):
    client, session, _tmp_path = client_with_db
    await client.post("/strategies", json=_BASE_BODY)
    tags = (
        (await session.execute(select(StrategyTag.tag).order_by(StrategyTag.tag))).scalars().all()
    )
    assert sorted(tags) == ["index", "intraday", "orb"]


async def test_post_strategy_does_not_create_transition_row(client_with_db):
    """Creation is not a state change. Transitions begin with the Runner."""
    client, session, _tmp_path = client_with_db
    await client.post("/strategies", json=_BASE_BODY)
    transitions = (await session.execute(select(Transition))).scalars().all()
    assert transitions == []


async def test_post_strategy_duplicate_slug_returns_409(client_with_db):
    client, _session, _tmp_path = client_with_db
    r1 = await client.post("/strategies", json=_BASE_BODY)
    assert r1.status_code == 201
    r2 = await client.post("/strategies", json=_BASE_BODY)
    assert r2.status_code == 409
    assert "exists" in r2.json().get("detail", "").lower()


async def test_post_strategy_rejects_invalid_slug(client_with_db):
    client, _session, _tmp_path = client_with_db
    body = {**_BASE_BODY, "slug": "Demo Strategy 2 !!"}
    r = await client.post("/strategies", json=body)
    assert r.status_code == 422
