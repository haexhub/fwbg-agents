"""Idempotency tests for scripts/backfill_llm_costs.py (Plan 018)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import AgentRun, LlmCall
from scripts.backfill_llm_costs import backfill


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/backfill.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


def _call(ar_id: int, model: str, cost_usd: float | None) -> LlmCall:
    return LlmCall(
        agent_run_id=ar_id,
        model=model,
        input_tokens=1_000_000,
        output_tokens=0,
        cost_usd=cost_usd,
        created_at=datetime.now(UTC),
    )


async def test_backfill_updates_only_priceable_null_rows_and_is_idempotent(db):
    now = datetime.now(UTC)
    async with db() as session:
        ar = AgentRun(agent_name="researcher", started_at=now, created_at=now)
        session.add(ar)
        await session.flush()
        session.add(_call(ar.id, "claude-opus-4-7", None))  # known model, NULL -> update
        session.add(_call(ar.id, "tavily-search", None))  # unknown model -> skip
        session.add(_call(ar.id, "claude-opus-4-7", 1.23))  # already priced -> untouched
        await session.commit()

    async with db() as session:
        updated, skipped = await backfill(session)
    assert (updated, skipped) == (1, 1)

    async with db() as session:
        rows = (await session.execute(select(LlmCall).order_by(LlmCall.id))).scalars().all()
        assert float(rows[0].cost_usd) == 5.0  # $5 per 1M input tokens
        assert rows[1].cost_usd is None
        assert float(rows[2].cost_usd) == 1.23

    # Second run: nothing left to price.
    async with db() as session:
        updated, skipped = await backfill(session)
    assert (updated, skipped) == (0, 1)
