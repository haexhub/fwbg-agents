"""M5d orchestrator integration tests — Planner → Implementer flow.

Exercises `author_plugin_from_strategy` with FunctionModel-stubbed Planner +
Implementer (no real LLM). Asserts:
- 2 AgentRuns created (plugin_planner + plugin_implementer)
- N LlmCalls under the implement-run (1 per gate-loop round)
- Plugin row created + transition SPECIFIED → AUTHORED
- planner-AR has plugin_id linked (for capability lookup)
- planner failure short-circuits the implementer
- implementer failure leaves last_failed_code.py + FAILED ARs
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.plugin_catalog import _load_fwbg_cached
from fwbg_agents.orchestrator.plugin_flow import (
    AuthorPluginPreconditionError,
    PluginAuthorError,
    author_plugin_from_strategy,
    lookup_plugin_capability,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
    Transition,
)

# ---------------------------------------------------------------------------
# Plan / result payload fixtures (mirroring M5d test conventions)
# ---------------------------------------------------------------------------


_PLAN_ARGS: dict[str, Any] = {
    "slug": "fancy_indicator",
    "class_name": "FancyIndicator",
    "phase": "indicators",
    "version": "0.1.0",
    "stateful": False,
    "depends_on": [],
    "params": [
        {
            "name": "window",
            "type": "int",
            "default": 14,
            "description": "Rolling window in bars",
            "min": 2,
            "max": 200,
            "step": 1,
            "required": True,
        }
    ],
    "feature_columns": ["fancy_value"],
    "algorithm_sketch": (
        "Compute a rolling mean of close prices over a configurable window. "
        "Shift the resulting series by 1 bar to prevent lookahead bias. "
        "Append the column to the input DataFrame."
    ),
    "edge_cases": ["empty DataFrame", "window larger than series length"],
    "expected_test_names": [
        "test_constant_price_yields_constant_mean",
        "test_no_lookahead_bias",
        "test_default_params",
    ],
}

_VALID_CODE = (
    "from fwbg_sdk.indicators import BaseIndicator, shift_features\n"
    "from fwbg_sdk.base import PluginPhase\n"
    "import pandas as pd\n"
    "\n"
    "class FancyIndicator(BaseIndicator):\n"
    "    name = 'fancy_indicator'\n"
    "    phase = PluginPhase.INDICATORS\n"
    "    version = '0.1.0'\n"
    "\n"
    "    def compute(self, df: pd.DataFrame, *, window: int = 14) -> pd.DataFrame:\n"
    "        features = {'fancy_value': df['close'].rolling(window).mean()}\n"
    "        return pd.concat([df, shift_features(features, df.index)], axis=1)\n"
    "\n"
    "    def get_feature_columns(self):\n"
    "        return ['fancy_value']\n"
)
_WRONG_CLASS_CODE = (
    "from fwbg_sdk.indicators import BaseIndicator\n"
    "class TotallyDifferentName(BaseIndicator):\n"
    "    name = 'fancy_indicator'\n"
    "    def compute(self, df, **p): return df\n"
)
_VALID_CONTRACT = {
    "name": "fancy_indicator",
    "kind": "indicator",
    "version": "v1",
    "inputs": [{"name": "ohlcv", "dtype": "ohlcv", "required": True, "description": ""}],
    "outputs": [
        {"name": "fancy_value", "dtype": "series", "length_invariant": "same_as_input"}
    ],
    "params": [
        {
            "name": "window",
            "dtype": "int",
            "default": 14,
            "min": 2,
            "max": 200,
            "description": "",
        }
    ],
    "invariants": ["outputs[0].length == inputs[0].length"],
    "test_scenarios": [
        {"name": "trending_up", "data_path": "test_scenarios/trending_up.parquet"}
    ],
}
_VALID_SPEC = (
    "# fancy_indicator\n\n"
    "A rolling mean of the close price over a configurable window. Useful as a "
    "baseline trend-following feature. Shift by 1 bar to avoid lookahead.\n"
)


def _author_result(code: str = _VALID_CODE) -> dict[str, Any]:
    return {
        "slug": "fancy_indicator",
        "python_code": code,
        "contract": _VALID_CONTRACT,
        "spec_md": _VALID_SPEC,
    }


def _stub_model(*responses: dict[str, Any]) -> FunctionModel:
    counter = {"i": 0}

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        idx = counter["i"]
        counter["i"] += 1
        return ModelResponse(parts=[ToolCallPart("final_result", responses[idx])])

    return FunctionModel(handler)


# ---------------------------------------------------------------------------
# Fixture: tmp data_dir + isolated sqlite DB + BACKTESTED parent + sidecar
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def author_env(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    monkeypatch.setattr(settings, "fwbg_repo_root", tmp_path / "no-fwbg")
    _load_fwbg_cached.cache_clear()

    db_url = f"sqlite+aiosqlite:///{tmp_path}/orchestrator.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    async with Session() as setup:
        s = Strategy(
            slug="parent_v1",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=0,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.commit()
        await setup.refresh(s)
        parent_id = s.id

    it_dir = settings.data_dir / "strategies" / "parent_v1" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(
        json.dumps({"name": "parent_v1", "pipeline": "orb_pipeline"})
    )
    (it_dir / "add_indicator_request.json").write_text(
        json.dumps(
            {
                "kind": "add_indicator",
                "confidence": 0.7,
                "reasoning": "no rolling-mean variant in catalog",
                "phase": "indicators",
                "capability": "rolling close-price mean",
                "category": "indicator",
                "strategy_id": parent_id,
                "strategy_slug": "parent_v1",
                "requested_at": now.isoformat(),
            }
        )
    )

    yield Session, parent_id, tmp_path
    await engine.dispose()


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


async def test_full_flow_creates_two_agent_runs_and_plugin(author_env):
    Session, parent_id, _ = author_env
    planner = _stub_model(_PLAN_ARGS)
    implementer = _stub_model(_author_result())

    async with Session() as session:
        plugin_id = await author_plugin_from_strategy(
            session,
            parent_id,
            planner_model=planner,
            implementer_model=implementer,
        )

    async with Session() as session:
        runs = (
            await session.execute(
                select(AgentRun).order_by(AgentRun.id)
            )
        ).scalars().all()
        ar_names = [ar.agent_name for ar in runs]
        ar_statuses = [ar.status for ar in runs]
        plugin = (
            await session.execute(select(Plugin).where(Plugin.id == plugin_id))
        ).scalar_one()
        transitions = (
            await session.execute(select(Transition).order_by(Transition.id))
        ).scalars().all()

    assert ar_names == ["plugin_planner", "plugin_implementer"]
    assert all(s == AgentRunStatus.DONE.value for s in ar_statuses)
    assert plugin.slug == "fancy_indicator"
    assert plugin.current_state == PluginState.AUTHORED.value
    # SPECIFIED → AUTHORED transition is recorded.
    assert any(t.from_state == PluginState.SPECIFIED.value for t in transitions)


async def test_planner_ar_links_to_plugin_for_capability_lookup(author_env):
    Session, parent_id, _ = author_env
    async with Session() as session:
        plugin_id = await author_plugin_from_strategy(
            session,
            parent_id,
            planner_model=_stub_model(_PLAN_ARGS),
            implementer_model=_stub_model(_author_result()),
        )

    async with Session() as session:
        cap = await lookup_plugin_capability(session, plugin_id)

    assert cap == "rolling close-price mean"


async def test_implementer_loop_persists_n_llm_calls(author_env):
    """Two failed rounds + one good → 3 LlmCall rows under the implementer-AR."""
    Session, parent_id, _ = author_env
    bad = _author_result(code=_WRONG_CLASS_CODE)
    good = _author_result(code=_VALID_CODE)

    async with Session() as session:
        await author_plugin_from_strategy(
            session,
            parent_id,
            planner_model=_stub_model(_PLAN_ARGS),
            implementer_model=_stub_model(bad, bad, good),
        )

    async with Session() as session:
        impl_ar = (
            await session.execute(
                select(AgentRun).where(AgentRun.agent_name == "plugin_implementer")
            )
        ).scalar_one()
        llm_calls = (
            await session.execute(
                select(LlmCall).where(LlmCall.agent_run_id == impl_ar.id)
            )
        ).scalars().all()

    assert len(llm_calls) == 3
    assert impl_ar.status == AgentRunStatus.DONE.value


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_planner_failure_short_circuits_implementer(author_env):
    """Planner emits wrong phase → PluginAuthorError, no implementer-AR."""
    Session, parent_id, _ = author_env
    bad_plan = {**_PLAN_ARGS, "phase": "preprocessing"}
    planner = _stub_model(bad_plan)

    async with Session() as session:
        with pytest.raises(PluginAuthorError):
            await author_plugin_from_strategy(
                session,
                parent_id,
                planner_model=planner,
                implementer_model=_stub_model(_author_result()),
            )

    async with Session() as session:
        runs = (
            await session.execute(select(AgentRun).order_by(AgentRun.id))
        ).scalars().all()
        plugins = (
            await session.execute(select(Plugin))
        ).scalars().all()

    assert len(runs) == 1
    assert runs[0].agent_name == "plugin_planner"
    assert runs[0].status == AgentRunStatus.FAILED.value
    assert "phase mismatch" in (runs[0].error or "")
    assert plugins == []


async def test_implementer_failure_marks_both_runs_and_stores_last_code(
    author_env, tmp_path
):
    """Implementer exhausts max_rounds → planner DONE, implementer FAILED with
    last_failed_code.py on disk."""
    Session, parent_id, _ = author_env
    bad = _author_result(code=_WRONG_CLASS_CODE)
    # max_rounds default = 5; provide 5 bad responses to exhaust the budget.
    implementer = _stub_model(bad, bad, bad, bad, bad)

    async with Session() as session:
        with pytest.raises(PluginAuthorError):
            await author_plugin_from_strategy(
                session,
                parent_id,
                planner_model=_stub_model(_PLAN_ARGS),
                implementer_model=implementer,
            )

    async with Session() as session:
        runs = (
            await session.execute(select(AgentRun).order_by(AgentRun.id))
        ).scalars().all()
        plugins = (
            await session.execute(select(Plugin))
        ).scalars().all()

    assert [ar.agent_name for ar in runs] == ["plugin_planner", "plugin_implementer"]
    assert runs[0].status == AgentRunStatus.DONE.value
    assert runs[1].status == AgentRunStatus.FAILED.value
    assert "ContractError" in (runs[1].error or "")
    # last_failed_code.py was written
    assert runs[1].output_artifact_path is not None
    from pathlib import Path

    assert Path(runs[1].output_artifact_path).is_file()
    assert plugins == []


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


async def test_precondition_blocks_before_any_agent_run(author_env):
    """Strategy in non-BACKTESTED state → 422 raised, no ARs persisted."""
    Session, parent_id, _ = author_env
    async with Session() as session:
        s = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        s.current_state = StrategyState.PROPOSED.value
        await session.commit()

    async with Session() as session:
        with pytest.raises(AuthorPluginPreconditionError):
            await author_plugin_from_strategy(
                session,
                parent_id,
                planner_model=_stub_model(_PLAN_ARGS),
                implementer_model=_stub_model(_author_result()),
            )

    async with Session() as session:
        runs = (await session.execute(select(AgentRun))).scalars().all()
    assert runs == []


async def test_precondition_strategy_not_found(author_env):
    Session, _, _ = author_env
    async with Session() as session:
        with pytest.raises(AuthorPluginPreconditionError) as exc_info:
            await author_plugin_from_strategy(
                session,
                strategy_id=99999,
                planner_model=_stub_model(_PLAN_ARGS),
                implementer_model=_stub_model(_author_result()),
            )
    assert "not found" in str(exc_info.value)
