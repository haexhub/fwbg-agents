"""Tests for the read-only strategy + plugin endpoints.

These run against an in-process FastAPI instance with a dependency override
that swaps in a tmp-path sqlite session. No HTTP socket, no real DB file
beyond the test's tmp_path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.orchestrator.lifecycle import transition_plugin, transition_strategy
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import (
    Plugin,
    PluginKind,
    PluginState,
    Strategy,
    StrategyState,
    StrategyTag,
)


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    """FastAPI client whose `get_session` is bound to a fresh tmp sqlite."""
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/api_test.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False)

    # Single session for the whole test so seed data and request handlers
    # see the same view.
    session = SessionMaker()

    async def _override_get_session():
        yield session

    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, session

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def test_list_strategies_empty(client_with_db):
    client, _ = client_with_db
    r = await client.get("/strategies")
    assert r.status_code == 200
    assert r.json() == {"strategies": []}


async def test_list_plugins_empty(client_with_db):
    client, _ = client_with_db
    r = await client.get("/plugins")
    assert r.status_code == 200
    assert r.json() == {"plugins": []}


async def test_strategy_detail_round_trip_with_transitions(client_with_db):
    client, session = client_with_db
    now = datetime.now(UTC)
    s = Strategy(
        slug="orb_dax_v1",
        current_state=StrategyState.PROPOSED.value,
        asset_class="INDEX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    session.add(StrategyTag(strategy_id=1, tag="breakout"))
    session.add(StrategyTag(strategy_id=1, tag="opening-range"))
    await session.commit()
    await session.refresh(s)

    await transition_strategy(session, s, StrategyState.BACKTESTED, reason="first backtest")

    r = await client.get(f"/strategies/{s.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["strategy"]["slug"] == "orb_dax_v1"
    assert body["strategy"]["current_state"] == "backtested"
    assert set(body["strategy"]["tags"]) == {"breakout", "opening-range"}
    assert len(body["transitions"]) == 1
    assert body["transitions"][0]["from_state"] == "proposed"
    assert body["transitions"][0]["to_state"] == "backtested"
    assert body["transitions"][0]["reason"] == "first backtest"


async def test_list_strategies_filters_by_state_and_asset_class(client_with_db):
    client, session = client_with_db
    now = datetime.now(UTC)
    session.add_all(
        [
            Strategy(
                slug="a",
                current_state="proposed",
                asset_class="FOREX",
                strategy_family="MR",
                created_at=now,
                updated_at=now,
            ),
            Strategy(
                slug="b",
                current_state="backtested",
                asset_class="FOREX",
                strategy_family="MR",
                created_at=now,
                updated_at=now,
            ),
            Strategy(
                slug="c",
                current_state="proposed",
                asset_class="INDEX",
                strategy_family="ORB",
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    await session.commit()

    r = await client.get("/strategies?state=proposed")
    assert {s["slug"] for s in r.json()["strategies"]} == {"a", "c"}

    r = await client.get("/strategies?asset_class=FOREX")
    assert {s["slug"] for s in r.json()["strategies"]} == {"a", "b"}

    r = await client.get("/strategies?state=proposed&asset_class=INDEX")
    assert {s["slug"] for s in r.json()["strategies"]} == {"c"}


async def test_list_strategies_rejects_invalid_state(client_with_db):
    client, _ = client_with_db
    r = await client.get("/strategies?state=bogus")
    assert r.status_code == 400


async def test_strategy_404(client_with_db):
    client, _ = client_with_db
    r = await client.get("/strategies/999")
    assert r.status_code == 404


async def test_plugin_detail_and_transitions(client_with_db):
    client, session = client_with_db
    now = datetime.now(UTC)
    p = Plugin(
        slug="atr_v2",
        current_state=PluginState.SPECIFIED.value,
        kind=PluginKind.INDICATOR.value,
        created_at=now,
        updated_at=now,
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    await transition_plugin(session, p, PluginState.AUTHORED, reason="code generated")

    r = await client.get(f"/plugins/{p.id}")
    assert r.status_code == 200
    assert r.json()["plugin"]["current_state"] == "authored"
    assert r.json()["transitions"][0]["to_state"] == "authored"

    r = await client.get(f"/plugins/{p.id}/transitions")
    assert r.status_code == 200
    assert len(r.json()["transitions"]) == 1
