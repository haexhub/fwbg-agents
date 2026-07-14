"""Exploration-balance digest — diversity pressure for the Researcher (Plan 010 WP3).

Deterministic (no LLM): counts existing strategies by
``strategy_family x asset_class x timeframe`` and renders a length-capped
digest for the ``{{ exploration_balance }}`` prompt slot, so the Researcher
sees which cells are already crowded and can prefer underexplored ones
(as long as the hypothesis stays mechanistically sound — this is pressure,
not a hard rule).

``timeframe`` isn't a Strategy column — it lives in each strategy's
``iteration_001/strategy.json``, so this reads one small file per strategy.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.models import Strategy

log = logging.getLogger(__name__)

DIGEST_MAX_CHARS = 3000


def _read_timeframe(slug: str) -> str:
    path = strategy_dir(slug) / "iteration_001" / "strategy.json"
    if not path.is_file():
        return "unknown"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return "unknown"
    tf = data.get("timeframe")
    return tf if isinstance(tf, str) and tf else "unknown"


async def exploration_balance_digest(
    session: AsyncSession, max_chars: int = DIGEST_MAX_CHARS
) -> str:
    """Render the distribution of existing strategies over
    family x asset_class x timeframe cells, most-crowded first."""
    rows = (
        await session.execute(select(Strategy.slug, Strategy.strategy_family, Strategy.asset_class))
    ).all()

    cells: dict[tuple[str, str, str], int] = {}
    for slug, family, asset_class in rows:
        key = (family or "unknown", asset_class or "unknown", _read_timeframe(slug))
        cells[key] = cells.get(key, 0) + 1

    if not cells:
        return "(no strategies proposed yet — every cell is unexplored)"

    lines = [
        "Existing strategies by strategy_family x asset_class x timeframe "
        "(most-crowded first):",
        "",
    ]
    for (family, asset_class, timeframe), count in sorted(
        cells.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        lines.append(f"- {family} x {asset_class} x {timeframe}: {count}")
    lines.append("")
    lines.append(
        "Prefer an underexplored cell for your hypothesis, but only if the "
        "mechanism is genuinely sound for it — do not force a family/asset/"
        "timeframe combination that lacks a real edge just to diversify."
    )
    text = "\n".join(lines)
    return text[:max_chars]
