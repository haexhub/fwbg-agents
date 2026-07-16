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
    monkeypatch.setattr(settings, "plugin_author_auto_max_attempts", 2)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/auto.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(auto_runner, "SessionLocal", Session)

    async def make_strategy(slug: str, *, state=StrategyState.PROPOSED, seed_json=True) -> int:
        async with Session() as s:
            now = datetime.now(UTC)
            row = Strategy(
                slug=slug,
                current_state=state.value,
                iteration_count=1,
                asset_class="FOREX",
                strategy_family="ORB",
                created_at=now,
                updated_at=now,
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

    async def add_run(
        agent: str, status: AgentRunStatus, strategy_id: int | None = None, error: str | None = None
    ):
        async with Session() as s:
            now = datetime.now(UTC)
            s.add(
                AgentRun(
                    agent_name=agent,
                    status=status.value,
                    error=error,
                    strategy_id=strategy_id,
                    started_at=now,
                    created_at=now,
                )
            )
            await s.commit()

    yield Session, make_strategy, add_run
    await engine.dispose()


async def test_toggle_is_persisted(env):
    assert await auto_runner.is_enabled() is False  # default off
    await auto_runner.set_enabled(True)
    assert await auto_runner.is_enabled() is True
    await auto_runner.set_enabled(False)
    assert await auto_runner.is_enabled() is False


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
    await auto_runner.set_enabled(False)
    assert await auto_runner.tick() is None


async def test_tick_runs_the_picked_strategy(env, monkeypatch):
    _, make_strategy, _ = env
    sid = await make_strategy("orb__forex__001")
    await auto_runner.set_enabled(True)

    ran: list[int] = []

    class _FakeRunner:
        def __init__(self, client, session):
            pass

        async def run(self, strategy):
            ran.append(strategy.id)

    class _FakeClient:
        def __init__(self, base_url=None, api_key=None):
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


def _seed_sidecar(slug: str) -> None:
    from fwbg_agents.config import settings

    (
        settings.data_dir / "strategies" / slug / "iteration_001" / "add_indicator_request.json"
    ).write_text(json.dumps({"capability": "pivot zones", "phase": "indicators"}))


async def test_pick_next_add_indicator_pending(env):
    Session, make_strategy, add_run = env
    sid = await make_strategy("orb__forex__ai1", state=StrategyState.BACKTESTED)
    _seed_sidecar("orb__forex__ai1")

    async with Session() as session:
        assert await auto_runner.pick_next_add_indicator_pending(session) == sid

    # A failed plugin_planner does NOT yet consume the budget (cap=2).
    await add_run("plugin_planner", AgentRunStatus.FAILED, sid)
    async with Session() as session:
        assert await auto_runner.pick_next_add_indicator_pending(session) == sid

    # Second failed planner hits the cap → no more auto-retries.
    await add_run("plugin_planner", AgentRunStatus.FAILED, sid)
    async with Session() as session:
        assert await auto_runner.pick_next_add_indicator_pending(session) is None

    # A successful plugin_implementer also closes the budget (chain completed).
    sid2 = await make_strategy("orb__forex__ai1b", state=StrategyState.BACKTESTED)
    _seed_sidecar("orb__forex__ai1b")
    await add_run("plugin_planner", AgentRunStatus.DONE, sid2)
    await add_run("plugin_implementer", AgentRunStatus.DONE, sid2)
    async with Session() as session:
        assert await auto_runner.pick_next_add_indicator_pending(session) is None


async def test_pick_next_add_indicator_skips_in_flight_chain(env):
    """A chain that is RUNNING or PENDING blocks the auto pick — two chains
    must never race on the same sidecar."""
    Session, make_strategy, add_run = env
    sid = await make_strategy("orb__forex__ai3", state=StrategyState.BACKTESTED)
    _seed_sidecar("orb__forex__ai3")

    for status in (AgentRunStatus.RUNNING, AgentRunStatus.PENDING):
        for agent in ("plugin_planner", "plugin_implementer", "plugin_author_flow"):
            await add_run(agent, status, sid)
            async with Session() as session:
                assert await auto_runner.pick_next_add_indicator_pending(session) is None
            async with Session() as s:
                await s.execute(AgentRun.__table__.delete().where(AgentRun.strategy_id == sid))
                await s.commit()

    async with Session() as session:
        assert await auto_runner.pick_next_add_indicator_pending(session) == sid


async def test_pick_next_add_indicator_ignores_orphaned_failures(env):
    """Orphaned planner failures (service restarts) were not the strategy's
    fault and must not consume the auto plugin-author budget."""
    from fwbg_agents.orchestrator.run_janitor import ORPHAN_ERROR

    Session, make_strategy, add_run = env
    sid = await make_strategy("orb__forex__ai4", state=StrategyState.BACKTESTED)
    _seed_sidecar("orb__forex__ai4")

    await add_run("plugin_planner", AgentRunStatus.FAILED, sid, error=ORPHAN_ERROR)
    await add_run("plugin_planner", AgentRunStatus.FAILED, sid, error=ORPHAN_ERROR)
    async with Session() as session:
        assert await auto_runner.pick_next_add_indicator_pending(session) == sid


async def test_pick_next_add_indicator_requires_sidecar(env):
    Session, make_strategy, _ = env
    await make_strategy("orb__forex__nosc", state=StrategyState.BACKTESTED)
    async with Session() as session:
        assert await auto_runner.pick_next_add_indicator_pending(session) is None


async def test_author_and_reiterate_happy_path(env, monkeypatch):
    from fwbg_agents.persistence.models import Plugin, PluginState

    Session, make_strategy, _ = env
    sid = await make_strategy("orb__forex__ai2", state=StrategyState.BACKTESTED)

    async with Session() as s:
        now = datetime.now(UTC)
        plugin = Plugin(
            slug="pivot_zones",
            current_state=PluginState.VERIFIED.value,
            kind="indicator",
            created_at=now,
            updated_at=now,
        )
        s.add(plugin)
        await s.commit()
        await s.refresh(plugin)
        plugin_id = plugin.id

    calls: dict[str, object] = {}

    async def fake_author(session, strategy_id):
        calls["author"] = strategy_id
        return plugin_id

    async def fake_evaluate(session, pid, **_kwargs):
        calls["evaluate"] = pid
        return 1

    async def fake_reiterate_with_plugin(session, strategy_id, plugin_slug):
        calls["reiterate"] = (strategy_id, plugin_slug)
        return 999

    monkeypatch.setattr(auto_runner, "author_plugin_from_strategy", fake_author)
    monkeypatch.setattr(auto_runner, "evaluate_plugin", fake_evaluate)
    monkeypatch.setattr(auto_runner, "reiterate_with_plugin", fake_reiterate_with_plugin)

    async with Session() as session:
        await auto_runner._author_and_reiterate(session, sid)

    assert calls == {
        "author": sid,
        "evaluate": plugin_id,
        "reiterate": (sid, "pivot_zones"),
    }

    # Verified plugin → the evaluator run is DONE.
    async with Session() as s:
        ar = (
            await s.execute(select(AgentRun).where(AgentRun.agent_name == "plugin_evaluator"))
        ).scalar_one()
        assert ar.status == AgentRunStatus.DONE.value


async def test_author_and_reiterate_stops_when_plugin_unverified(env, monkeypatch):
    from fwbg_agents.persistence.models import Plugin, PluginState

    Session, make_strategy, _ = env
    sid = await make_strategy("orb__forex__ai3", state=StrategyState.BACKTESTED)

    async with Session() as s:
        now = datetime.now(UTC)
        plugin = Plugin(
            slug="broken_plugin",
            current_state=PluginState.AUTHORED.value,
            kind="indicator",
            created_at=now,
            updated_at=now,
        )
        s.add(plugin)
        await s.commit()
        await s.refresh(plugin)
        plugin_id = plugin.id

    async def fake_author(session, strategy_id):
        return plugin_id

    async def fake_evaluate(session, pid, **_kwargs):
        return 1  # evaluation ran but did not verify

    async def fail_reiterate(session, strategy_id, plugin_slug):
        raise AssertionError("must not reiterate with an unverified plugin")

    monkeypatch.setattr(auto_runner, "author_plugin_from_strategy", fake_author)
    monkeypatch.setattr(auto_runner, "evaluate_plugin", fake_evaluate)
    monkeypatch.setattr(auto_runner, "reiterate_with_plugin", fail_reiterate)

    async with Session() as session:
        await auto_runner._author_and_reiterate(session, sid)  # must not raise

    # Non-verifying evaluation → the evaluator run is FAILED, not DONE.
    async with Session() as s:
        ar = (
            await s.execute(select(AgentRun).where(AgentRun.agent_name == "plugin_evaluator"))
        ).scalar_one()
        assert ar.status == AgentRunStatus.FAILED.value
        assert ar.error is not None


async def _run_analyze_with_fake_analyst(Session, monkeypatch, sid):
    """Drive _analyze_and_apply with a canned TuneParams recommendation."""
    from fwbg_agents.agents.analyst import TuneParams

    class _FakeClient:
        def __init__(self, *a, **kw): ...
        async def aclose(self): ...

    class _FakeAnalyst:
        def __init__(self, session, **kw): ...

        async def analyze(self, s):
            return TuneParams(
                confidence=0.6,
                reasoning="x",
                params=[{"param": "sl_mult", "new_range": [1.0, 2.0]}],
            )

    reiterated: list[int] = []

    async def fake_reiterate(session, strategy_id, **kwargs):
        reiterated.append(strategy_id)
        return 999

    monkeypatch.setattr(auto_runner, "FwbgClient", _FakeClient)
    monkeypatch.setattr(auto_runner, "Analyst", _FakeAnalyst)
    monkeypatch.setattr(auto_runner, "reiterate", fake_reiterate)

    async with Session() as session:
        await auto_runner._analyze_and_apply(session, sid)
    return reiterated


async def test_analyze_and_apply_respects_depth_cap(env, monkeypatch):
    from fwbg_agents.config import settings

    Session, make_strategy, _ = env
    sid = await make_strategy("orb__forex__deep", state=StrategyState.BACKTESTED)

    monkeypatch.setattr(settings, "reiterate_max_depth", 1)  # root is already at 1
    reiterated = await _run_analyze_with_fake_analyst(Session, monkeypatch, sid)
    assert reiterated == []


async def test_analyze_and_apply_reiterates_below_depth_cap(env, monkeypatch):
    from fwbg_agents.config import settings

    Session, make_strategy, _ = env
    sid = await make_strategy("orb__forex__shallow", state=StrategyState.BACKTESTED)

    monkeypatch.setattr(settings, "reiterate_max_depth", 5)
    reiterated = await _run_analyze_with_fake_analyst(Session, monkeypatch, sid)
    assert reiterated == [sid]


# ---------------------------------------------------------------------------
# Improvement-based abandon override
# ---------------------------------------------------------------------------


def _seed_results_metrics(slug: str, sharpe: float) -> None:
    """Write a minimal fwbg_results.json with the given Sharpe for slug."""
    from fwbg_agents.config import settings

    it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "fwbg_results.json").write_text(
        json.dumps({"assets": {"EURUSD": {"unified_metrics": {"sharpe": sharpe}}}})
    )


async def _run_analyze_with_abandon(Session, monkeypatch, sid):
    """Drive _analyze_and_apply with a canned Abandon recommendation."""
    from fwbg_agents.agents.analyst import Abandon

    class _FakeClient:
        def __init__(self, *a, **kw): ...
        async def aclose(self): ...

    class _FakeAnalyst:
        def __init__(self, session, **kw): ...

        async def analyze(self, s):
            return Abandon(
                confidence=0.8,
                reasoning="consistently negative returns",
                post_mortem_summary="never profitable",
                lessons=["avoid ORB on FOREX"],
            )

    reiterated: list[int] = []

    async def fake_reiterate(session, strategy_id, **kwargs):
        reiterated.append(strategy_id)
        return 999

    monkeypatch.setattr(auto_runner, "FwbgClient", _FakeClient)
    monkeypatch.setattr(auto_runner, "Analyst", _FakeAnalyst)
    monkeypatch.setattr(auto_runner, "reiterate", fake_reiterate)

    # validate_and_apply would fail on an unresolvable Abandon in tests; stub it.
    async def fake_validate(session, strategy, rec, **kw):
        pass

    monkeypatch.setattr(auto_runner, "validate_and_apply", fake_validate)

    async with Session() as session:
        await auto_runner._analyze_and_apply(session, sid)
    return reiterated


async def test_abandon_at_min_depth_without_improvement_passes_through(env, monkeypatch):
    """Abandon at depth >= min_iterations_before_abandon with no metric improvement
    is not overridden — the recommendation passes through to validate_and_apply."""
    from fwbg_agents.config import settings

    Session, make_strategy, _ = env
    monkeypatch.setattr(settings, "min_iterations_before_abandon", 1)
    monkeypatch.setattr(settings, "reiterate_max_depth", 10)

    sid = await make_strategy("orb__forex__noimprov", state=StrategyState.BACKTESTED)
    # Flat Sharpe — no improvement.
    _seed_results_metrics("orb__forex__noimprov", sharpe=0.2)

    reiterated = await _run_analyze_with_abandon(Session, monkeypatch, sid)
    assert reiterated == []  # no override, no reiterate


async def test_abandon_at_min_depth_with_improvement_overrides(env, monkeypatch):
    """Abandon at depth >= min_iterations_before_abandon but with a rising Sharpe
    is overridden with a synthetic tune_params iteration."""
    from datetime import UTC, datetime

    from fwbg_agents.config import settings

    Session, make_strategy, _ = env
    monkeypatch.setattr(settings, "min_iterations_before_abandon", 1)
    monkeypatch.setattr(settings, "reiterate_max_depth", 10)

    # Build a two-member chain: root with Sharpe 0.3, child with Sharpe 0.9.
    root_id = await make_strategy("orb__forex__root_improv", state=StrategyState.BACKTESTED)
    _seed_results_metrics("orb__forex__root_improv", sharpe=0.3)
    root_it = settings.data_dir / "strategies" / "orb__forex__root_improv" / "iteration_001"
    (root_it / "strategy.json").write_text(
        json.dumps({"exit_strategies": [{"params": {"sl_mult": 1.5}}]})
    )

    async with Session() as s:
        now = datetime.now(UTC)
        child = Strategy(
            slug="orb__forex__child_improv",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="ORB",
            parent_strategy_id=root_id,
            created_at=now,
            updated_at=now,
        )
        s.add(child)
        await s.commit()
        await s.refresh(child)
        child_sid = child.id

    child_slug = "orb__forex__child_improv"
    _seed_results_metrics(child_slug, sharpe=0.9)
    (settings.data_dir / "strategies" / child_slug / "iteration_001" / "strategy.json").write_text(
        json.dumps({"exit_strategies": [{"params": {"sl_mult": 1.5}}]})
    )

    reiterated = await _run_analyze_with_abandon(Session, monkeypatch, child_sid)
    assert reiterated == [child_sid]  # override fired, reiterate called


class _RepairRunner:
    """Fake Runner that fails with a fixable missing-dependency config error."""

    def __init__(self, client, session):
        pass

    async def run(self, strategy):
        from fwbg_agents.agents.runner import RunnerConfigError

        raise RunnerConfigError(
            "fwbg backtest failed: regime_cluster depends on regime",
            dependent="regime_cluster",
            dependency="regime",
        )


class _NoopClient:
    def __init__(self, base_url=None, api_key=None):
        pass

    async def aclose(self):
        pass


async def test_tick_auto_repairs_missing_dependency(env, monkeypatch):
    Session, make_strategy, _ = env
    from fwbg_agents.config import settings

    sid = await make_strategy("liquiditysweep__forex__026")
    await auto_runner.set_enabled(True)

    reiterated: list[int] = []

    async def fake_reiterate(session, strategy_id, **kwargs):
        reiterated.append(strategy_id)
        return 999

    async def fake_depth(session, strategy):
        return 0

    monkeypatch.setattr(auto_runner, "Runner", _RepairRunner)
    monkeypatch.setattr(auto_runner, "FwbgClient", _NoopClient)
    monkeypatch.setattr(auto_runner, "reiterate", fake_reiterate)
    monkeypatch.setattr(auto_runner, "generation_depth", fake_depth)

    assert await auto_runner.tick() == sid
    assert reiterated == [sid]

    sidecar = json.loads(
        (
            settings.data_dir
            / "strategies"
            / "liquiditysweep__forex__026"
            / "iteration_001"
            / "analyst_recommendation.json"
        ).read_text()
    )
    assert sidecar["kind"] == "modify_plugins"
    assert sidecar["ops"][0] == {
        "action": "add",
        "section": "indicators",
        "slug": "regime",
        "params": {},
        "before": "regime_cluster",
    }

    # The broken parent is superseded so the auto-runner will not re-backtest it
    # (which would spawn duplicate repair children).
    async with Session() as session:
        parent = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
    assert parent.current_state == StrategyState.ABANDONED.value


async def test_auto_repair_skipped_at_max_depth(env, monkeypatch):
    Session, make_strategy, _ = env
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "reiterate_max_depth", 3)
    sid = await make_strategy("liquiditysweep__forex__027")
    await auto_runner.set_enabled(True)

    reiterated: list[int] = []

    async def fake_reiterate(session, strategy_id, **kwargs):
        reiterated.append(strategy_id)
        return 999

    async def fake_depth(session, strategy):
        return 3  # already at the cap

    monkeypatch.setattr(auto_runner, "Runner", _RepairRunner)
    monkeypatch.setattr(auto_runner, "FwbgClient", _NoopClient)
    monkeypatch.setattr(auto_runner, "reiterate", fake_reiterate)
    monkeypatch.setattr(auto_runner, "generation_depth", fake_depth)

    await auto_runner.tick()

    assert reiterated == []  # no repair attempted
    async with Session() as session:
        parent = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
    assert parent.current_state == StrategyState.PROPOSED.value  # left for the retry cap


def test_dependent_pipeline_section_resolves_from_parent_pipeline():
    """The missing dependency is inserted into the same section that holds the
    dependent — resolved from the parent's own inline pipeline."""
    strategy_json = {
        "pipeline": {
            "indicators": [{"name": "opening_range"}],
            "feature_selection": [{"name": "some_selector"}],
        }
    }
    assert auto_runner._dependent_pipeline_section(strategy_json, "some_selector") == (
        "feature_selection"
    )
    assert auto_runner._dependent_pipeline_section(strategy_json, "opening_range") == "indicators"
    # Unknown dependent / non-inline pipeline → safe default.
    assert auto_runner._dependent_pipeline_section(strategy_json, "mystery") == "indicators"
    assert auto_runner._dependent_pipeline_section({}, "regime_cluster") == "indicators"
