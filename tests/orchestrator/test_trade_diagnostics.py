"""Trade-diagnostics tests (Plan 009 WP1).

Synthetic fold_results.json fixtures — no real fwbg run required. Covers the
per-bucket maths, the aggregate across symbols, the fwbg trade_analytics
passthrough, and graceful degradation on missing / malformed input.
"""

from __future__ import annotations

import json
from pathlib import Path

from fwbg_agents.orchestrator.trade_diagnostics import (
    compute_trade_diagnostics,
)


def _trade(pnl, *, entry, exit, bars, result=None):
    return {
        "pnl_raw": pnl,
        "result": result if result is not None else (1.0 if pnl > 0 else -1.0),
        "direction": "LONG",
        "entry_time": entry,
        "exit_time": exit,
        "bars_held": bars,
        "hour": int(entry[11:13]),
        "mae": abs(pnl) * 2,
        "mfe": abs(pnl) * 3,
    }


def _write_fold_results(run_dir: Path, symbol: str, trades: list[dict], analytics=None):
    sym_dir = run_dir / "grid_details" / symbol
    sym_dir.mkdir(parents=True)
    data = {"walk_forward": {"fold_details": [{"test_trades_detail": trades}]}}
    if analytics is not None:
        data["trade_analytics"] = analytics
    (sym_dir / "fold_results.json").write_text(json.dumps(data))


def test_buckets_and_distribution(tmp_path):
    trades = [
        _trade(0.03, entry="2025-01-06T09:00:00", exit="2025-01-06T10:00:00", bars=4),
        _trade(-0.01, entry="2025-01-07T14:00:00", exit="2025-01-07T15:00:00", bars=40),
        _trade(0.02, entry="2025-01-08T09:00:00", exit="2025-01-08T11:00:00", bars=8),
        _trade(-0.02, entry="2025-06-09T14:00:00", exit="2025-06-09T20:00:00", bars=200),
        _trade(0.04, entry="2026-01-05T09:00:00", exit="2026-01-05T10:00:00", bars=6),
    ]
    _write_fold_results(tmp_path, "EURUSD", trades)
    diag = compute_trade_diagnostics(tmp_path, ["EURUSD"])

    sym = diag.per_symbol[0]
    assert sym.symbol == "EURUSD"
    assert sym.n_trades == 5
    # payoff ratio = mean(win) / mean(|loss|) = mean(.03,.02,.04)/mean(.01,.02)
    assert sym.payoff_ratio is not None
    assert round(sym.payoff_ratio, 3) == round(0.03 / 0.015, 3)
    # entry hours 09 and 14 present
    hours = {b.key for b in sym.by_hour}
    assert "09h" in hours and "14h" in hours
    # two calendar years
    assert {y.year for y in sym.by_year} == {2025, 2026}
    # aggregate mirrors the single symbol
    assert diag.aggregate is not None
    assert diag.aggregate.n_trades == 5


def test_longest_loss_streak_and_top5_share(tmp_path):
    # Three consecutive losers in the middle, ordered by exit_time.
    trades = [
        _trade(0.10, entry="2025-01-01T09:00:00", exit="2025-01-01T10:00:00", bars=1),
        _trade(-0.01, entry="2025-01-02T09:00:00", exit="2025-01-02T10:00:00", bars=1),
        _trade(-0.01, entry="2025-01-03T09:00:00", exit="2025-01-03T10:00:00", bars=1),
        _trade(-0.01, entry="2025-01-04T09:00:00", exit="2025-01-04T10:00:00", bars=1),
        _trade(0.02, entry="2025-01-05T09:00:00", exit="2025-01-05T10:00:00", bars=1),
        _trade(0.02, entry="2025-01-06T09:00:00", exit="2025-01-06T10:00:00", bars=1),
        _trade(0.02, entry="2025-01-07T09:00:00", exit="2025-01-07T10:00:00", bars=1),
    ]
    _write_fold_results(tmp_path, "EURUSD", trades)
    sym = compute_trade_diagnostics(tmp_path, ["EURUSD"]).per_symbol[0]
    assert sym.longest_loss_streak == 3
    # Net = 0.10+0.02*3 - 0.01*3 = 0.13; top-5 pnls = .10,.02,.02,.02,-.01 = 0.15
    assert sym.top5_pnl_share is not None
    assert round(sym.top5_pnl_share, 3) == round(0.15 / 0.13, 3)


def test_trade_analytics_passthrough(tmp_path):
    analytics = {"mae_losers": {"median": 0.007}, "sl_potential": {"recovery_rate": 0.2}}
    _write_fold_results(
        tmp_path,
        "EURUSD",
        [_trade(0.01, entry="2025-01-01T09:00:00", exit="2025-01-01T10:00:00", bars=2)],
        analytics=analytics,
    )
    sym = compute_trade_diagnostics(tmp_path, ["EURUSD"]).per_symbol[0]
    assert sym.trade_analytics == analytics
    md = compute_trade_diagnostics(tmp_path, ["EURUSD"]).render_markdown()
    assert "sl_potential" in md


def test_aggregate_across_symbols(tmp_path):
    _write_fold_results(
        tmp_path,
        "EURUSD",
        [_trade(0.01, entry="2025-01-01T09:00:00", exit="2025-01-01T10:00:00", bars=2)],
    )
    _write_fold_results(
        tmp_path,
        "GBPUSD",
        [_trade(0.02, entry="2025-01-01T10:00:00", exit="2025-01-01T11:00:00", bars=3)],
    )
    diag = compute_trade_diagnostics(tmp_path, ["EURUSD", "GBPUSD"])
    assert [s.symbol for s in diag.per_symbol] == ["EURUSD", "GBPUSD"]
    assert diag.aggregate is not None
    assert diag.aggregate.n_trades == 2


def test_missing_run_dir_degrades(tmp_path):
    diag = compute_trade_diagnostics(tmp_path / "nope", ["EURUSD"])
    assert diag.aggregate is None
    assert diag.render_markdown() == "(no trade data)"
    assert diag.per_symbol[0].n_trades == 0


def test_malformed_fold_results_degrades(tmp_path):
    sym_dir = tmp_path / "grid_details" / "EURUSD"
    sym_dir.mkdir(parents=True)
    (sym_dir / "fold_results.json").write_text("{not json")
    diag = compute_trade_diagnostics(tmp_path, ["EURUSD"])
    assert diag.aggregate is None
    assert diag.render_markdown() == "(no trade data)"


def test_empty_trade_detail_degrades(tmp_path):
    _write_fold_results(tmp_path, "EURUSD", [])
    diag = compute_trade_diagnostics(tmp_path, ["EURUSD"])
    assert diag.aggregate is None
    assert diag.render_markdown() == "(no trade data)"


def test_trades_with_missing_or_bad_timestamps_do_not_crash(tmp_path):
    """Nulls / malformed times drop from time buckets but the trade still counts
    (via pnl) — the module must degrade, never raise."""
    trades = [
        # no hour, no entry_time, malformed exit_time
        {"pnl_raw": 0.02, "result": 1.0, "exit_time": "not-a-date"},
        # bad entry_time string, valid exit
        {
            "pnl_raw": -0.01,
            "result": -1.0,
            "entry_time": "13:00",
            "exit_time": "2025-03-01T13:00:00",
        },
    ]
    _write_fold_results(tmp_path, "EURUSD", trades)
    diag = compute_trade_diagnostics(tmp_path, ["EURUSD"])
    sym = diag.per_symbol[0]
    assert sym.n_trades == 2
    # No parseable entry hour/weekday → those buckets are empty, no crash.
    assert sym.by_hour == []
    assert sym.by_weekday == []
    # Only the one parseable exit year shows up.
    assert [y.year for y in sym.by_year] == [2025]
    # Renders without raising.
    assert "EURUSD" in diag.render_markdown()
