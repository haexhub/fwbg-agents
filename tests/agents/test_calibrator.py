"""Calibrator tests.

Calibrator behavior under M1-final:
- baseline JSON ALWAYS refreshed from scanned fwbg test_results
- per-class criteria YAML SEEDED from hand-curated defaults on first run
- existing YAMLs NEVER overwritten (preserves user edits in the dashboard)
- unknown symbols classify as FOREX (matches fwbg AssetRegistry default)
- merging picks up max_drawdown / profit_factor from
  grid_details/<symbol>/unified_metrics.json (where fwbg actually writes them)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fwbg_agents.agents.calibrator import (
    SYMBOL_ASSET_CLASS,
    calibrate,
    classify_symbol,
    _compute_sortino,
    _extract_metrics,
    _quantiles,
)
from fwbg_agents.agents.criteria_defaults import (
    DEFAULT_CRITERIA_BY_CLASS,
    default_criteria,
    known_asset_classes,
)


def _write_run(
    test_results_dir: Path,
    name: str,
    elite_results: list[dict],
    unified_metrics_by_symbol: dict[str, dict] | None = None,
    tr_trace_by_symbol: dict[str, list[float]] | None = None,
) -> None:
    """Write a fake fwbg run with optional per-symbol unified_metrics.json + trades.json."""
    run_dir = test_results_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(
        json.dumps(
            {
                "elite_count": len(elite_results),
                "elite_results": elite_results,
                "total_processed": 1,
                "profitable_count": len(elite_results),
                "significant_count": len(elite_results),
                "filtered_results_count": 0,
            }
        )
    )
    for symbol, unified in (unified_metrics_by_symbol or {}).items():
        sym_dir = run_dir / "grid_details" / symbol
        sym_dir.mkdir(parents=True, exist_ok=True)
        (sym_dir / "unified_metrics.json").write_text(json.dumps(unified))
    for symbol, trace in (tr_trace_by_symbol or {}).items():
        sym_dir = run_dir / "grid_details" / symbol
        sym_dir.mkdir(parents=True, exist_ok=True)
        (sym_dir / "trades.json").write_text(
            json.dumps({"tr_trace": trace, "trades_detailed": []})
        )


@pytest.fixture
def fake_test_results(tmp_path: Path) -> Path:
    """Three runs across FOREX + INDEX, one with empty elite_results."""
    root = tmp_path / "test_results"
    root.mkdir()
    _write_run(
        root,
        "20260301_run_a",
        [
            {"symbol": "EURUSD", "sharpe": 1.2, "win_rate": 0.55, "trades": 320,
             "monte_carlo": {"p_value": 0.03}},
        ],
        {"EURUSD": {"max_drawdown": 0.15, "profit_factor": 1.55, "annual_return": 0.08}},
    )
    _write_run(
        root,
        "20260302_run_b",
        [
            {"symbol": "EURUSD", "sharpe": 0.9, "win_rate": 0.48, "trades": 180,
             "monte_carlo": {"p_value": 0.07}},
            {"symbol": "GBPUSD", "sharpe": 1.5, "win_rate": 0.58, "trades": 410,
             "monte_carlo": {"p_value": 0.02}},
        ],
        {
            "EURUSD": {"max_drawdown": 0.21, "profit_factor": 1.20, "annual_return": 0.04},
            "GBPUSD": {"max_drawdown": 0.12, "profit_factor": 1.80, "annual_return": 0.10},
        },
    )
    _write_run(
        root,
        "20260303_run_c",
        [
            {"symbol": "NAS100", "sharpe": 0.6, "win_rate": 0.27, "trades": 750,
             "monte_carlo": {"p_value": 0.04}},
        ],
        {"NAS100": {"max_drawdown": 0.22, "profit_factor": 1.25, "annual_return": 0.06}},
    )
    _write_run(root, "20260304_empty", [])
    return root


# ----- helpers / pure functions ----------------------------------------------


def test_classify_symbol_known_and_unknown() -> None:
    assert classify_symbol("EURUSD") == "FOREX"
    assert classify_symbol("NAS100") == "INDEX"
    assert classify_symbol("BTCUSD") == "CRYPTO"
    assert classify_symbol("XAUUSD") == "COMMODITY"
    # Unknown symbol falls back to FOREX (matches AssetRegistry.get default).
    assert classify_symbol("WIBBLE") == "FOREX"
    assert classify_symbol("eurusd") == "FOREX"


def test_quantiles_distribution() -> None:
    q = _quantiles([1.0, 2.0, 3.0, 4.0])
    assert q is not None
    assert q["min"] == 1.0
    assert q["max"] == 4.0
    assert q["p50"] == pytest.approx(2.5)


def test_known_asset_classes_match_symbol_table() -> None:
    """Every asset class that appears in the symbol table must have defaults,
    and vice versa — otherwise the seeding silently skips classes."""
    classes_in_symbols = set(SYMBOL_ASSET_CLASS.values())
    classes_in_defaults = set(known_asset_classes())
    assert classes_in_symbols == classes_in_defaults, (
        f"mismatch: symbols={classes_in_symbols} defaults={classes_in_defaults}"
    )


# ----- defaults shape --------------------------------------------------------


def test_defaults_have_section_6_1_shape() -> None:
    for asset_class in known_asset_classes():
        c = default_criteria(asset_class)
        assert c is not None
        assert "backtest_to_paper" in c
        assert "paper_to_live" in c
        btp = c["backtest_to_paper"]
        assert isinstance(btp["required_all"], list)
        assert isinstance(btp["hard_blockers"], list)
        # Validated gate set: mc_pvalue / sharpe / profit_factor / min_trades
        flat_all = {k for entry in btp["required_all"] for k in entry}
        assert {"mc_pvalue", "sharpe", "profit_factor", "min_trades"} <= flat_all
        # Hard blocker on max_drawdown
        flat_blockers = {k for entry in btp["hard_blockers"] for k in entry}
        assert "max_drawdown" in flat_blockers


def test_defaults_max_drawdown_differs_per_class() -> None:
    """Risk profile differs: FOREX/INDEX strict, CRYPTO loose."""
    def md(asset_class: str) -> str:
        c = default_criteria(asset_class)
        assert c is not None
        entry = next(e for e in c["backtest_to_paper"]["hard_blockers"] if "max_drawdown" in e)
        return entry["max_drawdown"]
    assert md("FOREX") == "<= 0.25"
    assert md("INDEX") == "<= 0.25"
    assert md("COMMODITY") == "<= 0.3"
    assert md("CRYPTO") == "<= 0.4"


def test_default_criteria_returns_independent_copies() -> None:
    """Two calls must not share nested lists — otherwise edits leak across calls."""
    a = default_criteria("FOREX")
    b = default_criteria("FOREX")
    assert a == b
    assert a is not b
    a["backtest_to_paper"]["required_all"].append({"poisoned": "yes"})
    assert b is not None
    assert all("poisoned" not in entry for entry in b["backtest_to_paper"]["required_all"])


# ----- end-to-end calibrate behavior ----------------------------------------


def test_calibrate_seeds_all_known_classes_on_first_run(
    fake_test_results: Path, tmp_path: Path
) -> None:
    criteria_dir = tmp_path / "criteria"
    result = calibrate(test_results_dir=fake_test_results, criteria_dir=criteria_dir)

    seeded_names = {p.stem for p in result.seeded_criteria_files}
    assert seeded_names == set(known_asset_classes())
    assert result.preserved_criteria_files == []

    # YAML on disk is the defaults template (NOT data-derived).
    forex = yaml.safe_load((criteria_dir / "FOREX.yaml").read_text())
    assert forex["_meta"]["source"].startswith("criteria_defaults.py")
    md_entry = next(
        e for e in forex["backtest_to_paper"]["hard_blockers"] if "max_drawdown" in e
    )
    assert md_entry["max_drawdown"] == "<= 0.25"


def test_calibrate_preserves_existing_yaml(tmp_path: Path) -> None:
    """User edits in the dashboard must survive recalibration."""
    criteria_dir = tmp_path / "criteria"
    criteria_dir.mkdir()
    user_edit = "backtest_to_paper:\n  required_all: [{sharpe: '>= 9.99'}]\npaper_to_live: {}\n"
    (criteria_dir / "FOREX.yaml").write_text(user_edit)
    test_results = tmp_path / "test_results"
    test_results.mkdir()

    result = calibrate(test_results_dir=test_results, criteria_dir=criteria_dir)

    forex = yaml.safe_load((criteria_dir / "FOREX.yaml").read_text())
    sharpe_entry = next(
        e for e in forex["backtest_to_paper"]["required_all"] if "sharpe" in e
    )
    assert sharpe_entry["sharpe"] == ">= 9.99"  # untouched
    forex_path = criteria_dir / "FOREX.yaml"
    assert forex_path in result.preserved_criteria_files
    assert forex_path not in result.seeded_criteria_files


def test_calibrate_always_refreshes_baseline(fake_test_results: Path, tmp_path: Path) -> None:
    criteria_dir = tmp_path / "criteria"

    # Pre-seed all YAMLs so nothing new gets written there, only baseline must refresh.
    criteria_dir.mkdir()
    for ac in known_asset_classes():
        (criteria_dir / f"{ac}.yaml").write_text("backtest_to_paper: {}\npaper_to_live: {}\n")

    result = calibrate(test_results_dir=fake_test_results, criteria_dir=criteria_dir)
    assert result.seeded_criteria_files == []
    assert len(result.preserved_criteria_files) == len(known_asset_classes())

    baseline = json.loads((criteria_dir / "_calibration_baseline.json").read_text())
    assert baseline["runs_scanned"] == 4
    assert baseline["runs_with_elite"] == 3
    assert baseline["asset_class_counts"] == {"FOREX": 3, "INDEX": 1}
    # Real-data metrics (from unified_metrics merge) show up in baseline.
    assert "max_drawdown" in baseline["quantiles"]["FOREX"]
    assert "profit_factor" in baseline["quantiles"]["FOREX"]


def test_calibrate_handles_missing_test_results_dir(tmp_path: Path) -> None:
    criteria_dir = tmp_path / "criteria"
    result = calibrate(
        test_results_dir=tmp_path / "does-not-exist",
        criteria_dir=criteria_dir,
    )
    assert result.runs_scanned == 0
    # Seeding still happens — defaults are independent of historical data.
    assert {p.stem for p in result.seeded_criteria_files} == set(known_asset_classes())
    # Baseline is still written (empty stats, but present so the dashboard has
    # a stable read target).
    baseline = json.loads((criteria_dir / "_calibration_baseline.json").read_text())
    assert baseline["runs_scanned"] == 0


def test_unified_metrics_merge(tmp_path: Path) -> None:
    """max_drawdown + profit_factor live in grid_details/<sym>/unified_metrics.json."""
    test_results = tmp_path / "test_results"
    test_results.mkdir()
    _write_run(
        test_results,
        "merge_check",
        elite_results=[
            {"symbol": "EURUSD", "sharpe": 1.1, "trades": 240,
             "monte_carlo": {"p_value": 0.03}},
        ],
        unified_metrics_by_symbol={
            "EURUSD": {"max_drawdown": 0.15, "profit_factor": 1.45, "annual_return": 0.08},
        },
    )
    criteria_dir = tmp_path / "criteria"
    calibrate(test_results_dir=test_results, criteria_dir=criteria_dir)
    baseline = json.loads((criteria_dir / "_calibration_baseline.json").read_text())
    forex_metrics = baseline["quantiles"]["FOREX"]
    assert "max_drawdown" in forex_metrics
    assert "profit_factor" in forex_metrics
    assert forex_metrics["max_drawdown"]["n"] == 1.0


def test_extract_metrics_unified_takes_precedence_over_elite(tmp_path: Path) -> None:
    """If elite_results has a metric AND unified_metrics has it, unified wins."""
    run_dir = tmp_path
    sym_dir = run_dir / "grid_details" / "EURUSD"
    sym_dir.mkdir(parents=True)
    (sym_dir / "unified_metrics.json").write_text(
        json.dumps({"profit_factor": 1.7, "max_drawdown": 0.12})
    )
    elite = {"symbol": "EURUSD", "profit_factor": 9.99, "sharpe": 1.0}
    m = _extract_metrics(elite, run_dir=run_dir)
    assert m["profit_factor"] == 1.7  # unified wins
    assert m["max_drawdown"] == 0.12  # from unified
    assert m["sharpe"] == 1.0  # from elite (unified didn't have it)


def test_missing_unified_metrics_file_does_not_break(tmp_path: Path) -> None:
    """Older fwbg runs without grid_details/ should still produce a baseline."""
    test_results = tmp_path / "test_results"
    test_results.mkdir()
    _write_run(
        test_results,
        "no_unified",
        [{"symbol": "EURUSD", "sharpe": 1.0, "trades": 100, "monte_carlo": {"p_value": 0.04}}],
        unified_metrics_by_symbol=None,
    )
    criteria_dir = tmp_path / "criteria"
    result = calibrate(test_results_dir=test_results, criteria_dir=criteria_dir)
    assert result.runs_with_elite == 1
    baseline = json.loads((criteria_dir / "_calibration_baseline.json").read_text())
    assert "sharpe" in baseline["quantiles"]["FOREX"]
    assert "max_drawdown" not in baseline["quantiles"]["FOREX"]


# ----- sortino ---------------------------------------------------------------


def test_compute_sortino_math_known_series() -> None:
    """Sortino math sanity-check with a hand-computable series.

    pnls = [10, 10, 10, -5, -5]
      mean              = 4.0
      negative pnls     = [-5, -5]
      sum of sq(neg)    = 50
      downside_var      = 50 / 5 = 10
      downside_dev      = sqrt(10) ≈ 3.1623
      per_trade_sortino = 4 / 3.1623 ≈ 1.2649
      with trades_per_year = 100 (annualization √100 = 10)
      annualized        ≈ 12.649
    """
    s = _compute_sortino([10.0, 10.0, 10.0, -5.0, -5.0], trades_per_year=100.0)
    assert s is not None
    assert s == pytest.approx(12.649110640673518, rel=1e-6)


def test_compute_sortino_no_losing_trades_returns_none() -> None:
    """No negative trades → downside_dev is 0 → sortino is undefined."""
    assert _compute_sortino([1.0, 2.0, 3.0], trades_per_year=100.0) is None


def test_compute_sortino_missing_annualization_returns_none() -> None:
    """We refuse to emit a per-trade sortino — would mix incompatible units."""
    assert _compute_sortino([1.0, -1.0, 1.0, -1.0], trades_per_year=None) is None
    assert _compute_sortino([1.0, -1.0, 1.0, -1.0], trades_per_year=0.0) is None


def test_compute_sortino_too_few_trades() -> None:
    """One trade isn't a statistic."""
    assert _compute_sortino([5.0], trades_per_year=100.0) is None


def test_sortino_lands_in_baseline_quantiles(tmp_path: Path) -> None:
    """End-to-end: trades.json + unified_metrics.json → sortino in baseline."""
    test_results = tmp_path / "test_results"
    test_results.mkdir()
    _write_run(
        test_results,
        "with_trades",
        elite_results=[
            {"symbol": "EURUSD", "sharpe": 1.0, "trades": 100,
             "monte_carlo": {"p_value": 0.03}},
        ],
        unified_metrics_by_symbol={
            "EURUSD": {
                "trades": 100,
                "test_period_years": 1.0,  # → trades_per_year = 100
                "max_drawdown": 0.10,
                "profit_factor": 1.6,
            },
        },
        tr_trace_by_symbol={"EURUSD": [10.0, 10.0, 10.0, -5.0, -5.0]},
    )
    criteria_dir = tmp_path / "criteria"
    calibrate(test_results_dir=test_results, criteria_dir=criteria_dir)
    baseline = json.loads((criteria_dir / "_calibration_baseline.json").read_text())
    forex_metrics = baseline["quantiles"]["FOREX"]
    assert "sortino" in forex_metrics
    # Same series + annualization as test_compute_sortino_math_known_series.
    assert forex_metrics["sortino"]["p50"] == pytest.approx(12.649110640673518, rel=1e-6)


def test_sortino_skipped_when_no_test_period_years(tmp_path: Path) -> None:
    """Annualization requires test_period_years — if absent, sortino is dropped."""
    test_results = tmp_path / "test_results"
    test_results.mkdir()
    _write_run(
        test_results,
        "no_years",
        elite_results=[
            {"symbol": "EURUSD", "sharpe": 1.0, "trades": 100,
             "monte_carlo": {"p_value": 0.03}},
        ],
        unified_metrics_by_symbol={
            "EURUSD": {"trades": 100, "max_drawdown": 0.10, "profit_factor": 1.6},
        },
        tr_trace_by_symbol={"EURUSD": [10.0, -5.0, 10.0, -5.0]},
    )
    criteria_dir = tmp_path / "criteria"
    calibrate(test_results_dir=test_results, criteria_dir=criteria_dir)
    baseline = json.loads((criteria_dir / "_calibration_baseline.json").read_text())
    forex_metrics = baseline["quantiles"]["FOREX"]
    assert "sortino" not in forex_metrics
    # But the other metrics still come through.
    assert "max_drawdown" in forex_metrics
    assert "profit_factor" in forex_metrics
