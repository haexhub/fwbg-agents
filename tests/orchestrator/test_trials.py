"""Trial counting + Deflated Sharpe Ratio (Plan 010 WP2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.trials import (
    count_trials,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    per_trade_sharpe,
    record_trial_stat,
    series_moments,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState, TrialStat

# --- STOP-condition 2: reproduce Bailey & López de Prado (2014) §"A Numerical
# Example" (pp. 9-10) within ±0.01. Their inputs are annualized (250 obs/year);
# our formula is unit-agnostic, so both sides just need matching (here:
# daily) units. ---

_ANNUAL_SR = 2.5
_OBS_PER_YEAR = 250
_DAILY_SR = _ANNUAL_SR / _OBS_PER_YEAR**0.5
_DAILY_VAR = 0.5 / _OBS_PER_YEAR  # V[SR_n] = 1/2 annualized
_T = 1250


def test_expected_max_sharpe_matches_paper_example():
    sr0 = expected_max_sharpe(_DAILY_VAR, 100)
    assert sr0 == pytest.approx(0.1132, abs=0.01)


def test_dsr_matches_paper_non_normal_example():
    dsr = deflated_sharpe_ratio(_DAILY_SR, _DAILY_VAR, n_trials=100, n_obs=_T, skew=-3, kurtosis=10)
    assert dsr == pytest.approx(0.9004, abs=0.01)


def test_dsr_matches_paper_normal_returns_example():
    dsr = deflated_sharpe_ratio(_DAILY_SR, _DAILY_VAR, n_trials=88, n_obs=_T, skew=0, kurtosis=3)
    assert dsr == pytest.approx(0.9505, abs=0.01)


def test_dsr_matches_paper_fewer_trials_example():
    dsr = deflated_sharpe_ratio(_DAILY_SR, _DAILY_VAR, n_trials=46, n_obs=_T, skew=-3, kurtosis=10)
    assert dsr == pytest.approx(0.9505, abs=0.01)


def test_dsr_degenerate_denominator_returns_zero():
    """Pathological higher moments must not raise or return nonsense."""
    dsr = deflated_sharpe_ratio(5.0, 1.0, n_trials=10, n_obs=100, skew=10, kurtosis=1)
    assert dsr == 0.0


def test_dsr_single_trial_falls_back_to_plain_psr():
    """N<=1 -> no deflation; SR0=0, degrades to PSR against 0."""
    dsr_one_trial = deflated_sharpe_ratio(0.5, 1.0, n_trials=1, n_obs=200, skew=0, kurtosis=3)
    dsr_many_trials = deflated_sharpe_ratio(0.5, 1.0, n_trials=200, n_obs=200, skew=0, kurtosis=3)
    assert dsr_one_trial > dsr_many_trials


# --- per_trade_sharpe / series_moments -------------------------------------


def test_per_trade_sharpe_needs_at_least_two_trades():
    assert per_trade_sharpe([1.0]) is None
    assert per_trade_sharpe([]) is None


def test_per_trade_sharpe_zero_variance_is_none():
    assert per_trade_sharpe([1.0, 1.0, 1.0]) is None


def test_series_moments_matches_manual_computation():
    pnls = [1.0, -2.0, 3.0, -1.0, 2.0]
    moments = series_moments(pnls)
    assert moments is not None
    sr, skew, kurtosis = moments
    assert sr == pytest.approx(per_trade_sharpe(pnls))
    assert isinstance(skew, float)
    assert isinstance(kurtosis, float)


# --- count_trials ------------------------------------------------------


def _write_fold_results(run_dir, symbol: str, pnls: list[float]) -> None:
    sym_dir = run_dir / "grid_details" / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    (sym_dir / "fold_results.json").write_text(
        json.dumps(
            {
                "walk_forward": {
                    "fold_details": [
                        {
                            "test_trades_detail": [
                                {"pnl_raw": p, "entry_time": "2024-01-01T00:00:00"} for p in pnls
                            ]
                        }
                    ]
                }
            }
        )
    )


@pytest_asyncio.fixture
async def trials_env(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    monkeypatch.setattr(settings, "fwbg_test_results_dir", tmp_path / "test_results")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/trials.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    yield Session, settings
    await engine.dispose()


async def _seed_strategy(Session, slug: str, family: str):
    async with Session() as session:
        now = datetime.now(UTC)
        s = Strategy(
            slug=slug,
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family=family,
            created_at=now,
            updated_at=now,
        )
        session.add(s)
        await session.commit()
        return s.id


@pytest.mark.asyncio
async def test_count_trials_empty_when_no_strategies(trials_env):
    Session, _ = trials_env
    async with Session() as session:
        counts = await count_trials(session)
    assert counts.global_runs == 0
    assert counts.global_trials == 0
    assert counts.by_family == {}
    assert counts.trade_sharpes == []


@pytest.mark.asyncio
async def test_count_trials_counts_grid_combinations_and_falls_back_to_one(trials_env):
    Session, _ = trials_env
    sid = await _seed_strategy(Session, "orb__forex__001", "ORB")
    async with Session() as session:
        session.add(
            TrialStat(
                run_id="run_a",
                strategy_id=sid,
                strategy_family="ORB",
                n_trials=13,
                n_trades=0,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
        counts = await count_trials(session)

    assert counts.global_runs == 1
    assert counts.global_trials == 13  # 12 + 1 fallback
    assert counts.by_family == {"ORB": 13}


@pytest.mark.asyncio
async def test_count_trials_aggregates_per_family_across_strategies(trials_env):
    Session, _ = trials_env
    async with Session() as session:
        now = datetime.now(UTC)
        session.add_all(
            [
                TrialStat(
                    run_id="run_a", strategy_family="ORB", n_trials=5, n_trades=0, created_at=now
                ),
                TrialStat(
                    run_id="run_b", strategy_family="ORB", n_trials=7, n_trades=0, created_at=now
                ),
                TrialStat(
                    run_id="run_c",
                    strategy_family="MEAN_REVERSION",
                    n_trials=3,
                    n_trades=0,
                    created_at=now,
                ),
            ]
        )
        await session.commit()
        counts = await count_trials(session)

    assert counts.global_runs == 3
    assert counts.global_trials == 15
    assert counts.by_family == {"ORB": 12, "MEAN_REVERSION": 3}


@pytest.mark.asyncio
async def test_count_trials_collects_finite_durable_trade_sharpes(trials_env):
    Session, _ = trials_env
    async with Session() as session:
        now = datetime.now(UTC)
        session.add_all(
            [
                TrialStat(
                    run_id="run_a",
                    strategy_family="ORB",
                    n_trials=1,
                    trade_sharpe=0.5,
                    n_trades=4,
                    created_at=now,
                ),
                TrialStat(
                    run_id="run_nan",
                    strategy_family="ORB",
                    n_trials=1,
                    trade_sharpe=float("nan"),
                    n_trades=4,
                    created_at=now,
                ),
            ]
        )
        await session.commit()
        counts = await count_trials(session)
    assert len(counts.trade_sharpes) == 1
    assert counts.trade_sharpes == [0.5]


@pytest.mark.asyncio
async def test_record_trial_stat_survives_pruning_and_is_idempotent(trials_env):
    Session, settings = trials_env
    sid = await _seed_strategy(Session, "orb__forex__001", "ORB")
    async with Session() as session:
        strategy = await session.get(Strategy, sid)
        assert strategy is not None
        data = {"assets": {"EURUSD": {"total_combinations": 4}}}
        await record_trial_stat(
            session,
            run_id="run_gone",
            strategy=strategy,
            run_data=data,
            run_dir=settings.fwbg_test_results_dir / "run_gone",
        )
        await record_trial_stat(
            session,
            run_id="run_gone",
            strategy=strategy,
            run_data=data,
            run_dir=settings.fwbg_test_results_dir / "run_gone",
        )
        await session.commit()
        counts = await count_trials(session)
    assert counts.global_runs == 1
    assert counts.global_trials == 4
    assert counts.trade_sharpes == []


@pytest.mark.asyncio
async def test_record_trial_stat_filters_nan_only_series(trials_env):
    Session, settings = trials_env
    sid = await _seed_strategy(Session, "orb__forex__001", "ORB")
    _write_fold_results(settings.fwbg_test_results_dir / "run_nan", "EURUSD", [float("nan")])
    async with Session() as session:
        strategy = await session.get(Strategy, sid)
        assert strategy is not None
        await record_trial_stat(
            session,
            run_id="run_nan",
            strategy=strategy,
            run_data={"assets": {"EURUSD": {}}},
            run_dir=settings.fwbg_test_results_dir / "run_nan",
        )
        await session.commit()
        row = await session.scalar(select(TrialStat).where(TrialStat.run_id == "run_nan"))
    assert row is not None
    assert row.trade_sharpe is None
    assert row.n_trades == 0


@pytest.mark.asyncio
async def test_backfill_trial_stats_is_idempotent(trials_env, monkeypatch):
    from scripts import backfill_trial_stats as backfill_module

    Session, settings = trials_env
    await _seed_strategy(Session, "orb__forex__001", "ORB")
    monkeypatch.setattr(backfill_module, "SessionLocal", Session)
    for index in (1, 2):
        iteration = settings.data_dir / "strategies" / "orb__forex__001" / f"iteration_{index:03d}"
        iteration.mkdir(parents=True, exist_ok=True)
        (iteration / "fwbg_results.json").write_text(
            json.dumps(
                {
                    "run_id": f"backfill_{index}",
                    "assets": {"EURUSD": {"total_combinations": index}},
                }
            )
        )
    first = await backfill_module.backfill(
        data_dir=settings.data_dir,
        test_results_dir=settings.fwbg_test_results_dir,
    )
    second = await backfill_module.backfill(
        data_dir=settings.data_dir,
        test_results_dir=settings.fwbg_test_results_dir,
    )
    assert first == (2, 0)
    assert second == (0, 2)
