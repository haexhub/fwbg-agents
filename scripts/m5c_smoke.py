"""M5c smoke: drive the full plugin-author → evaluator → reiterate chain via the API.

Single-process ASGI-transport. Builds on the M5b smoke: pre-seeds a parent
strategy in BACKTESTED with an `add_indicator_request.json` sidecar, then:

    POST /strategies/{id}/author-plugin           →  AUTHORED  plugin
    POST /plugins/{id}/evaluate                   →  VERIFIED  plugin
    POST /strategies/{id}/reiterate-with-plugin   →  child Strategy PROPOSED
                                                     with the plugin slug
                                                     spliced into the right
                                                     list-field

The author uses a FunctionModel stub instead of a real LLM — the smoke covers
HTTP wiring + the deterministic splice path. A "live" variant would be an M6
stretch we deliberately do NOT add here.

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
)

DEADLINE_S = 60.0
POLL_INTERVAL_S = 0.5
SMOKE_STRATEGY_SLUG = "smoke_m5c_parent"
SMOKE_PLUGIN_SLUG = "smoke-m5c-rsi"

_PLUGIN_CODE = (
    "import pandas as pd\n"
    "\n"
    "def compute(df: pd.DataFrame, *, period: int = 14) -> pd.Series:\n"
    "    delta = df['close'].diff()\n"
    "    gain = delta.clip(lower=0).rolling(period, min_periods=1).mean()\n"
    "    loss = (-delta.clip(upper=0)).rolling(period, min_periods=1).mean()\n"
    "    rs = gain / loss.replace(0, 1e-12)\n"
    "    return 100 - (100 / (1 + rs))\n"
)
_CONTRACT = {
    "name": SMOKE_PLUGIN_SLUG,
    "kind": "indicator",
    "version": "v1",
    "inputs": [{"name": "ohlcv", "dtype": "ohlcv", "required": True, "description": ""}],
    "outputs": [{"name": "rsi", "dtype": "series", "length_invariant": "same_as_input"}],
    "params": [{"name": "period", "dtype": "int", "default": 14, "min": 2, "max": 200, "description": ""}],
    "invariants": ["outputs[0].length == inputs[0].length"],
    "test_scenarios": [
        {"name": "trending_up", "data_path": "test_scenarios/trending_up.parquet"},
        {"name": "sideways", "data_path": "test_scenarios/sideways.parquet"},
    ],
}
_SPEC_MD = (
    f"# {SMOKE_PLUGIN_SLUG}\n\n"
    "14-period RSI momentum filter used by the M5c smoke. Output series length "
    "matches input.\n"
)

_CAPABILITY = "14-period RSI for momentum confirmation"

# A fully-valid strategy.json the Translator can deep-copy + validate.
_PARENT_STRATEGY_JSON: dict = {
    "name": SMOKE_STRATEGY_SLUG,
    "description": "ORB rule-based on EURUSD M15 — smoke fixture",
    "hypothesis": "Opening range breakouts on EURUSD M15.",
    "expected_outcome": "sharpe > 1.0",
    "datasource": "forexsb",
    "pipeline": "orb_simple_v1",
    "model": "signal_orb_v1",
    "filters": "orb_scalping_v1",
    "validation": "walk_forward_intraday_v1",
    "resources": "standard_v1",
    "timeframe": "MINUTE_15",
    "exit_strategies": [
        {
            "name": "orb_based",
            "params": {"sl_mult": 1.0, "tp_mult": 5.0, "atr_period": 14},
            "ct": [0.5],
        },
    ],
    "tags": ["orb", "intraday", "forex_majors"],
    "optimization": {"grid_params": {"sl_mult": [0.9, 1.0, 1.1]}},
}

_PARENT_HYPOTHESIS: dict = {
    "title": "ORB on FOREX majors — smoke",
    "asset_class": "FOREX",
    "strategy_family": "ORB",
    "hypothesis": "OR breakouts on EURUSD M15.",
    "expected_edge_explanation": "Liquidity formation in early London.",
    "key_indicators": ["opening_range", "atr"],
    "tags": ["orb", "intraday", "forex_majors"],
    "sources": [{"url": "https://x", "title": "x", "why_relevant": "x"}],
    "differentiates_from": [],
}


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
    """Insert a BACKTESTED parent + write strategy/hypothesis/sidecar fixtures."""
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
        json.dumps(_PARENT_STRATEGY_JSON, indent=2)
    )
    (it_dir / "hypothesis.json").write_text(
        json.dumps(_PARENT_HYPOTHESIS, indent=2)
    )
    (it_dir / "add_indicator_request.json").write_text(
        json.dumps(
            {
                "kind": "add_indicator",
                "capability": _CAPABILITY,
                "category": "indicator",
                "phase": "indicator",
                "confidence": 0.85,
                "reasoning": "smoke synthetic - validate reiterate-with-plugin loop",
                "strategy_id": strategy_id,
                "strategy_slug": SMOKE_STRATEGY_SLUG,
                "requested_at": now.isoformat(),
            }
        )
    )
    return strategy_id


def _cleanup_previous_run() -> None:
    """Idempotent: remove the plugin dir from any prior smoke so the slug is free."""
    p_dir = settings.data_dir / "plugins" / SMOKE_PLUGIN_SLUG
    if p_dir.exists():
        shutil.rmtree(p_dir)


async def main() -> int:
    print(f"[m5c_smoke] data_dir={settings.data_dir}")
    _cleanup_previous_run()
    _patch_author_to_use_stub()

    print("[m5c_smoke] [1/4] seeding parent strategy + add_indicator_request.json sidecar")
    strategy_id = await _seed_parent_strategy()
    print(f"       → strategy_id={strategy_id} slug={SMOKE_STRATEGY_SLUG}")

    async with SessionLocal() as session:
        # Refuse to proceed when a stale plugin row with our slug exists. The
        # smoke leaves rows behind on purpose (traceable artifact) — re-runs
        # require manual cleanup of the plugin row OR a different slug.
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        print("[m5c_smoke] [2/4] POST /strategies/{id}/author-plugin")
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
        print(f"       ✓ plugin authored: slug={SMOKE_PLUGIN_SLUG} plugin_id={plugin_id}")

        print("[m5c_smoke] [3/4] POST /plugins/{id}/evaluate")
        r = await client.post(f"/plugins/{plugin_id}/evaluate")
        if r.status_code != 202:
            print(f"       ✗ unexpected status {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        eval_ar_id = r.json()["agent_run_id"]
        eval_ar = await _wait_for_run(eval_ar_id)
        if eval_ar.status != "done":
            print(f"       ✗ evaluate run failed: {eval_ar.error}", file=sys.stderr)
            return 1
        async with SessionLocal() as session:
            plugin = (
                await session.execute(select(Plugin).where(Plugin.id == plugin_id))
            ).scalar_one()
        if plugin.current_state != PluginState.VERIFIED.value:
            print(
                f"       ✗ plugin not VERIFIED: state={plugin.current_state}",
                file=sys.stderr,
            )
            return 1
        print("       ✓ plugin verified")

        print("[m5c_smoke] [4/4] POST /strategies/{id}/reiterate-with-plugin")
        r = await client.post(
            f"/strategies/{strategy_id}/reiterate-with-plugin",
            json={"plugin_slug": SMOKE_PLUGIN_SLUG},
        )
        if r.status_code != 202:
            print(f"       ✗ unexpected status {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        reiter_ar_id = r.json()["agent_run_id"]
        reiter_ar = await _wait_for_run(reiter_ar_id)
        if reiter_ar.status != "done":
            print(f"       ✗ reiterate run failed: {reiter_ar.error}", file=sys.stderr)
            return 1
        print("       ✓ reiterated with plugin")

    # Final assertions — the M5c-specific part.
    async with SessionLocal() as session:
        children = (
            await session.execute(
                select(Strategy).where(Strategy.parent_strategy_id == strategy_id)
            )
        ).scalars().all()

    if len(children) != 1:
        print(
            f"       ✗ expected exactly 1 child Strategy, got {len(children)}",
            file=sys.stderr,
        )
        return 1
    child = children[0]
    if child.current_state != StrategyState.PROPOSED.value:
        print(
            f"       ✗ child state={child.current_state}, expected PROPOSED",
            file=sys.stderr,
        )
        return 1

    child_dir = strategy_dir(child.slug) / "iteration_001"
    child_strategy_path = child_dir / "strategy.json"
    child_hypothesis_path = child_dir / "hypothesis.json"
    if not child_strategy_path.is_file():
        print(f"       ✗ missing {child_strategy_path}", file=sys.stderr)
        return 1
    if not child_hypothesis_path.is_file():
        print(f"       ✗ missing {child_hypothesis_path}", file=sys.stderr)
        return 1

    child_payload = json.loads(child_strategy_path.read_text())
    if child_payload.get("indicators") != [SMOKE_PLUGIN_SLUG]:
        print(
            f"       ✗ child indicators={child_payload.get('indicators')!r}, "
            f"expected [{SMOKE_PLUGIN_SLUG!r}]",
            file=sys.stderr,
        )
        return 1

    child_hypothesis = json.loads(child_hypothesis_path.read_text())
    iterations = child_hypothesis.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        print(
            f"       ✗ child hypothesis missing non-empty iterations[]: {iterations!r}",
            file=sys.stderr,
        )
        return 1
    last = iterations[-1]
    if last.get("plugin_slug") != SMOKE_PLUGIN_SLUG:
        print(
            f"       ✗ last iteration plugin_slug={last.get('plugin_slug')!r}, "
            f"expected {SMOKE_PLUGIN_SLUG!r}",
            file=sys.stderr,
        )
        return 1
    if SMOKE_PLUGIN_SLUG not in last.get("rationale", ""):
        print(
            f"       ✗ slug {SMOKE_PLUGIN_SLUG!r} not in rationale: "
            f"{last.get('rationale')!r}",
            file=sys.stderr,
        )
        return 1

    # Decision D: parent sidecar must still exist (append-only audit).
    parent_sidecar = (
        strategy_dir(SMOKE_STRATEGY_SLUG)
        / "iteration_001"
        / "add_indicator_request.json"
    )
    if not parent_sidecar.is_file():
        print(
            f"       ✗ parent sidecar missing (append-only audit broken): "
            f"{parent_sidecar}",
            file=sys.stderr,
        )
        return 1

    print(
        f"       ✓ child={child.slug} state={child.current_state} "
        f"indicators={child_payload['indicators']}"
    )
    print("[m5c_smoke] PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
