"""Plan 017 spike: can we compute a portfolio-correlation input today?

Read-only. For each strategy in the given lifecycle states (``--states``),
loads its most recent per-trade return series -- paper ``trades.jsonl``
preferred, backtest/holdout ``fold_results.json`` as fallback (the same
source `orchestrator/trade_diagnostics.py::_load_symbol_trades` reads) --
resamples it to daily P&L, and prints:

  - which strategies had *any* per-trade series available, and from where
  - the pairwise Pearson correlation matrix over overlapping days
  - the number of overlapping days per pair
  - a warning for any pair with < ``--min-overlap-days`` overlapping days

This is NOT the eventual gate. It only proves (or disproves) that the input
a `portfolio_check` gate would need already exists on disk / in the DB. See
docs/plans/2026-07-portfolio-risk-layer.md for the design this feeds.

No writes anywhere. Opens the SQLite DB with a read-only URI connection.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.trade_diagnostics import _load_symbol_trades

DEFAULT_STATES = ("paper_trading", "live_trading", "backtested")


# --- data access (read-only) -------------------------------------------------


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only via a SQLite URI connection."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _eligible_strategies(conn: sqlite3.Connection, states: tuple[str, ...]) -> list[dict]:
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"SELECT id, slug, current_state FROM strategy "
        f"WHERE current_state IN ({placeholders}) ORDER BY id",
        states,
    ).fetchall()
    return [{"id": r[0], "slug": r[1], "state": r[2]} for r in rows]


def _latest_backtested_payload(conn: sqlite3.Connection, strategy_id: int) -> dict | None:
    """Most recent ``strategy -> backtested`` transition payload, or None.

    ``runner.py`` stamps ``{"fwbg_run_id": ..., "universe": {"assets": [...]}}``
    on every such transition -- this is the only place the fwbg run id for a
    strategy's backtest is recorded.
    """
    row = conn.execute(
        "SELECT payload FROM transition WHERE entity_type='strategy' AND entity_id=? "
        "AND to_state='backtested' ORDER BY created_at DESC LIMIT 1",
        (strategy_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# --- per-source daily resampling --------------------------------------------


def _daily_from_rows(rows: list[tuple[str, float]]) -> pd.Series | None:
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "pnl"])
    df["day"] = pd.to_datetime(df["ts"], errors="coerce").dt.date
    df = df.dropna(subset=["day"])
    if df.empty:
        return None
    return df.groupby("day")["pnl"].sum()


def _paper_daily_series(fwbg_data_dir: Path, slug: str) -> tuple[pd.Series | None, str]:
    """Return (daily sum-of-pnl_pct series, reason-if-None) from trades.jsonl."""
    path = fwbg_data_dir / "account-trades" / slug / "trades.jsonl"
    if not path.is_file():
        return None, f"no paper trades.jsonl at {path}"
    rows: list[tuple[str, float]] = []
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            t = json.loads(raw)
        except json.JSONDecodeError:
            continue
        pnl, et = t.get("pnl_pct"), t.get("entry_time")
        if isinstance(pnl, (int, float)) and isinstance(et, str):
            rows.append((et, float(pnl)))
    daily = _daily_from_rows(rows)
    if daily is None:
        return None, "trades.jsonl has no closed trade with both pnl_pct and entry_time"
    return daily, "pnl_pct"


def _backtest_daily_series(
    test_results_dir: Path, run_id: str, symbols: list[str]
) -> tuple[pd.Series | None, str]:
    """Return (daily sum-of-pnl_raw series, reason-if-None) via fold_results.json."""
    all_trades: list[dict] = []
    for sym in symbols:
        trades, _ = _load_symbol_trades(test_results_dir / run_id, sym)
        all_trades.extend(trades)
    if not all_trades:
        return None, (
            f"no test_trades_detail under grid_details/*/fold_results.json for run {run_id!r}"
        )
    rows = [
        (
            t["exit_time"] if isinstance(t.get("exit_time"), str) else t.get("entry_time"),
            t["pnl_raw"],
        )
        for t in all_trades
        if isinstance(t.get("exit_time") or t.get("entry_time"), str)
    ]
    daily = _daily_from_rows(rows)
    if daily is None:
        return None, f"run {run_id!r} trades lack entry_time/exit_time (older fwbg installation)"
    return daily, "pnl_raw"


def load_strategy_series(
    conn: sqlite3.Connection,
    strategy: dict,
    test_results_dir: Path,
    fwbg_data_dir: Path,
) -> tuple[dict | None, str | None]:
    """Resolve one strategy's daily P&L series, preferring paper over backtest.

    Returns (result, None) on success where result = {slug, series, unit, source},
    or (None, reason) when no series could be loaded from either source.
    """
    daily, unit_or_reason = _paper_daily_series(fwbg_data_dir, strategy["slug"])
    if daily is not None:
        result = {
            "slug": strategy["slug"],
            "series": daily,
            "unit": unit_or_reason,
            "source": "paper",
        }
        return result, None
    paper_reason = unit_or_reason

    payload = _latest_backtested_payload(conn, strategy["id"])
    if payload is None:
        return None, f"paper: {paper_reason}; backtest: no strategy->backtested transition found"

    run_id = payload.get("fwbg_run_id")
    symbols = ((payload.get("universe") or {}).get("assets")) or []
    if not run_id or not symbols:
        return None, (
            f"paper: {paper_reason}; backtest: transition payload missing "
            f"fwbg_run_id/universe.assets (got run_id={run_id!r}, assets={symbols!r})"
        )

    daily, unit_or_reason = _backtest_daily_series(test_results_dir, run_id, symbols)
    if daily is None:
        return None, f"paper: {paper_reason}; backtest ({run_id}): {unit_or_reason}"
    return {
        "slug": strategy["slug"],
        "series": daily,
        "unit": unit_or_reason,
        "source": f"backtest:{run_id}",
    }, None


# --- correlation --------------------------------------------------------------


def _overlap_count(a: pd.Series, b: pd.Series) -> int:
    return int(a.index.intersection(b.index).size)


def print_report(loaded: list[dict], min_overlap_days: int) -> None:
    slugs = [item["slug"] for item in loaded]
    combined = pd.DataFrame({item["slug"]: item["series"] for item in loaded})

    units = {item["slug"]: item["unit"] for item in loaded}
    mixed_unit_pairs = [
        (a, b) for i, a in enumerate(slugs) for b in slugs[i + 1 :] if units[a] != units[b]
    ]

    print("\n=== Eligible strategies with a per-trade series ===")
    for item in loaded:
        s = item["series"]
        print(
            f"  {item['slug']}: source={item['source']} unit={item['unit']} "
            f"days={len(s)} span={min(s.index)}..{max(s.index)}"
        )

    if mixed_unit_pairs:
        print(
            "\nNOTE: unit mismatch -- 'pnl_pct' (paper, % of equity) vs 'pnl_raw' "
            "(backtest, quote-currency absolute) are not on the same scale. Pearson "
            "correlation is invariant to each series' own scale/offset, so the "
            "coefficient below is still a valid co-movement signal, but the two "
            "series are NOT directly comparable in magnitude (e.g. do not sum them "
            "as if they were the same currency exposure). Affected pairs:"
        )
        for a, b in mixed_unit_pairs:
            print(f"  - {a} ({units[a]}) vs {b} ({units[b]})")

    corr = combined.corr(method="pearson")
    print("\n=== Pairwise Pearson correlation (overlapping days only) ===")
    print(corr.round(3).to_string())

    print("\n=== Overlapping-day counts per pair ===")
    warnings = []
    for i, a in enumerate(slugs):
        for b in slugs[i + 1 :]:
            n = _overlap_count(combined[a].dropna(), combined[b].dropna())
            print(f"  {a} vs {b}: {n} overlapping days")
            if n < min_overlap_days:
                warnings.append((a, b, n))

    if warnings:
        print(f"\n=== WARNING: pairs below --min-overlap-days ({min_overlap_days}) ===")
        for a, b, n in warnings:
            print(f"  {a} vs {b}: only {n} overlapping days -- correlation not estimable")


# --- CLI ----------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Feasibility spike: compute a pairwise correlation matrix of daily "
            "P&L across strategies from data that already exists on disk / in "
            "the agents DB. Read-only."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=settings.data_dir / "state.db",
        help="Path to the agents SQLite state.db (opened read-only). Default: %(default)s",
    )
    parser.add_argument(
        "--test-results-dir",
        type=Path,
        default=settings.fwbg_test_results_dir,
        help="fwbg test_results dir (holds <run_id>/grid_details/<symbol>/fold_results.json). "
        "Default: %(default)s",
    )
    parser.add_argument(
        "--fwbg-data-dir",
        type=Path,
        default=settings.fwbg_data_dir,
        help="fwbg data dir root (holds account-trades/<slug>/trades.jsonl). Default: %(default)s",
    )
    parser.add_argument(
        "--states",
        type=str,
        default=",".join(DEFAULT_STATES),
        help="Comma-separated strategy lifecycle states to consider. Default: %(default)s",
    )
    parser.add_argument(
        "--min-overlap-days",
        type=int,
        default=20,
        help="Overlapping-day threshold below which a pair's correlation is flagged "
        "as not estimable. Default: %(default)s",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    states = tuple(s.strip() for s in args.states.split(",") if s.strip())

    if not args.db.is_file():
        print(f"no eligible strategies: db not found at {args.db}")
        return

    conn = _connect_ro(args.db)
    try:
        strategies = _eligible_strategies(conn, states)
        if not strategies:
            print(f"no eligible strategies: none of the strategies are in states {states}")
            return

        loaded: list[dict] = []
        skipped: list[tuple[str, str]] = []
        for strategy in strategies:
            result, reason = load_strategy_series(
                conn, strategy, args.test_results_dir, args.fwbg_data_dir
            )
            if result is not None:
                loaded.append(result)
            else:
                skipped.append((strategy["slug"], reason or "unknown"))

        print(f"considered {len(strategies)} strategies in states {states}:")
        for slug, reason in skipped:
            print(f"  SKIP {slug}: {reason}")

        if len(loaded) < 2:
            print(
                f"\nno eligible strategies: only {len(loaded)} strategy(ies) have a usable "
                "per-trade series (need >= 2 to compute any pairwise correlation)."
            )
            return

        print_report(loaded, args.min_overlap_days)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
