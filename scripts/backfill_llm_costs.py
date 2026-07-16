"""Backfill list-price USD estimates onto historical LlmCall rows (Plan 018).

Fills `cost_usd` for rows where it is NULL and the model matches the price
table in tools/llm_pricing.py. Idempotent: already-priced rows are never
touched, and unknown models (e.g. search-quota pseudo-calls like
"tavily-search") stay NULL — they surface as `unpriced_calls` in
GET /economics/summary.

Usage:
    uv run python scripts/backfill_llm_costs.py
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import LlmCall
from fwbg_agents.tools.llm_pricing import estimate_cost_usd

log = logging.getLogger("backfill_llm_costs")


async def backfill(session: AsyncSession) -> tuple[int, int]:
    """Price all NULL-cost rows the table knows; return (updated, skipped)."""
    rows = (
        (await session.execute(select(LlmCall).where(LlmCall.cost_usd.is_(None)))).scalars().all()
    )
    updated = skipped = 0
    for row in rows:
        cost = estimate_cost_usd(row.model, row.input_tokens, row.output_tokens)
        if cost is None:
            skipped += 1
            continue
        row.cost_usd = cost
        updated += 1
    await session.commit()
    return updated, skipped


async def main() -> None:
    async with SessionLocal() as session:
        updated, skipped = await backfill(session)
    print(f"updated {updated}, skipped {skipped} (unknown model)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
