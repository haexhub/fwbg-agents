"""Backfill durable trial statistics from existing strategy sidecars."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.trials import _trials_in_run, per_trade_sharpe, pnl_series
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import Strategy, TrialStat

log = logging.getLogger("backfill_trial_stats")


async def backfill(*, data_dir: Path, test_results_dir: Path) -> tuple[int, int]:
    """Insert snapshots for readable sidecars, skipping existing run IDs."""
    inserted = 0
    skipped = 0
    async with SessionLocal() as session:
        strategies = await session.execute(
            select(Strategy.slug, Strategy.id, Strategy.strategy_family)
        )
        strategy_by_slug = {slug: (strategy_id, family) for slug, strategy_id, family in strategies}
        for results_path in sorted(
            (data_dir / "strategies").glob("*/iteration_*/fwbg_results.json")
        ):
            try:
                run_data = json.loads(results_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("skipping %s (%s)", results_path, exc)
                continue
            run_id = run_data.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                log.warning("skipping %s (missing run_id)", results_path)
                continue
            if await session.scalar(select(TrialStat.id).where(TrialStat.run_id == run_id)):
                skipped += 1
                continue
            strategy_id, family = strategy_by_slug.get(
                results_path.parent.parent.name, (None, "unknown")
            )
            pnls = [x for x in pnl_series(test_results_dir / run_id) if math.isfinite(x)]
            session.add(
                TrialStat(
                    run_id=run_id,
                    strategy_id=strategy_id,
                    strategy_family=family or "unknown",
                    n_trials=_trials_in_run(run_data),
                    trade_sharpe=per_trade_sharpe(pnls),
                    n_trades=len(pnls),
                    created_at=datetime.now(UTC),
                )
            )
            inserted += 1
        await session.commit()
    return inserted, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=settings.data_dir)
    parser.add_argument("--test-results-dir", type=Path, default=settings.fwbg_test_results_dir)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    inserted, skipped = asyncio.run(
        backfill(data_dir=args.data_dir, test_results_dir=args.test_results_dir)
    )
    print(f"backfilled {inserted} rows, skipped {skipped} existing")


if __name__ == "__main__":
    main()
