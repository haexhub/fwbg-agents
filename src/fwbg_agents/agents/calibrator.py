"""Calibrator: seeds per-asset-class criteria YAMLs and refreshes baseline stats.

What it does:
1. **Always**: scan fwbg test_results, compute per-metric quantiles per asset
   class, write `_calibration_baseline.json` (read-only reference data).
2. **Idempotently**: for each known asset class with no existing YAML, seed
   `<class>.yaml` from `criteria_defaults.DEFAULT_CRITERIA_BY_CLASS`.
3. **Never overwrites**: existing YAMLs are preserved. User edits in the
   dashboard survive every `POST /calibrate`.

Why this split: deriving gate thresholds from a mostly-unprofitable historical
sample is unsound — see criteria_defaults.py for the rationale. The baseline
JSON remains useful for comparison (the dashboard renders it side-by-side
with the current YAMLs), but it never feeds the gates directly.

Pure stats / no LLM. See design doc section 4 (Calibrator) and 6.1.
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

from fwbg_agents.agents.criteria_defaults import (
    DEFAULT_CRITERIA_BY_CLASS,
    default_criteria,
)

log = logging.getLogger(__name__)


@dataclass
class CalibrationResult:
    """Outcome of a single Calibrator pass."""

    ran_at: datetime
    runs_scanned: int
    runs_with_elite: int
    asset_classes: dict[str, int]  # asset_class -> elite candidate count
    baseline_path: Path
    seeded_criteria_files: list[Path] = field(default_factory=list)
    preserved_criteria_files: list[Path] = field(default_factory=list)


def _classify_symbol(symbol: str, asset_map: dict[str, str]) -> str:
    """Return the asset class for `symbol` from fwbg's registry, defaulting to FOREX."""
    return asset_map.get(symbol.upper(), "FOREX")


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


def _load_unified_metrics(run_dir: Path, symbol: str) -> dict[str, Any]:
    """Read `grid_details/<symbol>/unified_metrics.json` if present.

    fwbg writes the richer per-symbol metrics (max_drawdown, profit_factor,
    annual_return, n_wins/n_losses, ...) into this nested file rather than
    duplicating them into elite_results.
    """
    path = run_dir / "grid_details" / symbol / "unified_metrics.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("calibrator: skipping %s — bad unified_metrics.json (%s)", path, exc)
        return {}


def _load_tr_trace(run_dir: Path, symbol: str) -> list[float]:
    """Read per-trade pnls from `grid_details/<symbol>/trades.json -> tr_trace`."""
    path = run_dir / "grid_details" / symbol / "trades.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("calibrator: skipping %s — bad trades.json (%s)", path, exc)
        return []
    trace = data.get("tr_trace") if isinstance(data, dict) else None
    if not isinstance(trace, list):
        return []
    cleaned: list[float] = []
    for v in trace:
        f = _safe_float(v)
        if f is not None:
            cleaned.append(f)
    return cleaned


def _compute_sortino(
    trade_pnls: list[float],
    trades_per_year: float | None,
) -> float | None:
    """Annualized Sortino ratio from a per-trade pnl series.

    sortino = mean(pnls) / sqrt(mean(min(pnl, 0)**2)) * sqrt(trades_per_year)

    Returns None if undefined (no negative trades → no downside dev, or
    fewer than 2 trades, or annualization factor unknown).
    """
    if not trade_pnls or len(trade_pnls) < 2:
        return None
    mean = sum(trade_pnls) / len(trade_pnls)
    # Downside deviation: only negative pnls contribute; positives count as 0.
    sq_neg = [p * p for p in trade_pnls if p < 0]
    if not sq_neg:
        # Strategy with zero losing trades — sortino is technically +inf.
        # We don't emit anything (caller filters None out of the baseline).
        return None
    downside_var = sum(sq_neg) / len(trade_pnls)
    if downside_var <= 0:
        return None
    downside_dev = downside_var**0.5
    per_trade = mean / downside_dev
    if trades_per_year is None or trades_per_year <= 0:
        return None
    return per_trade * (trades_per_year**0.5)


def _trades_per_year(unified: dict[str, Any]) -> float | None:
    """Compute annualized trade frequency from unified metrics."""
    trades = _safe_float(unified.get("trades"))
    years = _safe_float(unified.get("test_period_years"))
    if trades is None or years is None or years <= 0:
        return None
    return trades / years


def _extract_metrics(elite: dict[str, Any], run_dir: Path | None = None) -> dict[str, float]:
    """Pull observed metric values out of one elite_results entry, merging in
    the richer unified_metrics.json when present.

    Sortino is computed here too (from grid_details/<sym>/trades.json) because
    fwbg does not emit it. Annualized using trades / test_period_years from
    unified_metrics.json. Tracked in the baseline only — not gated yet, the
    user picks a threshold once real values are visible.
    """
    merged: dict[str, Any] = dict(elite)
    unified: dict[str, Any] = {}
    if run_dir is not None:
        symbol = elite.get("symbol")
        if isinstance(symbol, str):
            unified = _load_unified_metrics(run_dir, symbol)
            merged.update(unified)

    mc = merged.get("monte_carlo") or {}
    metrics_raw: dict[str, Any] = {
        "sharpe": merged.get("sharpe"),
        "win_rate": merged.get("win_rate"),
        "trades": merged.get("trades"),
        "profit_factor": merged.get("profit_factor"),
        "max_drawdown": merged.get("max_drawdown"),
        "calmar": merged.get("calmar"),
        "annual_return": merged.get("annual_return"),
        "rrr": merged.get("rrr"),
        "fold_stability": merged.get("fold_stability"),
        "mc_pvalue": mc.get("p_value") if isinstance(mc, dict) else None,
    }

    if run_dir is not None:
        symbol = elite.get("symbol")
        if isinstance(symbol, str):
            pnls = _load_tr_trace(run_dir, symbol)
            sortino = _compute_sortino(pnls, _trades_per_year(unified))
            if sortino is not None:
                metrics_raw["sortino"] = sortino

    return {k: v for k, v in ((k, _safe_float(v)) for k, v in metrics_raw.items()) if v is not None}


def _quantiles(values: list[float]) -> dict[str, float] | None:
    """Quantile summary used by the baseline JSON (informational only)."""
    if not values:
        return None
    sv = sorted(values)
    n = len(sv)

    def q(p: float) -> float:
        """Linearly interpolated quantile at fractional position p."""
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


def _build_baseline(
    test_results_dir: Path,
    symbol_asset_class: dict[str, str],
) -> tuple[dict[str, dict[str, dict[str, float]]], int, int, dict[str, int]]:
    """Scan all runs, return per-class per-metric quantiles + counters."""
    per_class_metric_values: dict[str, dict[str, list[float]]] = {}
    runs_scanned = 0
    runs_with_elite = 0
    elite_counts: dict[str, int] = {}

    if not test_results_dir.is_dir():
        log.warning("calibrator: %s does not exist; baseline will be empty", test_results_dir)
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
            asset_class = _classify_symbol(symbol, symbol_asset_class)
            elite_counts[asset_class] = elite_counts.get(asset_class, 0) + 1
            metrics = _extract_metrics(elite, run_dir=run_dir)
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


def _seed_criteria_yaml(asset_class: str, path: Path) -> None:
    """Write the hand-curated defaults for `asset_class` to `path`.

    Adds a `_meta.note` so the user understands at a glance that the file
    came from defaults (not from data) and is safe to edit.
    """
    template = default_criteria(asset_class)
    if template is None:
        return
    payload: dict[str, Any] = {
        "_meta": {
            "asset_class": asset_class,
            "seeded_at": datetime.now(UTC).isoformat(),
            "source": "criteria_defaults.py (hand-curated, not data-derived)",
            "note": (
                "Conservative starting thresholds. Tune in the dashboard once you "
                "have realized performance data per asset class. The "
                "_calibration_baseline.json sidecar shows what your historical "
                "fwbg runs actually look like."
            ),
        },
        **template,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def calibrate(
    test_results_dir: Path | None = None,
    criteria_dir: Path | None = None,
    symbol_asset_class: dict[str, str] | None = None,
) -> CalibrationResult:
    """Run one calibration pass.

    `symbol_asset_class` is a symbol→asset_class map fetched from fwbg's
    /api/assets endpoint before calling this function. When None (e.g. fwbg
    unreachable), classification falls back to "FOREX" for every symbol.

    Effects:
    - Always refreshes `<criteria_dir>/_calibration_baseline.json` from the
      latest fwbg test_results.
    - Seeds `<criteria_dir>/<asset_class>.yaml` from hand-curated defaults
      for every known class that does NOT already have a YAML.
    - Never overwrites existing YAMLs (preserves user edits).
    """
    from fwbg_agents.config import settings

    test_results_dir = test_results_dir or settings.fwbg_test_results_dir
    criteria_dir = criteria_dir or settings.criteria_dir
    criteria_dir.mkdir(parents=True, exist_ok=True)

    quantiles_by_class, runs_scanned, runs_with_elite, elite_counts = _build_baseline(
        test_results_dir, symbol_asset_class or {}
    )

    ran_at = datetime.now(UTC)
    seeded: list[Path] = []
    preserved: list[Path] = []

    for asset_class in DEFAULT_CRITERIA_BY_CLASS:
        out_path = criteria_dir / f"{asset_class}.yaml"
        if out_path.is_file():
            preserved.append(out_path)
            continue
        _seed_criteria_yaml(asset_class, out_path)
        seeded.append(out_path)
        log.info("calibrator: seeded %s from defaults", out_path)

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
        seeded_criteria_files=seeded,
        preserved_criteria_files=preserved,
    )
