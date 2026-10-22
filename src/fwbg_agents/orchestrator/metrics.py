"""Shared fwbg run-result aggregation.

Used by both the analyst's promotion checks and the runner's iteration
backtests, and by the promote gate (Plan 009 WP4) — kept in one place so the
two money-adjacent call sites can't silently diverge.
"""

from __future__ import annotations

import statistics
from typing import Any


def median_metrics_across_assets(run: dict[str, Any]) -> dict[str, float]:
    """Per-metric median across every asset that produced unified_metrics.

    A strategy is judged over its whole universe rather than its single best
    symbol (which invites selection bias — a strong result on one asset
    carried the whole gate). For a single-asset universe the median equals
    that asset's metrics, so single-asset strategies are unaffected. Returns
    an empty dict when no asset produced metrics.

    A ``sharpe`` of ``None`` counts as 0.0: fwbg writes it for a ``no_edge``
    symbol, which was evaluated and found no edge — dropping it would let the
    remaining symbols carry the median. Other non-numeric values are left out.
    """
    per_metric: dict[str, list[float]] = {}
    for sym in (run.get("assets") or {}).values():
        m = sym.get("unified_metrics") or {}
        for k, v in m.items():
            if k == "sharpe" and v is None:
                v = 0.0
            if isinstance(v, (int, float)):
                per_metric.setdefault(k, []).append(float(v))
    return {k: float(statistics.median(vs)) for k, vs in per_metric.items() if vs}
