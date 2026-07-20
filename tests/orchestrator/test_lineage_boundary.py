"""Tests for the frozen per-lineage holdout boundary (Plan 014).

Covers: freeze-once semantics (the sidecar wins over whatever "today" would
now compute), root resolution over a multi-generation chain, and the cycle
guard in `lineage_root`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import fwbg_agents.agents.runner as runner_mod
from fwbg_agents.orchestrator import lineage_boundary as lb
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    monkeypatch.setattr(settings, "holdout_months", 24)

    db_url = f"sqlite+aiosqlite:///{tmp_path}/lineage.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def _make_strategy(session, slug: str, *, parent_id: int | None = None) -> Strategy:
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=StrategyState.PROPOSED.value,
        iteration_count=0,
        parent_strategy_id=parent_id,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    return s


async def test_freeze_once_ignores_later_today(db, monkeypatch):
    Session = db
    async with Session() as session:
        root = await _make_strategy(session, "root_v1")

    calls = iter(["2024-01-01", "2099-12-31"])
    monkeypatch.setattr(runner_mod, "_months_ago_iso", lambda months: next(calls))

    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == root.id))).scalar_one()
        first = await lb.get_or_freeze_boundary(session, strat)

    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == root.id))).scalar_one()
        second = await lb.get_or_freeze_boundary(session, strat)

    assert first == "2024-01-01"
    assert second == "2024-01-01"  # frozen, not recomputed from the second "today"


async def test_root_resolution_over_three_generations(db):
    Session = db
    async with Session() as session:
        root = await _make_strategy(session, "gen1")
    async with Session() as session:
        gen2 = await _make_strategy(session, "gen2", parent_id=root.id)
    async with Session() as session:
        gen3 = await _make_strategy(session, "gen3", parent_id=gen2.id)

    async with Session() as session:
        leaf = (await session.execute(select(Strategy).where(Strategy.id == gen3.id))).scalar_one()
        resolved_root = await lb.lineage_root(session, leaf)

    assert resolved_root.slug == "gen1"


async def test_boundary_shared_across_lineage_members(db):
    """A child resolves the same frozen boundary the root already froze."""
    Session = db
    async with Session() as session:
        root = await _make_strategy(session, "root_shared")
    async with Session() as session:
        child = await _make_strategy(session, "child_shared", parent_id=root.id)

    async with Session() as session:
        r = (await session.execute(select(Strategy).where(Strategy.id == root.id))).scalar_one()
        root_boundary = await lb.get_or_freeze_boundary(session, r)

    async with Session() as session:
        c = (await session.execute(select(Strategy).where(Strategy.id == child.id))).scalar_one()
        child_boundary = await lb.get_or_freeze_boundary(session, c)

    assert root_boundary == child_boundary
    assert lb.boundary_path("root_shared").is_file()
    assert not lb.boundary_path("child_shared").is_file()


async def test_cycle_guard_returns_last_non_repeated_ancestor(db):
    Session = db
    async with Session() as session:
        a = await _make_strategy(session, "cyc_a")
    async with Session() as session:
        b = await _make_strategy(session, "cyc_b", parent_id=a.id)
    # Manufacture a cycle: a's parent is b (a <-> b loop). Real code never
    # creates this; the guard exists purely as a defensive backstop.
    async with Session() as session:
        await session.execute(
            update(Strategy).where(Strategy.id == a.id).values(parent_strategy_id=b.id)
        )
        await session.commit()

    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == a.id))).scalar_one()
        root = await lb.lineage_root(session, strat)

    assert root.slug in {"cyc_a", "cyc_b"}
