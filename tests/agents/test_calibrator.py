"""Calibrator unit tests.

Drives the Calibrator against a fake test_results directory and checks that:
- runs are discovered and elite candidates grouped by asset class
- per-asset YAML is written in the section 6.1 shape
- _calibration_baseline.json captures the raw quantiles
- unknown symbols fall back to FOREX (matches AssetRegistry.get behavior)
- absent metrics are simply not emitted (forward-compatible)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fwbg_agents.agents.calibrator import (
    calibrate,
    classify_symbol,
    _build_criteria_yaml,
    _quantiles,
)


def _write_run(test_results_dir: Path, name: str, elite_results: list[dict]) -> None:
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


@pytest.fixture
def fake_test_results(tmp_path: Path) -> Path:
    """Three runs across FOREX + INDEX, one run with empty elite_results."""
    root = tmp_path / "test_results"
    root.mkdir()
    _write_run(
        root,
        "20260301_run_a",
        [
            {
                "symbol": "EURUSD",
                "sharpe": 1.2,
                "win_rate": 0.55,
                "calmar": 2.0,
                "trades": 320,
                "rrr": 2,
                "fold_stability": 0.8,
                "monte_carlo": {"p_value": 0.03, "is_significant": True},
            },
        ],
    )
    _write_run(
        root,
        "20260302_run_b",
        [
            {
                "symbol": "EURUSD",
                "sharpe": 0.9,
                "win_rate": 0.48,
                "calmar": 1.4,
                "trades": 180,
                "rrr": 2,
                "fold_stability": 0.65,
                "monte_carlo": {"p_value": 0.07, "is_significant": False},
            },
            {
                "symbol": "GBPUSD",
                "sharpe": 1.5,
                "win_rate": 0.58,
                "calmar": 2.8,
                "trades": 410,
                "rrr": 3,
                "fold_stability": 0.72,
                "monte_carlo": {"p_value": 0.02, "is_significant": True},
            },
        ],
    )
    _write_run(
        root,
        "20260303_run_c",
        [
            {
                "symbol": "NAS100",
                "sharpe": 0.6,
                "win_rate": 0.27,
                "calmar": 3.8,
                "trades": 750,
                "rrr": 3,
                "fold_stability": 0.62,
                "monte_carlo": {"p_value": 0.04, "is_significant": True},
            },
            {
                "symbol": "DAX",
                "sharpe": 1.1,
                "win_rate": 0.51,
                "calmar": 1.9,
                "trades": 220,
                "rrr": 2,
                "fold_stability": 0.7,
                "monte_carlo": {"p_value": 0.08, "is_significant": False},
            },
        ],
    )
    _write_run(root, "20260304_empty", [])
    return root


def test_classify_symbol_known_and_unknown() -> None:
    assert classify_symbol("EURUSD") == "FOREX"
    assert classify_symbol("NAS100") == "INDEX"
    assert classify_symbol("BTCUSD") == "CRYPTO"
    assert classify_symbol("XAUUSD") == "COMMODITY"
    # Unknown symbol falls back to FOREX (matches AssetRegistry.get default).
    assert classify_symbol("WIBBLE") == "FOREX"
    # Case-insensitive match.
    assert classify_symbol("eurusd") == "FOREX"


def test_quantiles_single_value() -> None:
    q = _quantiles([0.5])
    assert q is not None
    assert q["min"] == 0.5 == q["max"]
    assert q["n"] == 1


def test_quantiles_distribution() -> None:
    q = _quantiles([1.0, 2.0, 3.0, 4.0])
    assert q is not None
    assert q["min"] == 1.0
    assert q["max"] == 4.0
    assert q["p50"] == pytest.approx(2.5)


def test_calibrate_writes_yaml_and_baseline(fake_test_results: Path, tmp_path: Path) -> None:
    criteria_dir = tmp_path / "criteria"
    result = calibrate(test_results_dir=fake_test_results, criteria_dir=criteria_dir)

    assert result.runs_scanned == 4
    assert result.runs_with_elite == 3
    assert result.asset_classes == {"FOREX": 3, "INDEX": 2}

    forex_path = criteria_dir / "FOREX.yaml"
    index_path = criteria_dir / "INDEX.yaml"
    assert forex_path.is_file()
    assert index_path.is_file()
    assert forex_path in result.criteria_files
    assert index_path in result.criteria_files

    forex = yaml.safe_load(forex_path.read_text())
    # Section 6.1 shape
    assert "backtest_to_paper" in forex
    assert "paper_to_live" in forex
    btp = forex["backtest_to_paper"]
    for k in ("required_all", "required_any", "hard_blockers"):
        assert isinstance(btp[k], list)
    # mc_pvalue should be a gate (we have it on every elite).
    flat_required_all = {k for entry in btp["required_all"] for k in entry}
    assert "mc_pvalue" in flat_required_all
    # calmar is a hard blocker fallback (we have it on every elite).
    flat_blockers = {k for entry in btp["hard_blockers"] for k in entry}
    assert "calmar" in flat_blockers

    # Threshold expression style: "OP value"
    for entry in btp["required_all"]:
        for value in entry.values():
            assert isinstance(value, str)
            assert value.split()[0] in {">=", "<=", ">", "<"}

    meta = forex["_meta"]
    assert meta["asset_class"] == "FOREX"
    assert "mc_pvalue" in meta["metrics_available"]

    baseline_path = criteria_dir / "_calibration_baseline.json"
    assert baseline_path.is_file()
    baseline = json.loads(baseline_path.read_text())
    assert baseline["runs_scanned"] == 4
    assert baseline["asset_class_counts"] == {"FOREX": 3, "INDEX": 2}
    # Per-class per-metric quantiles preserved for comparison later.
    assert "FOREX" in baseline["quantiles"]
    assert "sharpe" in baseline["quantiles"]["FOREX"]
    assert baseline["quantiles"]["FOREX"]["sharpe"]["n"] == 3.0


def test_missing_metrics_are_omitted(tmp_path: Path) -> None:
    """When DSR/PBO/max_drawdown aren't present, they should not appear in the YAML."""
    test_results = tmp_path / "test_results"
    test_results.mkdir()
    _write_run(
        test_results,
        "minimal",
        [
            {"symbol": "EURUSD", "sharpe": 1.0, "win_rate": 0.5, "trades": 100,
             "monte_carlo": {"p_value": 0.04}},
        ],
    )
    criteria_dir = tmp_path / "criteria"
    calibrate(test_results_dir=test_results, criteria_dir=criteria_dir)
    forex = yaml.safe_load((criteria_dir / "FOREX.yaml").read_text())

    blockers = {k for entry in forex["backtest_to_paper"]["hard_blockers"] for k in entry}
    assert "max_drawdown" not in blockers  # absent from fixture
    required_all = {k for entry in forex["backtest_to_paper"]["required_all"] for k in entry}
    assert "dsr" not in required_all
    assert "pbo" not in required_all
    assert "mc_pvalue" in required_all


def test_missing_test_results_dir_is_noop(tmp_path: Path) -> None:
    """Calibrator must not crash when fwbg's results dir is missing."""
    criteria_dir = tmp_path / "criteria"
    result = calibrate(
        test_results_dir=tmp_path / "does-not-exist",
        criteria_dir=criteria_dir,
    )
    assert result.runs_scanned == 0
    assert result.asset_classes == {}
    assert result.criteria_files == []
    # Baseline is still written even if empty, so the dashboard has a stable read target.
    baseline_path = criteria_dir / "_calibration_baseline.json"
    assert baseline_path.is_file()
    baseline = json.loads(baseline_path.read_text())
    assert baseline["runs_scanned"] == 0


def test_build_criteria_yaml_uses_higher_is_better_polarity() -> None:
    """Sharpe wants >=, mc_pvalue wants <=. Verify the comparator direction."""
    metrics = {
        "sharpe": {"min": 0.5, "p25": 0.8, "p50": 1.1, "p75": 1.4, "max": 1.6, "mean": 1.1, "stdev": 0.3, "n": 5.0},
        "mc_pvalue": {"min": 0.01, "p25": 0.02, "p50": 0.04, "p75": 0.07, "max": 0.09, "mean": 0.05, "stdev": 0.02, "n": 5.0},
        "trades": {"min": 50, "p25": 100, "p50": 200, "p75": 350, "max": 500, "mean": 240, "stdev": 130, "n": 5.0},
    }
    yaml_dict = _build_criteria_yaml("FOREX", metrics)
    required_all = yaml_dict["backtest_to_paper"]["required_all"]
    mc_entry = next(e for e in required_all if "mc_pvalue" in e)
    assert mc_entry["mc_pvalue"].startswith("<=")  # smaller is better
    required_any = yaml_dict["backtest_to_paper"]["required_any"]
    sharpe_entry = next(e for e in required_any if "sharpe" in e)
    assert sharpe_entry["sharpe"].startswith(">=")
    assert isinstance(sharpe_entry["min_trades"], int)
