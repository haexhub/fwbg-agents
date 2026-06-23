"""Hand-curated default success thresholds per asset class.

Auto-deriving thresholds from historical fwbg-test_results yielded noise when
the historical sample was mostly unprofitable — the Calibrator would have
legitimized bad strategies as "normal". These defaults are deliberately
conservative ("I'd rather have nothing pass than promote a mediocre strategy").

Validated with user 2026-06-23:
- mc_pvalue <= 0.05  universal (p-value is asset-agnostic)
- sharpe    >= 1.5   universal (strict — many backtests will bounce off, that's intended)
- profit_factor >= 1.6  universal
- min_trades >= 300  universal (n=300 keeps Sharpe-stderr ≈ ±0.12)
- max_drawdown asset-specific:
    FOREX/INDEX <= 0.25 (real-MDD multiplier ~1.5-2x → live ~40-50%)
    COMMODITY   <= 0.30
    CRYPTO      <= 0.40 (BTC itself has historical 50%+ drawdowns)

Dropped from gates (rationale):
- win_rate: profit_factor mathematically subsumes it; standalone gate adds noise
- calmar: composite of annual_return/max_drawdown — double-counts with max_dd gate
- dsr / pbo / tail_ratio: not emitted by fwbg today; multiple-testing
  corrections are agent-level (Analyst, M3)
- sortino: planned follow-up — fwbg doesn't emit it, fwbg-agents will compute
  from trades.json
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_PAPER_TO_LIVE: dict[str, Any] = {
    "realized_vs_backtest": {
        "sharpe_deviation_max": 0.40,
        "drawdown_breach_factor": 1.5,
        "profit_factor_deviation_max": 0.30,
    },
    "minimum_sample": {"trades": 30, "days_running": 60},
    "regime_check": {"require_no_distribution_shift": True},
}


def _criteria(max_drawdown: float) -> dict[str, Any]:
    return {
        "backtest_to_paper": {
            "required_all": [
                {"mc_pvalue": "<= 0.05"},
                {"sharpe": ">= 1.5"},
                {"profit_factor": ">= 1.6"},
                {"min_trades": ">= 300"},
            ],
            "hard_blockers": [
                {"max_drawdown": f"<= {max_drawdown}"},
            ],
        },
        "paper_to_live": _PAPER_TO_LIVE,
    }


DEFAULT_CRITERIA_BY_CLASS: dict[str, dict[str, Any]] = {
    "FOREX": _criteria(max_drawdown=0.25),
    "INDEX": _criteria(max_drawdown=0.25),
    "COMMODITY": _criteria(max_drawdown=0.30),
    "CRYPTO": _criteria(max_drawdown=0.40),
}


def default_criteria(asset_class: str) -> dict[str, Any] | None:
    """Return a deep copy of the defaults for `asset_class`, or None if unknown."""
    template = DEFAULT_CRITERIA_BY_CLASS.get(asset_class)
    if template is None:
        return None
    return deepcopy(template)


def known_asset_classes() -> list[str]:
    return sorted(DEFAULT_CRITERIA_BY_CLASS.keys())
