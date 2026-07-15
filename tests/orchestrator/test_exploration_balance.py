"""Exploration-balance digest tests (Plan 010 WP3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.exploration_balance import exploration_balance_digest
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/exploration.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session, settings
    await engine.dispose()


async def _seed_strategy(Session, settings, slug, family, asset_class, timeframe=None):
    async with Session() as session:
        now = datetime.now(UTC)
        session.add(
            Strategy(
                slug=slug,
                current_state=StrategyState.PROPOSED.value,
                iteration_count=1,
                asset_class=asset_class,
                strategy_family=family,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    if timeframe is not None:
        it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
        it_dir.mkdir(parents=True, exist_ok=True)
        (it_dir / "strategy.json").write_text(json.dumps({"timeframe": timeframe}))


async def test_empty_db_says_every_cell_unexplored(db):
    Session, _settings = db
    async with Session() as session:
        digest = await exploration_balance_digest(session)
    assert "unexplored" in digest


async def test_counts_grouped_by_family_asset_timeframe(db):
    Session, settings = db
    await _seed_strategy(Session, settings, "mr_a", "mean_reversion", "FOREX", "MINUTE_15")
    await _seed_strategy(Session, settings, "mr_b", "mean_reversion", "FOREX", "MINUTE_15")
    await _seed_strategy(Session, settings, "orb_a", "ORB", "FOREX", "HOUR")

    async with Session() as session:
        digest = await exploration_balance_digest(session)

    assert "mean_reversion x FOREX x MINUTE_15: 2" in digest
    assert "ORB x FOREX x HOUR: 1" in digest
    # Most-crowded cell listed first.
    assert digest.index("mean_reversion x FOREX x MINUTE_15") < digest.index("ORB x FOREX x HOUR")


async def test_missing_strategy_json_falls_back_to_unknown_timeframe(db):
    Session, settings = db
    await _seed_strategy(Session, settings, "mr_a", "mean_reversion", "FOREX")  # no strategy.json

    async with Session() as session:
        digest = await exploration_balance_digest(session)

    assert "mean_reversion x FOREX x unknown: 1" in digest


async def test_digest_is_capped_at_max_chars(db):
    Session, settings = db
    for i in range(50):
        await _seed_strategy(Session, settings, f"s_{i}", f"family_{i}", "FOREX", "HOUR")

    async with Session() as session:
        digest = await exploration_balance_digest(session, max_chars=200)

    assert len(digest) <= 200


async def test_truncation_drops_cells_but_keeps_the_instruction_trailer(db):
    """When the cell list outgrows the budget, the least-crowded cells are
    dropped — the 'prefer underexplored' instruction (the point of the digest)
    must survive."""
    Session, settings = db
    for i in range(50):
        await _seed_strategy(Session, settings, f"s_{i}", f"family_{i}", "FOREX", "HOUR")

    async with Session() as session:
        digest = await exploration_balance_digest(session, max_chars=800)

    assert len(digest) <= 800
    assert "Prefer an underexplored cell" in digest
    assert "less-crowded cells omitted" in digest
