"""Runner agent tests.

The Runner is deterministic (no LLM). It owns the choreography:
- copy strategy.json into fwbg's strategies_dir
- POST /api/runs/start → job_id
- poll get_progress until completed/failed/timeout
- on completion: fetch full run, write fwbg_results.json, transition to BACKTESTED
- on failure/timeout: leave strategy in PROPOSED, mark AgentRun failed

Uses a FakeFwbgClient injected via the constructor instead of httpx.MockTransport
— easier to script per-call return values for polling.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.runner import Runner, RunnerError
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
    Transition,
)


class FakeFwbgClient:
    """Scriptable fake — each method returns the next item from its queue."""

    def __init__(
        self,
        start_response: dict[str, Any] | None = None,
        progress_responses: list[dict[str, Any]] | None = None,
        run_response: dict[str, Any] | None = None,
    ):
        self.start_response = start_response or {"job_id": "job_test_42", "status": "running"}
        self.progress_responses = list(progress_responses or [{"status": "completed"}])
        self.run_response = run_response or {
            "run_id": "job_test_42",
            "status": "completed",
            "assets": {
                "EURUSD": {
                    "symbol": "EURUSD",
                    "status": "completed",
                    "unified_metrics": {
                        "sharpe": 2.0,
                        "profit_factor": 1.9,
                        "trades": 420,
                        "mc_pvalue": 0.02,
                        "max_drawdown": 0.18,
                    },
                }
            },
        }
        self.calls: list[tuple[str, tuple, dict]] = []

    async def start_run(self, *args, **kwargs):
        self.calls.append(("start_run", args, kwargs))
        return self.start_response

    async def get_progress(self, run_id):
        self.calls.append(("get_progress", (run_id,), {}))
        if not self.progress_responses:
            return {"status": "completed"}
        return self.progress_responses.pop(0)

    async def get_run(self, run_id):
        self.calls.append(("get_run", (run_id,), {}))
        return self.run_response


# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_and_strategy(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    monkeypatch.setattr(settings, "fwbg_strategies_dir", tmp_path / "fwbg_strategies")
    # Make poll loops fast so the test takes ~ms.
    monkeypatch.setattr(settings, "runner_poll_interval_seconds", 0.001)
    monkeypatch.setattr(settings, "runner_poll_timeout_seconds", 5.0)

    db_url = f"sqlite+aiosqlite:///{tmp_path}/runner.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug="demo_orb_v1",
            current_state=StrategyState.PROPOSED.value,
            iteration_count=0,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.commit()
        await setup.refresh(s)
        strategy_id = s.id

    # Seed iteration_001/strategy.json on disk.
    it_dir = settings.data_dir / "strategies" / "demo_orb_v1" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(json.dumps({"name": "demo_orb_v1"}))

    yield Session, strategy_id, tmp_path

    await engine.dispose()


async def test_runner_happy_path_transitions_to_backtested(db_and_strategy):
    SessionMaker, strategy_id, tmp_path = db_and_strategy
    fake = FakeFwbgClient(
        progress_responses=[{"status": "running"}, {"status": "running"}, {"status": "completed"}],
    )

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        runner = Runner(fake, session)
        result = await runner.run(s)

    assert result.fwbg_run_id == "job_test_42"
    assert result.metrics["sharpe"] == 2.0
    # strategy.json was copied to fwbg's strategies dir
    copied = tmp_path / "fwbg_strategies" / "demo_orb_v1__it001.json"
    assert copied.is_file()
    # fwbg_results.json was saved in the iteration dir
    results_path = (
        tmp_path / "agents_data" / "strategies" / "demo_orb_v1"
        / "iteration_001" / "fwbg_results.json"
    )
    assert results_path.is_file()
    assert json.loads(results_path.read_text())["status"] == "completed"

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        assert s.current_state == StrategyState.BACKTESTED.value
        agent_runs = (await v.execute(select(AgentRun))).scalars().all()
        assert len(agent_runs) == 1
        assert agent_runs[0].status == AgentRunStatus.DONE.value
        assert agent_runs[0].agent_name == "runner"
        assert agent_runs[0].strategy_id == strategy_id
        transitions = (await v.execute(select(Transition))).scalars().all()
        assert len(transitions) == 1
        assert transitions[0].to_state == StrategyState.BACKTESTED.value
        assert transitions[0].payload["fwbg_run_id"] == "job_test_42"
        assert transitions[0].payload["backtest_metrics"]["sharpe"] == 2.0


async def test_runner_fwbg_failed_no_transition(db_and_strategy):
    SessionMaker, strategy_id, _tmp = db_and_strategy
    fake = FakeFwbgClient(progress_responses=[{"status": "failed", "message": "data missing"}])

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        runner = Runner(fake, session)
        with pytest.raises(RunnerError) as exc:
            await runner.run(s)
        assert "failed" in str(exc.value).lower()

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        assert s.current_state == StrategyState.PROPOSED.value  # unchanged
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.FAILED.value
        assert (await v.execute(select(Transition))).scalars().all() == []


async def test_runner_timeout_no_transition(db_and_strategy, monkeypatch):
    SessionMaker, strategy_id, _tmp = db_and_strategy
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "runner_poll_timeout_seconds", 0.01)
    # Always return running → loop should hit the timeout.
    fake = FakeFwbgClient(progress_responses=[{"status": "running"}] * 1000)

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        runner = Runner(fake, session)
        with pytest.raises(RunnerError) as exc:
            await runner.run(s)
        assert "timeout" in str(exc.value).lower()

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        assert s.current_state == StrategyState.PROPOSED.value


async def test_runner_missing_strategy_json_raises(db_and_strategy):
    SessionMaker, strategy_id, tmp_path = db_and_strategy
    (
        tmp_path / "agents_data" / "strategies" / "demo_orb_v1"
        / "iteration_001" / "strategy.json"
    ).unlink()

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        runner = Runner(FakeFwbgClient(), session)
        with pytest.raises(FileNotFoundError):
            await runner.run(s)
