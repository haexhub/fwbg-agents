"""M6a smoke: drive the live-telemetry endpoints end-to-end via the API.

Walks the full M6a happy path + edge cases:

    [seed]                                Strategy in PAPER_TRADING with
                                          paper_account_id + fixture files on disk
    GET /strategies/{id}/paper-summary    → 200 with computed stats
    GET /strategies/{id}/paper-positions  → 200 with 2 positions w/ SL/TP
    [state-guard]                         flip to PROPOSED, both endpoints 409
    [404-check]                           restore PAPER_TRADING, delete fixture
                                          files, both endpoints 404

The smoke owns its own subdirectory of `data/` for fixtures so it does not
collide with real fwbg `data/account-trades/` content. Stage-0 cleanup wipes
prior smoke artifacts so re-runs work without manual intervention.

Prereq: `uv run alembic upgrade head`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fwbg_agents.config import settings
from fwbg_agents.main import app
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import Strategy, StrategyState

SMOKE_STRATEGY_SLUG = "smoke_m6a_paper_001"
SMOKE_PAPER_ACCOUNT_ID = "ig-demo-001"
SMOKE_PAPER_PHASE_TARGET_DAYS = 90

# Smoke-local fwbg_data_dir so we don't pollute the real fwbg data tree.
SMOKE_FWBG_DATA_DIR = Path("data/smoke/m6a")


def _write_fixture_files(data_dir: Path, slug: str) -> None:
    """Write trades.jsonl (30 trades, 45-day span), status.json, positions.json."""
    acct_dir = data_dir / "account-trades" / slug
    acct_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    first_entry = now - timedelta(days=45)
    last_entry = now - timedelta(hours=1)
    n = 30
    # Linear span across 45 days; alternate winners/losers for ~60% wins
    # (18 winners, 12 losers — index 0,2,3,5,6,8,9,11,...).
    delta = (last_entry - first_entry) / (n - 1)

    trades_lines: list[str] = []
    for i in range(n):
        entry_time = first_entry + delta * i
        # 60% wins: every 3rd trade loses (indices 1, 4, 7, ...). 30/3 = 10 losers
        # — close enough to "~60%", exact win-rate is not asserted.
        is_loser = (i % 3) == 1
        pnl_pct = -0.005 if is_loser else 0.005
        trade = {
            "symbol": "EURUSD",
            "side": "buy" if i % 2 == 0 else "sell",
            "quantity": 1000,
            "entry_time": entry_time.isoformat(),
            "exit_time": (entry_time + timedelta(hours=2)).isoformat(),
            "entry_price": 1.08,
            "exit_price": 1.08 * (1 + pnl_pct),
            "pnl_pct": pnl_pct,
        }
        trades_lines.append(json.dumps(trade))
    (acct_dir / "trades.jsonl").write_text("\n".join(trades_lines) + "\n")

    # Equity curve: 30 points climbing 10000 → 11200 with one dip to 10900.
    starting_equity = 10000.0
    current_equity = 11200.0
    curve: list[dict] = []
    for i in range(n):
        if i == 20:
            eq = 10900.0  # the dip — produces a non-zero max-dd
        else:
            # Linear climb from 10000 to 11200 across 30 points.
            eq = starting_equity + (current_equity - starting_equity) * (i / (n - 1))
        ts = first_entry + delta * i
        curve.append({"timestamp": ts.isoformat(), "equity": round(eq, 2)})

    status = {
        "current_equity": current_equity,
        "starting_equity": starting_equity,
        "equity_curve_sample": curve,
    }
    (acct_dir / "status.json").write_text(json.dumps(status, indent=2))

    positions = {
        "strategy_slug": slug,
        "updated_at": now.isoformat(),
        "positions": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "quantity": 1000,
                "entry_price": 1.08,
                "current_price": 1.085,
                "stop_loss": 1.07,
                "take_profit": 1.10,
                "unrealised_pnl_pct": 0.0046,
                "opened_at": (now - timedelta(hours=3)).isoformat(),
            },
            {
                "symbol": "GBPUSD",
                "side": "sell",
                "quantity": 500,
                "entry_price": 1.25,
                "current_price": 1.247,
                "stop_loss": 1.26,
                "take_profit": 1.23,
                "unrealised_pnl_pct": 0.0024,
                "opened_at": (now - timedelta(hours=1)).isoformat(),
            },
        ],
    }
    (acct_dir / "positions.json").write_text(json.dumps(positions, indent=2))


async def _seed_strategy() -> int:
    """Insert (or refresh) a PAPER_TRADING Strategy with the smoke's paper fields."""
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(Strategy).where(Strategy.slug == SMOKE_STRATEGY_SLUG)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.current_state = StrategyState.PAPER_TRADING.value
            existing.paper_account_id = SMOKE_PAPER_ACCOUNT_ID
            existing.paper_phase_target_days = SMOKE_PAPER_PHASE_TARGET_DAYS
            existing.updated_at = now
            await session.commit()
            return existing.id

        s = Strategy(
            slug=SMOKE_STRATEGY_SLUG,
            current_state=StrategyState.PAPER_TRADING.value,
            iteration_count=0,
            asset_class="FOREX",
            strategy_family="ORB",
            paper_account_id=SMOKE_PAPER_ACCOUNT_ID,
            paper_phase_target_days=SMOKE_PAPER_PHASE_TARGET_DAYS,
            created_at=now,
            updated_at=now,
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        return s.id


async def _cleanup_previous_run() -> None:
    """Stage-0 idempotency: wipe prior smoke artifacts.

    Strategy has no smoke-created children in M6a (unlike M5c) — just the
    parent row + the per-strategy account-trades dir under SMOKE_FWBG_DATA_DIR.
    """
    async with SessionLocal() as session:
        prior = (
            await session.execute(
                select(Strategy).where(Strategy.slug == SMOKE_STRATEGY_SLUG)
            )
        ).scalar_one_or_none()
        if prior is not None:
            await session.delete(prior)
            await session.commit()

    acct_dir = SMOKE_FWBG_DATA_DIR / "account-trades" / SMOKE_STRATEGY_SLUG
    if acct_dir.exists():
        shutil.rmtree(acct_dir)


async def _set_state(strategy_id: int, state: StrategyState) -> None:
    async with SessionLocal() as session:
        s = (
            await session.execute(select(Strategy).where(Strategy.id == strategy_id))
        ).scalar_one()
        s.current_state = state.value
        s.updated_at = datetime.now(UTC)
        await session.commit()


async def main() -> int:
    # Override settings.fwbg_data_dir for the duration of the smoke so the
    # endpoints + fixture writer agree on the same root. The override sticks
    # for the lifetime of the process — the smoke owns this directory.
    settings.fwbg_data_dir = SMOKE_FWBG_DATA_DIR.resolve()
    print(f"[m6a_smoke] data_dir={settings.fwbg_data_dir}")

    print("[m6a_smoke] [0/7] cleanup of prior smoke artifacts (idempotent)")
    await _cleanup_previous_run()

    print(
        "[m6a_smoke] [1/7] seed Strategy in PAPER_TRADING with "
        f"paper_account_id={SMOKE_PAPER_ACCOUNT_ID!r}, "
        f"paper_phase_target_days={SMOKE_PAPER_PHASE_TARGET_DAYS}"
    )
    strategy_id = await _seed_strategy()
    print(f"       → strategy_id={strategy_id} slug={SMOKE_STRATEGY_SLUG}")

    print(
        "[m6a_smoke] [2/7] write fixture files: trades.jsonl (30 trades, 45-day span), "
        "status.json (equity 10000→11200 with dip), positions.json (2 positions w/ SL/TP)"
    )
    _write_fixture_files(settings.fwbg_data_dir, SMOKE_STRATEGY_SLUG)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        print("[m6a_smoke] [3/7] GET /strategies/{id}/paper-summary")
        r = await client.get(f"/strategies/{strategy_id}/paper-summary")
        if r.status_code != 200:
            print(f"       ✗ unexpected status {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        summary = r.json()
        if summary.get("trades_total") != 30:
            print(
                f"       ✗ trades_total={summary.get('trades_total')!r}, expected 30",
                file=sys.stderr,
            )
            return 1
        if summary.get("days_in_paper", 0) < 44:
            print(
                f"       ✗ days_in_paper={summary.get('days_in_paper')!r}, expected >= 44",
                file=sys.stderr,
            )
            return 1
        sharpe = summary.get("sharpe_paper")
        if not isinstance(sharpe, (int, float)):
            print(
                f"       ✗ sharpe_paper not numeric: {sharpe!r}",
                file=sys.stderr,
            )
            return 1
        max_dd = summary.get("max_dd_paper")
        if not isinstance(max_dd, (int, float)) or max_dd <= 0:
            print(
                f"       ✗ max_dd_paper expected >0 numeric: {max_dd!r}",
                file=sys.stderr,
            )
            return 1
        print(
            f"       ✓ status=200 sharpe_paper={sharpe:.3f} "
            f"trades_total={summary['trades_total']} "
            f"days_in_paper={summary['days_in_paper']} max_dd={max_dd:.3f}"
        )

        print("[m6a_smoke] [4/7] GET /strategies/{id}/paper-positions")
        r = await client.get(f"/strategies/{strategy_id}/paper-positions")
        if r.status_code != 200:
            print(f"       ✗ unexpected status {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        positions_payload = r.json()
        positions = positions_payload.get("positions") or []
        if len(positions) != 2:
            print(
                f"       ✗ positions count={len(positions)}, expected 2",
                file=sys.stderr,
            )
            return 1
        first = positions[0]
        if first.get("stop_loss") != 1.07 or first.get("take_profit") != 1.10:
            print(
                f"       ✗ first position SL/TP mismatch: "
                f"sl={first.get('stop_loss')!r} tp={first.get('take_profit')!r}",
                file=sys.stderr,
            )
            return 1
        print(
            f"       ✓ status=200 positions={len(positions)} "
            f"first.stop_loss={first['stop_loss']} first.take_profit={first['take_profit']}"
        )

        print(
            "[m6a_smoke] [5/7] state-guard: transition Strategy to PROPOSED, retry endpoints"
        )
        await _set_state(strategy_id, StrategyState.PROPOSED)
        r = await client.get(f"/strategies/{strategy_id}/paper-summary")
        if r.status_code != 409:
            print(
                f"       ✗ paper-summary expected 409, got {r.status_code}: {r.text}",
                file=sys.stderr,
            )
            return 1
        r = await client.get(f"/strategies/{strategy_id}/paper-positions")
        if r.status_code != 409:
            print(
                f"       ✗ paper-positions expected 409, got {r.status_code}: {r.text}",
                file=sys.stderr,
            )
            return 1
        print("       ✓ both endpoints return 409")

        print(
            "[m6a_smoke] [6/7] 404-check: restore PAPER_TRADING, delete fixture files, "
            "retry endpoints"
        )
        await _set_state(strategy_id, StrategyState.PAPER_TRADING)
        acct_dir = settings.fwbg_data_dir / "account-trades" / SMOKE_STRATEGY_SLUG
        if acct_dir.exists():
            shutil.rmtree(acct_dir)
        r = await client.get(f"/strategies/{strategy_id}/paper-summary")
        if r.status_code != 404:
            print(
                f"       ✗ paper-summary expected 404, got {r.status_code}: {r.text}",
                file=sys.stderr,
            )
            return 1
        r = await client.get(f"/strategies/{strategy_id}/paper-positions")
        if r.status_code != 404:
            print(
                f"       ✗ paper-positions expected 404, got {r.status_code}: {r.text}",
                file=sys.stderr,
            )
            return 1
        print("       ✓ both endpoints return 404")

    print("[m6a_smoke] [7/7] all assertions passed")
    print("[m6a_smoke] PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
