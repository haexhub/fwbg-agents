"""M5d smoke: end-to-end Planner -> Implementer plugin authoring via the API.

Drives the M5d split-flow without hitting a real LLM (FunctionModel stubs for
both PluginPlanner and PluginImplementer model factories). Assertions specific
to M5d:

  - exactly two inner AgentRun rows: plugin_planner + plugin_implementer
  - at least one LlmCall row under the plugin_implementer AR (gate-loop rounds)
  - data/plugin-runs/<slug>/plan.json round-trips into PluginPlan
  - plugin.py + contract.yaml + spec.md persisted under data/plugins/<slug>/v1/
  - Plugin row in AUTHORED with the SPECIFIED -> AUTHORED transition recorded

Prereq: `uv run alembic upgrade head`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from fwbg_agents.config import settings
from fwbg_agents.main import app
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import (
    AgentRun,
    LlmCall,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
    Transition,
)

DEADLINE_S = 60.0
POLL_INTERVAL_S = 0.5
SMOKE_STRATEGY_SLUG = "smoke_m5d_parent"
SMOKE_PLUGIN_SLUG = "smoke-m5d-zscore"
SMOKE_CAPABILITY = "rolling close-price z-score for mean-reversion"

_PLAN_STUB: dict = {
    "slug": SMOKE_PLUGIN_SLUG,
    "class_name": "SmokeM5dZscore",
    "phase": "indicators",
    "version": "0.1.0",
    "stateful": False,
    "depends_on": [],
    "params": [
        {
            "name": "window",
            "type": "int",
            "default": 30,
            "description": "Lookback window for the z-score",
            "min": 5,
            "max": 200,
            "step": 1,
            "required": True,
        }
    ],
    "feature_columns": ["smoke_m5d_zscore"],
    "algorithm_sketch": (
        "Compute a rolling z-score of the close price over a configurable window. "
        "Standardize via (close - rolling_mean) / rolling_std. Shift by 1 bar "
        "to avoid lookahead bias."
    ),
    "edge_cases": ["zero-variance windows", "fewer rows than window"],
    "expected_test_names": [
        "test_constant_price_yields_zero",
        "test_no_lookahead_bias",
        "test_default_params",
    ],
}

_PLUGIN_CODE = (
    "import pandas as pd\n"
    "\n"
    "try:\n"
    "    from fwbg_sdk.indicators import BaseIndicator\n"
    "    from fwbg_sdk.base import PluginPhase\n"
    "except ImportError:\n"
    "    class BaseIndicator:  # type: ignore[no-redef]\n"
    "        pass\n"
    "    class PluginPhase:  # type: ignore[no-redef]\n"
    "        INDICATORS = 'indicators'\n"
    "\n"
    "\n"
    "class SmokeM5dZscore(BaseIndicator):\n"
    "    name = 'smoke-m5d-zscore'\n"
    "    phase = PluginPhase.INDICATORS\n"
    "    version = '0.1.0'\n"
    "\n"
    "    def compute(self, df, *, window=30):\n"
    "        return compute(df, window=window)\n"
    "\n"
    "    def get_feature_columns(self):\n"
    "        return ['smoke_m5d_zscore']\n"
    "\n"
    "\n"
    "def compute(df: pd.DataFrame, *, window: int = 30) -> pd.Series:\n"
    "    close = df['close']\n"
    "    mean = close.rolling(window, min_periods=1).mean()\n"
    "    std = close.rolling(window, min_periods=1).std().replace(0, 1e-12)\n"
    "    return (close - mean) / std\n"
)

_CONTRACT: dict = {
    "name": SMOKE_PLUGIN_SLUG,
    "kind": "indicator",
    "version": "v1",
    "inputs": [{"name": "ohlcv", "dtype": "ohlcv", "required": True, "description": ""}],
    "outputs": [
        {"name": "smoke_m5d_zscore", "dtype": "series", "length_invariant": "same_as_input"}
    ],
    "params": [
        {"name": "window", "dtype": "int", "default": 30, "min": 5, "max": 200, "description": ""}
    ],
    "invariants": ["outputs[0].length == inputs[0].length"],
    "test_scenarios": [{"name": "trending_up", "data_path": "test_scenarios/trending_up.parquet"}],
}

_SPEC_MD = (
    f"# {SMOKE_PLUGIN_SLUG}\n\n"
    "Rolling close-price z-score over a configurable lookback. Useful as a "
    "mean-reversion entry filter. Output series length matches input.\n"
)

_PARENT_STRATEGY_JSON: dict = {
    "name": SMOKE_STRATEGY_SLUG,
    "description": "ORB rule-based on EURUSD M15 — m5d smoke fixture",
    "hypothesis": "Opening range breakouts on EURUSD M15.",
    "expected_outcome": "sharpe > 1.0",
    "datasource": "forexsb",
    "pipeline": "orb_simple_v1",
    "model": "xgboost",
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


def _stub_planner_model() -> FunctionModel:
    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("final_result", _PLAN_STUB)])

    return FunctionModel(handler)


def _stub_implementer_model() -> FunctionModel:
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


def _patch_factories_to_use_stubs() -> None:
    """Monkey-patch the planner+implementer model resolver so the smoke
    drives the real Planner -> Implementer flow without an LLM.

    The agents resolve their model via ``tools.llm.model_for()``, imported into
    each agent module's namespace; patch that seam so the stub reaches them."""
    from fwbg_agents.agents import plugin_implementer as pi
    from fwbg_agents.agents import plugin_planner as pp

    planner_stub = _stub_planner_model()
    implementer_stub = _stub_implementer_model()
    pp.model_for = lambda _agent_name: planner_stub
    pi.model_for = lambda _agent_name: implementer_stub


async def _wait_for_run(agent_run_id: int, deadline_s: float = DEADLINE_S) -> AgentRun:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        async with SessionLocal() as session:
            ar = (
                await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
            ).scalar_one()
            if ar.status in {"done", "failed"}:
                return ar
        await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"agent_run {agent_run_id} did not finish in {deadline_s}s")


async def _seed_parent_strategy() -> int:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(Strategy).where(Strategy.slug == SMOKE_STRATEGY_SLUG))
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
    (it_dir / "strategy.json").write_text(json.dumps(_PARENT_STRATEGY_JSON, indent=2))
    (it_dir / "add_indicator_request.json").write_text(
        json.dumps(
            {
                "kind": "add_indicator",
                "capability": SMOKE_CAPABILITY,
                "category": "indicator",
                "phase": "indicators",
                "confidence": 0.85,
                "reasoning": "smoke synthetic — drive the M5d split-flow",
                "strategy_id": strategy_id,
                "strategy_slug": SMOKE_STRATEGY_SLUG,
                "requested_at": now.isoformat(),
            },
            indent=2,
        )
    )
    return strategy_id


async def _assert_split_flow_artifacts(plugin_id: int) -> None:
    """The core M5d-specific check: 2 inner AgentRuns + LlmCalls + plan.json."""
    async with SessionLocal() as session:
        # Filter to the two inner ARs the orchestrator creates; the API also
        # writes a `plugin_author_flow` outer AR linked to plugin_id, which is
        # the user-facing poll target and not part of the split-flow assertion.
        runs = (
            (
                await session.execute(
                    select(AgentRun)
                    .where(
                        (AgentRun.plugin_id == plugin_id)
                        & (AgentRun.agent_name.in_(("plugin_planner", "plugin_implementer")))
                    )
                    .order_by(AgentRun.id)
                )
            )
            .scalars()
            .all()
        )
        names = [ar.agent_name for ar in runs]
        if names != ["plugin_planner", "plugin_implementer"]:
            raise AssertionError(
                f"expected inner ARs [plugin_planner, plugin_implementer], got {names}"
            )
        if not all(ar.status == "done" for ar in runs):
            raise AssertionError(f"both inner ARs must be DONE; got: {[ar.status for ar in runs]}")
        impl_ar = runs[1]
        llm_calls = (
            (await session.execute(select(LlmCall).where(LlmCall.agent_run_id == impl_ar.id)))
            .scalars()
            .all()
        )
        if not llm_calls:
            raise AssertionError("expected at least 1 LlmCall under the plugin_implementer AR")
        plugin = (await session.execute(select(Plugin).where(Plugin.id == plugin_id))).scalar_one()
        if plugin.current_state != PluginState.AUTHORED.value:
            raise AssertionError(f"plugin should be AUTHORED; got {plugin.current_state!r}")
        transitions = (
            (await session.execute(select(Transition).where(Transition.entity_id == plugin_id)))
            .scalars()
            .all()
        )
        if not any(
            t.from_state == PluginState.SPECIFIED.value and t.to_state == PluginState.AUTHORED.value
            for t in transitions
        ):
            raise AssertionError("expected SPECIFIED -> AUTHORED transition for the plugin")

    # plan.json round-trips into PluginPlan
    from fwbg_agents.agents.plugin_planner import PluginPlan

    plan_path = settings.data_dir / "plugin-runs" / SMOKE_PLUGIN_SLUG / "plan.json"
    if not plan_path.is_file():
        raise AssertionError(f"missing plan.json at {plan_path}")
    plan_data = json.loads(plan_path.read_text())
    PluginPlan.model_validate(plan_data)  # raises on schema mismatch

    # plugin.py + contract.yaml + spec.md exist
    plugin_dir = settings.data_dir / "plugins" / SMOKE_PLUGIN_SLUG / "v1"
    for name in ("plugin.py", "contract.yaml", "spec.md"):
        if not (plugin_dir / name).is_file():
            raise AssertionError(f"missing artifact {plugin_dir / name}")


async def main() -> int:
    print(f"[m5d_smoke] data_dir={settings.data_dir}")
    _patch_factories_to_use_stubs()

    # Clear any prior smoke leftovers so the assertions key off a fresh row set.
    sdir = strategy_dir(SMOKE_STRATEGY_SLUG)
    if sdir.exists():
        shutil.rmtree(sdir)
    plugin_runs_dir = settings.data_dir / "plugin-runs" / SMOKE_PLUGIN_SLUG
    if plugin_runs_dir.exists():
        shutil.rmtree(plugin_runs_dir)
    plugins_dir = settings.data_dir / "plugins" / SMOKE_PLUGIN_SLUG
    if plugins_dir.exists():
        shutil.rmtree(plugins_dir)

    print("[m5d_smoke] [1/3] seeding parent strategy + add_indicator sidecar")
    strategy_id = await _seed_parent_strategy()
    print(f"       -> strategy_id={strategy_id} slug={SMOKE_STRATEGY_SLUG}")

    print("[m5d_smoke] [2/3] POST /strategies/{id}/author-plugin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/strategies/{strategy_id}/author-plugin")
        if resp.status_code != 202:
            print(f"       x unexpected status {resp.status_code}: {resp.text}", file=sys.stderr)
            return 1
        outer_ar_id = resp.json()["agent_run_id"]

    outer = await _wait_for_run(outer_ar_id)
    if outer.status != "done":
        print(
            f"       x outer AR {outer_ar_id} ended in {outer.status!r}: {outer.error}",
            file=sys.stderr,
        )
        return 1
    plugin_id = outer.plugin_id
    print(f"       v plugin authored: slug={SMOKE_PLUGIN_SLUG} plugin_id={plugin_id}")

    print("[m5d_smoke] [3/3] asserting split-flow artifacts (2 ARs + LlmCalls + plan.json)")
    try:
        await _assert_split_flow_artifacts(plugin_id)
    except AssertionError as exc:
        print(f"       x assertion failed: {exc}", file=sys.stderr)
        return 1
    print("       v 2 inner ARs (plugin_planner, plugin_implementer) DONE")
    print("       v >=1 LlmCall under the plugin_implementer AR")
    print("       v plan.json + plugin.py + contract.yaml + spec.md on disk")
    print("       v Plugin row AUTHORED with SPECIFIED -> AUTHORED transition")

    print("[m5d_smoke] PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
