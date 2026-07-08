"""Unit tests for the fail_agent_run() error-path helper.

Same tmp-sqlite pattern as test_agent_run.py. The commit-failure case uses a
mock session so we can force commit() to raise without corrupting the real one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.persistence.agent_runs import fail_agent_run
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import AgentRun, AgentRunStatus


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


async def _running_run(db) -> AgentRun:
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
    return row


async def test_fail_agent_run_sets_fields_and_commits(db):
    row = await _running_run(db)
    exc = ValueError("boom")

    msg = await fail_agent_run(db, row, exc)

    assert msg == "boom"
    assert row.status == AgentRunStatus.FAILED.value
    assert row.error == "boom"
    assert row.ended_at is not None

    # Persisted, not just in-memory.
    await db.refresh(row)
    assert row.status == AgentRunStatus.FAILED.value
    assert row.error == "boom"


async def test_fail_agent_run_transient_prefix(db):
    row = await _running_run(db)

    msg = await fail_agent_run(db, row, ValueError("dropped"), transient=True)

    assert msg == "transient: dropped"
    assert row.error == "transient: dropped"


async def test_fail_agent_run_returns_stored_message(db):
    """Return value equals the persisted error (used for event emission)."""
    row = await _running_run(db)
    msg = await fail_agent_run(db, row, RuntimeError("classified"))
    assert msg == row.error


async def test_fail_agent_run_swallows_commit_failure(caplog):
    """A commit failure is logged, never raised — must not mask the original."""
    session = AsyncMock()
    session.commit.side_effect = RuntimeError("db gone")
    row = AgentRun(
        agent_name="runner",
        status=AgentRunStatus.RUNNING.value,
        started_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )

    msg = await fail_agent_run(session, row, ValueError("boom"))

    assert msg == "boom"
    assert row.status == AgentRunStatus.FAILED.value
    assert row.error == "boom"
    assert "failed to persist FAILED status" in caplog.text
