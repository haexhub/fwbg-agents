"""Universe planning for the adaptive Runner (Phase 2).

Strategy-first research produces a `suggested_universe` — the Researcher's
recommendation of where an edge should be tested. The Runner tries that
recommendation first and, if it yields nothing (no data, or the backtest
found no results), broadens the universe step by step.

The ladder, most-specific first:

    1. "suggested"     — exactly the researcher's symbols + asset classes
    2. "class"         — drop the specific symbols, keep the classes
                         (plus the strategy's own asset_class if set)
    3. "unconstrained" — pass nothing; fwbg picks its default universe

Levels that would duplicate an earlier one are skipped, so a strategy with no
suggestion and no asset_class collapses to a single "unconstrained" attempt.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UniverseAttempt:
    """One rung of the fallback ladder passed to fwbg's /runs/start.

    `assets` are concrete symbols; `asset_classes` are class names. Either may
    be None (omitted from the request). Both None = unconstrained.
    """

    assets: tuple[str, ...] | None
    asset_classes: tuple[str, ...] | None
    label: str


def _clean(entries: list, scope: str) -> list[str]:
    """Ordered, de-duplicated `value`s for one scope from suggested_universe."""
    out: list[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("scope") != scope:
            continue
        value = e.get("value")
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


def timeframes_by_symbol(strategy) -> dict[str, str]:
    """Map each suggested symbol to its recommended timeframe (if any).

    Used to ask fwbg to ensure the right resolution before a backtest.
    """
    out: dict[str, str] = {}
    for e in strategy.suggested_universe or []:
        if not isinstance(e, dict) or e.get("scope") != "symbol":
            continue
        value, tf = e.get("value"), e.get("timeframe")
        if isinstance(value, str) and value and isinstance(tf, str) and tf:
            out.setdefault(value, tf)
    return out


def plan_universe_attempts(strategy) -> list[UniverseAttempt]:
    """Build the ordered fallback ladder for `strategy` (see module docstring)."""
    su = strategy.suggested_universe or []
    symbols = _clean(su, "symbol")
    classes = _clean(su, "asset_class")

    attempts: list[UniverseAttempt] = []
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()

    def add(assets: list[str], asset_classes: list[str], label: str) -> None:
        """Append a deduped UniverseAttempt for the given assets/classes combination."""
        key = (tuple(assets), tuple(asset_classes))
        if key in seen:
            return
        seen.add(key)
        attempts.append(
            UniverseAttempt(
                assets=tuple(assets) or None,
                asset_classes=tuple(asset_classes) or None,
                label=label,
            )
        )

    # 1. exactly what the researcher suggested
    if symbols or classes:
        add(symbols, classes, "suggested")

    # 2. broaden: drop specific symbols, keep classes plus the strategy's class
    broadened = list(classes)
    if strategy.asset_class and strategy.asset_class not in broadened:
        broadened.append(strategy.asset_class)
    if broadened:
        add([], broadened, "class")

    # 3. unconstrained fallback
    add([], [], "unconstrained")

    return attempts
