"""List-price USD estimation for LLM calls.

The haex-claude-proxy uses subscription pricing, so figures produced here are
estimates at public Anthropic list price — meant for relative comparison
(cost per lineage, per outcome), not billing.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

from fwbg_agents.config import settings

log = logging.getLogger("fwbg_agents.llm_pricing")

# model-substring -> (usd_per_1M_input_tokens, usd_per_1M_output_tokens).
# Matched by the LONGEST key contained in the recorded model string, so
# date-suffixed variants (e.g. "claude-opus-4-7-20260115") still match.
# prices as of 2026-07 — update when models change
DEFAULT_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
}


@lru_cache(maxsize=4)
def _parse_table(raw: str) -> dict[str, tuple[float, float]] | None:
    """Parse a JSON price-table override; None if malformed (falls back to default)."""
    try:
        parsed = json.loads(raw)
        return {str(k): (float(v[0]), float(v[1])) for k, v in parsed.items()}
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        log.warning("invalid llm_price_table_json (%s); using built-in table", exc)
        return None


def _price_table() -> dict[str, tuple[float, float]]:
    """The active price table: settings JSON override or the built-in default."""
    raw = settings.llm_price_table_json
    if not raw:
        return DEFAULT_PRICE_TABLE
    return _parse_table(raw) or DEFAULT_PRICE_TABLE


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """List-price estimate in USD; None for unknown models (never guess)."""
    table = _price_table()
    matches = [key for key in table if key in model]
    if not matches:
        return None
    usd_per_1m_in, usd_per_1m_out = table[max(matches, key=len)]
    return (input_tokens * usd_per_1m_in + output_tokens * usd_per_1m_out) / 1_000_000
