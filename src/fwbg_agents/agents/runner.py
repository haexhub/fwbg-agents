"""Runner agent — drives fwbg's backtest API.

Deterministic, no LLM. The Runner is the critical path: a hallucination here
would mean backtests don't start or results are misread, so it deliberately
contains no model-driven logic. The Analyst (M3) handles interpretation.

Flow:
    1. AgentRun inserted (status=running, agent_name="runner").
    2. Read iteration_001/strategy.json from disk.
    3. Copy that file into fwbg's strategies_dir as <slug>__it001.json
       (fwbg's POST /runs/start expects a name pointing at a file on disk).
    4. fwbg_client.start_run(name) → job_id.
    5. Poll get_progress() until status in {completed, failed} or timeout.
    6. On completion: fetch full run, write fwbg_results.json into iteration_001/,
       transition_strategy(s, BACKTESTED, payload=...), mark AgentRun done.
    7. On failure/timeout: mark AgentRun failed; strategy stays in PROPOSED.

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
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)

log = logging.getLogger(__name__)


class RunnerError(RuntimeError):
    """Raised when fwbg reports a failed run or polling times out."""


class _FwbgClientProto(Protocol):
    async def start_run(self, strategy_name: str, **kwargs: Any) -> dict[str, Any]: ...
    async def get_progress(self, run_id: str) -> dict[str, Any]: ...
    async def get_run(self, run_id: str) -> dict[str, Any]: ...


class RunnerResult(BaseModel):
    fwbg_run_id: str
    results_path: str
    iteration_dir: str
    metrics: dict[str, float]


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

            # Copy strategy.json into fwbg's strategies_dir.
            settings.fwbg_strategies_dir.mkdir(parents=True, exist_ok=True)
            fwbg_name = _safe_fwbg_strategy_name(strategy.slug, 1)
            fwbg_file = settings.fwbg_strategies_dir / f"{fwbg_name}.json"
            shutil.copyfile(src, fwbg_file)

            start = await self.fwbg.start_run(fwbg_name)
            job_id = start["job_id"]
            log.info("runner: started fwbg job %s for %s", job_id, strategy.slug)

            # Poll.
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

            # Fetch full run + persist.
            run_data = await self.fwbg.get_run(job_id)
            results_path = iteration_dir / "fwbg_results.json"
            results_path.write_text(json.dumps(run_data, indent=2, sort_keys=True))
            metrics = _best_symbol_metrics(run_data)

            await transition_strategy(
                self.session,
                strategy,
                StrategyState.BACKTESTED,
                reason="runner: fwbg backtest completed",
                payload={
                    "fwbg_run_id": job_id,
                    "results_path": str(results_path),
                    "backtest_metrics": metrics,
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
            )
        except Exception as exc:
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await self.session.commit()
            raise
