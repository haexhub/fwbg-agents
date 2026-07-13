"""Deterministic trade-level diagnostics for the Analyst (Plan 009 WP1).

The Analyst otherwise sees only aggregate metrics (Sharpe/PF) per asset and has
to *guess* the failure mode — and therefore the right iteration lever
(``tune_params`` vs ``change_exit`` vs ``modify_plugins``). This module derives
a concrete failure-mode picture from the individual trades so that guess becomes
evidence-based.

Data source (see the fwbg-agents memory note ``plan-009-fwbg-trade-schema``):
fwbg persists per-trade detail ONLY in
``grid_details/<symbol>/fold_results.json`` under
``walk_forward.fold_details[].test_trades_detail`` — the ``trades_detailed``
list in ``trades.json`` is always empty for normal runs. fwbg also pre-computes
MAE/MFE and SL/TP-potential into ``fold_results.json -> trade_analytics``, which
we surface verbatim instead of recomputing.

Pure and deterministic: no LLM, no market-data join (a regime split would need
that and is intentionally out of scope — see the plan). Any missing or malformed
input degrades to "(no trade data)" rather than raising.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class Bucket(BaseModel):
    """P&L rolled up over a group of trades (hour / weekday / holding band)."""

    key: str
    count: int
    expectancy: float  # mean pnl_raw over the bucket
    total_pnl: float


class YearSegment(BaseModel):
    """Per-calendar-year result — exposes an edge that lived in only one year."""

    year: int
    count: int
    total_pnl: float
    max_drawdown: float  # deepest drop of the within-year cumulative-pnl curve


class SymbolDiagnostics(BaseModel):
    """Failure-mode picture for one symbol (or the aggregate across all)."""

    symbol: str
    n_trades: int
    by_hour: list[Bucket]
    by_weekday: list[Bucket]
    by_holding: list[Bucket]
    by_year: list[YearSegment]
    payoff_ratio: float | None  # mean win / mean |loss|
    longest_loss_streak: int
    top5_pnl_share: float | None  # sum(5 largest pnls) / net pnl (>1 ⇒ clustering)
    trade_analytics: dict[str, Any] | None  # fwbg's MAE/MFE + SL/TP potential, verbatim


class TradeDiagnostics(BaseModel):
    """Per-symbol diagnostics plus an aggregate, with a Markdown renderer."""

    per_symbol: list[SymbolDiagnostics]
    aggregate: SymbolDiagnostics | None

    def render_markdown(self) -> str:
        """Render a compact Markdown report for the Analyst prompt."""
        if self.aggregate is None or self.aggregate.n_trades == 0:
            return "(no trade data)"
        parts = [_render_symbol(self.aggregate, heading="All assets (aggregate)")]
        for sym in self.per_symbol:
            if sym.n_trades:
                parts.append(_render_symbol(sym, heading=f"{sym.symbol}"))
        return "\n\n".join(parts)


# --- extraction -------------------------------------------------------------


def _load_symbol_trades(run_dir: Path, symbol: str) -> tuple[list[dict], dict | None]:
    """Return (per-trade dicts across all folds, trade_analytics) for a symbol.

    Reads ``grid_details/<symbol>/fold_results.json``. Missing or malformed →
    ([], None).
    """
    path = run_dir / "grid_details" / symbol / "fold_results.json"
    if not path.is_file():
        return [], None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("trade_diagnostics: skipping %s — bad fold_results.json (%s)", path, exc)
        return [], None
    trades: list[dict] = []
    for fold in (data.get("walk_forward") or {}).get("fold_details") or []:
        for t in fold.get("test_trades_detail") or []:
            if isinstance(t, dict) and isinstance(t.get("pnl_raw"), (int, float)):
                trades.append(t)
    analytics = (
        data.get("trade_analytics") if isinstance(data.get("trade_analytics"), dict) else None
    )
    return trades, analytics


# --- pure computations ------------------------------------------------------


def _bucketize(trades: list[dict], key_fn) -> list[Bucket]:
    """Group trades by ``key_fn(trade) -> str|None`` (None drops the trade)."""
    groups: dict[str, list[float]] = {}
    for t in trades:
        key = key_fn(t)
        if key is None:
            continue
        groups.setdefault(key, []).append(float(t["pnl_raw"]))
    return [
        Bucket(
            key=key,
            count=len(pnls),
            expectancy=statistics.mean(pnls),
            total_pnl=sum(pnls),
        )
        for key, pnls in sorted(groups.items())
    ]


def _entry_dt(t: dict) -> datetime | None:
    raw = t.get("entry_time")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _hour_key(t: dict) -> str | None:
    hour = t.get("hour")
    if isinstance(hour, int):
        return f"{hour:02d}h"
    dt = _entry_dt(t)
    return f"{dt.hour:02d}h" if dt else None


def _weekday_key(t: dict) -> str | None:
    dt = _entry_dt(t)
    return _WEEKDAYS[dt.weekday()] if dt else None


def _holding_buckets(trades: list[dict]) -> list[Bucket]:
    """Split trades into holding-duration quartiles by ``bars_held``."""
    held = [t for t in trades if isinstance(t.get("bars_held"), (int, float))]
    if len(held) < 4:
        return _bucketize(held, lambda t: f"{int(t['bars_held'])} bars")
    values = sorted(int(t["bars_held"]) for t in held)
    q = [
        values[len(values) // 4],
        values[len(values) // 2],
        values[3 * len(values) // 4],
    ]

    def band(t: dict) -> str:
        b = int(t["bars_held"])
        if b <= q[0]:
            return f"Q1 (<={q[0]} bars)"
        if b <= q[1]:
            return f"Q2 ({q[0] + 1}-{q[1]} bars)"
        if b <= q[2]:
            return f"Q3 ({q[1] + 1}-{q[2]} bars)"
        return f"Q4 (>{q[2]} bars)"

    return _bucketize(held, band)


def _year_segments(trades: list[dict]) -> list[YearSegment]:
    """Per-year cumulative P&L + intra-year max drawdown, ordered by exit time."""
    by_year: dict[int, list[float]] = {}
    for t in sorted(trades, key=lambda x: str(x.get("exit_time") or "")):
        exit_time = t.get("exit_time")
        if not isinstance(exit_time, str):
            continue
        try:
            year = datetime.fromisoformat(exit_time).year
        except ValueError:
            continue
        by_year.setdefault(year, []).append(float(t["pnl_raw"]))
    segments = []
    for year, pnls in sorted(by_year.items()):
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        segments.append(
            YearSegment(year=year, count=len(pnls), total_pnl=sum(pnls), max_drawdown=max_dd)
        )
    return segments


def _payoff_ratio(pnls: list[float]) -> float | None:
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    if not wins or not losses:
        return None
    return statistics.mean(wins) / statistics.mean(losses)


def _longest_loss_streak(trades: list[dict]) -> int:
    """Longest run of consecutive losers, ordered by exit time."""
    streak = 0
    longest = 0
    for t in sorted(trades, key=lambda x: str(x.get("exit_time") or "")):
        if float(t["pnl_raw"]) < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    return longest


def _top5_pnl_share(pnls: list[float]) -> float | None:
    """Share of net P&L delivered by the 5 largest trades (>1 ⇒ clustering)."""
    net = sum(pnls)
    if net <= 0 or len(pnls) <= 5:
        return None
    top5 = sum(sorted(pnls, reverse=True)[:5])
    return top5 / net


def _diagnose(symbol: str, trades: list[dict], analytics: dict | None) -> SymbolDiagnostics:
    pnls = [float(t["pnl_raw"]) for t in trades]
    return SymbolDiagnostics(
        symbol=symbol,
        n_trades=len(trades),
        by_hour=_bucketize(trades, _hour_key),
        by_weekday=_bucketize(trades, _weekday_key),
        by_holding=_holding_buckets(trades),
        by_year=_year_segments(trades),
        payoff_ratio=_payoff_ratio(pnls),
        longest_loss_streak=_longest_loss_streak(trades),
        top5_pnl_share=_top5_pnl_share(pnls),
        trade_analytics=analytics,
    )


def compute_trade_diagnostics(run_dir: Path, symbols: list[str]) -> TradeDiagnostics:
    """Build diagnostics for every symbol in ``symbols`` from a fwbg run dir."""
    per_symbol: list[SymbolDiagnostics] = []
    all_trades: list[dict] = []
    for symbol in symbols:
        trades, analytics = _load_symbol_trades(run_dir, symbol)
        per_symbol.append(_diagnose(symbol, trades, analytics))
        all_trades.extend(trades)
    aggregate = _diagnose("ALL", all_trades, None) if all_trades else None
    return TradeDiagnostics(per_symbol=per_symbol, aggregate=aggregate)


# --- rendering --------------------------------------------------------------


def _fmt_buckets(buckets: list[Bucket]) -> str:
    if not buckets:
        return "  (none)"
    return "\n".join(
        f"  - {b.key}: expectancy={b.expectancy:+.5f} pnl={b.total_pnl:+.4f} (n={b.count})"
        for b in buckets
    )


def _render_symbol(d: SymbolDiagnostics, *, heading: str) -> str:
    lines = [f"### {heading} — {d.n_trades} trades"]
    if d.payoff_ratio is not None:
        lines.append(f"- payoff ratio (avg win / avg loss): {d.payoff_ratio:.2f}")
    lines.append(f"- longest losing streak: {d.longest_loss_streak}")
    if d.top5_pnl_share is not None:
        lines.append(
            f"- top-5 trades deliver {d.top5_pnl_share:.0%} of net P&L "
            "(>100% ⇒ result rests on a few trades — doubt robustness)"
        )
    lines.append("- P&L by entry hour:")
    lines.append(_fmt_buckets(d.by_hour))
    lines.append("- P&L by weekday:")
    lines.append(_fmt_buckets(d.by_weekday))
    lines.append("- P&L by holding-duration quartile:")
    lines.append(_fmt_buckets(d.by_holding))
    if d.by_year:
        lines.append("- P&L by year (is the edge only in one year?):")
        lines.extend(
            f"  - {y.year}: pnl={y.total_pnl:+.4f} maxDD={y.max_drawdown:.4f} (n={y.count})"
            for y in d.by_year
        )
    if d.trade_analytics:
        lines.append("- MAE/MFE + SL/TP potential (from fwbg, use to tell exit vs entry apart):")
        lines.append("```json")
        lines.append(json.dumps(d.trade_analytics, indent=2, sort_keys=True))
        lines.append("```")
    return "\n".join(lines)
