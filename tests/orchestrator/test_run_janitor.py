"""Startup janitor — orphaned PENDING/RUNNING runs are failed, and those
orphan-failures do not eat auto-runner retry attempts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator import auto_runner, run_janitor
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "runner_auto_max_attempts", 2)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/janitor.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(run_janitor, "SessionLocal", Session)
    monkeypatch.setattr(auto_runner, "SessionLocal", Session)

    async def add_run(
        agent: str,
        status: AgentRunStatus,
        strategy_id: int | None = None,
        error: str | None = None,
    ) -> int:
        async with Session() as s:
            now = datetime.now(UTC)
            row = AgentRun(
                agent_name=agent, status=status.value, strategy_id=strategy_id,
                error=error, started_at=now, created_at=now,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row.id

    async def make_strategy(slug: str) -> int:
        async with Session() as s:
            now = datetime.now(UTC)
            row = Strategy(
                slug=slug, current_state=StrategyState.PROPOSED.value,
                iteration_count=1, asset_class="FOREX", strategy_family="ORB",
                created_at=now, updated_at=now,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            sid = row.id
        it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
        it_dir.mkdir(parents=True, exist_ok=True)
        (it_dir / "strategy.json").write_text(json.dumps({"name": slug}))
        return sid

    yield Session, add_run, make_strategy
    await engine.dispose()


async def test_orphans_are_failed_and_terminal_runs_untouched(env):
    Session, add_run, _ = env
    pending_id = await add_run("runner", AgentRunStatus.PENDING)
    running_id = await add_run("research_flow", AgentRunStatus.RUNNING)
    done_id = await add_run("runner", AgentRunStatus.DONE)
    failed_id = await add_run("runner", AgentRunStatus.FAILED, error="real failure")

    assert await run_janitor.fail_orphaned_runs() == 2

    async with Session() as s:
        rows = {
            r.id: r
            for r in (await s.execute(select(AgentRun))).scalars().all()
        }
    assert rows[pending_id].status == AgentRunStatus.FAILED.value
    assert rows[pending_id].error == run_janitor.ORPHAN_ERROR
    assert rows[pending_id].ended_at is not None
    assert rows[running_id].status == AgentRunStatus.FAILED.value
    assert rows[done_id].status == AgentRunStatus.DONE.value
    assert rows[done_id].error is None
    assert rows[failed_id].error == "real failure"


async def test_noop_on_clean_db(env):
    assert await run_janitor.fail_orphaned_runs() == 0


async def test_janitor_unblocks_auto_runner_single_flight(env):
    Session, add_run, make_strategy = env
    sid = await make_strategy("orb__forex__001")
    await add_run("runner", AgentRunStatus.PENDING, strategy_id=sid)

    async with Session() as s:
        assert await auto_runner.pick_next_strategy_id(s) is None  # blocked

    await run_janitor.fail_orphaned_runs()

    async with Session() as s:
        assert await auto_runner.pick_next_strategy_id(s) == sid  # unblocked


async def test_periodic_sweep_respects_per_agent_caps(env, monkeypatch):
    """The live-process sweep fails over-long pure-LLM runs but spares young
    runs and long-running backtests (runner cap = hours)."""
    Session, _, _ = env
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "llm_run_cap_seconds", 1800)  # 30 min
    monkeypatch.setattr(settings, "runner_poll_timeout_seconds", 60 * 60 * 8)

    async def add_with_age(agent: str, minutes_ago: int) -> int:
        async with Session() as s:
            started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
            row = AgentRun(
                agent_name=agent, status=AgentRunStatus.RUNNING.value,
                started_at=started, created_at=started,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row.id

    stale_analyst = await add_with_age("analyst", minutes_ago=45)   # > 30 min cap
    young_analyst = await add_with_age("analyst", minutes_ago=5)    # < 30 min cap
    long_runner = await add_with_age("runner", minutes_ago=120)     # 2h < 8h cap

    killed = await run_janitor.sweep_stale_runs()
    assert killed == 1

    async with Session() as s:
        rows = {r.id: r for r in (await s.execute(select(AgentRun))).scalars().all()}
    assert rows[stale_analyst].status == AgentRunStatus.FAILED.value
    assert rows[stale_analyst].error == run_janitor.STALE_ERROR
    assert rows[young_analyst].status == AgentRunStatus.RUNNING.value
    assert rows[long_runner].status == AgentRunStatus.RUNNING.value


async def test_run_registry_cancels_tracked_task():
    from fwbg_agents.orchestrator import run_registry

    cancelled = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(work())
    await asyncio.sleep(0)  # let it start
    run_registry.register(42, task)

    assert run_registry.request_cancel(42) is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    # auto-deregistered on completion → a second cancel is a no-op
    assert run_registry.request_cancel(42) is False


async def test_orphan_failures_do_not_count_toward_retry_cap(env):
    Session, add_run, make_strategy = env
    sid = await make_strategy("orb__forex__001")
    # Two orphaned attempts (restarts) + one genuine failure: cap is 2, but
    # only the genuine failure may count.
    await add_run("runner", AgentRunStatus.FAILED, strategy_id=sid,
                  error=run_janitor.ORPHAN_ERROR)
    await add_run("runner", AgentRunStatus.FAILED, strategy_id=sid,
                  error=run_janitor.ORPHAN_ERROR)
    await add_run("runner", AgentRunStatus.FAILED, strategy_id=sid,
                  error="fwbg reported status='failed'")

    async with Session() as s:
        assert await auto_runner.pick_next_strategy_id(s) == sid

    await add_run("runner", AgentRunStatus.FAILED, strategy_id=sid,
                  error="fwbg reported status='failed'")

    async with Session() as s:
        assert await auto_runner.pick_next_strategy_id(s) is None  # capped
