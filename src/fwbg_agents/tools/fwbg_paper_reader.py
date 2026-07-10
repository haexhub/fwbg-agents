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
    max_dd_paper: float  # 0.0..1.0
    trades_total: int
    trades_today: int
    days_in_paper: int
    win_rate: float  # 0.0..1.0
    last_trade_at: datetime | None
    current_equity: float
    starting_equity: float
    equity_curve_sample: list[dict[str, Any]]


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
    """Annualised Sharpe assuming ~252 trading days. 0.0 if undefined."""
    if len(pnls) < 2:
        return 0.0
    mean = statistics.mean(pnls)
    std = statistics.pstdev(pnls)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(252)


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
    win_rate = (wins / closed) if closed > 0 else 0.0
    trades_today = sum(1 for et in entry_times if et.date() == today_utc)
    last_trade_at = max(entry_times) if entry_times else None
    days_in_paper = (now - min(entry_times)).days if entry_times else 0
    trades_total = len(parsed_trades)

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
        max_dd_paper=max_dd_paper,
        trades_total=trades_total,
        trades_today=trades_today,
        days_in_paper=days_in_paper,
        win_rate=win_rate,
        last_trade_at=last_trade_at,
        current_equity=current_equity,
        starting_equity=starting_equity,
        equity_curve_sample=equity_curve_sample,
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
