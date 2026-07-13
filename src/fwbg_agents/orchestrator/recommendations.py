"""Hard-rule validation of Analyst recommendations.

Every Analyst output (Promote / Abandon / TuneParams / ChangeExit) passes
through `validate_and_apply` before any state change happens. The LLM is
not trusted to bypass guards:

- Promote     → criteria YAML re-check (re-use M2's `check_backtest_criteria`).
                Pass → transition backtested → paper_trading with metrics +
                recommendation in payload. Fail → InvalidTransitionError raised.
- Abandon     → write post_mortem.yaml; transition → abandoned.
- TuneParams  → no transition. The recommendation is persisted as a sidecar
  ChangeExit    JSON for the Translator (M4) to pick up and re-iterate.

This module is risk-conscious by design (see feedback memory): a Promote
recommendation cannot promote a strategy that fails the gates, no matter
how confident the LLM is.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.analyst import (
    Abandon,
    AddIndicator,
    AnalystRecommendation,
    ChangeExit,
    ModifyPlugins,
    Promote,
    TuneParams,
)
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import (
    strategy_dir,
    transition_strategy,
)
from fwbg_agents.orchestrator.lineage import generation_depth
from fwbg_agents.orchestrator.promote_gate import run_promote_gate
from fwbg_agents.persistence.models import (
    Strategy,
    StrategyState,
    Transition,
)


class RecommendationRejectedError(ValueError):
    """Raised when an iteration recommendation violates a hard universe rule
    (Plan 009 WP3). Callers treat it like any other rejected recommendation."""


log = logging.getLogger(__name__)


def _rec_to_dict(rec: AnalystRecommendation) -> dict[str, Any]:
    """Serialize an AnalystRecommendation to a JSON-compatible dict."""
    return rec.model_dump(mode="json")  # type: ignore[union-attr]


def _backtested_universe(strategy: Strategy) -> tuple[list[str], set[str]]:
    """(all backtested symbols, errored symbols) from the last fwbg_results.json.

    An errored symbol is one fwbg produced no usable metrics for — it may be
    dropped even during the phase-1 funnel.
    """
    path = strategy_dir(strategy.slug) / "iteration_001" / "fwbg_results.json"
    if not path.is_file():
        return [], set()
    try:
        assets = (json.loads(path.read_text()).get("assets") or {}) or {}
    except (OSError, json.JSONDecodeError):
        return [], set()
    all_syms = list(assets.keys())
    errored = {
        sym
        for sym, data in assets.items()
        if data.get("status") == "error" or not (data.get("unified_metrics") or {})
    }
    return all_syms, errored


async def _enforce_universe_rules(
    session: AsyncSession, strategy: Strategy, rec: AnalystRecommendation
) -> None:
    """Deterministic phase-funnel guards for `target_assets` (Plan 009 WP3).

    Raises `RecommendationRejectedError` when the requested narrowing is not
    allowed. An empty `target_assets` (keep the parent universe) always passes.
    """
    targets = list(getattr(rec, "target_assets", []) or [])
    if not targets:
        return

    universe, errored = _backtested_universe(strategy)
    target_set = set(targets)

    # Rule 3: may only narrow within the universe that was actually backtested.
    if universe and not target_set.issubset(set(universe)):
        raise RecommendationRejectedError(
            f"target_assets {sorted(target_set - set(universe))} are not in the "
            f"backtested universe {sorted(universe)}"
        )

    # Rule 1: no narrowing before the phase boundary — except dropping assets
    # fwbg could not evaluate (errored).
    depth = await generation_depth(session, strategy)
    if depth < settings.universe_narrowing_min_iteration:
        dropped = set(universe) - target_set
        non_errored_dropped = dropped - errored
        if non_errored_dropped:
            raise RecommendationRejectedError(
                f"universe narrowing is not allowed before iteration "
                f"{settings.universe_narrowing_min_iteration} (current generation "
                f"{depth}); assets {sorted(non_errored_dropped)} did not error and "
                "must be kept while the whole universe is still being optimized"
            )

    # Rule 2: never narrow below the floor, unless the edge is asset-specific.
    asset_specific = bool((strategy.metadata_json or {}).get("asset_specific"))
    if not asset_specific and len(target_set) < settings.universe_min_size:
        raise RecommendationRejectedError(
            f"target_assets narrows to {len(target_set)} asset(s), below "
            f"universe_min_size={settings.universe_min_size} (mark the hypothesis "
            "asset_specific if the edge is bound to one instrument)"
        )


async def validate_and_apply(
    session: AsyncSession,
    strategy: Strategy,
    rec: AnalystRecommendation,
    *,
    metrics: dict[str, float],
    fwbg_client: Any = None,
) -> Transition | None:
    """Apply a recommendation against hard rules.

    Returns the new transition row when a state change happened, or None when
    the recommendation only resulted in a sidecar artifact (tune_params,
    change_exit) or a promote that did not clear the gate. Raises
    `InvalidTransitionError` if a promote recommendation fails the criteria gate.

    A `promote` additionally runs the holdout + cost-stress promote gate (Plan
    009 WP4) when `fwbg_client` is supplied; a failed gate keeps the strategy
    BACKTESTED (the median criteria gate in `transition_strategy` still applies
    on top).
    """
    if isinstance(rec, Promote):
        log.info("validate_and_apply: promote %s", strategy.slug)
        if fwbg_client is not None:
            gate = await run_promote_gate(session, strategy, fwbg_client=fwbg_client)
            if not gate.passed:
                log.info(
                    "validate_and_apply: promote gate failed for %s "
                    "(fail_count=%d) — staying BACKTESTED",
                    strategy.slug,
                    gate.fail_count,
                )
                return None
        else:
            log.warning(
                "validate_and_apply: promote for %s without fwbg_client — "
                "skipping holdout/cost-stress gate",
                strategy.slug,
            )
        return await transition_strategy(
            session,
            strategy,
            StrategyState.PAPER_TRADING,
            reason="analyst: promote",
            payload={
                "backtest_metrics": metrics,
                "recommendation": _rec_to_dict(rec),
            },
            created_by="analyst",
        )

    if isinstance(rec, Abandon):
        log.info("validate_and_apply: abandon %s", strategy.slug)
        pm_path = strategy_dir(strategy.slug) / "post_mortem.yaml"
        pm_path.parent.mkdir(parents=True, exist_ok=True)
        pm_path.write_text(
            yaml.safe_dump(
                {
                    "slug": strategy.slug,
                    "asset_class": strategy.asset_class,
                    "strategy_family": strategy.strategy_family,
                    "summary": rec.post_mortem_summary,
                    "lessons": rec.lessons,
                    "confidence": rec.confidence,
                    "reasoning": rec.reasoning,
                    "metrics_at_abandon": metrics,
                    "written_at": datetime.now(UTC).isoformat(),
                },
                sort_keys=False,
            )
        )
        return await transition_strategy(
            session,
            strategy,
            StrategyState.ABANDONED,
            reason="analyst: abandon",
            payload={
                "post_mortem_path": str(pm_path),
                "recommendation": _rec_to_dict(rec),
            },
            created_by="analyst",
        )

    # TuneParams / ChangeExit / ModifyPlugins — record-only. M4 Translator re-iterates.
    if isinstance(rec, (TuneParams, ChangeExit, ModifyPlugins)):
        # Phase-funnel guards: reject an illegal universe narrowing before it is
        # recorded for the Translator (Plan 009 WP3).
        await _enforce_universe_rules(session, strategy, rec)
        iteration_dir = strategy_dir(strategy.slug) / "iteration_001"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        sidecar = iteration_dir / "analyst_recommendation.json"
        sidecar.write_text(json.dumps(_rec_to_dict(rec), indent=2))
        log.info("validate_and_apply: record-only rec for %s (%s)", strategy.slug, rec.kind)
        return None

    # AddIndicator — record-only. M5b PluginAuthor picks up the sidecar and
    # writes a fresh plugin. No state change here; the strategy stays in
    # BACKTESTED until the plugin verifies and a new iteration can be authored.
    if isinstance(rec, AddIndicator):
        iteration_dir = strategy_dir(strategy.slug) / "iteration_001"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        sidecar = iteration_dir / "add_indicator_request.json"
        sidecar.write_text(
            json.dumps(
                {
                    **_rec_to_dict(rec),
                    "strategy_id": strategy.id,
                    "strategy_slug": strategy.slug,
                    "requested_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
        )
        log.info(
            "validate_and_apply: add_indicator request for %s (capability=%r, category=%s)",
            strategy.slug,
            rec.capability,
            rec.category,
        )
        return None

    raise ValueError(f"unknown recommendation kind: {rec!r}")
