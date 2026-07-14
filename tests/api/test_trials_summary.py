"""Tests for GET /api/trials/summary (Plan 010 WP2 — dashboard DSR display)."""

from __future__ import annotations

import json

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "fwbg_test_results_dir", tmp_path / "test_results")

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


async def test_trials_summary_empty_when_no_backtests(client_with_db):
    client, _session, _tmp_path = client_with_db
    resp = await client.get("/api/trials/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "n_trials": 0,
        "sr_variance_across_trials": None,
        "sr_variance_sample_size": 0,
    }


async def test_trials_summary_counts_trials_and_variance(client_with_db):
    client, _session, _tmp_path = client_with_db
    from fwbg_agents.config import settings

    it_dir = settings.data_dir / "strategies" / "demo__forex__001" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "fwbg_results.json").write_text(
        json.dumps(
            {
                "run_id": "run_a",
                "assets": {"EURUSD": {"unified_metrics": {}, "total_combinations": 4}},
            }
        )
    )
    sym_dir = settings.fwbg_test_results_dir / "run_a" / "grid_details" / "EURUSD"
    sym_dir.mkdir(parents=True, exist_ok=True)
    (sym_dir / "fold_results.json").write_text(
        json.dumps(
            {
                "walk_forward": {
                    "fold_details": [
                        {
                            "test_trades_detail": [
                                {"pnl_raw": p, "entry_time": "2024-01-01T00:00:00"}
                                for p in [5, -4, 6, -5, 4]
                            ]
                        }
                    ]
                }
            }
        )
    )

    resp = await client.get("/api/trials/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_trials"] == 4
    # Only 1 run with trade data -> variance across trials undefined (<2 samples).
    assert body["sr_variance_across_trials"] is None
    assert body["sr_variance_sample_size"] == 1
