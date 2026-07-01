"""Runner agent tests (adaptive, Phase 2).

The Runner is deterministic (no LLM). It owns the choreography:
- copy strategy.json into fwbg's strategies_dir
- for each universe rung (suggested symbols -> class -> unconstrained):
    - ensure data for the rung's symbols (drop unavailable ones)
    - POST /api/runs/start -> job_id, poll until completed/failed/timeout
    - first rung with non-empty metrics wins
- on success: write fwbg_results.json, transition to BACKTESTED
- if every rung is exhausted: leave strategy PROPOSED, mark AgentRun failed

Uses a scriptable FakeFwbgClient injected via the constructor.
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
from fwbg_agents.tools.fwbg_client import FwbgClientError


def _metrics_run(sharpe: float = 2.0) -> dict[str, Any]:
    return {
        "run_id": "job_test_42",
        "status": "completed",
        "assets": {
            "EURUSD": {
                "symbol": "EURUSD",
                "status": "completed",
                "unified_metrics": {
                    "sharpe": sharpe,
                    "profit_factor": 1.9,
                    "trades": 420,
                    "mc_pvalue": 0.02,
                    "max_drawdown": 0.18,
                },
            }
        },
    }


def _empty_run() -> dict[str, Any]:
    return {"run_id": "job_test_42", "status": "completed", "assets": {}}


class FakeFwbgClient:
    """Scriptable fake.

    - progress_responses: consumed per get_progress; the last one repeats once
      the queue drains (so multiple attempts each see a terminal status).
    - run_responses: consumed per get_run (one per completed attempt); repeats
      the last. Defaults to a single run with sharpe 2.0.
    - ensure_responses: symbol -> dict (or an Exception to raise). Missing
      symbols default to {"status": "ready"}.
    - ensure_status_responses: consumed per get_ensure_status; repeats "ready".
    """

    def __init__(
        self,
        *,
        start_response: dict[str, Any] | None = None,
        progress_responses: list[dict[str, Any]] | None = None,
        run_responses: list[dict[str, Any]] | None = None,
        ensure_responses: dict[str, Any] | None = None,
        ensure_status_responses: list[dict[str, Any]] | None = None,
    ):
        self.start_response = start_response or {"job_id": "job_test_42", "status": "running"}
        self._progress_q = list(progress_responses or [{"status": "completed"}])
        self._last_progress = {"status": "completed"}
        self._run_q = list(run_responses) if run_responses is not None else [_metrics_run()]
        self._last_run = self._run_q[0]
        self.ensure_responses = ensure_responses or {}
        self._ensure_status_q = list(ensure_status_responses or [])
        self.calls: list[tuple[str, tuple, dict]] = []

    def calls_of(self, name: str) -> list[tuple[str, tuple, dict]]:
        return [c for c in self.calls if c[0] == name]

    async def start_run(self, strategy_name, *, assets=None, asset_classes=None, **kwargs):
        self.calls.append(
            ("start_run", (strategy_name,), {"assets": assets, "asset_classes": asset_classes})
        )
        return dict(self.start_response)

    async def get_progress(self, run_id):
        self.calls.append(("get_progress", (run_id,), {}))
        if self._progress_q:
            self._last_progress = self._progress_q.pop(0)
        return self._last_progress

    async def get_run(self, run_id):
        self.calls.append(("get_run", (run_id,), {}))
        if self._run_q:
            self._last_run = self._run_q.pop(0)
        return self._last_run

    async def ensure_data(self, symbol, *, timeframe=None, **kwargs):
        self.calls.append(("ensure_data", (symbol,), {"timeframe": timeframe}))
        resp = self.ensure_responses.get(symbol, {"status": "ready"})
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def get_ensure_status(self, task_id):
        self.calls.append(("get_ensure_status", (task_id,), {}))
        if self._ensure_status_q:
            return self._ensure_status_q.pop(0)
        return {"status": "ready"}


# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def runner_env(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    monkeypatch.setattr(settings, "fwbg_strategies_dir", tmp_path / "fwbg_strategies")
    # Make poll loops fast so tests take ~ms.
    monkeypatch.setattr(settings, "runner_poll_interval_seconds", 0.001)
    monkeypatch.setattr(settings, "runner_poll_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "data_ensure_poll_interval_seconds", 0.001)
    monkeypatch.setattr(settings, "data_ensure_timeout_seconds", 5.0)

    db_url = f"sqlite+aiosqlite:///{tmp_path}/runner.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def make_strategy(
        *, slug="demo_orb_v1", asset_class="FOREX", suggested_universe=None, seed_json=True
    ) -> int:
        async with Session() as setup:
            now = datetime.now(UTC)
            s = Strategy(
                slug=slug,
                current_state=StrategyState.PROPOSED.value,
                iteration_count=0,
                asset_class=asset_class,
                strategy_family="ORB",
                suggested_universe=suggested_universe,
                created_at=now,
                updated_at=now,
            )
            setup.add(s)
            await setup.commit()
            await setup.refresh(s)
            sid = s.id
        if seed_json:
            it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
            it_dir.mkdir(parents=True, exist_ok=True)
            (it_dir / "strategy.json").write_text(json.dumps({"name": slug}))
        return sid

    yield Session, make_strategy, tmp_path

    await engine.dispose()


async def _run(SessionMaker, sid, fake) -> Any:
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        return await Runner(fake, session).run(s)


async def test_runner_happy_path_transitions_to_backtested(runner_env):
    SessionMaker, make_strategy, tmp_path = runner_env
    sid = await make_strategy()  # asset_class=FOREX, no suggestion -> class rung
    fake = FakeFwbgClient(
        progress_responses=[{"status": "running"}, {"status": "running"}, {"status": "completed"}],
    )

    result = await _run(SessionMaker, sid, fake)

    assert result.fwbg_run_id == "job_test_42"
    assert result.metrics["sharpe"] == 2.0
    assert result.universe["label"] == "class"
    # first rung uses the strategy's asset class
    start = fake.calls_of("start_run")[0]
    assert start[2]["asset_classes"] == ["FOREX"]
    assert start[2]["assets"] is None
    # artifacts on disk
    assert (tmp_path / "fwbg_strategies" / "demo_orb_v1__it001.json").is_file()
    results_path = (
        tmp_path / "agents_data" / "strategies" / "demo_orb_v1" / "iteration_001" / "fwbg_results.json"
    )
    assert json.loads(results_path.read_text())["status"] == "completed"

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.current_state == StrategyState.BACKTESTED.value
        agent_runs = (await v.execute(select(AgentRun))).scalars().all()
        assert len(agent_runs) == 1
        assert agent_runs[0].status == AgentRunStatus.DONE.value
        transitions = (await v.execute(select(Transition))).scalars().all()
        assert len(transitions) == 1
        assert transitions[0].payload["fwbg_run_id"] == "job_test_42"
        assert transitions[0].payload["backtest_metrics"]["sharpe"] == 2.0
        assert transitions[0].payload["universe"]["label"] == "class"


async def test_runner_uses_suggested_symbols_and_ensures_data(runner_env):
    SessionMaker, make_strategy, _tmp = runner_env
    sid = await make_strategy(
        suggested_universe=[
            {"scope": "symbol", "value": "EURUSD", "timeframe": "HOUR_1", "rationale": "x"},
        ],
    )
    fake = FakeFwbgClient()

    result = await _run(SessionMaker, sid, fake)

    assert result.universe["label"] == "suggested"
    # ensured data for the suggested symbol with its timeframe
    ensure = fake.calls_of("ensure_data")
    assert len(ensure) == 1
    assert ensure[0][1] == ("EURUSD",)
    assert ensure[0][2]["timeframe"] == "HOUR_1"
    # ran with exactly that symbol
    start = fake.calls_of("start_run")[0]
    assert start[2]["assets"] == ["EURUSD"]


async def test_runner_waits_for_download_then_runs(runner_env):
    SessionMaker, make_strategy, _tmp = runner_env
    sid = await make_strategy(
        suggested_universe=[{"scope": "symbol", "value": "EURUSD", "rationale": "x"}],
    )
    fake = FakeFwbgClient(
        ensure_responses={"EURUSD": {"status": "downloading", "task_id": "t1"}},
        ensure_status_responses=[{"status": "running"}, {"status": "ready"}],
    )

    result = await _run(SessionMaker, sid, fake)

    assert result.metrics["sharpe"] == 2.0
    assert len(fake.calls_of("get_ensure_status")) == 2  # polled running, then ready
    assert fake.calls_of("start_run")[0][2]["assets"] == ["EURUSD"]


async def test_runner_falls_back_when_symbol_has_no_data(runner_env):
    SessionMaker, make_strategy, _tmp = runner_env
    sid = await make_strategy(
        asset_class="FOREX",
        suggested_universe=[{"scope": "symbol", "value": "XYZ", "rationale": "x"}],
    )
    fake = FakeFwbgClient(
        ensure_responses={"XYZ": FwbgClientError(404, "No data available for 'XYZ'")},
    )

    result = await _run(SessionMaker, sid, fake)

    # suggested symbol dropped -> fell back to the class rung
    assert result.universe["label"] == "class"
    starts = fake.calls_of("start_run")
    assert len(starts) == 1  # symbol rung was skipped before starting a run
    assert starts[0][2]["asset_classes"] == ["FOREX"]


async def test_runner_falls_back_on_empty_metrics(runner_env):
    SessionMaker, make_strategy, _tmp = runner_env
    sid = await make_strategy(
        asset_class="FOREX",
        suggested_universe=[{"scope": "symbol", "value": "EURUSD", "rationale": "x"}],
    )
    # first completed run has no assets -> no metrics -> fall back; second has metrics
    fake = FakeFwbgClient(run_responses=[_empty_run(), _metrics_run(sharpe=1.5)])

    result = await _run(SessionMaker, sid, fake)

    assert result.metrics["sharpe"] == 1.5
    assert result.universe["label"] == "class"
    starts = fake.calls_of("start_run")
    assert starts[0][2]["assets"] == ["EURUSD"]      # attempt 1: suggested
    assert starts[1][2]["asset_classes"] == ["FOREX"]  # attempt 2: class


async def test_runner_all_attempts_fail_no_transition(runner_env):
    SessionMaker, make_strategy, _tmp = runner_env
    sid = await make_strategy()  # -> class, unconstrained rungs
    fake = FakeFwbgClient(progress_responses=[{"status": "failed", "message": "data missing"}])

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        with pytest.raises(RunnerError) as exc:
            await Runner(fake, session).run(s)
        assert "exhausted" in str(exc.value).lower()

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.current_state == StrategyState.PROPOSED.value  # unchanged
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.FAILED.value
        assert (await v.execute(select(Transition))).scalars().all() == []
    # both rungs were attempted
    assert len(fake.calls_of("start_run")) == 2


async def test_runner_timeout_no_transition(runner_env, monkeypatch):
    SessionMaker, make_strategy, _tmp = runner_env
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "runner_poll_timeout_seconds", 0.01)
    sid = await make_strategy()
    fake = FakeFwbgClient(progress_responses=[{"status": "running"}])  # never terminal

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        with pytest.raises(RunnerError) as exc:
            await Runner(fake, session).run(s)
        assert "timeout" in str(exc.value).lower()

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.current_state == StrategyState.PROPOSED.value


async def test_runner_missing_strategy_json_raises(runner_env):
    SessionMaker, make_strategy, tmp_path = runner_env
    sid = await make_strategy(seed_json=False)

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        with pytest.raises(FileNotFoundError):
            await Runner(FakeFwbgClient(), session).run(s)
