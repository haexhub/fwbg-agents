"""Tests for the M6b paper-analyze endpoint.

POST /strategies/{id}/paper-analyze   -> 202 + AgentRun envelope

Mirrors the M5c reiterate-with-plugin test shape: in-process FastAPI via
ASGI transport; the background task is monkeypatched away so the focus is
the endpoint's preconditions + AgentRun bookkeeping. End-to-end execution
is covered by the M6b smoke script (Task 8).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest_asyncio
import yaml
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


def _write_paper_criteria(criteria_dir, asset_class: str = "forex"):
    paper_dir = criteria_dir / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / f"{asset_class}.yaml").write_text(
        yaml.safe_dump(
            {
                "paper_to_live": {
                    "required_all": [
                        {"sharpe_paper": ">= 0.5"},
                        {"trades_total": ">= 1"},
                    ]
                }
            }
        )
    )


def _seed_account_trades(fwbg_data_dir, slug: str):
    """Write trades.jsonl + status.json under <fwbg_data_dir>/account-trades/<slug>/."""
    base = fwbg_data_dir / "account-trades" / slug
    base.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    trade_lines = [
        json.dumps(
            {
                "entry_time": (now - timedelta(days=2)).isoformat(),
                "pnl_pct": 0.012,
            }
        ),
        json.dumps(
            {
                "entry_time": (now - timedelta(days=1)).isoformat(),
                "pnl_pct": -0.005,
            }
        ),
        json.dumps(
            {
                "entry_time": now.isoformat(),
                "pnl_pct": 0.020,
            }
        ),
    ]
    (base / "trades.jsonl").write_text("\n".join(trade_lines) + "\n")
    (base / "status.json").write_text(
        json.dumps(
            {
                "current_equity": 10250.0,
                "starting_equity": 10000.0,
                "equity_curve_sample": [
                    {"t": (now - timedelta(days=2)).isoformat(), "equity": 10000.0},
                    {"t": (now - timedelta(days=1)).isoformat(), "equity": 10120.0},
                    {"t": now.isoformat(), "equity": 10250.0},
                ],
            }
        )
    )


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents-data")
    fwbg_data_dir = tmp_path / "fwbg-data"
    fwbg_data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "fwbg_data_dir", fwbg_data_dir)
    _write_paper_criteria(settings.criteria_dir, asset_class="forex")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/paper_analyze.db"
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
        yield client, session, settings, fwbg_data_dir

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def _seed_strategy(session, *, slug: str, state: str) -> Strategy:
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=state,
        iteration_count=1,
        asset_class="forex",
        strategy_family="ORB",
        paper_phase_target_days=90,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_post_paper_analyze_returns_404_when_strategy_missing(client_with_db):
    client, _, _, _ = client_with_db
    r = await client.post("/strategies/99999/paper-analyze", json={})
    assert r.status_code == 404, r.text
    assert "not found" in r.json()["detail"]


async def test_post_paper_analyze_returns_422_when_strategy_not_in_paper_trading(
    client_with_db,
):
    client, session, _, _ = client_with_db
    s = await _seed_strategy(
        session, slug="not_paper_yet", state=StrategyState.PROPOSED.value
    )
    r = await client.post(f"/strategies/{s.id}/paper-analyze", json={})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "PAPER_TRADING" in detail
    assert "proposed" in detail or StrategyState.PROPOSED.value in detail


async def test_post_paper_analyze_returns_422_when_no_on_disk_data(client_with_db):
    client, session, _, _ = client_with_db
    s = await _seed_strategy(
        session, slug="paper_no_data", state=StrategyState.PAPER_TRADING.value
    )
    # No trades.jsonl / status.json written under fwbg_data_dir.
    r = await client.post(f"/strategies/{s.id}/paper-analyze", json={})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "paper" in detail.lower()
    assert s.slug in detail


async def test_post_paper_analyze_returns_202_and_agent_run_envelope(
    client_with_db, monkeypatch
):
    client, session, _, fwbg_data_dir = client_with_db
    s = await _seed_strategy(
        session, slug="paper_ok", state=StrategyState.PAPER_TRADING.value
    )
    _seed_account_trades(fwbg_data_dir, s.slug)

    captured: list[tuple] = []

    async def fake_bg(strategy_id: int, agent_run_id: int) -> None:
        captured.append((strategy_id, agent_run_id))

    from fwbg_agents.api import strategies as strategies_api

    monkeypatch.setattr(strategies_api, "_run_paper_analyze_background", fake_bg)

    r = await client.post(f"/strategies/{s.id}/paper-analyze", json={})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "scheduled"
    assert "agent_run_id" in body
    ar_id = body["agent_run_id"]

    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == ar_id))
    ).scalar_one()
    assert ar.agent_name == "paper_analyst"
    assert ar.status == AgentRunStatus.PENDING.value
    assert ar.strategy_id == s.id

    assert captured == [(s.id, ar_id)]
