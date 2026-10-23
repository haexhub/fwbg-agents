"""Promote gate: holdout + cost-stress backtests (Plan 009 WP4).

Before a BACKTESTED strategy may advance to paper trading it must clear two
extra sequential backtests (one fwbg run at a time — no fan-out):

1. **Holdout** — a register holdout look of the same universe (Plan 028
   D6): fwbg develops on data before the research card's cutoff (``B``, the
   lineage's frozen boundary, for an auto-created card) and
   evaluates the frozen setting once on ``[cutoff, today)``, data no
   iteration of this lineage ever saw. Catches in-sample overfitting from the
   iteration chain that the in-sample metrics cannot. A further look on the
   same card carries a reason (D3); the register refuses it otherwise.
2. **Cost stress** — a development run at 2x spread/slippage (fwbg clamps it
   to the card's cutoff). Catches an edge that only survives unrealistically
   low transaction costs.
3. **Deflated Sharpe Ratio** (Plan 010 WP2) — the holdout look's per-trade
   Sharpe must beat the expected max Sharpe of N zero-skill trials
   (N = every backtest the factory ever ran, see
   ``orchestrator/trials.py``) with probability ≥ ``settings.dsr_min``.
   Catches selection bias: the more the loop searches, the higher the bar.
   Without a readable holdout trade series it fails (D9). The look itself is
   not a selection trial and stays out of that census (D10).

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
import math
import statistics
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.analyst import _median_metrics_across_assets
from fwbg_agents.agents.runner import Runner, RunnerError
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import check_criteria_section, strategy_dir
from fwbg_agents.orchestrator.lineage_boundary import lineage_root
from fwbg_agents.orchestrator.research_card import ensure_research_id
from fwbg_agents.orchestrator.trials import (
    count_trials,
    deflated_sharpe_ratio,
    holdout_pnl_series,
    record_trial_stat,
    series_moments,
)
from fwbg_agents.persistence.agent_runs import (
    fail_agent_run,
    finish_agent_run,
    start_agent_run,
)
from fwbg_agents.persistence.models import AgentRunStatus, Strategy
from fwbg_agents.run_events import emit_run_event
from fwbg_agents.tools.fwbg_client import FwbgClientError, safe_fwbg_strategy_name

log = logging.getLogger(__name__)

COST_STRESS_MULTIPLIER = 2.0
HOLDOUT_NOTE = "promote gate holdout look"
COST_STRESS_NOTE = f"promote gate cost stress x{COST_STRESS_MULTIPLIER:g}"


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


async def _holdout_reason(fwbg_client, research_id: str, slug: str) -> str | None:
    """The reason a further holdout look needs, or None for the card's first look.

    Any earlier look counts, a failed one too (fwbg's gate rule). Raises
    FwbgClientError or httpx.TransportError when the card cannot be read and
    ValueError when it carries no trial list.
    """
    # ponytail: a look still pending in fwbg (started, not yet recorded) is not
    # among the card's trials, so no reason is sent and fwbg refuses it as
    # holdout_repeat — a failed gate run. Upgrade path: fwbg lists the kind of
    # the card's pending runs.
    card = await fwbg_client.get_research_card(research_id)
    trials = card.get("trials") if isinstance(card, dict) else None
    if not isinstance(trials, list):
        raise ValueError(f"research card {research_id} carries no trial list")
    if any(isinstance(trial, dict) and trial.get("kind") == "holdout" for trial in trials):
        return f"promote gate: further holdout look ({slug})"
    return None


def _holdout_median_metrics(run_data: dict[str, Any]) -> dict[str, float]:
    """Cross-asset median of a holdout look, where every symbol counts.

    A symbol of the look without holdout metrics (its development found no
    edge or failed, or it made no trades after the cutoff) has empty
    ``unified_metrics`` in fwbg's ``get_run`` and counts with a sharpe of 0.0:
    it showed no edge after the cutoff, and leaving it out would let the
    symbols that worked carry the median. Its other metrics stay left out.
    """
    assets = {
        sym: asset
        if asset.get("unified_metrics")
        else {**asset, "unified_metrics": {"sharpe": 0.0}}
        for sym, asset in (run_data.get("assets") or {}).items()
        if isinstance(asset, dict)
    }
    return _median_metrics_across_assets({"assets": assets})


def _failed_gate_run(agent_run_id: int, label: str, exc: Exception) -> GateRun:
    """Turn a gate exception into a failed result while preserving its run ID."""
    emit_run_event(agent_run_id, "promote_gate_failed", label=label, reason=str(exc))
    return GateRun(
        label=label,
        passed=False,
        metrics={},
        failures=[str(exc)],
        error=str(exc),
        fwbg_run_id=getattr(exc, "fwbg_run_id", None),
    )


async def _run_dsr_check(
    session: AsyncSession, holdout_job_id: str
) -> tuple[bool, float | None, int | None, list[str]]:
    """Deflated-Sharpe check on the holdout look's trade-P&L series.

    Returns (passed, dsr, n_trials, failures). Unreadable holdout trades, a look
    without trades and a series too short or flat for the moments each fail
    closed, as do non-finite inputs. The visible cold-start branch passes with
    fewer than two historical Sharpes.
    """
    raw_pnls = holdout_pnl_series(settings.fwbg_test_results_dir / holdout_job_id)
    if raw_pnls is None:
        return (
            False,
            None,
            None,
            [
                f"dsr: holdout trades of run {holdout_job_id} unreadable or not from an "
                "evaluated holdout look; failing closed"
            ],
        )
    if not raw_pnls:
        return (
            False,
            None,
            None,
            [f"dsr: holdout look {holdout_job_id} made no trades; failing closed"],
        )
    if any(not math.isfinite(value) for value in raw_pnls):
        counts = await count_trials(session)
        return False, None, counts.global_trials, ["dsr is NaN — non-finite inputs; failing closed"]
    moments = series_moments(raw_pnls)
    if moments is None:
        return (
            False,
            None,
            None,
            ["dsr: holdout trade series too short or flat to judge; failing closed"],
        )
    sr, skew, kurtosis = moments

    counts = await count_trials(session)
    if len(counts.trade_sharpes) < 2:
        return (
            True,
            None,
            counts.global_trials,
            ["dsr skipped: <2 historical trial sharpes (pass-open)"],
        )
    sr_variance = statistics.variance(counts.trade_sharpes)
    if not math.isfinite(sr_variance):
        return False, None, counts.global_trials, ["dsr variance is non-finite; failing closed"]

    dsr = deflated_sharpe_ratio(
        sr,
        sr_variance_across_trials=sr_variance,
        n_trials=max(counts.global_trials, 1),
        n_obs=len(raw_pnls),
        skew=skew,
        kurtosis=kurtosis,
    )
    if math.isnan(dsr):
        return False, None, counts.global_trials, ["dsr is NaN — non-finite inputs; failing closed"]
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

    Never raises for a backtest failure — a run that errors or that the
    research register refuses, and an unreadable research card, count as a
    gate failure (fail-closed: an unvalidatable strategy must not promote).
    A lineage without a research card is a configuration error, not a
    backtest failure: ``ResearchCardError`` propagates, the AgentRun fails
    and the lineage's attempt budget is left untouched.
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

        research_id = await ensure_research_id(session, strategy, fwbg_client)
        runner = Runner(fwbg_client, session)
        fwbg_name = _fwbg_name(strategy)
        assets = _universe_assets(strategy) or None

        # No window on either run: the card's cutoff splits development from
        # the holdout look, and fwbg clamps the cost-stress run to it.
        specs: list[tuple[str, str, dict[str, Any]]] = [
            ("holdout", "promote_holdout", {"holdout": True, "note": HOLDOUT_NOTE}),
            # ponytail: a gate retry on the very same strategy repeats this exact
            # configuration; fwbg refuses it as duplicate_fingerprint (the result
            # would be identical anyway), a failed gate run that uses an attempt.
            # Upgrade path: reuse the earlier cost-stress result for the same
            # fingerprint instead of rerunning it.
            (
                "cost_stress",
                "promote_cost_stress",
                {"cost_multiplier": COST_STRESS_MULTIPLIER, "note": COST_STRESS_NOTE},
            ),
        ]
        runs: list[GateRun] = []
        holdout_job_id: str | None = None
        for label, section, kwargs in specs:
            emit_run_event(ar.id, "promote_gate_submitted", label=label)
            if label == "holdout":
                try:
                    reason = await _holdout_reason(fwbg_client, research_id, strategy.slug)
                except (FwbgClientError, httpx.TransportError, ValueError) as exc:
                    runs.append(_failed_gate_run(ar.id, label, exc))
                    continue
                kwargs = {**kwargs, "reason": reason}
            try:
                job_id, run_data = await runner.execute_backtest(
                    fwbg_name,
                    assets=assets,
                    asset_classes=None,
                    agent_run_id=ar.id,
                    research_id=research_id,
                    **kwargs,
                )
            except RunnerError as exc:
                runs.append(_failed_gate_run(ar.id, label, exc))
                continue
            if label == "holdout":
                # A look evaluates one frozen setting and selects nothing, so it
                # stays out of the trial census (D10), as in fwbg's register.
                holdout_job_id = job_id
            else:
                await record_trial_stat(
                    session,
                    run_id=job_id,
                    strategy=strategy,
                    run_data=run_data,
                    run_dir=settings.fwbg_test_results_dir / job_id,
                )
            metrics = (
                _holdout_median_metrics(run_data)
                if label == "holdout"
                else _median_metrics_across_assets(run_data)
            )
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

        dsr: float | None = None
        n_trials: int | None = None
        if holdout_job_id:
            # Without a look there are no trades to judge; the failed holdout
            # run already fails the gate, so no DSR row pretends to have passed.
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
