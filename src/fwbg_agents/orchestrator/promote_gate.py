"""Promote gate: holdout + cost-stress backtests (Plan 009 WP4).

Before a BACKTESTED strategy may advance to paper trading it must clear two
extra sequential backtests (one fwbg run at a time — no fan-out):

1. **Holdout** — the same universe on ``[B, today)``, where ``B`` is the
   lineage's frozen data boundary (see ``orchestrator/lineage_boundary.py``) —
   a window no iteration of this lineage ever saw (iteration backtests end at
   ``B``, frozen once for the whole family). Catches in-sample overfitting
   from the iteration chain that the in-sample metrics cannot.
2. **Cost stress** — the full window at 2x spread/slippage. Catches an edge
   that only survives unrealistically low transaction costs.
3. **Deflated Sharpe Ratio** (Plan 010 WP2) — the holdout run's per-trade
   Sharpe must beat the expected max Sharpe of N zero-skill trials
   (N = every backtest the factory ever ran, see
   ``orchestrator/trials.py``) with probability ≥ ``settings.dsr_min``.
   Catches selection bias: the more the loop searches, the higher the bar.

Both are checked against deliberately milder thresholds than the main
``backtest_to_paper`` gate (they run on shorter / harder windows), stored in the
criteria YAML as ``promote_holdout`` / ``promote_cost_stress`` sections.

Gate attempts are budgeted per lineage (``settings.promote_max_attempts``):
the cumulative ``fail_count`` lives at the lineage root's sidecar, shared by
every child strategy, and once the budget is exhausted further Promote
recommendations fail the gate without running a backtest.

On failure the strategy stays BACKTESTED and the result is persisted as a
``promote_gate_results.json`` sidecar at the lineage root's directory (with
the cumulative ``fail_count``) so the next Analyst pass can see it and decide
iterate vs. abandon.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.analyst import _median_metrics_across_assets
from fwbg_agents.agents.runner import Runner, RunnerError
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import check_criteria_section, strategy_dir
from fwbg_agents.orchestrator.lineage_boundary import get_or_freeze_boundary, lineage_root
from fwbg_agents.orchestrator.trials import (
    count_trials,
    deflated_sharpe_ratio,
    pnl_series,
    series_moments,
)
from fwbg_agents.persistence.agent_runs import (
    fail_agent_run,
    finish_agent_run,
    start_agent_run,
)
from fwbg_agents.persistence.models import AgentRunStatus, Strategy
from fwbg_agents.run_events import emit_run_event
from fwbg_agents.tools.fwbg_client import safe_fwbg_strategy_name

log = logging.getLogger(__name__)

COST_STRESS_MULTIPLIER = 2.0


class GateRun(BaseModel):
    """Outcome of a single gate backtest."""

    label: str
    passed: bool
    metrics: dict
    failures: list[str]
    fwbg_run_id: str | None = None
    error: str | None = None


class PromoteGateResult(BaseModel):
    """Combined result of the holdout + cost-stress + DSR checks."""

    passed: bool
    runs: list[GateRun]
    fail_count: int  # cumulative promote-gate failures for this strategy
    dsr: float | None = None  # Deflated Sharpe Ratio of the holdout run
    n_trials: int | None = None  # search breadth the DSR deflated against


def _fwbg_name(strategy: Strategy) -> str:
    return (strategy.metadata_json or {}).get("fwbg_strategy_name") or safe_fwbg_strategy_name(
        strategy.slug, 1
    )


def _universe_assets(strategy: Strategy) -> list[str]:
    """Assets backtested in the last iteration (from its fwbg_results.json)."""
    path = strategy_dir(strategy.slug) / "iteration_001" / "fwbg_results.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return list((data.get("assets") or {}).keys())


async def _run_dsr_check(
    session: AsyncSession, holdout_job_id: str | None
) -> tuple[bool, float | None, int | None, list[str]]:
    """Deflated-Sharpe check on the holdout run's trade-P&L series.

    Returns (passed, dsr, n_trials, failures). Passes trivially (no data to
    judge) when the holdout run produced no readable trades or fewer than 2
    historical trial-Sharpes exist to estimate cross-trial variance from —
    an unproven DSR must never block a strategy that otherwise cleared
    holdout + cost-stress; it can only ever add a stricter check once there
    is enough history to compute one.
    """
    if not holdout_job_id:
        return True, None, None, []
    pnls = pnl_series(settings.fwbg_test_results_dir / holdout_job_id)
    moments = series_moments(pnls)
    if moments is None:
        return True, None, None, []
    sr, skew, kurtosis = moments

    counts = await count_trials(session)
    if len(counts.trade_sharpes) < 2:
        return True, None, counts.global_trials, []
    sr_variance = statistics.variance(counts.trade_sharpes)

    dsr = deflated_sharpe_ratio(
        sr,
        sr_variance_across_trials=sr_variance,
        n_trials=max(counts.global_trials, 1),
        n_obs=len(pnls),
        skew=skew,
        kurtosis=kurtosis,
    )
    if dsr < settings.dsr_min:
        return (
            False,
            dsr,
            counts.global_trials,
            [f"dsr={dsr:.3f} < dsr_min={settings.dsr_min} (n_trials={counts.global_trials})"],
        )
    return True, dsr, counts.global_trials, []


def _gate_sidecar_path(root_slug: str) -> Path:
    """Sidecar path for the lineage-shared gate result — lives at the root."""
    return strategy_dir(root_slug) / "promote_gate_results.json"


def _fail_count(root: Strategy) -> int:
    """Cumulative promote-gate failures for `root`'s whole lineage."""
    sidecar = _gate_sidecar_path(root.slug)
    if not sidecar.is_file():
        return 0
    try:
        return int(json.loads(sidecar.read_text()).get("fail_count", 0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0


async def run_promote_gate(
    session: AsyncSession, strategy: Strategy, *, fwbg_client
) -> PromoteGateResult:
    """Run the holdout + cost-stress backtests and evaluate the gate.

    Never raises for a backtest failure — a run that errors counts as a gate
    failure (fail-closed: an unvalidatable strategy must not promote).
    """
    ar = await start_agent_run(session, agent_name="promote_gate", strategy_id=strategy.id)
    try:
        root = await lineage_root(session, strategy)
        fail_count_so_far = _fail_count(root)
        if fail_count_so_far >= settings.promote_max_attempts:
            emit_run_event(ar.id, "promote_gate_failed", reason="attempt budget exhausted")
            result = PromoteGateResult(
                passed=False, runs=[], fail_count=fail_count_so_far, dsr=None, n_trials=None
            )
            await finish_agent_run(session, ar, status=AgentRunStatus.DONE)
            return result

        runner = Runner(fwbg_client, session)
        fwbg_name = _fwbg_name(strategy)
        assets = _universe_assets(strategy) or None
        today = date.today().isoformat()
        holdout_start = await get_or_freeze_boundary(session, strategy)

        specs: list[tuple[str, str, dict[str, Any]]] = [
            ("holdout", "promote_holdout", {"start_date": holdout_start, "end_date": today}),
            ("cost_stress", "promote_cost_stress", {"cost_multiplier": COST_STRESS_MULTIPLIER}),
        ]
        runs: list[GateRun] = []
        holdout_job_id: str | None = None
        for label, section, kwargs in specs:
            emit_run_event(ar.id, "promote_gate_submitted", label=label)
            try:
                job_id, run_data = await runner.execute_backtest(
                    fwbg_name,
                    assets=assets,
                    asset_classes=None,
                    agent_run_id=ar.id,
                    **kwargs,
                )
            except RunnerError as exc:
                runs.append(
                    GateRun(
                        label=label, passed=False, metrics={}, failures=[str(exc)], error=str(exc)
                    )
                )
                emit_run_event(ar.id, "promote_gate_failed", label=label, reason=str(exc))
                continue
            if label == "holdout":
                holdout_job_id = job_id
            metrics = _median_metrics_across_assets(run_data)
            ok, failures = check_criteria_section(
                asset_class=strategy.asset_class,  # type: ignore[arg-type]  # set for any BACKTESTED strategy
                metrics=metrics,
                section=section,
            )
            runs.append(
                GateRun(
                    label=label, passed=ok, metrics=metrics, failures=failures, fwbg_run_id=job_id
                )
            )
            emit_run_event(
                ar.id,
                "promote_gate_done" if ok else "promote_gate_failed",
                label=label,
                passed=ok,
            )

        emit_run_event(ar.id, "promote_gate_submitted", label="dsr")
        dsr_ok, dsr, n_trials, dsr_failures = await _run_dsr_check(session, holdout_job_id)
        runs.append(
            GateRun(
                label="dsr",
                passed=dsr_ok,
                metrics={"dsr": dsr} if dsr is not None else {},
                failures=dsr_failures,
                fwbg_run_id=holdout_job_id,
            )
        )
        emit_run_event(
            ar.id,
            "promote_gate_done" if dsr_ok else "promote_gate_failed",
            label="dsr",
            passed=dsr_ok,
        )

        passed = bool(runs) and all(r.passed for r in runs)
        fail_count = fail_count_so_far + (0 if passed else 1)
        result = PromoteGateResult(
            passed=passed, runs=runs, fail_count=fail_count, dsr=dsr, n_trials=n_trials
        )

        sidecar = _gate_sidecar_path(root.slug)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(result.model_dump_json(indent=2))
        await finish_agent_run(
            session, ar, status=AgentRunStatus.DONE, output_artifact_path=str(sidecar)
        )
        return result
    except Exception as exc:
        await fail_agent_run(session, ar, exc)
        raise
