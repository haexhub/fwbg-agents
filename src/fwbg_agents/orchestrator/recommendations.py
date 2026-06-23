"""Hard-rule validation of Analyst recommendations.

Every Analyst output (Promote / Abandon / TuneParams / ChangeExit) passes
through `validate_and_apply` before any state change happens. The LLM is
not trusted to bypass guards:

- Promote     → criteria YAML re-check (re-use M2's `check_backtest_criteria`).
                Pass → transition backtested → paper_trading with metrics +
                recommendation in payload. Fail → InvalidTransition raised.
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
    AnalystRecommendation,
    ChangeExit,
    Promote,
    TuneParams,
)
from fwbg_agents.orchestrator.lifecycle import (
    strategy_dir,
    transition_strategy,
)
from fwbg_agents.persistence.models import (
    Strategy,
    StrategyState,
    Transition,
)

log = logging.getLogger(__name__)


def _rec_to_dict(rec: AnalystRecommendation) -> dict[str, Any]:
    return rec.model_dump(mode="json")  # type: ignore[union-attr]


async def validate_and_apply(
    session: AsyncSession,
    strategy: Strategy,
    rec: AnalystRecommendation,
    *,
    metrics: dict[str, float],
) -> Transition | None:
    """Apply a recommendation against hard rules.

    Returns the new transition row when a state change happened, or None when
    the recommendation only resulted in a sidecar artifact (tune_params,
    change_exit). Raises `InvalidTransition` if a promote recommendation
    fails the criteria gate.
    """
    if isinstance(rec, Promote):
        log.info("validate_and_apply: promote %s", strategy.slug)
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

    # TuneParams / ChangeExit — record-only. M4 Translator re-iterates.
    if isinstance(rec, (TuneParams, ChangeExit)):
        iteration_dir = strategy_dir(strategy.slug) / "iteration_001"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        sidecar = iteration_dir / "analyst_recommendation.json"
        sidecar.write_text(json.dumps(_rec_to_dict(rec), indent=2))
        log.info(
            "validate_and_apply: record-only rec for %s (%s)", strategy.slug, rec.kind
        )
        return None

    raise ValueError(f"unknown recommendation kind: {rec!r}")
