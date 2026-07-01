"""Runner agent — drives fwbg's backtest API (adaptive, Phase 2).

Deterministic, no LLM. The Runner is the critical path: a hallucination here
would mean backtests don't start or results are misread, so it deliberately
contains no model-driven logic. The Analyst (M3) handles interpretation.

Strategy-first: the Researcher recommends *where* to test an edge
(`suggested_universe`). The Runner tries that recommendation first and, if it
yields no usable result, broadens the universe (see orchestrator/universe.py).
Before a backtest it asks fwbg to ensure OHLCV data exists for the concrete
symbols, triggering an on-demand download when the cache is cold.

Flow:
    1. AgentRun inserted (status=running, agent_name="runner").
    2. Read iteration_001/strategy.json, copy into fwbg's strategies_dir as
       <slug>__it001.json (fwbg's /runs/start expects a file on disk).
    3. For each universe attempt (most-specific first):
         a. ensure data for the attempt's symbols (drop the unavailable ones);
         b. fwbg_client.start_run(name, assets=..., asset_classes=...) → job_id;
         c. poll get_progress() until terminal / timeout;
         d. on completion, fetch the run and extract metrics.
       The first attempt that produces non-empty metrics wins; others fall
       through to the next rung.
    4. On success: write fwbg_results.json, transition to BACKTESTED (payload
       records the winning universe), mark AgentRun done.
    5. If every attempt is exhausted: mark AgentRun failed; strategy stays
       PROPOSED.

Metrics extraction: fwbg returns one unified_metrics dict per symbol. M3 picks
the symbol with the highest sharpe — single-symbol aggregation. Multi-symbol
aggregation can come later when actual multi-symbol strategies show up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir, transition_strategy
from fwbg_agents.orchestrator.universe import (
    UniverseAttempt,
    plan_universe_attempts,
    timeframes_by_symbol,
)
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)
from fwbg_agents.tools.fwbg_client import FwbgClientError

log = logging.getLogger(__name__)


class RunnerError(RuntimeError):
    """Raised when every universe attempt fails (fwbg errors / no results)."""


class _FwbgClientProto(Protocol):
    async def start_run(self, strategy_name: str, **kwargs: Any) -> dict[str, Any]: ...
    async def get_progress(self, run_id: str) -> dict[str, Any]: ...
    async def get_run(self, run_id: str) -> dict[str, Any]: ...
    async def ensure_data(self, symbol: str, **kwargs: Any) -> dict[str, Any]: ...
    async def get_ensure_status(self, task_id: str) -> dict[str, Any]: ...


class RunnerResult(BaseModel):
    fwbg_run_id: str
    results_path: str
    iteration_dir: str
    metrics: dict[str, float]
    universe: dict[str, Any] = {}


_TERMINAL_FWBG_STATUSES = frozenset({"completed", "failed", "error", "cancelled"})


def _safe_fwbg_strategy_name(slug: str, iteration: int) -> str:
    """fwbg validates names against [\\w\\-]; keep ASCII + drop punctuation."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", slug)
    return f"{cleaned}__it{iteration:03d}"


def _best_symbol_metrics(run: dict[str, Any]) -> dict[str, float]:
    """Pick the symbol with the highest sharpe; return its unified_metrics."""
    assets = run.get("assets") or {}
    if not assets:
        return {}
    best: tuple[float, dict[str, float]] = (float("-inf"), {})
    for sym in assets.values():
        m = sym.get("unified_metrics") or {}
        sh = m.get("sharpe")
        if sh is None:
            continue
        try:
            shv = float(sh)
        except (TypeError, ValueError):
            continue
        if shv > best[0]:
            best = (shv, m)
    return {k: float(v) for k, v in best[1].items() if isinstance(v, (int, float))}


class Runner:
    def __init__(
        self,
        fwbg_client: _FwbgClientProto,
        session: AsyncSession,
    ):
        self.fwbg = fwbg_client
        self.session = session

    async def run(self, strategy: Strategy) -> RunnerResult:
        now = datetime.now(UTC)
        ar = AgentRun(
            agent_name="runner",
            status=AgentRunStatus.RUNNING.value,
            strategy_id=strategy.id,
            started_at=now,
            created_at=now,
        )
        self.session.add(ar)
        await self.session.commit()
        await self.session.refresh(ar)

        try:
            iteration_dir = strategy_dir(strategy.slug) / "iteration_001"
            src = iteration_dir / "strategy.json"
            if not src.is_file():
                raise FileNotFoundError(f"missing strategy.json at {src}")
            ar.input_artifact_path = str(src)

            # Copy strategy.json into fwbg's strategies_dir (once for all attempts).
            settings.fwbg_strategies_dir.mkdir(parents=True, exist_ok=True)
            fwbg_name = _safe_fwbg_strategy_name(strategy.slug, 1)
            fwbg_file = settings.fwbg_strategies_dir / f"{fwbg_name}.json"
            shutil.copyfile(src, fwbg_file)

            attempts = plan_universe_attempts(strategy)
            tf_by_symbol = timeframes_by_symbol(strategy)
            last_reason = "no universe attempts produced results"

            for attempt in attempts:
                assets = await self._resolve_assets(attempt, tf_by_symbol)
                if assets is None and attempt.assets:
                    # Attempt was symbol-only and no symbol had data — broaden.
                    last_reason = f"no data for suggested symbols {list(attempt.assets)}"
                    log.info("runner: %s; falling back", last_reason)
                    continue

                asset_classes = list(attempt.asset_classes) if attempt.asset_classes else None
                log.info(
                    "runner: attempt %r for %s (assets=%s, asset_classes=%s)",
                    attempt.label, strategy.slug, assets, asset_classes,
                )
                try:
                    job_id, run_data = await self._execute_backtest(
                        fwbg_name, assets=assets, asset_classes=asset_classes
                    )
                except RunnerError as exc:
                    last_reason = f"attempt {attempt.label!r}: {exc}"
                    log.info("runner: %s; falling back", last_reason)
                    continue

                metrics = _best_symbol_metrics(run_data)
                if not metrics:
                    last_reason = f"attempt {attempt.label!r} completed but produced no metrics"
                    log.info("runner: %s; falling back", last_reason)
                    continue

                # Success — persist and transition.
                results_path = iteration_dir / "fwbg_results.json"
                results_path.write_text(json.dumps(run_data, indent=2, sort_keys=True))
                universe = {
                    "label": attempt.label,
                    "assets": assets,
                    "asset_classes": asset_classes,
                }
                await transition_strategy(
                    self.session,
                    strategy,
                    StrategyState.BACKTESTED,
                    reason=f"runner: fwbg backtest completed (universe={attempt.label})",
                    payload={
                        "fwbg_run_id": job_id,
                        "results_path": str(results_path),
                        "backtest_metrics": metrics,
                        "universe": universe,
                    },
                    created_by=f"runner#{ar.id}",
                )

                ar.status = AgentRunStatus.DONE.value
                ar.ended_at = datetime.now(UTC)
                ar.output_artifact_path = str(results_path)
                await self.session.commit()

                return RunnerResult(
                    fwbg_run_id=job_id,
                    results_path=str(results_path),
                    iteration_dir=str(iteration_dir),
                    metrics=metrics,
                    universe=universe,
                )

            raise RunnerError(
                f"all {len(attempts)} universe attempt(s) exhausted; last: {last_reason}"
            )
        except Exception as exc:
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await self.session.commit()
            raise

    async def _resolve_assets(
        self, attempt: UniverseAttempt, tf_by_symbol: dict[str, str]
    ) -> list[str] | None:
        """Ensure data for the attempt's symbols; return the available subset.

        Returns None when the attempt has no symbols (class-scope / unconstrained
        — nothing to ensure) *and* when it had symbols but none have data (the
        caller then decides whether a class fallback is still runnable).
        """
        if not attempt.assets:
            return None
        ready: list[str] = []
        for symbol in attempt.assets:
            tf = tf_by_symbol.get(symbol) or settings.default_timeframe
            if await self._ensure_data_ready(symbol, tf):
                ready.append(symbol)
            else:
                log.info("runner: no data for %s (%s), dropping from universe", symbol, tf)
        return ready or None

    async def _ensure_data_ready(self, symbol: str, timeframe: str) -> bool:
        """Best-effort: ask fwbg to provision data for `symbol`, wait for a
        cold download to finish. False if the symbol has no obtainable data."""
        try:
            resp = await self.fwbg.ensure_data(symbol, timeframe=timeframe)
        except FwbgClientError as exc:
            log.info("runner: ensure_data(%s) unavailable: %s", symbol, exc)
            return False

        status = resp.get("status")
        if status == "ready":
            return True
        if status != "downloading":
            return False

        task_id = resp.get("task_id")
        if not task_id:
            return False
        deadline = time.monotonic() + settings.data_ensure_timeout_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(settings.data_ensure_poll_interval_seconds)
            try:
                st = await self.fwbg.get_ensure_status(task_id)
            except FwbgClientError as exc:
                log.info("runner: ensure status(%s) failed: %s", task_id, exc)
                return False
            s = st.get("status")
            if s == "ready":
                return True
            if s == "error":
                log.info("runner: download failed for %s: %s", symbol, st.get("error"))
                return False
        log.info("runner: data ensure timed out for %s", symbol)
        return False

    async def _execute_backtest(
        self,
        fwbg_name: str,
        *,
        assets: list[str] | None,
        asset_classes: list[str] | None,
    ) -> tuple[str, dict[str, Any]]:
        """Start one fwbg run and poll it to completion. Raises RunnerError on
        a failed/errored run or a polling timeout."""
        start = await self.fwbg.start_run(
            fwbg_name, assets=assets, asset_classes=asset_classes
        )
        job_id = start["job_id"]
        log.info("runner: started fwbg job %s", job_id)

        deadline = time.monotonic() + settings.runner_poll_timeout_seconds
        status = "running"
        last_progress: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_progress = await self.fwbg.get_progress(job_id)
            status = last_progress.get("status", "running")
            if status in _TERMINAL_FWBG_STATUSES:
                break
            await asyncio.sleep(settings.runner_poll_interval_seconds)
        else:
            raise RunnerError(f"polling timeout for {job_id} (last status={status!r})")

        if status != "completed":
            msg = last_progress.get("message") or last_progress.get("error_message") or status
            raise RunnerError(f"fwbg reported status={status!r}: {msg}")

        return job_id, await self.fwbg.get_run(job_id)
