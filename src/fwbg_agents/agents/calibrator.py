"""Calibrator: derives per-asset-class success thresholds from existing fwbg runs.

Scans `settings.fwbg_test_results_dir/<run>/results.json + strategy.json`,
groups elite candidates by asset class (FOREX / INDEX / COMMODITY / CRYPTO),
computes quantiles of available metrics, and writes:

- `data/criteria/<ASSET_CLASS>.yaml` — editable success thresholds in the
  schema documented in section 6.1 of the design doc.
- `data/criteria/_calibration_baseline.json` — raw stats + per-metric quantiles,
  preserved for comparison when criteria are recalibrated later.

Pure stats, no LLM. See design doc section 4 (Calibrator row) and section 6.1.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# Symbol -> asset class. Mirrors fwbg.data.assets.AssetRegistry.DEFAULT_ASSETS so
# fwbg-agents does not need a runtime dependency on fwbg. Unknown symbols fall
# back to FOREX (same behavior as AssetRegistry.get).
SYMBOL_ASSET_CLASS: dict[str, str] = {
    # FOREX - Majors
    "EURUSD": "FOREX", "GBPUSD": "FOREX", "USDJPY": "FOREX", "USDCHF": "FOREX",
    "USDCAD": "FOREX", "AUDUSD": "FOREX", "NZDUSD": "FOREX",
    # FOREX - Crosses
    "EURGBP": "FOREX", "EURCAD": "FOREX", "EURCHF": "FOREX", "EURNZD": "FOREX",
    # Indices
    "DAX": "INDEX", "DOW30": "INDEX", "SPX500": "INDEX", "NAS100": "INDEX",
    "FTSE100": "INDEX", "EU50": "INDEX", "CAC40": "INDEX", "JP225": "INDEX",
    "ASX200": "INDEX", "HK50": "INDEX",
    # Commodities
    "XAUUSD": "COMMODITY", "GOLD": "COMMODITY", "XAGUSD": "COMMODITY",
    "SILVER": "COMMODITY", "BRENT": "COMMODITY",
    # Crypto
    "BTCUSD": "CRYPTO", "ETHUSD": "CRYPTO",
}


# Metrics we try to extract from each elite result.
# value: (extractor description, "higher is better"?)
# Quantile target: lower 50%/75% if higher-is-better; upper 50%/25% otherwise.
METRIC_HIGHER_IS_BETTER: dict[str, bool] = {
    "sharpe": True,
    "win_rate": True,
    "trades": True,
    "calmar": True,
    "profit_factor": True,
    "rrr": True,
    "fold_stability": True,
    "dsr": True,
    "max_drawdown": False,
    "pbo": False,
    "mc_pvalue": False,
}


@dataclass
class CalibrationResult:
    """Outcome of a single Calibrator run."""

    ran_at: datetime
    runs_scanned: int
    runs_with_elite: int
    asset_classes: dict[str, int]  # asset_class -> elite candidate count
    baseline_path: Path
    criteria_files: list[Path] = field(default_factory=list)


def classify_symbol(symbol: str) -> str:
    """Return the asset class for `symbol`, defaulting to FOREX for unknowns."""
    return SYMBOL_ASSET_CLASS.get(symbol.upper(), "FOREX")


def _safe_float(value: Any) -> float | None:
    """Coerce to float; return None for missing, non-numeric, or non-finite."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _extract_metrics(elite: dict[str, Any]) -> dict[str, float]:
    """Pull the success metrics we care about out of one elite_results entry."""
    mc = elite.get("monte_carlo") or {}
    metrics_raw: dict[str, Any] = {
        "sharpe": elite.get("sharpe"),
        "win_rate": elite.get("win_rate"),
        "trades": elite.get("trades"),
        "calmar": elite.get("calmar"),
        "profit_factor": elite.get("profit_factor"),
        "rrr": elite.get("rrr"),
        "fold_stability": elite.get("fold_stability"),
        "dsr": elite.get("dsr"),
        "max_drawdown": elite.get("max_drawdown"),
        "pbo": elite.get("pbo"),
        "mc_pvalue": mc.get("p_value") if isinstance(mc, dict) else None,
    }
    return {k: v for k, v in ((k, _safe_float(v)) for k, v in metrics_raw.items()) if v is not None}


def _quantiles(values: list[float]) -> dict[str, float] | None:
    """Compute the quantiles we need to fill the YAML schema."""
    if not values:
        return None
    sv = sorted(values)
    n = len(sv)

    def q(p: float) -> float:
        # Linear interpolation between closest ranks.
        if n == 1:
            return sv[0]
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return sv[lo] * (1 - frac) + sv[hi] * frac

    return {
        "min": sv[0],
        "p25": q(0.25),
        "p50": q(0.50),
        "p75": q(0.75),
        "max": sv[-1],
        "mean": statistics.fmean(sv),
        "stdev": statistics.pstdev(sv) if n > 1 else 0.0,
        "n": float(n),
    }


def _scan_run(run_dir: Path) -> list[dict[str, Any]]:
    """Return the list of elite results (with symbol attached) for one run."""
    results_path = run_dir / "results.json"
    if not results_path.is_file():
        return []
    try:
        data = json.loads(results_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("calibrator: skipping %s — unreadable results.json (%s)", run_dir.name, exc)
        return []
    elite = data.get("elite_results")
    if not isinstance(elite, list):
        return []
    return [e for e in elite if isinstance(e, dict) and e.get("symbol")]


def _build_quantiles_by_class(
    test_results_dir: Path,
) -> tuple[dict[str, dict[str, dict[str, float]]], int, int, dict[str, int]]:
    """Scan all runs, return per-class per-metric quantiles + counters."""
    per_class_metric_values: dict[str, dict[str, list[float]]] = {}
    runs_scanned = 0
    runs_with_elite = 0
    elite_counts: dict[str, int] = {}

    if not test_results_dir.is_dir():
        log.warning("calibrator: %s does not exist; calibration will be empty", test_results_dir)
        return {}, 0, 0, {}

    for run_dir in sorted(test_results_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        runs_scanned += 1
        elites = _scan_run(run_dir)
        if not elites:
            continue
        runs_with_elite += 1
        for elite in elites:
            symbol = elite.get("symbol")
            if not isinstance(symbol, str):
                continue
            asset_class = classify_symbol(symbol)
            elite_counts[asset_class] = elite_counts.get(asset_class, 0) + 1
            metrics = _extract_metrics(elite)
            bucket = per_class_metric_values.setdefault(asset_class, {})
            for metric, value in metrics.items():
                bucket.setdefault(metric, []).append(value)

    quantiles_by_class: dict[str, dict[str, dict[str, float]]] = {}
    for asset_class, metric_map in per_class_metric_values.items():
        per_metric: dict[str, dict[str, float]] = {}
        for metric, values in metric_map.items():
            q = _quantiles(values)
            if q is not None:
                per_metric[metric] = q
        quantiles_by_class[asset_class] = per_metric

    return quantiles_by_class, runs_scanned, runs_with_elite, elite_counts


def _threshold_op(metric: str, percentile_value: float) -> str:
    """Render a threshold expression like '>= 1.2' or '< 0.05'."""
    higher = METRIC_HIGHER_IS_BETTER.get(metric, True)
    rounded = round(percentile_value, 4)
    return f">= {rounded}" if higher else f"<= {rounded}"


def _build_criteria_yaml(
    asset_class: str,
    metrics: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Build the section-6.1 YAML dict from per-metric quantiles.

    Strategy:
    - backtest_to_paper.required_all: median (p50) of each available
      gate metric (sharpe, mc_pvalue, dsr, pbo when present).
    - required_any (alt-paths): 25th percentile relaxations on sharpe/trades
      or profit_factor/win_rate (if those exist).
    - hard_blockers: max_drawdown if present, plus calmar as a fallback signal.
    - paper_to_live: conservative defaults; these are not derived from
      backtest stats — they live as editable defaults the user tunes after
      observing real paper trading drift.

    Metrics absent from the runs (e.g. DSR / PBO when fwbg has not started
    emitting them) are simply omitted; the YAML stays schema-compatible.
    """

    def gate_p50(metric: str) -> str | None:
        if metric not in metrics:
            return None
        return _threshold_op(metric, metrics[metric]["p50"])

    def gate_p25(metric: str) -> str | None:
        if metric not in metrics:
            return None
        higher = METRIC_HIGHER_IS_BETTER.get(metric, True)
        pct = metrics[metric]["p25"] if higher else metrics[metric]["p75"]
        return _threshold_op(metric, pct)

    required_all: list[dict[str, str]] = []
    for metric in ("dsr", "pbo", "mc_pvalue"):
        op = gate_p50(metric)
        if op is not None:
            required_all.append({metric: op})

    required_any: list[dict[str, Any]] = []
    sharpe_op = gate_p25("sharpe")
    trades_n = metrics.get("trades", {}).get("p25")
    if sharpe_op is not None and trades_n is not None:
        required_any.append({"sharpe": sharpe_op, "min_trades": int(round(trades_n))})
    pf_op = gate_p25("profit_factor")
    wr_op = gate_p25("win_rate")
    if pf_op is not None and wr_op is not None:
        required_any.append({"profit_factor": pf_op, "win_rate": wr_op})
    elif wr_op is not None and sharpe_op is None:
        required_any.append({"win_rate": wr_op})

    hard_blockers: list[dict[str, str]] = []
    if "max_drawdown" in metrics:
        # p75 of observed drawdowns becomes the upper bound (still tolerates 3/4 of survivors).
        hard_blockers.append({"max_drawdown": _threshold_op("max_drawdown", metrics["max_drawdown"]["p75"])})
    if "calmar" in metrics:
        hard_blockers.append({"calmar": _threshold_op("calmar", metrics["calmar"]["p25"])})

    return {
        "_meta": {
            "asset_class": asset_class,
            "calibrated_at": datetime.now(UTC).isoformat(),
            "metrics_available": sorted(metrics.keys()),
            "note": (
                "Auto-generated by Calibrator. Edit thresholds in the dashboard; "
                "rerun /calibrate to refresh from fresh fwbg runs."
            ),
        },
        "backtest_to_paper": {
            "required_all": required_all,
            "required_any": required_any,
            "hard_blockers": hard_blockers,
        },
        "paper_to_live": {
            "realized_vs_backtest": {
                "sharpe_deviation_max": 0.40,
                "drawdown_breach_factor": 1.5,
                "win_rate_deviation_max": 0.10,
            },
            "minimum_sample": {"trades": 30, "days_running": 60},
            "regime_check": {"require_no_distribution_shift": True},
        },
    }


def calibrate(
    test_results_dir: Path | None = None,
    criteria_dir: Path | None = None,
) -> CalibrationResult:
    """Run a full calibration pass and persist criteria + baseline."""
    from fwbg_agents.config import settings

    test_results_dir = test_results_dir or settings.fwbg_test_results_dir
    criteria_dir = criteria_dir or settings.criteria_dir
    criteria_dir.mkdir(parents=True, exist_ok=True)

    quantiles_by_class, runs_scanned, runs_with_elite, elite_counts = _build_quantiles_by_class(
        test_results_dir
    )

    ran_at = datetime.now(UTC)
    criteria_files: list[Path] = []
    for asset_class, metrics in sorted(quantiles_by_class.items()):
        if not metrics:
            continue
        yaml_dict = _build_criteria_yaml(asset_class, metrics)
        out_path = criteria_dir / f"{asset_class}.yaml"
        out_path.write_text(yaml.safe_dump(yaml_dict, sort_keys=False, allow_unicode=True))
        criteria_files.append(out_path)
        log.info("calibrator: wrote %s (%d metrics)", out_path, len(metrics))

    baseline_path = criteria_dir / "_calibration_baseline.json"
    baseline_payload = {
        "ran_at": ran_at.isoformat(),
        "test_results_dir": str(test_results_dir),
        "runs_scanned": runs_scanned,
        "runs_with_elite": runs_with_elite,
        "asset_class_counts": elite_counts,
        "quantiles": quantiles_by_class,
    }
    baseline_path.write_text(json.dumps(baseline_payload, indent=2, sort_keys=True))

    return CalibrationResult(
        ran_at=ran_at,
        runs_scanned=runs_scanned,
        runs_with_elite=runs_with_elite,
        asset_classes=elite_counts,
        baseline_path=baseline_path,
        criteria_files=criteria_files,
    )
