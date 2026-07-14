"""Trial counting + Deflated Sharpe Ratio (Plan 010 WP2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.trials import (
    count_trials,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    per_trade_sharpe,
    pnl_series,
    series_moments,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState

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
    dsr = deflated_sharpe_ratio(
        _DAILY_SR, _DAILY_VAR, n_trials=100, n_obs=_T, skew=-3, kurtosis=10
    )
    assert dsr == pytest.approx(0.9004, abs=0.01)


def test_dsr_matches_paper_normal_returns_example():
    dsr = deflated_sharpe_ratio(_DAILY_SR, _DAILY_VAR, n_trials=88, n_obs=_T, skew=0, kurtosis=3)
    assert dsr == pytest.approx(0.9505, abs=0.01)


def test_dsr_matches_paper_fewer_trials_example():
    dsr = deflated_sharpe_ratio(
        _DAILY_SR, _DAILY_VAR, n_trials=46, n_obs=_T, skew=-3, kurtosis=10
    )
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
                                {"pnl_raw": p, "entry_time": "2024-01-01T00:00:00"}
                                for p in pnls
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
    Session, settings = trials_env
    await _seed_strategy(Session, "orb__forex__001", "ORB")

    it_dir = settings.data_dir / "strategies" / "orb__forex__001" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "fwbg_results.json").write_text(
        json.dumps(
            {
                "run_id": "run_a",
                "assets": {
                    "EURUSD": {"unified_metrics": {"sharpe": 1.0}, "total_combinations": 12},
                    "GBPUSD": {"unified_metrics": {"sharpe": 0.8}},  # missing -> 1 trial
                },
            }
        )
    )

    async with Session() as session:
        counts = await count_trials(session)

    assert counts.global_runs == 1
    assert counts.global_trials == 13  # 12 + 1 fallback
    assert counts.by_family == {"ORB": 13}


@pytest.mark.asyncio
async def test_count_trials_aggregates_per_family_across_strategies(trials_env):
    Session, settings = trials_env
    await _seed_strategy(Session, "orb__forex__001", "ORB")
    await _seed_strategy(Session, "orb__forex__002", "ORB")
    await _seed_strategy(Session, "meanrev__forex__001", "MEAN_REVERSION")

    for slug, run_id, combos in [
        ("orb__forex__001", "run_a", 5),
        ("orb__forex__002", "run_b", 7),
        ("meanrev__forex__001", "run_c", 3),
    ]:
        it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
        it_dir.mkdir(parents=True, exist_ok=True)
        (it_dir / "fwbg_results.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "assets": {"EURUSD": {"unified_metrics": {}, "total_combinations": combos}},
                }
            )
        )

    async with Session() as session:
        counts = await count_trials(session)

    assert counts.global_runs == 3
    assert counts.global_trials == 15
    assert counts.by_family == {"ORB": 12, "MEAN_REVERSION": 3}


@pytest.mark.asyncio
async def test_count_trials_collects_per_trade_sharpes_from_surviving_run_dirs(trials_env):
    Session, settings = trials_env
    await _seed_strategy(Session, "orb__forex__001", "ORB")

    it_dir = settings.data_dir / "strategies" / "orb__forex__001" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "fwbg_results.json").write_text(
        json.dumps({"run_id": "run_a", "assets": {"EURUSD": {"unified_metrics": {}}}})
    )
    _write_fold_results(
        settings.fwbg_test_results_dir / "run_a", "EURUSD", [1.0, -2.0, 3.0, -1.0]
    )

    async with Session() as session:
        counts = await count_trials(session)

    assert len(counts.trade_sharpes) == 1
    expected_sr = per_trade_sharpe(pnl_series(settings.fwbg_test_results_dir / "run_a"))
    assert counts.trade_sharpes[0] == pytest.approx(expected_sr)


@pytest.mark.asyncio
async def test_count_trials_skips_pruned_run_dirs(trials_env):
    """A run whose fwbg_test_results_dir entry was pruned by retention still
    counts as a trial but contributes no trade-Sharpe sample."""
    Session, settings = trials_env
    await _seed_strategy(Session, "orb__forex__001", "ORB")

    it_dir = settings.data_dir / "strategies" / "orb__forex__001" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "fwbg_results.json").write_text(
        json.dumps({"run_id": "run_gone", "assets": {"EURUSD": {"unified_metrics": {}}}})
    )

    async with Session() as session:
        counts = await count_trials(session)

    assert counts.global_runs == 1
    assert counts.trade_sharpes == []
