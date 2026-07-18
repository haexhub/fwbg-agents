"""Runner agent tests (adaptive, Phase 2).

The Runner is deterministic (no LLM). It owns the choreography:
- ensure the strategy exists in fwbg via POST /api/strategies (409 = already
  published, leave untouched)
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

from fwbg_agents.agents.runner import (
    Runner,
    RunnerError,
    UnrunnableStrategyError,
    _median_metrics_across_assets,
    _months_ago_iso,
)
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


def _errored_run(*symbols: str) -> dict[str, Any]:
    """A run fwbg marked completed, but every asset errored (no metrics)."""
    syms = symbols or ("EURUSD",)
    return {
        "run_id": "job_test_42",
        "status": "completed",
        "assets": {
            s: {"symbol": s, "status": "error", "total_combinations": 0, "unified_metrics": {}}
            for s in syms
        },
    }


_DEP_LOG = [
    {
        "level": "error",
        "symbol": "EURUSD",
        "stage": "processing",
        "message": (
            "Plugin 'regime_cluster' depends on 'regime', but 'regime' is not in the "
            "pipeline. Either add 'regime' to the pipeline or remove 'regime_cluster'."
        ),
    }
]


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
        create_strategy_error: Exception | None = None,
        start_errors: list[Exception] | None = None,
        list_runs_response: list[dict[str, Any]] | None = None,
        logs_response: list[dict[str, Any]] | None = None,
    ):
        self.logs_response = list(logs_response or [])
        self.create_strategy_error = create_strategy_error
        self._start_errors = list(start_errors or [])
        self.list_runs_response = list(list_runs_response or [])
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

    async def create_strategy(self, name, data):
        self.calls.append(("create_strategy", (name,), {"data": data}))
        if self.create_strategy_error is not None:
            raise self.create_strategy_error
        return {"filename": name, "name": name, "status": "created"}

    async def start_run(self, strategy_name, *, assets=None, asset_classes=None, **kwargs):
        self.calls.append(
            (
                "start_run",
                (strategy_name,),
                {"assets": assets, "asset_classes": asset_classes, **kwargs},
            )
        )
        if self._start_errors:
            raise self._start_errors.pop(0)
        return dict(self.start_response)

    async def list_runs(self):
        self.calls.append(("list_runs", (), {}))
        return list(self.list_runs_response)

    async def get_progress(self, run_id):
        self.calls.append(("get_progress", (run_id,), {}))
        if self._progress_q:
            nxt = self._progress_q.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            self._last_progress = nxt
        return self._last_progress

    async def get_run(self, run_id):
        self.calls.append(("get_run", (run_id,), {}))
        if self._run_q:
            self._last_run = self._run_q.pop(0)
        return self._last_run

    async def get_run_logs(self, run_id, *, limit=500):
        self.calls.append(("get_run_logs", (run_id,), {"limit": limit}))
        return list(self.logs_response)

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
    # strategy was published to fwbg via POST /api/strategies (no file copy)
    create = fake.calls_of("create_strategy")[0]
    assert create[1] == ("demo_orb_v1__it001",)
    assert create[2]["data"] == {"name": "demo_orb_v1"}
    results_path = (
        tmp_path
        / "agents_data"
        / "strategies"
        / "demo_orb_v1"
        / "iteration_001"
        / "fwbg_results.json"
    )
    assert json.loads(results_path.read_text())["status"] == "completed"


async def test_runner_leaves_existing_fwbg_strategy_untouched(runner_env):
    """409 from create_strategy = the strategy is already in fwbg (research
    flow published it, user may have edited it) — the run must proceed on the
    existing file, not overwrite it."""
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient(
        create_strategy_error=FwbgClientError(409, "Strategy already exists"),
    )

    result = await _run(SessionMaker, sid, fake)

    assert result.fwbg_run_id == "job_test_42"
    assert fake.calls_of("start_run")[0][1] == ("demo_orb_v1__it001",)


async def test_runner_prefers_published_fwbg_name_from_metadata(runner_env):
    """When the research flow had to publish under a suffixed name (collision),
    the runner must backtest that exact fwbg strategy."""
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    async with SessionMaker() as setup:
        s = (await setup.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        s.metadata_json = {"fwbg_strategy_name": "demo_orb_v1__it001_v2"}
        await setup.commit()
    fake = FakeFwbgClient(
        create_strategy_error=FwbgClientError(409, "Strategy already exists"),
    )

    await _run(SessionMaker, sid, fake)

    assert fake.calls_of("create_strategy")[0][1] == ("demo_orb_v1__it001_v2",)
    assert fake.calls_of("start_run")[0][1] == ("demo_orb_v1__it001_v2",)

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


async def test_runner_keeps_class_when_symbol_of_same_rung_has_no_data(runner_env):
    # A "suggested" rung naming both a symbol and a class should still run on
    # the class when the symbol has no data — not skip the whole rung.
    SessionMaker, make_strategy, _tmp = runner_env
    sid = await make_strategy(
        asset_class="INDEX",
        suggested_universe=[
            {"scope": "symbol", "value": "XYZ", "rationale": "x"},
            {"scope": "asset_class", "value": "FOREX", "rationale": "y"},
        ],
    )
    fake = FakeFwbgClient(
        ensure_responses={"XYZ": FwbgClientError(404, "No data available for 'XYZ'")},
    )

    result = await _run(SessionMaker, sid, fake)

    assert result.universe["label"] == "suggested"
    starts = fake.calls_of("start_run")
    assert len(starts) == 1
    assert starts[0][2]["assets"] is None  # dataless symbol dropped
    assert starts[0][2]["asset_classes"] == ["FOREX"]  # class portion still ran


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
    assert starts[0][2]["assets"] == ["EURUSD"]  # attempt 1: suggested
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
    SessionMaker, make_strategy, _tmp = runner_env
    sid = await make_strategy(seed_json=False)

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        with pytest.raises(FileNotFoundError):
            await Runner(FakeFwbgClient(), session).run(s)


# ---------------------------------------------------------------------------
# Poll outage tolerance: a transient fwbg outage mid-backtest (watchtower
# recreate, dropped keep-alive) must not abort a run that is still crunching
# on the fwbg side. Only a sustained outage may fail the attempt.
# ---------------------------------------------------------------------------


async def test_poll_tolerates_transient_errors_and_completes(runner_env, monkeypatch):
    import httpx as _httpx

    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "runner_poll_outage_tolerance_seconds", 5.0)
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient(
        progress_responses=[
            {"status": "running"},
            _httpx.ReadError("keep-alive dropped"),
            FwbgClientError(502, "bad gateway during restart"),
            {"status": "running"},
            {"status": "completed"},
        ],
    )

    result = await _run(SessionMaker, sid, fake)

    assert result.fwbg_run_id == "job_test_42"
    # All progress polls happened despite two mid-run errors.
    assert len(fake.calls_of("get_progress")) >= 5


async def test_poll_fails_after_sustained_outage(runner_env, monkeypatch):
    import httpx as _httpx

    from fwbg_agents.config import settings

    # Zero tolerance: the first transport error immediately exceeds the
    # outage window, so every universe attempt fails and the run is failed.
    monkeypatch.setattr(settings, "runner_poll_outage_tolerance_seconds", 0.0)
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    errors = [_httpx.ReadError(f"down {i}") for i in range(50)]
    fake = FakeFwbgClient(progress_responses=errors)

    with pytest.raises(RunnerError) as excinfo:
        await _run(SessionMaker, sid, fake)

    assert "unreachable" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Global single-flight (fwbg has ONE backtest slot, FWBG_MAX_CONCURRENT_RUNS=1):
# - an already-active fwbg run of the same strategy is adopted, never
#   duplicated (a lost /runs/start response + retry used to launch a copy);
# - while the slot is taken by something else (429), the Runner waits.
# ---------------------------------------------------------------------------


async def test_adopts_active_run_of_same_strategy_instead_of_starting(runner_env):
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient(
        list_runs_response=[
            {
                "run_id": "job_external_7",
                "status": "running",
                "strategy_name": "demo_orb_v1__it001",
            },
        ],
    )

    result = await _run(SessionMaker, sid, fake)

    assert result.fwbg_run_id == "job_external_7"
    assert fake.calls_of("start_run") == []  # adopted, never started a copy


async def test_active_run_of_other_strategy_is_not_adopted(runner_env):
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient(
        list_runs_response=[
            {
                "run_id": "job_other",
                "status": "running",
                "strategy_name": "somebody_elses_strategy",
            },
        ],
    )

    result = await _run(SessionMaker, sid, fake)

    assert result.fwbg_run_id == "job_test_42"
    assert len(fake.calls_of("start_run")) == 1


async def test_waits_for_busy_slot_then_starts(runner_env, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "runner_busy_wait_seconds", 0.001)
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient(
        start_errors=[
            FwbgClientError(429, "Too many active runs (limit 1)"),
            FwbgClientError(429, "Too many active runs (limit 1)"),
        ],
    )

    result = await _run(SessionMaker, sid, fake)

    assert result.fwbg_run_id == "job_test_42"
    assert len(fake.calls_of("start_run")) == 3  # 2x 429, then the slot


async def test_gives_up_when_slot_stays_busy(runner_env, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "runner_busy_wait_seconds", 0.001)
    monkeypatch.setattr(settings, "runner_poll_timeout_seconds", 0.05)
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient(
        start_errors=[FwbgClientError(429, "busy")] * 1000,
    )

    with pytest.raises(RunnerError) as excinfo:
        await _run(SessionMaker, sid, fake)

    assert "slot stayed busy" in str(excinfo.value)


async def test_non_429_start_error_is_not_retried(runner_env):
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient(
        start_errors=[FwbgClientError(500, "boom")] * 10,
    )

    # Hard errors propagate unchanged (pre-existing semantics) — only 429
    # means "wait for the slot".
    with pytest.raises(FwbgClientError):
        await _run(SessionMaker, sid, fake)

    assert len(fake.calls_of("start_run")) == 1


async def test_missing_dependency_raises_config_error(runner_env):
    """A completed run whose assets all errored on a missing pipeline
    dependency short-circuits with RunnerConfigError carrying the parsed
    (dependent, dependency) — broadening the universe cannot help."""
    from fwbg_agents.agents.runner import RunnerConfigError
    from fwbg_agents.run_events import read_run_events

    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient(
        run_responses=[_errored_run("EURUSD", "GBPUSD")],
        logs_response=_DEP_LOG,
    )

    with pytest.raises(RunnerConfigError) as excinfo:
        await _run(SessionMaker, sid, fake)

    err = excinfo.value
    assert err.dependent == "regime_cluster"
    assert err.dependency == "regime"
    assert "regime_cluster" in str(err)
    # Short-circuited: only the first rung ran, not all three.
    assert len(fake.calls_of("start_run")) == 1
    # The AgentRun is marked failed with the real fwbg reason recorded.
    async with SessionMaker() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.agent_name == "runner"))
        ).scalar_one()
        assert ar.status == AgentRunStatus.FAILED.value
        assert "regime" in (ar.error or "")
    # A backtest_error timeline event was emitted with the errored assets.
    events = read_run_events(ar.id)
    err_events = [e for e in events if e["type"] == "backtest_error"]
    assert err_events
    assert set(err_events[0]["errored_assets"]) == {"EURUSD", "GBPUSD"}


async def test_non_dependency_error_falls_through_to_exhaustion(runner_env):
    """Assets errored, but not on a fixable dependency problem: the run still
    falls through the rungs and ends in a generic RunnerError whose message
    carries the real fwbg reason (not an opaque 'no metrics')."""
    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    other_log = [{"level": "error", "symbol": "EURUSD", "message": "insufficient bars for warmup"}]
    fake = FakeFwbgClient(
        run_responses=[_errored_run("EURUSD")],
        logs_response=other_log,
    )

    with pytest.raises(RunnerError) as excinfo:
        await _run(SessionMaker, sid, fake)

    assert "insufficient bars for warmup" in str(excinfo.value)


def test_median_metrics_across_assets():
    """The runner stores the per-metric median across the universe, not the
    metrics of the single best symbol."""
    run = {
        "assets": {
            "EURUSD": {"unified_metrics": {"sharpe": 2.0, "trades": 400}},
            "GBPUSD": {"unified_metrics": {"sharpe": 1.0, "trades": 200}},
            "USDJPY": {"unified_metrics": {"sharpe": 0.0, "trades": 600}},
        }
    }
    assert _median_metrics_across_assets(run) == {"sharpe": 1.0, "trades": 400.0}
    assert _median_metrics_across_assets({"assets": {}}) == {}


def test_months_ago_iso_subtracts_calendar_months():
    from datetime import date

    iso = _months_ago_iso(24)
    d = date.fromisoformat(iso)
    today = date.today()
    # 24 months back = 2 calendar years earlier (same-ish day).
    assert d.year == today.year - 2
    assert d.month == today.month


async def test_runner_reserves_holdout_window(runner_env):
    """Iteration backtests must end at today - holdout_months so no iteration
    ever sees the reserved holdout tail."""
    from datetime import date

    SessionMaker, make_strategy, _ = runner_env
    sid = await make_strategy()
    fake = FakeFwbgClient()
    await _run(SessionMaker, sid, fake)

    end_date = fake.calls_of("start_run")[0][2]["end_date"]
    assert end_date is not None
    assert date.fromisoformat(end_date) < date.today()


def _write_strategy_json(tmp_path, slug: str, data: dict) -> None:
    sj = (
        tmp_path
        / "agents_data"
        / "strategies"
        / slug
        / "iteration_001"
        / "strategy.json"
    )
    sj.write_text(json.dumps(data))


async def test_runner_rejects_signal_model_without_source(runner_env):
    """A signal model with no entry-signal source must not be dispatched: the
    backtest would skip every fold and 'complete' in seconds with zero results.
    """
    SessionMaker, make_strategy, tmp_path = runner_env
    sid = await make_strategy(slug="sig_nosrc_v1")
    _write_strategy_json(
        tmp_path,
        "sig_nosrc_v1",
        {
            "name": "sig_nosrc_v1",
            "model": {"type": "signal", "trade_directions": ["long"]},
            "filters": {"min_trades": 50},  # no allowed_hours/days
            "pipeline": {"indicators": [{"name": "computed_signal", "params": {}}]},
        },
    )
    fake = FakeFwbgClient()

    with pytest.raises(UnrunnableStrategyError):
        await _run(SessionMaker, sid, fake)

    # Never dispatched: no fwbg strategy created, no run started.
    assert fake.calls_of("create_strategy") == []
    assert fake.calls_of("start_run") == []


async def test_runner_dispatches_signal_model_with_time_filter(runner_env):
    """A signal model whose source is a time filter runs normally."""
    SessionMaker, make_strategy, tmp_path = runner_env
    sid = await make_strategy(slug="sig_ok_v1")
    _write_strategy_json(
        tmp_path,
        "sig_ok_v1",
        {
            "name": "sig_ok_v1",
            "model": {"type": "signal", "trade_directions": ["long"]},
            "filters": {"min_trades": 50, "allowed_hours": [8, 9, 10]},
            "pipeline": {"indicators": [{"name": "computed_signal", "params": {}}]},
        },
    )
    fake = FakeFwbgClient()

    result = await _run(SessionMaker, sid, fake)

    assert result.fwbg_run_id == "job_test_42"
    assert fake.calls_of("start_run")  # dispatched
