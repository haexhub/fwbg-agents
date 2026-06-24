"""ORM tests for the M6a Strategy paper-trading columns.

Tests live under tests/persistence/ (the M6a task brief said tests/db/ but this
repo's convention is tests/persistence/). They mirror test_agent_run.py:
create_all against a tmp sqlite, no alembic round-trip here.

We must verify the SERVER-SIDE default for paper_phase_target_days: a fresh
Strategy created WITHOUT specifying that column must come back as 90 after
commit + refresh. Passing 90 explicitly would only exercise the Python default.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState


@pytest_asyncio.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(db_url, echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def _make_strategy(slug: str, **overrides) -> Strategy:
    now = datetime.now(UTC)
    kwargs = dict(
        slug=slug,
        current_state=StrategyState.PROPOSED.value,
        iteration_count=0,
        asset_class="crypto",
        strategy_family="trend",
        created_at=now,
        updated_at=now,
    )
    kwargs.update(overrides)
    return Strategy(**kwargs)


async def test_new_strategy_has_null_paper_account_id(db):
    """paper_account_id is nullable and defaults to NULL when not set."""
    row = _make_strategy("s-null-account")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    assert row.paper_account_id is None


async def test_paper_account_id_can_be_set(db):
    """paper_account_id round-trips as a free-form string."""
    row = _make_strategy("s-with-account", paper_account_id="ig-demo-001")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    assert row.paper_account_id == "ig-demo-001"


async def test_new_strategy_has_default_paper_phase_target_days_90(db):
    """server_default="90" backfills fresh rows without the kwarg."""
    row = _make_strategy("s-default-target")
    # Crucial: do NOT pass paper_phase_target_days — we're testing server_default.
    db.add(row)
    await db.commit()
    await db.refresh(row)
    assert row.paper_phase_target_days == 90


async def test_paper_phase_target_days_can_be_overridden(db):
    """Explicit value overrides the server default."""
    row = _make_strategy("s-custom-target", paper_phase_target_days=120)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    assert row.paper_phase_target_days == 120
