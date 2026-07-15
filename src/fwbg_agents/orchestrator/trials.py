"""Trial counting + Deflated Sharpe Ratio (Plan 010 WP2).

An agent factory is a mass search over one dataset: with hundreds of
backtests, a handful of in-sample "hits" are pure chance. The promote gate
therefore benchmarks a candidate's Sharpe not against 0 but against the
Sharpe the *best of N random trials* would be expected to show
(Bailey & López de Prado 2014, "The Deflated Sharpe Ratio").

Trial counting is filesystem-based: every completed backtest left an
``iteration_*/fwbg_results.json`` sidecar, and each asset in it records its
grid-search breadth (``total_combinations``). Grid combinations count as
trials where readable; assets that don't expose them (``0``/missing) count
conservatively as one trial each — that undercounts true search breadth, so
the resulting DSR is an *upper* bound and the gate stays honest-or-stricter
as artifacts improve.

Unit discipline: the DSR inputs must share one unit system. Everything here
is **per-trade**: the candidate SR is mean/std of the trade-P&L series, skew
and kurtosis come from the same series, ``n_obs`` is its length, and the
across-trials SR variance is estimated from per-trade SRs of historical runs
whose fwbg run dirs still exist (retention may have pruned older ones — the
variance sample is a subset of the trials, which is the best available
estimate, not a fabricated one).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from pathlib import Path
from statistics import NormalDist

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.trade_diagnostics import _load_symbol_trades
from fwbg_agents.persistence.models import Strategy

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
    """Census over ``data/strategies/*/iteration_*/fwbg_results.json``."""
    rows = await session.execute(select(Strategy.slug, Strategy.strategy_family))
    family_by_slug = {slug: family for slug, family in rows}

    strategies_root = settings.data_dir / "strategies"
    global_runs = 0
    global_trials = 0
    by_family: dict[str, int] = {}
    trade_sharpes: list[float] = []

    if strategies_root.is_dir():
        for results_path in sorted(strategies_root.glob("*/iteration_*/fwbg_results.json")):
            try:
                run_data = json.loads(results_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("count_trials: skipping %s (%s)", results_path, exc)
                continue
            slug = results_path.parent.parent.name
            trials = _trials_in_run(run_data)
            global_runs += 1
            global_trials += trials
            family = family_by_slug.get(slug) or "unknown"
            by_family[family] = by_family.get(family, 0) + trials

            run_id = run_data.get("run_id")
            if isinstance(run_id, str) and run_id:
                sr = per_trade_sharpe(pnl_series(settings.fwbg_test_results_dir / run_id))
                if sr is not None:
                    trade_sharpes.append(sr)

    return TrialCounts(
        global_runs=global_runs,
        global_trials=global_trials,
        by_family=by_family,
        trade_sharpes=trade_sharpes,
    )


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
