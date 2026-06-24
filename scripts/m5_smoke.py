"""M5 smoke: drive PluginAuthor + PluginEvaluator end-to-end via the API.

Single-process ASGI-transport. Pre-seeds a parent strategy in BACKTESTED with
an `add_indicator_request.json` sidecar (mirroring the M5a Analyst output),
then exercises:
    POST /strategies/{id}/author-plugin   →  AUTHORED  on disk + DB
    POST /plugins/{id}/evaluate           →  VERIFIED  on disk + DB
    GET  /plugins/{id}/verification-runs  →  one row, status=passed

The author uses a FunctionModel stub instead of a real LLM — the smoke covers
HTTP wiring + the deterministic verification path. A "live" variant against
haex-claude-proxy would be an M5b stretch we deliberately do NOT add here.

Prereq: `uv run alembic upgrade head`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from fwbg_agents.api import plugins as plugins_api
from fwbg_agents.config import settings
from fwbg_agents.main import app
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import (
    AgentRun,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
    VerificationRun,
)

DEADLINE_S = 60.0
POLL_INTERVAL_S = 0.5
SMOKE_STRATEGY_SLUG = "smoke_m5_parent"
SMOKE_PLUGIN_SLUG = "smoke-m5-ma"

_PLUGIN_CODE = (
    "import pandas as pd\n"
    "\n"
    "def compute(df: pd.DataFrame, *, window: int = 14) -> pd.Series:\n"
    "    return df['close'].rolling(window, min_periods=1).mean()\n"
)
_CONTRACT = {
    "name": SMOKE_PLUGIN_SLUG,
    "kind": "indicator",
    "version": "v1",
    "inputs": [{"name": "ohlcv", "dtype": "ohlcv", "required": True, "description": ""}],
    "outputs": [{"name": "ma", "dtype": "series", "length_invariant": "same_as_input"}],
    "params": [{"name": "window", "dtype": "int", "default": 14, "min": 2, "max": 200, "description": ""}],
    "invariants": ["outputs[0].length == inputs[0].length"],
    "test_scenarios": [
        {"name": "trending_up", "data_path": "test_scenarios/trending_up.parquet"},
        {"name": "sideways", "data_path": "test_scenarios/sideways.parquet"},
    ],
}
_SPEC_MD = (
    f"# {SMOKE_PLUGIN_SLUG}\n\n"
    "Rolling-mean close-price smoother used by the M5b smoke. Window is the only "
    "parameter; output series length matches input.\n"
)


def _stub_author_model() -> FunctionModel:
    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result",
                    {
                        "slug": SMOKE_PLUGIN_SLUG,
                        "python_code": _PLUGIN_CODE,
                        "contract": _CONTRACT,
                        "spec_md": _SPEC_MD,
                    },
                )
            ]
        )

    return FunctionModel(handler)


def _patch_author_to_use_stub() -> None:
    """Monkey-patch the background helper so the smoke does not hit a real LLM."""
    from fwbg_agents.orchestrator import plugin_flow as pf
    from fwbg_agents.agents import plugin_author as pa

    async def fake_author_from_strategy(session, strategy_id: int, *, model=None) -> int:
        strategy = (
            await session.execute(select(Strategy).where(Strategy.id == strategy_id))
        ).scalar_one()
        sidecar = pf._find_latest_sidecar(strategy.slug)
        assert sidecar is not None, "smoke fixture must have written the sidecar"
        author = pa.PluginAuthor(session, model=_stub_author_model())
        return await author.run_fresh(sidecar_path=sidecar, parent_strategy=strategy)

    plugins_api.author_plugin_from_strategy = fake_author_from_strategy
    pf.author_plugin_from_strategy = fake_author_from_strategy


async def _wait_for_run(agent_run_id: int, deadline_s: float = DEADLINE_S) -> AgentRun:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        async with SessionLocal() as session:
            ar = (
                await session.execute(
                    select(AgentRun).where(AgentRun.id == agent_run_id)
                )
            ).scalar_one()
            if ar.status in {"done", "failed"}:
                return ar
        await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"agent_run {agent_run_id} did not finish in {deadline_s}s")


async def _seed_parent_strategy() -> int:
    """Insert a BACKTESTED parent + write the add_indicator_request sidecar."""
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(Strategy).where(Strategy.slug == SMOKE_STRATEGY_SLUG)
            )
        ).scalar_one_or_none()
        if existing is not None:
            strategy_id = existing.id
            existing.current_state = StrategyState.BACKTESTED.value
            existing.updated_at = now
            await session.commit()
        else:
            s = Strategy(
                slug=SMOKE_STRATEGY_SLUG,
                current_state=StrategyState.BACKTESTED.value,
                iteration_count=0,
                asset_class="FOREX",
                strategy_family="ORB",
                created_at=now,
                updated_at=now,
            )
            session.add(s)
            await session.commit()
            await session.refresh(s)
            strategy_id = s.id

    it_dir = strategy_dir(SMOKE_STRATEGY_SLUG) / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(
        json.dumps({"name": SMOKE_STRATEGY_SLUG, "pipeline": "orb_pipeline"}, indent=2)
    )
    (it_dir / "add_indicator_request.json").write_text(
        json.dumps(
            {
                "kind": "add_indicator",
                "confidence": 0.7,
                "reasoning": "smoke synthetic",
                "phase": "indicators",
                "capability": "rolling close-price mean",
                "category": "indicator",
                "strategy_id": strategy_id,
                "strategy_slug": SMOKE_STRATEGY_SLUG,
                "requested_at": now.isoformat(),
            }
        )
    )
    return strategy_id


async def _cleanup_previous_run() -> None:
    """Stage-0 idempotency: auto-clean Strategy artefacts so re-runs work.

    Plugin DB rows are intentionally NOT auto-removed — they are traceable
    artifacts of a prior smoke run. `main()` checks separately and aborts
    with a helpful message if a prior plugin row exists.

    Removes (best-effort, no-op when absent):
      - DB Strategy row with SMOKE_STRATEGY_SLUG
      - data/strategies/<strategy_slug>/
      - data/plugins/<plugin_slug>/ (file-system cleanup is fine; the DB row
        is what's preserved for traceability)
    """
    async with SessionLocal() as session:
        prior_strategy = (
            await session.execute(
                select(Strategy).where(Strategy.slug == SMOKE_STRATEGY_SLUG)
            )
        ).scalar_one_or_none()
        if prior_strategy is not None:
            await session.delete(prior_strategy)
        await session.commit()

    p_dir = settings.data_dir / "plugins" / SMOKE_PLUGIN_SLUG
    if p_dir.exists():
        shutil.rmtree(p_dir)
    s_dir = settings.data_dir / "strategies" / SMOKE_STRATEGY_SLUG
    if s_dir.exists():
        shutil.rmtree(s_dir)


async def main() -> int:
    print(f"[m5_smoke] data_dir={settings.data_dir}")
    await _cleanup_previous_run()

    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(Plugin).where(Plugin.slug == SMOKE_PLUGIN_SLUG)
            )
        ).scalar_one_or_none()
        if existing is not None:
            print(
                f"       ⚠ existing plugin {SMOKE_PLUGIN_SLUG!r} (id={existing.id}) — "
                "the smoke cannot proceed without a clean slug; "
                "manually delete the plugin row or pick a different slug."
            )
            return 1

    _patch_author_to_use_stub()

    print("[1/4] seeding parent strategy + add_indicator_request.json sidecar")
    strategy_id = await _seed_parent_strategy()
    print(f"       → strategy_id={strategy_id} slug={SMOKE_STRATEGY_SLUG}")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        print("[2/4] POST /strategies/{id}/author-plugin")
        r = await client.post(f"/strategies/{strategy_id}/author-plugin")
        if r.status_code != 202:
            print(f"       ✗ unexpected status {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        ar_id = r.json()["agent_run_id"]
        ar = await _wait_for_run(ar_id)
        if ar.status != "done":
            print(f"       ✗ author run failed: {ar.error}", file=sys.stderr)
            return 1
        plugin_id = ar.plugin_id
        print(f"       ✓ plugin_id={plugin_id}")

        print("[3/4] POST /plugins/{id}/evaluate")
        r = await client.post(f"/plugins/{plugin_id}/evaluate")
        if r.status_code != 202:
            print(f"       ✗ unexpected status {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        eval_ar_id = r.json()["agent_run_id"]
        eval_ar = await _wait_for_run(eval_ar_id)
        if eval_ar.status != "done":
            print(f"       ✗ evaluate run failed: {eval_ar.error}", file=sys.stderr)
            return 1
        print("       ✓ evaluate completed")

        print("[4/4] verifying final state")
        r = await client.get(f"/plugins/{plugin_id}/verification-runs")
        runs = r.json()["verification_runs"]
        if len(runs) != 1 or runs[0]["status"] != "passed":
            print(f"       ✗ unexpected verification runs: {runs}", file=sys.stderr)
            return 1

    async with SessionLocal() as session:
        plugin = (
            await session.execute(select(Plugin).where(Plugin.id == plugin_id))
        ).scalar_one()
        vrs = (
            await session.execute(
                select(VerificationRun).where(VerificationRun.plugin_id == plugin_id)
            )
        ).scalars().all()

    if plugin.current_state != PluginState.VERIFIED.value:
        print(
            f"       ✗ plugin not VERIFIED: state={plugin.current_state}",
            file=sys.stderr,
        )
        return 1

    p_dir = settings.data_dir / "plugins" / SMOKE_PLUGIN_SLUG / "v1"
    must_exist = ["plugin.py", "contract.yaml", "spec.md"]
    for name in must_exist:
        if not (p_dir / name).is_file():
            print(f"       ✗ missing {name} in {p_dir}", file=sys.stderr)
            return 1
    parquets = list((p_dir / "test_scenarios").glob("*.parquet"))
    if len(parquets) < 1:
        print(f"       ✗ no parquet scenarios in {p_dir / 'test_scenarios'}", file=sys.stderr)
        return 1
    if (p_dir / "error_log.json").exists():
        print(f"       ✗ unexpected error_log.json from a passed run", file=sys.stderr)
        return 1

    print(
        f"       ✓ plugin={plugin.slug} state={plugin.current_state} "
        f"verification_runs={len(vrs)} parquets={len(parquets)}"
    )
    print("[m5_smoke] PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
