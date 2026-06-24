"""Tests for GET /strategies/{id}/paper-summary — dashboard-polled, no LLM (M6a Task 5).

Behaviour-only: response code + body shape. Strategy state guard enforces that
the endpoint only returns data while the strategy is in PAPER_TRADING or
LIVE_TRADING; otherwise 409 with the actual state in the detail. 404 when
the strategy doesn't exist or no on-disk telemetry has been written yet.
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
        asset_class="INDEX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


def _seed_paper_files(data_dir, slug: str) -> None:
    acc = data_dir / "account-trades" / slug
    acc.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    # status.json (gives us starting/current equity + an equity_curve_sample)
    (acc / "status.json").write_text(
        json.dumps(
            {
                "strategy_slug": slug,
                "updated_at": now,
                "current_equity": 105.0,
                "starting_equity": 100.0,
                "equity_curve_sample": [
                    {"t": now, "equity": 100.0},
                    {"t": now, "equity": 110.0},
                    {"t": now, "equity": 105.0},
                ],
            }
        )
    )
    # trades.jsonl — two trades, one win one loss
    trades = [
        {
            "trade_id": "t1",
            "strategy_slug": slug,
            "closed_at": now,
            "pnl_pct": 0.02,
        },
        {
            "trade_id": "t2",
            "strategy_slug": slug,
            "closed_at": now,
            "pnl_pct": -0.01,
        },
    ]
    (acc / "trades.jsonl").write_text("\n".join(json.dumps(t) for t in trades) + "\n")


async def test_returns_200_with_summary_when_on_disk_data_exists(client_with_db):
    client, session, tmp_path = client_with_db
    s = await _seed_strategy(session, "paper_strat_1", StrategyState.PAPER_TRADING)
    _seed_paper_files(tmp_path, s.slug)

    r = await client.get(f"/strategies/{s.id}/paper-summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy_slug"] == "paper_strat_1"
    assert "sharpe_paper" in body
    assert body["trades_total"] == 2
    assert "max_dd_paper" in body
    assert "win_rate" in body
    assert "equity_curve_sample" in body
    assert body["current_equity"] == 105.0


async def test_returns_404_when_no_on_disk_data(client_with_db):
    client, session, _tmp_path = client_with_db
    s = await _seed_strategy(session, "paper_strat_empty", StrategyState.PAPER_TRADING)

    r = await client.get(f"/strategies/{s.id}/paper-summary")
    assert r.status_code == 404, r.text
    assert "no paper-trade data" in r.json()["detail"]


async def test_returns_409_when_strategy_in_proposed_state(client_with_db):
    client, session, _tmp_path = client_with_db
    s = await _seed_strategy(session, "proposed_strat", StrategyState.PROPOSED)

    r = await client.get(f"/strategies/{s.id}/paper-summary")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "PAPER_TRADING" in detail
    assert StrategyState.PROPOSED.value in detail


async def test_accepts_live_trading_state(client_with_db):
    client, session, tmp_path = client_with_db
    s = await _seed_strategy(session, "live_strat_1", StrategyState.LIVE_TRADING)
    _seed_paper_files(tmp_path, s.slug)

    r = await client.get(f"/strategies/{s.id}/paper-summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy_slug"] == "live_strat_1"
    assert body["trades_total"] == 2


async def test_returns_404_when_strategy_id_not_found(client_with_db):
    client, _session, _tmp_path = client_with_db
    r = await client.get("/strategies/99999/paper-summary")
    assert r.status_code == 404, r.text
    assert "not found" in r.json()["detail"].lower()
