"""Tests for `fwbg_paper_reader` — on-disk telemetry ingestion (M6a Task 4).

Reads three files written by fwbg's TradingBot under
`<FWBG_DATA_DIR>/account-trades/<strategy_slug>/`:
- trades.jsonl (append-only)
- status.json (overwrite)
- positions.json (overwrite)

Tests are behaviour-only (no implementation details) and use `tmp_path`
so each test gets an isolated data dir.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fwbg_agents.tools.fwbg_paper_reader import (
    PaperPositions,
    PaperTradeSummary,
    read_paper_positions,
    read_paper_summary,
)


def _account_dir(data_dir: Path, slug: str) -> Path:
    d = data_dir / "account-trades" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_trades(dir_: Path, trades: list[dict]) -> None:
    (dir_ / "trades.jsonl").write_text("\n".join(json.dumps(t) for t in trades) + "\n")


def _write_status(dir_: Path, status: dict) -> None:
    (dir_ / "status.json").write_text(json.dumps(status))


def _write_positions(dir_: Path, payload: dict) -> None:
    (dir_ / "positions.json").write_text(json.dumps(payload))


# ---------- read_paper_summary ----------


def test_summary_returns_none_when_no_files_exist(tmp_path: Path) -> None:
    assert read_paper_summary("foo", tmp_path) is None


def test_summary_with_status_only_zero_trades(tmp_path: Path) -> None:
    slug = "s1"
    d = _account_dir(tmp_path, slug)
    now = datetime.now(UTC)
    _write_status(
        d,
        {
            "strategy_slug": slug,
            "updated_at": now.isoformat(),
            "current_equity": 90.0,
            "starting_equity": 100.0,
            "equity_curve_sample": [
                {"t": now.isoformat(), "equity": 100.0},
                {"t": now.isoformat(), "equity": 120.0},
                {"t": now.isoformat(), "equity": 90.0},
            ],
        },
    )
    out = read_paper_summary(slug, tmp_path)
    assert isinstance(out, PaperTradeSummary)
    assert out.trades_total == 0
    assert out.sharpe_paper == 0.0
    assert out.win_rate == 0.0
    assert out.last_trade_at is None
    # peak=120, drop to 90 -> dd = (120-90)/120 = 0.25
    assert math.isclose(out.max_dd_paper, 0.25, abs_tol=1e-9)


def test_summary_computes_sharpe_from_trades(tmp_path: Path) -> None:
    slug = "s2"
    d = _account_dir(tmp_path, slug)
    now = datetime.now(UTC)
    # 30 pnl_pct values: 15 of 0.003, 15 of -0.001 -> mean = 0.001, pstdev = 0.002
    pnls = [0.003] * 15 + [-0.001] * 15
    trades = [
        {
            "trade_id": f"t{i}",
            "strategy_slug": slug,
            "symbol": "EURUSD",
            "side": "buy",
            "entry_time": (now - timedelta(days=30 - i)).isoformat(),
            "exit_time": (now - timedelta(days=30 - i)).isoformat(),
            "entry_price": 1.0,
            "exit_price": 1.0 + pnls[i],
            "pnl_pct": pnls[i],
            "quantity": 1000,
            "fees": 0.0,
        }
        for i in range(30)
    ]
    _write_trades(d, trades)
    out = read_paper_summary(slug, tmp_path)
    assert out is not None
    expected = (0.001 / 0.002) * math.sqrt(252)
    assert math.isclose(out.sharpe_paper, expected, abs_tol=0.01)


def test_summary_computes_max_dd_from_equity_curve(tmp_path: Path) -> None:
    slug = "s3"
    d = _account_dir(tmp_path, slug)
    now = datetime.now(UTC)
    _write_status(
        d,
        {
            "strategy_slug": slug,
            "updated_at": now.isoformat(),
            "current_equity": 105.0,
            "starting_equity": 100.0,
            "equity_curve_sample": [
                {"t": now.isoformat(), "equity": 100.0},
                {"t": now.isoformat(), "equity": 120.0},
                {"t": now.isoformat(), "equity": 90.0},
                {"t": now.isoformat(), "equity": 105.0},
            ],
        },
    )
    out = read_paper_summary(slug, tmp_path)
    assert out is not None
    assert math.isclose(out.max_dd_paper, 0.25, abs_tol=1e-9)


def test_summary_win_rate_from_trades(tmp_path: Path) -> None:
    slug = "s4"
    d = _account_dir(tmp_path, slug)
    now = datetime.now(UTC)
    pnls = [0.01] * 6 + [-0.01] * 4
    trades = [
        {
            "trade_id": f"t{i}",
            "strategy_slug": slug,
            "symbol": "EURUSD",
            "side": "buy",
            "entry_time": (now - timedelta(hours=10 - i)).isoformat(),
            "exit_time": (now - timedelta(hours=10 - i)).isoformat(),
            "entry_price": 1.0,
            "exit_price": 1.0 + pnls[i],
            "pnl_pct": pnls[i],
            "quantity": 1000,
            "fees": 0.0,
        }
        for i in range(10)
    ]
    _write_trades(d, trades)
    out = read_paper_summary(slug, tmp_path)
    assert out is not None
    assert math.isclose(out.win_rate, 0.6, abs_tol=1e-9)


def test_summary_days_in_paper_from_first_trade(tmp_path: Path) -> None:
    slug = "s5"
    d = _account_dir(tmp_path, slug)
    now = datetime.now(UTC)
    first = now - timedelta(days=45)
    trades = [
        {
            "trade_id": "t0",
            "strategy_slug": slug,
            "symbol": "EURUSD",
            "side": "buy",
            "entry_time": first.isoformat(),
            "exit_time": first.isoformat(),
            "entry_price": 1.0,
            "exit_price": 1.01,
            "pnl_pct": 0.01,
            "quantity": 1000,
            "fees": 0.0,
        },
        {
            "trade_id": "t1",
            "strategy_slug": slug,
            "symbol": "EURUSD",
            "side": "buy",
            "entry_time": now.isoformat(),
            "exit_time": now.isoformat(),
            "entry_price": 1.0,
            "exit_price": 0.99,
            "pnl_pct": -0.01,
            "quantity": 1000,
            "fees": 0.0,
        },
    ]
    _write_trades(d, trades)
    out = read_paper_summary(slug, tmp_path)
    assert out is not None
    assert 44 <= out.days_in_paper <= 46


def test_summary_trades_today_filters_by_utc_date(tmp_path: Path) -> None:
    slug = "s6"
    d = _account_dir(tmp_path, slug)
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    today_trades = [
        {
            "trade_id": f"today-{i}",
            "strategy_slug": slug,
            "symbol": "EURUSD",
            "side": "buy",
            "entry_time": now.isoformat(),
            "exit_time": None,
            "entry_price": 1.0,
            "exit_price": None,
            "pnl_pct": None,
            "quantity": 1000,
            "fees": 0.0,
        }
        for i in range(3)
    ]
    yest_trades = [
        {
            "trade_id": f"yest-{i}",
            "strategy_slug": slug,
            "symbol": "EURUSD",
            "side": "buy",
            "entry_time": yesterday.isoformat(),
            "exit_time": yesterday.isoformat(),
            "entry_price": 1.0,
            "exit_price": 1.001,
            "pnl_pct": 0.001,
            "quantity": 1000,
            "fees": 0.0,
        }
        for i in range(5)
    ]
    _write_trades(d, today_trades + yest_trades)
    out = read_paper_summary(slug, tmp_path)
    assert out is not None
    assert out.trades_today == 3


# ---------- read_paper_positions ----------


def test_positions_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert read_paper_positions("nope", tmp_path) is None


def test_positions_parses_sl_tp_current_price(tmp_path: Path) -> None:
    slug = "p1"
    d = _account_dir(tmp_path, slug)
    now = datetime.now(UTC)
    opened = now - timedelta(hours=2)
    _write_positions(
        d,
        {
            "strategy_slug": slug,
            "updated_at": now.isoformat(),
            "positions": [
                {
                    "symbol": "EURUSD",
                    "side": "buy",
                    "quantity": 1000.0,
                    "entry_price": 1.0823,
                    "current_price": 1.0851,
                    "stop_loss": 1.0790,
                    "take_profit": 1.0900,
                    "unrealised_pnl_pct": 0.0026,
                    "opened_at": opened.isoformat(),
                }
            ],
        },
    )
    out = read_paper_positions(slug, tmp_path)
    assert isinstance(out, PaperPositions)
    assert len(out.positions) == 1
    p = out.positions[0]
    assert p.symbol == "EURUSD"
    assert p.side == "buy"
    assert p.quantity == 1000.0
    assert math.isclose(p.entry_price, 1.0823)
    assert p.current_price is not None and math.isclose(p.current_price, 1.0851)
    assert p.stop_loss is not None and math.isclose(p.stop_loss, 1.0790)
    assert p.take_profit is not None and math.isclose(p.take_profit, 1.0900)
    assert p.unrealised_pnl_pct is not None and math.isclose(p.unrealised_pnl_pct, 0.0026)


def test_positions_empty_list_when_file_has_empty_positions(tmp_path: Path) -> None:
    slug = "p2"
    d = _account_dir(tmp_path, slug)
    now = datetime.now(UTC)
    _write_positions(
        d,
        {
            "strategy_slug": slug,
            "updated_at": now.isoformat(),
            "positions": [],
        },
    )
    out = read_paper_positions(slug, tmp_path)
    assert isinstance(out, PaperPositions)
    assert out.positions == []
    # updated_at round-tripped to a tz-aware datetime
    assert out.updated_at.tzinfo is not None
