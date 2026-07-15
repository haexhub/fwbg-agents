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
import re
import sqlite3
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
    by_vol_regime: list[Bucket]  # fwbg-computed ATR tercile at entry (Plan 010 WP5)
    by_trend_regime: list[Bucket]  # fwbg-computed ADX bucket at entry (Plan 010 WP5)
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


def _vol_regime_key(t: dict) -> str | None:
    regime = t.get("vol_regime")
    return regime if isinstance(regime, str) else None


def _trend_regime_key(t: dict) -> str | None:
    regime = t.get("trend_regime")
    return regime if isinstance(regime, str) else None


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
        by_vol_regime=_bucketize(trades, _vol_regime_key),
        by_trend_regime=_bucketize(trades, _trend_regime_key),
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
    if d.by_vol_regime:
        lines.append("- P&L by volatility regime at entry (ATR tercile):")
        lines.append(_fmt_buckets(d.by_vol_regime))
    if d.by_trend_regime:
        lines.append("- P&L by trend regime at entry (ADX bucket):")
        lines.append(_fmt_buckets(d.by_trend_regime))
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


# --- trade store (Analyst tool-use, Plan 010 WP4) ---------------------------

TRADE_QUERY_ROW_CAP = 200

# Anything beyond a read of the `trades` table is out of scope for the
# Analyst's ad-hoc queries — reject rather than let a stray write/PRAGMA slip
# through to the shared in-memory connection.
_FORBIDDEN_SQL_KEYWORDS = (
    "PRAGMA",
    "ATTACH",
    "DETACH",
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "VACUUM",
    "REINDEX",
    "TRIGGER",
)


def _sql_safe_value(v: Any) -> Any:
    """SQLite can only bind None/int/float/str/bytes — JSON-encode anything else."""
    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    return json.dumps(v)


def build_trade_store(run_dir: Path, symbols: list[str]) -> sqlite3.Connection:
    """Load every trade of every symbol/fold of one run into an in-memory
    SQLite ``trades`` table (columns = union of trade-dict keys + ``symbol``
    + ``fold``). Built once, up front — no filesystem access happens from
    inside the query tools that read from the returned connection.
    """
    rows: list[dict] = []
    columns: dict[str, None] = {}
    for symbol in symbols:
        path = run_dir / "grid_details" / symbol / "fold_results.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("trade_store: skipping %s — bad fold_results.json (%s)", path, exc)
            continue
        folds = (data.get("walk_forward") or {}).get("fold_details") or []
        for fold_idx, fold in enumerate(folds):
            for t in fold.get("test_trades_detail") or []:
                if not isinstance(t, dict):
                    continue
                row = dict(t)
                row["symbol"] = symbol
                row["fold"] = fold_idx
                rows.append(row)
                for k in row:
                    columns.setdefault(k, None)

    # pydantic-ai runs `tool_plain` sync callables in a worker-thread executor,
    # so the connection built on the event-loop thread is used from a
    # different thread on every tool call — sqlite3's default same-thread
    # check would reject that. Calls are still sequential (one awaited tool
    # call at a time), so this is safe.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    col_names = list(columns) or ["symbol", "fold"]
    # Column names come from artifact JSON keys — escape quotes so a stray
    # '"' in a key can't break out of the identifier.
    quoted = ", ".join('"{}"'.format(c.replace('"', '""')) for c in col_names)
    conn.execute(f"CREATE TABLE trades ({quoted})")
    if rows:
        placeholders = ", ".join("?" for _ in col_names)
        conn.executemany(
            f"INSERT INTO trades ({quoted}) VALUES ({placeholders})",
            [[_sql_safe_value(row.get(c)) for c in col_names] for row in rows],
        )
    conn.commit()
    return conn


def validate_select_sql(sql: str) -> str | None:
    """Return an error message if ``sql`` isn't a single safe SELECT, else None."""
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if ";" in stripped:
        return "only a single SELECT statement is allowed (no ';')"
    if not re.match(r"(?is)^\s*SELECT\b", stripped):
        return "only SELECT statements are allowed"
    upper = stripped.upper()
    for kw in _FORBIDDEN_SQL_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return f"forbidden keyword: {kw}"
    return None


def query_trades(conn: sqlite3.Connection, sql: str) -> str:
    """Run a read-only, single-statement SELECT against the trade store.

    Returns compact JSON on success (capped at ``TRADE_QUERY_ROW_CAP`` rows,
    enforced by ``fetchmany`` so the query's own ORDER BY is respected — not
    by wrapping in a sub-SELECT, which SQLite doesn't guarantee to preserve).
    On rejection or a SQL error, returns a plain error string so the model
    can see what went wrong and self-correct.
    """
    error = validate_select_sql(sql)
    if error:
        return f"query rejected: {error}"
    stripped = sql.strip().rstrip(";")
    try:
        cur = conn.execute(stripped)
        cols = [d[0] for d in cur.description or []]
        fetched = cur.fetchmany(TRADE_QUERY_ROW_CAP)
    except sqlite3.Error as exc:
        return f"query error: {exc}"
    rows = [dict(zip(cols, row, strict=True)) for row in fetched]
    return json.dumps(rows, separators=(",", ":"), default=str)


def describe_trades(conn: sqlite3.Connection) -> dict[str, Any]:
    """Column list + row count + entry-time range per symbol, so the model
    doesn't have to guess the schema before querying."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()]
    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    per_symbol = []
    if "symbol" in columns:
        has_entry_time = "entry_time" in columns
        time_cols = "MIN(entry_time), MAX(entry_time)" if has_entry_time else "NULL, NULL"
        for symbol, count, min_t, max_t in conn.execute(
            f"SELECT symbol, COUNT(*), {time_cols} FROM trades GROUP BY symbol ORDER BY symbol"
        ).fetchall():
            per_symbol.append(
                {"symbol": symbol, "count": count, "min_entry_time": min_t, "max_entry_time": max_t}
            )
    return {"columns": columns, "total_rows": total, "per_symbol": per_symbol}
