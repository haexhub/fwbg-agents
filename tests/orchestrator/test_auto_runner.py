"""Runner auto mode — persisted toggle, single-flight picking, retry cap."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator import auto_runner
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

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/auto.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(auto_runner, "SessionLocal", Session)

    async def make_strategy(slug: str, *, state=StrategyState.PROPOSED, seed_json=True) -> int:
        async with Session() as s:
            now = datetime.now(UTC)
            row = Strategy(
                slug=slug, current_state=state.value, iteration_count=1,
                asset_class="FOREX", strategy_family="ORB",
                created_at=now, updated_at=now,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
            sid = row.id
        if seed_json:
            it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
            it_dir.mkdir(parents=True, exist_ok=True)
            (it_dir / "strategy.json").write_text(json.dumps({"name": slug}))
        return sid

    async def add_run(agent: str, status: AgentRunStatus, strategy_id: int | None = None):
        async with Session() as s:
            now = datetime.now(UTC)
            s.add(AgentRun(agent_name=agent, status=status.value,
                           strategy_id=strategy_id, started_at=now, created_at=now))
            await s.commit()

    yield Session, make_strategy, add_run
    await engine.dispose()


def test_toggle_is_persisted(env, tmp_path):
    assert auto_runner.is_enabled() is False  # default off
    auto_runner.set_enabled(True)
    assert auto_runner.is_enabled() is True
    auto_runner.set_enabled(False)
    assert auto_runner.is_enabled() is False


async def test_picks_oldest_ready_proposed(env):
    Session, make_strategy, _ = env
    first = await make_strategy("orb__forex__001")
    await make_strategy("orb__forex__002")

    async with Session() as session:
        assert await auto_runner.pick_next_strategy_id(session) == first


async def test_skips_untranslated_and_non_proposed(env):
    Session, make_strategy, _ = env
    await make_strategy("orb__forex__001", seed_json=False)  # no strategy.json yet
    await make_strategy("orb__forex__002", state=StrategyState.BACKTESTED)
    ready = await make_strategy("orb__forex__003")

    async with Session() as session:
        assert await auto_runner.pick_next_strategy_id(session) == ready


async def test_single_flight_while_runner_active(env):
    Session, make_strategy, add_run = env
    await make_strategy("orb__forex__001")

    from sqlalchemy import update

    await add_run("runner", AgentRunStatus.RUNNING)
    async with Session() as session:
        assert await auto_runner.pick_next_strategy_id(session) is None

    async with Session() as s:
        await s.execute(update(AgentRun).values(status=AgentRunStatus.DONE.value))
        await s.commit()

    async with Session() as session:
        assert await auto_runner.pick_next_strategy_id(session) is not None


async def test_research_does_not_block_backtest(env):
    Session, make_strategy, add_run = env
    await make_strategy("orb__forex__001")

    for research_agent in ("research_flow", "reiterate"):
        await add_run(research_agent, AgentRunStatus.RUNNING)
        async with Session() as session:
            assert await auto_runner.pick_next_strategy_id(session) is not None


async def test_retry_cap_skips_repeatedly_failing_strategy(env):
    Session, make_strategy, add_run = env
    flaky = await make_strategy("orb__forex__001")
    healthy = await make_strategy("orb__forex__002")

    await add_run("runner", AgentRunStatus.FAILED, strategy_id=flaky)
    async with Session() as session:
        assert await auto_runner.pick_next_strategy_id(session) == flaky  # 1 fail: retry

    await add_run("runner", AgentRunStatus.FAILED, strategy_id=flaky)
    async with Session() as session:
        assert await auto_runner.pick_next_strategy_id(session) == healthy  # capped


async def test_tick_disabled_does_nothing(env):
    _, make_strategy, _ = env
    await make_strategy("orb__forex__001")
    auto_runner.set_enabled(False)
    assert await auto_runner.tick() is None


async def test_tick_runs_the_picked_strategy(env, monkeypatch):
    _, make_strategy, _ = env
    sid = await make_strategy("orb__forex__001")
    auto_runner.set_enabled(True)

    ran: list[int] = []

    class _FakeRunner:
        def __init__(self, client, session):
            pass

        async def run(self, strategy):
            ran.append(strategy.id)

    class _FakeClient:
        def __init__(self, base_url=None):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(auto_runner, "Runner", _FakeRunner)
    monkeypatch.setattr(auto_runner, "FwbgClient", _FakeClient)

    assert await auto_runner.tick() == sid
    assert ran == [sid]


async def test_abandon_capped_proposed_frees_the_queue(env):
    Session, make_strategy, add_run = env
    capped = await make_strategy("orb__forex__001")
    healthy = await make_strategy("orb__forex__002")
    await add_run("runner", AgentRunStatus.FAILED, strategy_id=capped)
    await add_run("runner", AgentRunStatus.FAILED, strategy_id=capped)  # 2 → capped
    await add_run("runner", AgentRunStatus.FAILED, strategy_id=healthy)  # 1 → ok

    async with Session() as session:
        assert await auto_runner.abandon_capped_proposed(session) == 1

    async with Session() as session:
        rows = {r.id: r for r in (await session.execute(select(Strategy))).scalars().all()}
    assert rows[capped].current_state == StrategyState.ABANDONED.value
    assert rows[healthy].current_state == StrategyState.PROPOSED.value


async def test_pick_next_backtested_unanalyzed(env):
    from fwbg_agents.config import settings

    Session, make_strategy, add_run = env

    def _seed_results(slug: str):
        (
            settings.data_dir / "strategies" / slug / "iteration_001" / "fwbg_results.json"
        ).write_text("{}")

    # backtested, has results, no analyst run → picked
    bt = await make_strategy("orb__forex__001", state=StrategyState.BACKTESTED)
    _seed_results("orb__forex__001")
    # backtested but already analyzed → skipped
    done = await make_strategy("orb__forex__002", state=StrategyState.BACKTESTED)
    _seed_results("orb__forex__002")
    await add_run("analyst", AgentRunStatus.DONE, strategy_id=done)
    # backtested but no results file → skipped
    await make_strategy("orb__forex__003", state=StrategyState.BACKTESTED)

    async with Session() as session:
        assert await auto_runner.pick_next_backtested_unanalyzed(session) == bt
