"""ORM tests for agent_run + llm_call.

Tables are created via `Base.metadata.create_all` against a tmp sqlite — same
pattern as test_lifecycle.py. The alembic migration is tested separately by
upgrading the live schema in scripts/m3_smoke.py (manual). Reversibility is
documented but not unit-tested: alembic round-trips on sqlite have well-known
quirks and aren't load-bearing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
)


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


async def test_insert_agent_run_minimal(db):
    now = datetime.now(UTC)
    row = AgentRun(
        agent_name="runner",
        status=AgentRunStatus.RUNNING.value,
        started_at=now,
        created_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    assert row.id is not None
    assert row.status == "running"
    assert row.agent_name == "runner"


async def test_insert_agent_run_with_strategy_link(db):
    """strategy_id is a nullable FK — runner-style agent runs set it."""
    now = datetime.now(UTC)
    row = AgentRun(
        agent_name="runner",
        status=AgentRunStatus.DONE.value,
        strategy_id=42,
        input_artifact_path="data/strategies/foo/iteration_001/strategy.json",
        output_artifact_path="data/strategies/foo/iteration_001/fwbg_results.json",
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    assert row.strategy_id == 42
    assert row.output_artifact_path.endswith("fwbg_results.json")


async def test_insert_llm_call_links_to_agent_run(db):
    now = datetime.now(UTC)
    run = AgentRun(
        agent_name="analyst",
        status=AgentRunStatus.RUNNING.value,
        started_at=now,
        created_at=now,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    call = LlmCall(
        agent_run_id=run.id,
        model="claude-opus-4-7",
        input_tokens=1234,
        output_tokens=567,
        latency_ms=4200,
        created_at=now,
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)
    assert call.id is not None
    assert call.agent_run_id == run.id
    assert call.input_tokens == 1234

    # Query back via the FK
    rows = (await db.execute(select(LlmCall).where(LlmCall.agent_run_id == run.id))).scalars().all()
    assert len(rows) == 1


async def test_agent_run_status_enum_values():
    """Enum values are stable strings (used in API + persistence)."""
    assert AgentRunStatus.PENDING.value == "pending"
    assert AgentRunStatus.RUNNING.value == "running"
    assert AgentRunStatus.DONE.value == "done"
    assert AgentRunStatus.FAILED.value == "failed"
