"""Trial counting + Deflated Sharpe Ratio (Plan 010 WP2).

An agent factory is a mass search over one dataset: with hundreds of
backtests, a handful of in-sample "hits" are pure chance. The promote gate
therefore benchmarks a candidate's Sharpe not against 0 but against the
Sharpe the *best of N random trials* would be expected to show
(Bailey & López de Prado 2014, "The Deflated Sharpe Ratio").

Trial counting is persisted at run completion. Undercounting ``n_trials``
lowers E[max SR] and therefore weakens the gate; the durable census prevents
silent undercounting when retention prunes run artifacts. Assets that don't
expose ``total_combinations`` still count as 1 — a known, explicit undercount.

Unit discipline: the DSR inputs must share one unit system. Everything here
is **per-trade**: the candidate SR is mean/std of the trade-P&L series, skew
and kurtosis come from the same series, ``n_obs`` is its length, and the
across-trials SR variance is estimated from durable per-run snapshots.
"""

from __future__ import annotations

import logging
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.trade_diagnostics import _load_symbol_trades
from fwbg_agents.persistence.models import Strategy, TrialStat

log = logging.getLogger(__name__)

EULER_GAMMA = 0.5772156649015329


class TrialCounts(BaseModel):
    """Search-breadth census across all completed backtests."""

    global_runs: int  # completed backtest runs (fwbg_results.json sidecars)
    global_trials: int  # incl. grid combinations where readable (≥ global_runs)
    by_family: dict[str, int]  # trials per strategy_family
    trade_sharpes: list[float]  # per-trade SR per run with surviving artifacts


def _trials_in_run(run_data: dict) -> int:
    """Trials one backtest run represents: grid combos summed across assets,
    one per asset where the artifact doesn't expose the grid size."""
    trials = 0
    for sym in (run_data.get("assets") or {}).values():
        if not isinstance(sym, dict):
            continue
        tc = sym.get("total_combinations")
        trials += tc if isinstance(tc, int) and tc > 0 else 1
    return max(trials, 1)


def pnl_series(run_dir: Path) -> list[float]:
    """All per-trade P&L values of one fwbg run (all symbols, all folds)."""
    pnls: list[float] = []
    grid = run_dir / "grid_details"
    if not grid.is_dir():
        return pnls
    for sym_dir in sorted(grid.iterdir()):
        if not sym_dir.is_dir():
            continue
        trades, _ = _load_symbol_trades(run_dir, sym_dir.name)
        pnls.extend(float(t["pnl_raw"]) for t in trades)
    return pnls


def per_trade_sharpe(pnls: list[float]) -> float | None:
    """Mean/std of the trade-P&L series; None when undefined (<2 trades, flat)."""
    if len(pnls) < 2:
        return None
    std = statistics.pstdev(pnls)
    if std == 0:
        return None
    return statistics.mean(pnls) / std


async def count_trials(session: AsyncSession) -> TrialCounts:
    """DB-backed census of durable completed-backtest snapshots."""
    totals = (
        await session.execute(select(func.count(TrialStat.id), func.sum(TrialStat.n_trials)))
    ).one()
    global_runs = int(totals[0] or 0)
    global_trials = int(totals[1] or 0)
    family_rows = await session.execute(
        select(TrialStat.strategy_family, func.sum(TrialStat.n_trials)).group_by(
            TrialStat.strategy_family
        )
    )
    by_family = {family: int(trials) for family, trials in family_rows}
    sharpe_rows = await session.scalars(
        select(TrialStat.trade_sharpe).where(TrialStat.trade_sharpe.is_not(None))
    )
    trade_sharpes = [float(sr) for sr in sharpe_rows if sr is not None and math.isfinite(sr)]

    return TrialCounts(
        global_runs=global_runs,
        global_trials=global_trials,
        by_family=by_family,
        trade_sharpes=trade_sharpes,
    )


async def record_trial_stat(
    session: AsyncSession,
    *,
    run_id: str,
    strategy: Strategy,
    run_data: dict,
    run_dir: Path,
) -> None:
    """Insert one durable run snapshot; failures never fail the backtest."""
    try:
        async with session.begin_nested():
            existing = await session.scalar(select(TrialStat.id).where(TrialStat.run_id == run_id))
            if existing is not None:
                return
            pnls = [value for value in pnl_series(run_dir) if math.isfinite(value)]
            session.add(
                TrialStat(
                    run_id=run_id,
                    strategy_id=strategy.id,
                    strategy_family=strategy.strategy_family or "unknown",
                    n_trials=_trials_in_run(run_data),
                    trade_sharpe=per_trade_sharpe(pnls),
                    n_trades=len(pnls),
                    created_at=datetime.now(UTC),
                )
            )
            await session.flush()
    except Exception:
        log.exception("record_trial_stat: failed for run %s", run_id)


def expected_max_sharpe(sr_variance_across_trials: float, n_trials: int) -> float:
    """E[max SR] of ``n_trials`` zero-skill trials (Bailey/López de Prado 2014).

    E[max] ~= sqrt(V[SR]) * ((1-gamma)*Phi^-1(1-1/N) + gamma*Phi^-1(1-1/(N*e)))
    """
    if n_trials <= 1 or sr_variance_across_trials <= 0:
        return 0.0
    nd = NormalDist()
    return math.sqrt(sr_variance_across_trials) * (
        (1 - EULER_GAMMA) * nd.inv_cdf(1 - 1 / n_trials)
        + EULER_GAMMA * nd.inv_cdf(1 - 1 / (n_trials * math.e))
    )


def deflated_sharpe_ratio(
    sr: float,
    sr_variance_across_trials: float,
    n_trials: int,
    n_obs: int,
    skew: float,
    kurtosis: float,
) -> float:
    """Probability that the true Sharpe exceeds the best-of-N-noise benchmark.

    ``sr``/``sr_variance_across_trials`` must share one per-period unit
    system; ``kurtosis`` is raw (normal = 3), not excess. With
    ``n_trials <= 1`` or zero variance this degrades to the plain PSR
    against 0.
    """
    if n_obs <= 1:
        return 0.0
    sr0 = expected_max_sharpe(sr_variance_across_trials, n_trials)
    denom = 1 - skew * sr + (kurtosis - 1) / 4 * sr * sr
    if denom <= 0:
        # Degenerate higher moments — the PSR variance estimate is invalid.
        return 0.0
    z = (sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return NormalDist().cdf(z)


def series_moments(pnls: list[float]) -> tuple[float, float, float] | None:
    """(per-trade SR, skew, raw kurtosis) of a P&L series; None if undefined."""
    if not all(math.isfinite(x) for x in pnls):
        return None
    n = len(pnls)
    if n < 2:
        return None
    mean = statistics.fmean(pnls)
    m2 = sum((x - mean) ** 2 for x in pnls) / n
    if m2 == 0:
        return None
    m3 = sum((x - mean) ** 3 for x in pnls) / n
    m4 = sum((x - mean) ** 4 for x in pnls) / n
    sr = mean / math.sqrt(m2)
    return sr, m3 / m2**1.5, m4 / m2**2
