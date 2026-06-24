"""Tests for GET /strategies/{id}/paper-positions — dashboard live-view (M6a Task 6).

Behaviour-only: response code + body shape. Reuses the same state guard as
/paper-summary (PAPER_TRADING or LIVE_TRADING only); 404 when the strategy
doesn't exist or no positions.json snapshot has been written yet. Empty
positions list still returns 200 — the dashboard distinguishes "no snapshot
yet" (404) from "snapshot says no open positions" (200, positions=[]).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import Strategy, StrategyState


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "fwbg_data_dir", tmp_path)

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


async def _seed_strategy(session, slug: str, state: StrategyState) -> Strategy:
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=state.value,
        iteration_count=0,
        asset_class="FX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


def _write_positions(data_dir, slug: str, payload: dict) -> None:
    acc = data_dir / "account-trades" / slug
    acc.mkdir(parents=True, exist_ok=True)
    (acc / "positions.json").write_text(json.dumps(payload))


async def test_returns_200_with_positions_when_file_exists(client_with_db):
    client, session, tmp_path = client_with_db
    s = await _seed_strategy(session, "pos_strat_1", StrategyState.PAPER_TRADING)
    now = datetime.now(UTC).isoformat()
    _write_positions(
        tmp_path,
        s.slug,
        {
            "strategy_slug": s.slug,
            "updated_at": now,
            "positions": [
                {
                    "symbol": "EURUSD",
                    "side": "buy",
                    "quantity": 1000.0,
                    "entry_price": 1.08,
                    "current_price": 1.085,
                    "stop_loss": 1.07,
                    "take_profit": 1.10,
                    "unrealised_pnl_pct": 0.0046,
                    "opened_at": now,
                }
            ],
        },
    )

    r = await client.get(f"/strategies/{s.id}/paper-positions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy_slug"] == "pos_strat_1"
    assert len(body["positions"]) == 1
    pos = body["positions"][0]
    assert pos["symbol"] == "EURUSD"
    assert pos["side"] == "buy"
    assert pos["quantity"] == 1000.0
    assert pos["entry_price"] == 1.08
    assert pos["current_price"] == 1.085
    assert pos["stop_loss"] == 1.07
    assert pos["take_profit"] == 1.10
    assert pos["unrealised_pnl_pct"] == 0.0046
    assert pos["opened_at"] is not None


async def test_returns_200_with_empty_positions_when_no_open_positions(client_with_db):
    client, session, tmp_path = client_with_db
    s = await _seed_strategy(session, "pos_strat_empty", StrategyState.PAPER_TRADING)
    now = datetime.now(UTC).isoformat()
    _write_positions(
        tmp_path,
        s.slug,
        {
            "strategy_slug": s.slug,
            "updated_at": now,
            "positions": [],
        },
    )

    r = await client.get(f"/strategies/{s.id}/paper-positions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["positions"] == []
    assert body["updated_at"] is not None
    # round-trip parseable
    datetime.fromisoformat(body["updated_at"])


async def test_returns_404_when_file_missing(client_with_db):
    client, session, _tmp_path = client_with_db
    s = await _seed_strategy(session, "pos_strat_missing", StrategyState.PAPER_TRADING)

    r = await client.get(f"/strategies/{s.id}/paper-positions")
    assert r.status_code == 404, r.text
    assert "no positions snapshot" in r.json()["detail"]


async def test_returns_409_when_strategy_in_proposed_state(client_with_db):
    client, session, _tmp_path = client_with_db
    s = await _seed_strategy(session, "pos_proposed", StrategyState.PROPOSED)

    r = await client.get(f"/strategies/{s.id}/paper-positions")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "PAPER_TRADING" in detail
    assert StrategyState.PROPOSED.value in detail


async def test_positions_payload_includes_sl_tp_current_price(client_with_db):
    client, session, tmp_path = client_with_db
    s = await _seed_strategy(session, "pos_sltp", StrategyState.LIVE_TRADING)
    now = datetime.now(UTC).isoformat()
    _write_positions(
        tmp_path,
        s.slug,
        {
            "strategy_slug": s.slug,
            "updated_at": now,
            "positions": [
                {
                    "symbol": "EURUSD",
                    "side": "buy",
                    "quantity": 500.0,
                    "entry_price": 1.08,
                    "current_price": 1.085,
                    "stop_loss": 1.07,
                    "take_profit": 1.10,
                    "unrealised_pnl_pct": 0.0046,
                    "opened_at": now,
                }
            ],
        },
    )

    r = await client.get(f"/strategies/{s.id}/paper-positions")
    assert r.status_code == 200, r.text
    pos = r.json()["positions"][0]
    assert pos["stop_loss"] == 1.07
    assert pos["take_profit"] == 1.10
    assert pos["current_price"] == 1.085
