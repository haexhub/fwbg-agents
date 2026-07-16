"""On-disk telemetry reader for fwbg paper-trading state (M6a Task 4).

Reads three files written by fwbg's `TradingBot` under
`<FWBG_DATA_DIR>/account-trades/<strategy_slug>/`:

- `trades.jsonl`  — append-only, one JSON line per executed trade
- `status.json`   — overwrite, current/starting equity + equity_curve_sample
- `positions.json` — overwrite, list of currently-open positions w/ SL/TP

Formulas (sharpe / max-dd / win-rate / days-in-paper) are intentionally
inlined here — we do NOT import from `fwbg` to keep the agents service
cross-repo decoupled.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

log = logging.getLogger(__name__)


# ---------- models ----------


class PaperTradeSummary(BaseModel):
    """Aggregated paper-trading performance summary for a strategy."""

    strategy_slug: str
    sharpe_paper: float
    sharpe_paper_per_trade: float
    max_dd_paper: float  # 0.0..1.0
    trades_total: int
    trades_today: int
    days_in_paper: int
    win_rate: float  # 0.0..1.0
    last_trade_at: datetime | None
    current_equity: float
    starting_equity: float
    equity_curve_sample: list[dict[str, Any]]
    # Fill-fidelity metrics (plan 016) — None until trades carry
    # signal_price/assumed_spread (fwbg bot telemetry). Legacy trades.jsonl
    # files therefore keep producing a fully-valid summary with these unset.
    avg_entry_slippage: float | None
    avg_assumed_half_spread: float | None
    fill_fidelity_ratio: float | None
    fidelity_sample_size: int


class PaperPosition(BaseModel):
    """A single open paper-trading position."""

    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    entry_price: float
    current_price: float | None
    stop_loss: float | None
    take_profit: float | None
    unrealised_pnl_pct: float | None
    opened_at: datetime


class PaperPositions(BaseModel):
    """All open paper-trading positions for a strategy at a point in time."""

    strategy_slug: str
    updated_at: datetime
    positions: list[PaperPosition]


# ---------- helpers ----------


def _account_dir(fwbg_data_dir: Path, strategy_slug: str) -> Path:
    """Return the fwbg account-trades directory for a strategy slug."""
    return fwbg_data_dir / "account-trades" / strategy_slug


def _parse_trades_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file; skip and log corrupt lines, don't raise."""
    trades: list[dict[str, Any]] = []
    text = path.read_text()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            trades.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            log.warning(
                "fwbg_paper_reader: skipping corrupt trades.jsonl line %d in %s: %s",
                lineno,
                path,
                exc,
            )
    return trades


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 string to a timezone-aware datetime, or return None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _compute_sharpe(pnls: list[float]) -> float:
    """Annualised Sharpe, treating each trade as one daily return (sqrt(252)).

    NOT comparable to the backtest's per-trade Sharpe (mean/std of the
    trade-P&L series, no annualisation — see `orchestrator/trials.py` "Unit
    discipline"); prefer `_compute_sharpe_per_trade` /
    `sharpe_paper_per_trade` for backtest-vs-paper comparisons. 0.0 if
    undefined.
    """
    if len(pnls) < 2:
        return 0.0
    mean = statistics.mean(pnls)
    std = statistics.pstdev(pnls)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(252)


def _compute_sharpe_per_trade(pnls: list[float]) -> float:
    """Per-trade Sharpe (mean/pstdev, no annualisation). 0.0 if undefined.

    Mirrors the backtest side's unit discipline so paper and backtest Sharpe
    are directly comparable.
    """
    if len(pnls) < 2:
        return 0.0
    mean = statistics.mean(pnls)
    std = statistics.pstdev(pnls)
    if std == 0:
        return 0.0
    return mean / std


def _compute_max_dd(equity_curve_sample: list[dict[str, Any]]) -> float:
    """Max drawdown from a running peak. Skip points with peak == 0."""
    peak = 0.0
    max_dd = 0.0
    for point in equity_curve_sample:
        eq = point.get("equity")
        if not isinstance(eq, (int, float)):
            continue
        eq_f = float(eq)
        if eq_f > peak:
            peak = eq_f
        if peak <= 0:
            continue
        dd = (peak - eq_f) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _compute_fidelity(
    parsed_trades: list[dict[str, Any]],
) -> tuple[float | None, float | None, float | None, int]:
    """Aggregate entry-slippage vs. assumed-spread fidelity across trades.

    A trade contributes only when it carries finite `entry_price`,
    `signal_price`, and `assumed_spread` (fwbg bot telemetry, plan 016).
    Legacy trades.jsonl entries without these fields are skipped, not
    zero-filled — so a legacy-only file yields `(None, None, None, 0)`.

    Returns (avg_entry_slippage, avg_assumed_half_spread,
    fill_fidelity_ratio, fidelity_sample_size). `fill_fidelity_ratio` is
    None when the average half-spread is 0/undefined.
    """
    slippages: list[float] = []
    half_spreads: list[float] = []
    for trade in parsed_trades:
        entry_price = trade.get("entry_price")
        signal_price = trade.get("signal_price")
        assumed_spread = trade.get("assumed_spread")
        if not (
            isinstance(entry_price, (int, float))
            and isinstance(signal_price, (int, float))
            and isinstance(assumed_spread, (int, float))
        ):
            continue
        entry_price = float(entry_price)
        signal_price = float(signal_price)
        assumed_spread = float(assumed_spread)
        if not (
            math.isfinite(entry_price)
            and math.isfinite(signal_price)
            and math.isfinite(assumed_spread)
        ):
            continue
        slippages.append(abs(entry_price - signal_price))
        half_spreads.append(assumed_spread / 2)

    if not slippages:
        return None, None, None, 0

    avg_entry_slippage = statistics.mean(slippages)
    avg_assumed_half_spread = statistics.mean(half_spreads)
    fill_fidelity_ratio = (
        avg_entry_slippage / avg_assumed_half_spread if avg_assumed_half_spread else None
    )
    return avg_entry_slippage, avg_assumed_half_spread, fill_fidelity_ratio, len(slippages)


# ---------- public API ----------


def read_paper_summary(strategy_slug: str, fwbg_data_dir: Path) -> PaperTradeSummary | None:
    """Aggregate trades.jsonl + status.json into a single summary.

    Returns None when both files are missing. Either file alone is enough.
    """
    base = _account_dir(fwbg_data_dir, strategy_slug)
    trades_path = base / "trades.jsonl"
    status_path = base / "status.json"

    has_trades = trades_path.exists()
    has_status = status_path.exists()
    if not has_trades and not has_status:
        return None

    # ---- trades ----
    parsed_trades: list[dict[str, Any]] = []
    if has_trades:
        parsed_trades = _parse_trades_jsonl(trades_path)

    pnl_values: list[float] = []
    entry_times: list[datetime] = []
    wins = 0
    closed = 0
    for trade in parsed_trades:
        pnl = trade.get("pnl_pct")
        if isinstance(pnl, (int, float)):
            pnl_values.append(float(pnl))
            closed += 1
            if pnl > 0:
                wins += 1
        et = _parse_dt(trade.get("entry_time"))
        if et is not None:
            entry_times.append(et)

    now = datetime.now(UTC)
    today_utc = now.date()

    sharpe_paper = _compute_sharpe(pnl_values)
    sharpe_paper_per_trade = _compute_sharpe_per_trade(pnl_values)
    win_rate = (wins / closed) if closed > 0 else 0.0
    trades_today = sum(1 for et in entry_times if et.date() == today_utc)
    last_trade_at = max(entry_times) if entry_times else None
    days_in_paper = (now - min(entry_times)).days if entry_times else 0
    trades_total = len(parsed_trades)
    (
        avg_entry_slippage,
        avg_assumed_half_spread,
        fill_fidelity_ratio,
        fidelity_sample_size,
    ) = _compute_fidelity(parsed_trades)

    # ---- status ----
    current_equity = 0.0
    starting_equity = 0.0
    equity_curve_sample: list[dict[str, Any]] = []
    if has_status:
        try:
            status = json.loads(status_path.read_text())
        except json.JSONDecodeError as exc:
            log.warning(
                "fwbg_paper_reader: corrupt status.json for %s: %s",
                strategy_slug,
                exc,
            )
            status = {}
        current_equity = float(status.get("current_equity", 0.0) or 0.0)
        starting_equity = float(status.get("starting_equity", 0.0) or 0.0)
        raw_curve = status.get("equity_curve_sample") or []
        if isinstance(raw_curve, list):
            equity_curve_sample = raw_curve

    max_dd_paper = _compute_max_dd(equity_curve_sample)

    return PaperTradeSummary(
        strategy_slug=strategy_slug,
        sharpe_paper=sharpe_paper,
        sharpe_paper_per_trade=sharpe_paper_per_trade,
        max_dd_paper=max_dd_paper,
        trades_total=trades_total,
        trades_today=trades_today,
        days_in_paper=days_in_paper,
        win_rate=win_rate,
        last_trade_at=last_trade_at,
        current_equity=current_equity,
        starting_equity=starting_equity,
        equity_curve_sample=equity_curve_sample,
        avg_entry_slippage=avg_entry_slippage,
        avg_assumed_half_spread=avg_assumed_half_spread,
        fill_fidelity_ratio=fill_fidelity_ratio,
        fidelity_sample_size=fidelity_sample_size,
    )


def read_paper_positions(strategy_slug: str, fwbg_data_dir: Path) -> PaperPositions | None:
    """Read positions.json. Returns None when the file does not exist."""
    path = _account_dir(fwbg_data_dir, strategy_slug) / "positions.json"
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        log.warning(
            "fwbg_paper_reader: corrupt positions.json for %s: %s",
            strategy_slug,
            exc,
        )
        return None

    raw_positions = payload.get("positions") or []
    if not isinstance(raw_positions, list):
        raw_positions = []

    normalised: list[dict[str, Any]] = []
    for raw in raw_positions:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        side = item.get("side")
        if isinstance(side, str):
            item["side"] = side.lower()
        normalised.append(item)

    return PaperPositions(
        strategy_slug=payload.get("strategy_slug", strategy_slug),
        updated_at=payload.get("updated_at"),
        positions=normalised,  # type: ignore[arg-type]  # pydantic coerces dict -> PaperPosition
    )
