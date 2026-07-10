"""Lifecycle state machine for strategies and plugins.

The only legal way to change `strategy.current_state` or `plugin.current_state`
is via `transition_strategy` / `transition_plugin`. Every call:

1. Refuses the transition if the source state is terminal.
2. Refuses if `(from_state, to_state)` is not in the edge table.
3. Runs a deterministic guard for that edge (no LLMs in guards — risk-conscious).
4. Materialises the entity's filesystem directory lazily.
5. Updates `current_state` and inserts a new `transition` audit row, committing
   both in one transaction.

Transition rows themselves are insert-only — append-only is a design constraint
(see project memory `feedback-no-hard-delete`). `current_state` is a
denormalisation of the latest transition row for fast list queries.
"""

from __future__ import annotations

import logging
import math
import operator
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.persistence.models import (
    PLUGIN_TERMINAL_STATES,
    STRATEGY_TERMINAL_STATES,
    EntityType,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
    Transition,
)

log = logging.getLogger(__name__)


class InvalidTransitionError(Exception):
    """Raised by `transition_strategy` / `transition_plugin` when the requested
    edge is illegal or its guard fails. The exception message is safe to surface
    in API responses — guards put the failing rule into the message."""


# Forward edges, excluding the universal `→ abandoned` edge (handled inline).
VALID_STRATEGY_TRANSITIONS: dict[StrategyState, frozenset[StrategyState]] = {
    StrategyState.PROPOSED: frozenset({StrategyState.BACKTESTED}),
    StrategyState.BACKTESTED: frozenset({StrategyState.PAPER_TRADING}),
    StrategyState.PAPER_TRADING: frozenset({StrategyState.LIVE_TRADING}),
    StrategyState.LIVE_TRADING: frozenset(),
    StrategyState.ABANDONED: frozenset(),
}

VALID_PLUGIN_TRANSITIONS: dict[PluginState, frozenset[PluginState]] = {
    PluginState.SPECIFIED: frozenset({PluginState.AUTHORED}),
    PluginState.AUTHORED: frozenset({PluginState.VERIFIED}),
    PluginState.VERIFIED: frozenset({PluginState.ADOPTED_IN_FWBG}),
    PluginState.ADOPTED_IN_FWBG: frozenset(),
    PluginState.ABANDONED: frozenset(),
}


# ---------------------------------------------------------------------------
# Filesystem layout (lazy)
# ---------------------------------------------------------------------------


def strategy_dir(slug: str) -> Path:
    """Return the filesystem directory for a strategy's artifacts."""
    return settings.data_dir / "strategies" / slug


def plugin_dir(slug: str) -> Path:
    """Return the filesystem directory for a plugin's artifacts."""
    return settings.data_dir / "plugins" / slug


# ---------------------------------------------------------------------------
# Criteria evaluator
# ---------------------------------------------------------------------------

# Order matters: longest operator prefix first so '>=' matches before '>'.
_OPERATORS: tuple[tuple[str, Callable[[float, float], bool]], ...] = (
    (">=", operator.ge),
    ("<=", operator.le),
    ("==", operator.eq),
    ("!=", operator.ne),
    (">", operator.gt),
    ("<", operator.lt),
)


def _eval_comparator(expr: str, value: float) -> bool:
    """Parse 'op rhs' from a YAML threshold string and apply it to `value`.

    Supports: '>=', '<=', '>', '<', '==', '!='. Whitespace tolerant.
    Raises ValueError on unparseable expressions — these point at corrupt
    criteria YAML and should fail loudly.
    """
    expr = expr.strip()
    for op_str, op_fn in _OPERATORS:
        if expr.startswith(op_str):
            rhs_text = expr[len(op_str) :].strip()
            try:
                rhs = float(rhs_text)
            except ValueError as exc:
                raise ValueError(f"non-numeric rhs in comparator: {expr!r}") from exc
            return op_fn(float(value), rhs)
    raise ValueError(f"unknown comparator expression: {expr!r}")


def _criteria_path(asset_class: str) -> Path:
    """Return the YAML criteria file path for a given asset class."""
    return settings.criteria_dir / f"{asset_class}.yaml"


def _metric_float(val: Any) -> float | None:
    """Coerce a metric value to a finite float, or None if it is not numeric
    (or is NaN/inf). Lets criteria evaluation reject a malformed metric cleanly
    instead of raising an unhandled ValueError on a bare `float(val)`."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def check_backtest_criteria(
    *, asset_class: str, metrics: Mapping[str, float]
) -> tuple[bool, list[str]]:
    """Evaluate `metrics` against `criteria/<asset_class>.yaml`.

    Sections honored:
      - `backtest_to_paper.required_all` — every rule must pass.
      - `backtest_to_paper.hard_blockers` — every rule must pass; failures
        are tagged so downstream code can distinguish a hard block from a
        soft criterion miss.
      - `backtest_to_paper.required_any` — at least one group must pass
        entirely.

    Fail-closed behaviour: if no YAML exists for the asset class, returns
    `(False, [...])`. The backtested → paper gate is money-adjacent and must
    not pass unconditionally on a fresh checkout — run POST /calibrate to seed
    criteria first.
    """
    path = _criteria_path(asset_class)
    if not path.is_file():
        return False, [
            f"no criteria defined for asset class {asset_class!r}; "
            "run POST /calibrate to seed criteria before promoting to paper"
        ]
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return False, [f"criteria YAML for {asset_class} is invalid: {exc}"]

    btp = data.get("backtest_to_paper", {}) or {}
    failures: list[str] = []

    for rule in btp.get("required_all", []) or []:
        for metric, expr in rule.items():
            if metric.startswith("_"):
                continue
            val = metrics.get(metric)
            if val is None:
                failures.append(f"missing metric: {metric}")
                continue
            fval = _metric_float(val)
            if fval is None:
                failures.append(f"{metric}={val!r} is not numeric")
                continue
            if not _eval_comparator(str(expr), fval):
                failures.append(f"{metric}={val} fails {expr}")

    for rule in btp.get("hard_blockers", []) or []:
        for metric, expr in rule.items():
            if metric.startswith("_"):
                continue
            val = metrics.get(metric)
            if val is None:
                failures.append(f"missing metric (hard): {metric}")
                continue
            fval = _metric_float(val)
            if fval is None:
                failures.append(f"hard_blocker: {metric}={val!r} is not numeric")
                continue
            if not _eval_comparator(str(expr), fval):
                failures.append(f"hard_blocker: {metric}={val} fails {expr}")

    any_groups = btp.get("required_any", []) or []
    if any_groups:
        any_ok = False
        for group in any_groups:
            group_ok = True
            for metric, expr in group.items():
                if metric.startswith("_"):
                    continue
                val = metrics.get(metric)
                fval = _metric_float(val)
                if fval is None or not _eval_comparator(str(expr), fval):
                    group_ok = False
                    break
            if group_ok:
                any_ok = True
                break
        if not any_ok:
            failures.append("required_any: no group passed")

    return not failures, failures


# ---------------------------------------------------------------------------
# Strategy guards
# ---------------------------------------------------------------------------


def _guard_strategy_proposed_to_backtested(_strategy: Strategy, _payload: dict[str, Any]) -> None:
    """M2: no precondition. The Runner (M3) chooses when to submit a backtest."""


def _guard_strategy_backtested_to_paper(strategy: Strategy, payload: dict[str, Any]) -> None:
    """Enforce backtest criteria before promoting a strategy to paper trading."""
    metrics = payload.get("backtest_metrics") or {}
    if not metrics:
        raise InvalidTransitionError(
            "backtested → paper_trading requires backtest_metrics in payload"
        )
    ok, failed = check_backtest_criteria(asset_class=strategy.asset_class, metrics=metrics)
    if not ok:
        raise InvalidTransitionError(f"criteria not met: {failed}")


def _guard_strategy_paper_to_live(_strategy: Strategy, payload: dict[str, Any]) -> None:
    """`requires_human_approval` is a design constraint. The dashboard click
    that produces this payload arrives in M7, but the state machine refuses
    auto-promotion from day one."""
    if not payload.get("human_approval"):
        raise InvalidTransitionError(
            "paper_trading → live_trading requires human_approval=True in payload"
        )


_STRATEGY_GUARDS: dict[
    tuple[StrategyState, StrategyState],
    Callable[[Strategy, dict[str, Any]], None],
] = {
    (StrategyState.PROPOSED, StrategyState.BACKTESTED): _guard_strategy_proposed_to_backtested,
    (StrategyState.BACKTESTED, StrategyState.PAPER_TRADING): _guard_strategy_backtested_to_paper,
    (StrategyState.PAPER_TRADING, StrategyState.LIVE_TRADING): _guard_strategy_paper_to_live,
}


def _guard_strategy_abandon(_strategy: Strategy, payload: dict[str, Any]) -> None:
    """Require a post-mortem path before a strategy can be abandoned."""
    if not payload.get("post_mortem_path"):
        raise InvalidTransitionError("abandon transition requires post_mortem_path in payload")


def _guard_plugin_abandon(_plugin: Plugin, payload: dict[str, Any]) -> None:
    """Require a post-mortem path before a plugin can be abandoned."""
    if not payload.get("post_mortem_path"):
        raise InvalidTransitionError("abandon transition requires post_mortem_path in payload")


# ---------------------------------------------------------------------------
# Transition entry points
# ---------------------------------------------------------------------------


async def transition_strategy(
    session: AsyncSession,
    strategy: Strategy,
    to_state: StrategyState,
    *,
    reason: str = "",
    payload: dict[str, Any] | None = None,
    created_by: str = "system",
) -> Transition:
    """Validate, mkdir, update state, append transition row — atomically."""
    payload = dict(payload or {})
    current = StrategyState(strategy.current_state)
    if current in STRATEGY_TERMINAL_STATES:
        raise InvalidTransitionError(
            f"{strategy.slug} is terminal ({current.value}); cannot transition"
        )

    if to_state == StrategyState.ABANDONED:
        _guard_strategy_abandon(strategy, payload)
        strategy.post_mortem_path = str(payload["post_mortem_path"])
    else:
        if to_state not in VALID_STRATEGY_TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"{strategy.slug}: {current.value} → {to_state.value} is not a valid edge"
            )
        guard = _STRATEGY_GUARDS.get((current, to_state))
        if guard is not None:
            guard(strategy, payload)

    strategy_dir(strategy.slug).mkdir(parents=True, exist_ok=True)
    if to_state == StrategyState.ABANDONED:
        Path(payload["post_mortem_path"]).parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    row = Transition(
        entity_type=EntityType.STRATEGY.value,
        entity_id=strategy.id,
        from_state=current.value,
        to_state=to_state.value,
        reason=reason,
        payload=payload,
        created_by=created_by,
        created_at=now,
    )
    strategy.current_state = to_state.value
    strategy.updated_at = now
    session.add(row)
    await session.commit()
    await session.refresh(strategy)
    await session.refresh(row)
    log.info(
        "transition strategy slug=%s %s → %s reason=%r",
        strategy.slug,
        current.value,
        to_state.value,
        reason,
    )
    return row


async def transition_plugin(
    session: AsyncSession,
    plugin: Plugin,
    to_state: PluginState,
    *,
    reason: str = "",
    payload: dict[str, Any] | None = None,
    created_by: str = "system",
) -> Transition:
    """Validate, mkdir, update state, append transition row — atomically."""
    payload = dict(payload or {})
    current = PluginState(plugin.current_state)
    if current in PLUGIN_TERMINAL_STATES:
        raise InvalidTransitionError(
            f"{plugin.slug} is terminal ({current.value}); cannot transition"
        )

    if to_state == PluginState.ABANDONED:
        _guard_plugin_abandon(plugin, payload)
        plugin.post_mortem_path = str(payload["post_mortem_path"])
    else:
        if to_state not in VALID_PLUGIN_TRANSITIONS[current]:
            raise InvalidTransitionError(
                f"{plugin.slug}: {current.value} → {to_state.value} is not a valid edge"
            )

    plugin_dir(plugin.slug).mkdir(parents=True, exist_ok=True)
    if to_state == PluginState.ABANDONED:
        Path(payload["post_mortem_path"]).parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    row = Transition(
        entity_type=EntityType.PLUGIN.value,
        entity_id=plugin.id,
        from_state=current.value,
        to_state=to_state.value,
        reason=reason,
        payload=payload,
        created_by=created_by,
        created_at=now,
    )
    plugin.current_state = to_state.value
    plugin.updated_at = now
    session.add(row)
    await session.commit()
    await session.refresh(plugin)
    await session.refresh(row)
    log.info(
        "transition plugin slug=%s %s → %s reason=%r",
        plugin.slug,
        current.value,
        to_state.value,
        reason,
    )
    return row
