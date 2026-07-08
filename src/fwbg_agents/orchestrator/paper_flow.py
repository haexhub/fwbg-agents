"""Paper-flow orchestrator (M6b Task 5).

`paper_analyze` loads on-disk paper-trading telemetry, runs PaperAnalyst,
persists the recommendation as a JSON sidecar under the same
`strategy_dir(slug)` as M3, and flags `Strategy.metadata_json` so the
dashboard can surface promote/abandon recommendations.

Notes:
  - This function NEVER transitions state. The paper→live edge requires
    explicit `human_approval=True` payload (see `lifecycle.py`); only the
    dashboard click (M7) can flip it.
  - `metadata_json` is reassigned, not mutated in place — SQLAlchemy
    JSON change tracking on a mutable dict is fragile; reassignment is
    the documented-safe path.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.paper_analyst import (
    AbandonPaper,
    PaperAnalyst,
    PromotePaperToLive,
)
from fwbg_agents.config import settings as default_settings
from fwbg_agents.orchestrator.criteria_paper import (
    evaluate_paper_criteria,
    load_paper_criteria,
)
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.agent_runs import fail_agent_run
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)
from fwbg_agents.tools.fwbg_paper_reader import (
    read_paper_positions,
    read_paper_summary,
)

log = logging.getLogger(__name__)


class PaperFlowError(Exception):
    """Raised when paper_analyze cannot run (wrong state, missing data)."""


async def paper_analyze(
    strategy_id: int,
    session: AsyncSession,
    *,
    settings=None,
    analyst=None,
    existing_ar: AgentRun | None = None,
) -> AgentRun:
    """Run PaperAnalyst against on-disk telemetry, persist sidecar + flag.

    Returns the completed `AgentRun` row. Raises `PaperFlowError` for
    pre-flight failures (strategy not found, wrong state, no telemetry).
    Re-raises any exception from analyst.analyze_sync after marking the
    AgentRun row as FAILED.

    `existing_ar`: when supplied (M6b API endpoint path), reuse this row
    instead of creating a new one — the endpoint pre-creates a PENDING row
    so the HTTP client can poll immediately. Standalone callers (smoke
    scripts, tests) leave it None to get the original create-internally
    behaviour.

    Sidecar-orphan note: if the success commit fails mid-flight after the
    sidecar write but before the DB update lands, the on-disk sidecar will
    exist while its AgentRun row stays RUNNING (then gets marked FAILED by
    the except block). Consumers reading sidecars by
    AgentRun.output_artifact_path should tolerate finding a sidecar whose
    AgentRun.status is not DONE.
    """
    settings = settings if settings is not None else default_settings
    analyst = analyst if analyst is not None else PaperAnalyst()

    strategy = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if strategy is None:
        raise PaperFlowError(f"strategy {strategy_id} not found")
    if strategy.current_state != StrategyState.PAPER_TRADING.value:
        raise PaperFlowError(
            f"strategy {strategy_id} not in PAPER_TRADING "
            f"(state={strategy.current_state})"
        )

    # Let FileNotFoundError from missing paper-criteria YAML propagate —
    # callers can treat it as "open gate" if they want.
    criteria = load_paper_criteria(strategy.asset_class)

    fwbg_data_dir = Path(settings.fwbg_data_dir)
    summary = read_paper_summary(strategy.slug, fwbg_data_dir)
    if summary is None:
        raise PaperFlowError(
            f"no on-disk paper-trading data for slug={strategy.slug!r}"
        )
    positions = read_paper_positions(strategy.slug, fwbg_data_dir)

    now = datetime.now(UTC)
    if existing_ar is not None:
        ar = existing_ar
        ar.status = AgentRunStatus.RUNNING.value
        ar.started_at = now
        await session.commit()
        await session.refresh(ar)
    else:
        ar = AgentRun(
            agent_name="paper_analyst",
            status=AgentRunStatus.RUNNING.value,
            strategy_id=strategy.id,
            started_at=now,
            created_at=now,
        )
        session.add(ar)
        await session.commit()
        await session.refresh(ar)

    try:
        eval_res = evaluate_paper_criteria(summary, criteria)
        out = analyst.analyze_sync(
            summary=summary,
            positions=positions,
            paper_criteria=criteria,
            paper_phase_target_days=strategy.paper_phase_target_days,
            paper_criteria_eval=eval_res,
            strategy_slug=strategy.slug,
        )

        sidecar_dir = strategy_dir(strategy.slug)
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = sidecar_dir / f"paper_analyst_{ar.id}.json"
        sidecar_path.write_text(json.dumps(out.model_dump(), indent=2))

        # Reassign — don't mutate in place. SQLA JSON change-tracking on
        # a mutable dict is fragile; a fresh dict is the safe path.
        if isinstance(out, PromotePaperToLive):
            meta = dict(strategy.metadata_json or {})
            meta["paper_analyst_promote_recommended"] = True
            strategy.metadata_json = meta
        elif isinstance(out, AbandonPaper):
            meta = dict(strategy.metadata_json or {})
            meta["paper_analyst_abandon_recommended"] = True
            meta["paper_analyst_post_mortem_path"] = out.post_mortem_path
            strategy.metadata_json = meta
        # ContinueObservation: no metadata write — sidecar only.

        ar.status = AgentRunStatus.DONE.value
        ar.output_artifact_path = str(sidecar_path)
        ar.ended_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(ar)
        log.info(
            "paper_analyze: slug=%s decision=%s sidecar=%s",
            strategy.slug,
            type(out).__name__,
            sidecar_path,
        )
        return ar
    except Exception as exc:
        log.exception("paper_analyze: slug=%s FAILED", strategy.slug)
        await fail_agent_run(session, ar, exc)
        raise
