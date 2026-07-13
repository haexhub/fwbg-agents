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
    2. Read iteration_001/strategy.json and ensure it exists in fwbg as
       <slug>__it001 via POST /api/strategies (fwbg's /runs/start expects a
       strategy file on its side). Normally the research flow has already
       published it — a 409 means it's there (possibly edited by the user in
       the dashboard) and is left untouched.
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
import calendar
import json
import logging
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir, transition_strategy
from fwbg_agents.orchestrator.metrics import (
    median_metrics_across_assets as _median_metrics_across_assets,
)
from fwbg_agents.orchestrator.trade_diagnostics import compute_trade_diagnostics
from fwbg_agents.orchestrator.universe import (
    UniverseAttempt,
    plan_universe_attempts,
    timeframes_by_symbol,
)
from fwbg_agents.persistence.agent_runs import (
    fail_agent_run,
    finish_agent_run,
    start_agent_run,
)
from fwbg_agents.persistence.models import (
    AgentRunStatus,
    Strategy,
    StrategyState,
)
from fwbg_agents.run_events import emit_run_event
from fwbg_agents.tools.fwbg_client import FwbgClientError, safe_fwbg_strategy_name

log = logging.getLogger(__name__)


class RunnerError(RuntimeError):
    """Raised when every universe attempt fails (fwbg errors / no results)."""


class RunnerConfigError(RunnerError):
    """fwbg completed the run but every asset errored on a fixable
    strategy-config problem (e.g. a plugin missing its pipeline dependency).

    Broadening the universe cannot fix such an error — the same pipeline fails
    on every symbol — so the Runner short-circuits and raises this instead of
    exhausting all rungs. It carries the parsed dependency (`dependent` needs
    `dependency` upstream) so the orchestrator can auto-repair by reiteration.
    """

    def __init__(
        self, message: str, *, dependent: str | None = None, dependency: str | None = None
    ):
        super().__init__(message)
        self.dependent = dependent
        self.dependency = dependency


# fwbg's pipeline builder rejects a plugin whose declared dependency is absent
# with a stable message; we parse it to drive the deterministic auto-repair.
_DEP_ERROR_RE = re.compile(
    r"Plugin '(?P<dependent>[^']+)' depends on '(?P<dependency>[^']+)', "
    r"but '(?P=dependency)' is not in the pipeline"
)


def _asset_error_symbols(run_data: dict[str, Any]) -> list[str]:
    """Symbols whose per-asset backtest fwbg marked as errored."""
    assets = run_data.get("assets") or {}
    return [sym for sym, a in assets.items() if isinstance(a, dict) and a.get("status") == "error"]


def _parse_dependency_error(messages: list[str]) -> tuple[str, str] | None:
    """Return (dependent, dependency) from the first missing-dependency error."""
    for m in messages:
        match = _DEP_ERROR_RE.search(m)
        if match:
            return match.group("dependent"), match.group("dependency")
    return None


class _FwbgClientProto(Protocol):
    async def create_strategy(self, name: str, data: dict[str, Any]) -> dict[str, Any]: ...
    async def start_run(
        self,
        strategy_name: str,
        *,
        asset_classes: list[str] | None = ...,
        assets: list[str] | None = ...,
        description: str | None = ...,
        start_date: str | None = ...,
        end_date: str | None = ...,
        cost_multiplier: float | None = ...,
    ) -> dict[str, Any]: ...
    async def list_runs(self) -> list[dict[str, Any]]: ...
    async def get_progress(self, run_id: str) -> dict[str, Any]: ...
    async def get_run(self, run_id: str) -> dict[str, Any]: ...
    async def get_run_logs(self, run_id: str, *, limit: int = ...) -> list[dict[str, Any]]: ...
    async def ensure_data(
        self,
        symbol: str,
        *,
        timeframe: str | None = ...,
        date_from: str | None = ...,
        date_to: str | None = ...,
    ) -> dict[str, Any]: ...
    async def get_ensure_status(self, task_id: str) -> dict[str, Any]: ...


class RunnerResult(BaseModel):
    """Result of a successful Runner backtest attempt."""

    fwbg_run_id: str
    results_path: str
    iteration_dir: str
    metrics: dict[str, float]
    universe: dict[str, Any] = {}


_TERMINAL_FWBG_STATUSES = frozenset({"completed", "failed", "error", "cancelled"})


def _months_ago_iso(months: int) -> str:
    """ISO date (YYYY-MM-DD) `months` calendar months before today.

    Used to end iteration backtests before the reserved holdout window so no
    iteration ever trains/tests on the most recent `holdout_months` of data.
    """
    today = date.today()
    year, month = today.year, today.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def _write_trade_diagnostics(iteration_dir: Path, job_id: str, run_data: dict[str, Any]) -> None:
    """Write a ``trade_diagnostics.md`` sidecar for the Analyst. Non-fatal.

    A backtest without diagnostics is still a valid backtest — any failure here
    (missing fwbg run dir, unreadable folds) is logged and swallowed.
    """
    try:
        symbols = list((run_data.get("assets") or {}).keys())
        run_dir = settings.fwbg_test_results_dir / job_id
        diagnostics = compute_trade_diagnostics(run_dir, symbols)
        (iteration_dir / "trade_diagnostics.md").write_text(diagnostics.render_markdown())
    except Exception:
        # Diagnostics must never fail an otherwise-valid backtest.
        log.warning("runner: trade diagnostics failed for %s; continuing", job_id, exc_info=True)


class Runner:
    """Deterministic agent that drives fwbg's backtest API."""

    def __init__(
        self,
        fwbg_client: _FwbgClientProto,
        session: AsyncSession,
    ):
        """Initialize."""
        self.fwbg = fwbg_client
        self.session = session

    async def run(self, strategy: Strategy) -> RunnerResult:
        """Execute backtests across universe attempts and return the first successful result."""
        ar = await start_agent_run(self.session, agent_name="runner", strategy_id=strategy.id)

        try:
            iteration_dir = strategy_dir(strategy.slug) / "iteration_001"
            src = iteration_dir / "strategy.json"
            if not src.is_file():
                raise FileNotFoundError(f"missing strategy.json at {src}")
            ar.input_artifact_path = str(src)

            # Ensure the strategy exists in fwbg (once for all attempts). The
            # research flow normally publishes it right after translation; a
            # 409 means it's already there — possibly hand-edited in the
            # dashboard — and MUST NOT be overwritten.
            fwbg_name = (strategy.metadata_json or {}).get(
                "fwbg_strategy_name"
            ) or safe_fwbg_strategy_name(strategy.slug, 1)
            try:
                created = await self.fwbg.create_strategy(fwbg_name, json.loads(src.read_text()))
                fwbg_name = created.get("filename", fwbg_name)
            except FwbgClientError as exc:
                if exc.status != 409:
                    raise
                log.info(
                    "runner: strategy %r already exists in fwbg; leaving it untouched",
                    fwbg_name,
                )

            attempts = plan_universe_attempts(strategy)
            tf_by_symbol = timeframes_by_symbol(strategy)
            last_reason = "no universe attempts produced results"

            for attempt in attempts:
                assets = await self._resolve_assets(attempt, tf_by_symbol)
                asset_classes = list(attempt.asset_classes) if attempt.asset_classes else None
                if assets is None and attempt.assets and asset_classes is None:
                    # Rung was symbol-only and no symbol had data — nothing to
                    # run, so broaden. (A rung that also names classes still runs
                    # on those, dropping only the dataless symbols.)
                    last_reason = f"no data for suggested symbols {list(attempt.assets)}"
                    log.info("runner: %s; falling back", last_reason)
                    continue

                log.info(
                    "runner: attempt %r for %s (assets=%s, asset_classes=%s)",
                    attempt.label,
                    strategy.slug,
                    assets,
                    asset_classes,
                )
                try:
                    job_id, run_data = await self.execute_backtest(
                        fwbg_name,
                        assets=assets,
                        asset_classes=asset_classes,
                        agent_run_id=ar.id,
                        # Reserve the most recent `holdout_months` as an unseen
                        # holdout — the promote gate validates on it later.
                        end_date=_months_ago_iso(settings.holdout_months),
                    )
                except RunnerError as exc:
                    last_reason = f"attempt {attempt.label!r}: {exc}"
                    log.info("runner: %s; falling back", last_reason)
                    continue

                metrics = _median_metrics_across_assets(run_data)
                if not metrics:
                    errored = _asset_error_symbols(run_data)
                    if errored:
                        # fwbg completed the run but every asset errored — pull
                        # the real reason from the run logs instead of reporting
                        # an opaque "no metrics".
                        messages = await self._fetch_error_messages(job_id)
                        detail = "; ".join(messages) or "assets errored (no log detail)"
                        emit_run_event(
                            agent_run_id=ar.id,
                            type="backtest_error",
                            fwbg_run_id=job_id,
                            errored_assets=errored,
                            detail=detail,
                        )
                        dep = _parse_dependency_error(messages)
                        if dep is not None:
                            # A missing pipeline dependency fails identically on
                            # every symbol — broadening the universe is futile.
                            raise RunnerConfigError(
                                f"fwbg backtest for {strategy.slug!r} failed: {detail}",
                                dependent=dep[0],
                                dependency=dep[1],
                            )
                        last_reason = f"attempt {attempt.label!r}: {detail}"
                    else:
                        last_reason = f"attempt {attempt.label!r} completed but produced no metrics"
                    log.info("runner: %s; falling back", last_reason)
                    continue

                # Success — persist and transition.
                results_path = iteration_dir / "fwbg_results.json"
                results_path.write_text(json.dumps(run_data, indent=2, sort_keys=True))
                _write_trade_diagnostics(iteration_dir, job_id, run_data)
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

                emit_run_event(
                    agent_run_id=ar.id,
                    type="backtest_done",
                    fwbg_run_id=job_id,
                    universe=attempt.label,
                    metrics=metrics,
                )
                await finish_agent_run(
                    self.session,
                    ar,
                    status=AgentRunStatus.DONE,
                    output_artifact_path=str(results_path),
                )

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
            await fail_agent_run(
                self.session, ar, exc, transient=isinstance(exc, httpx.TransportError)
            )
            raise

    async def _fetch_error_messages(self, job_id: str) -> list[str]:
        """Distinct error-level messages from a run's fwbg logs (best-effort)."""
        try:
            logs = await self.fwbg.get_run_logs(job_id)
        except (httpx.TransportError, FwbgClientError) as exc:
            log.info("runner: could not fetch logs for %s: %s", job_id, exc)
            return []
        seen: list[str] = []
        for entry in logs:
            if not isinstance(entry, dict) or entry.get("level") != "error":
                continue
            msg = entry.get("message")
            if isinstance(msg, str) and msg and msg not in seen:
                seen.append(msg)
        return seen

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

    async def _acquire_run(
        self,
        fwbg_name: str,
        *,
        assets: list[str] | None,
        asset_classes: list[str] | None,
        start_date: str | None = None,
        end_date: str | None = None,
        cost_multiplier: float | None = None,
    ) -> str:
        """Get a job_id for this strategy: adopt an already-active fwbg run
        of the same strategy, or start a new one — waiting while fwbg's
        single backtest slot is taken.

        Adopting closes the duplicate-run hole: a /runs/start whose response
        is lost in a transport blip still starts the run on the fwbg side;
        the retry must attach to it instead of launching a second copy.
        fwbg enforces one concurrent run (FWBG_MAX_CONCURRENT_RUNS=1) and
        answers 429 while busy — the intended behaviour then is to wait for
        the slot, not to burn a universe attempt.
        """
        deadline = time.monotonic() + settings.runner_poll_timeout_seconds
        while True:
            try:
                active = [
                    r
                    for r in await self.fwbg.list_runs()
                    if r.get("status") == "running" and r.get("strategy_name") == fwbg_name
                ]
            except (httpx.TransportError, FwbgClientError):
                active = []
            if active:
                job_id = active[0].get("run_id") or active[0].get("job_id")
                log.info(
                    "runner: adopting already-active fwbg job %s for %s",
                    job_id,
                    fwbg_name,
                )
                return job_id  # type: ignore[return-value]  # adopted runs always carry run_id (list_runs contract)

            try:
                start = await self.fwbg.start_run(
                    fwbg_name,
                    assets=assets,
                    asset_classes=asset_classes,
                    start_date=start_date,
                    end_date=end_date,
                    cost_multiplier=cost_multiplier,
                )
            except FwbgClientError as exc:
                if exc.status != 429:
                    raise
                if time.monotonic() >= deadline:
                    raise RunnerError(
                        f"fwbg backtest slot stayed busy for "
                        f"{settings.runner_poll_timeout_seconds:.0f}s"
                    ) from exc
                log.info("runner: fwbg busy (429), waiting for the slot")
                await asyncio.sleep(settings.runner_busy_wait_seconds)
                continue
            job_id = start["job_id"]
            log.info("runner: started fwbg job %s", job_id)
            return job_id

    async def execute_backtest(
        self,
        fwbg_name: str,
        *,
        assets: list[str] | None,
        asset_classes: list[str] | None,
        agent_run_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        cost_multiplier: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Start one fwbg run and poll it to completion. Raises RunnerError on
        a failed/errored run or a polling timeout."""
        job_id = await self._acquire_run(
            fwbg_name,
            assets=assets,
            asset_classes=asset_classes,
            start_date=start_date,
            end_date=end_date,
            cost_multiplier=cost_multiplier,
        )
        # Anchor event for the dashboard's link to /runs/<fwbg_run_id>.
        emit_run_event(
            agent_run_id,
            "backtest_submitted",
            fwbg_run_id=job_id,
            assets=assets,
            asset_classes=asset_classes,
        )

        deadline = time.monotonic() + settings.runner_poll_timeout_seconds
        status = "running"
        emitted_status: str | None = None
        last_progress: dict[str, Any] = {}
        # Transient outage tolerance: fwbg-api can be briefly unreachable
        # while the backtest keeps running on its side (watchtower recreating
        # the container, dropped keep-alive connections under load). A poll
        # failure must not abort the run — only a sustained outage may.
        outage_deadline: float | None = None
        while time.monotonic() < deadline:
            try:
                last_progress = await self.fwbg.get_progress(job_id)
            except (httpx.TransportError, FwbgClientError) as exc:
                now = time.monotonic()
                if outage_deadline is None:
                    outage_deadline = now + settings.runner_poll_outage_tolerance_seconds
                if now >= outage_deadline:
                    raise RunnerError(
                        f"fwbg unreachable for "
                        f"{settings.runner_poll_outage_tolerance_seconds:.0f}s "
                        f"while polling {job_id}: {exc}"
                    ) from exc
                log.warning("runner: poll for %s failed (%s), tolerating", job_id, exc)
                await asyncio.sleep(settings.runner_poll_interval_seconds)
                continue
            outage_deadline = None
            status = last_progress.get("status", "running")
            # Emit only on status transition — polling runs every few seconds
            # for hours; a per-poll event would flood the timeline.
            if status != emitted_status:
                emit_run_event(
                    agent_run_id,
                    "backtest_progress",
                    fwbg_run_id=job_id,
                    status=status,
                )
                emitted_status = status
            if status in _TERMINAL_FWBG_STATUSES:
                break
            await asyncio.sleep(settings.runner_poll_interval_seconds)
        else:
            raise RunnerError(f"polling timeout for {job_id} (last status={status!r})")

        if status != "completed":
            msg = last_progress.get("message") or last_progress.get("error_message") or status
            raise RunnerError(f"fwbg reported status={status!r}: {msg}")

        return job_id, await self.fwbg.get_run(job_id)
