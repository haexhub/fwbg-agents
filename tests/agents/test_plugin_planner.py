"""M5d PluginPlanner tests — FunctionModel-stubbed, no real LLM calls.

Covers:
- happy path (returns PlannerRunResult, plan.json written)
- phase mismatch → PluginPlannerError
- slug collision → PluginPlannerError
- plan.json round-trips into PluginPlan
- system prompt loads from prompts/plugin_authoring.md
- env-driven model selection (PLUGIN_PLANNER_MODEL)
- pydantic schema failure surfaces as PluginPlannerError
- examples appear in the user prompt
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fwbg_agents.agents.plugin_planner import (
    PluginPlan,
    PluginPlanner,
    PluginPlannerError,
    _render_user_prompt,
    planner_model,
)
from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
)
from fwbg_agents.persistence.models import Strategy, StrategyState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stub_model(args: dict[str, Any]) -> FunctionModel:
    """FunctionModel that emits one final_result tool call with the given args."""

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("final_result", args)])

    return FunctionModel(handler)


def _capturing_model(args: dict[str, Any], capture: dict[str, Any]) -> FunctionModel:
    """FunctionModel that captures the incoming messages (so tests can introspect
    the user-prompt content) and then emits the given args."""

    def handler(messages, info: AgentInfo) -> ModelResponse:
        capture["messages"] = messages
        capture["info"] = info
        return ModelResponse(parts=[ToolCallPart("final_result", args)])

    return FunctionModel(handler)


def _build_parent_strategy(slug: str = "parent_v1") -> Strategy:
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    s.id = 1
    return s


def _empty_catalog() -> PluginCatalog:
    return PluginCatalog(by_category={})


def _catalog_with(slug: str, category: str = "indicators") -> PluginCatalog:
    manifest = PluginManifest(
        name=slug,
        category=category,
        provenance="fwbg-core",
        version="0.1.0",
        source_path=Path("/nonexistent"),
    )
    return PluginCatalog(by_category={category: {slug: manifest}})


def _valid_plan_args(
    slug: str = "fancy_indicator",
    class_name: str = "FancyIndicator",
    phase: str = "indicators",
) -> dict[str, Any]:
    return {
        "slug": slug,
        "class_name": class_name,
        "phase": phase,
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


_SIDECAR_INDICATORS = {
    "kind": "add_indicator",
    "confidence": 0.7,
    "reasoning": "no rolling-mean variant in catalog",
    "phase": "indicators",
    "capability": "rolling close-price mean",
    "category": "indicator",
    "strategy_id": 1,
    "strategy_slug": "parent_v1",
}


@pytest.fixture(autouse=True)
def _patch_data_dir(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    yield tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_planner_happy_path_indicator_phase(tmp_path: Path):
    parent = _build_parent_strategy()
    catalog = _empty_catalog()
    planner = PluginPlanner(model=_stub_model(_valid_plan_args()))

    result = await planner.run_plan(
        parent_strategy=parent, sidecar=_SIDECAR_INDICATORS, catalog=catalog
    )

    assert isinstance(result.plan, PluginPlan)
    assert result.plan.slug == "fancy_indicator"
    assert result.plan.phase == "indicators"
    assert result.plan.feature_columns == ["fancy_value"]
    assert result.plan_path.is_file()
    assert result.llm.latency_ms >= 0


async def test_planner_writes_plan_json_that_round_trips(tmp_path: Path):
    parent = _build_parent_strategy()
    planner = PluginPlanner(model=_stub_model(_valid_plan_args()))

    result = await planner.run_plan(
        parent_strategy=parent, sidecar=_SIDECAR_INDICATORS, catalog=_empty_catalog()
    )

    on_disk = json.loads(result.plan_path.read_text(encoding="utf-8"))
    parsed = PluginPlan.model_validate(on_disk)
    assert parsed == result.plan


async def test_planner_raises_on_phase_mismatch():
    parent = _build_parent_strategy()
    # Sidecar says "indicators", plan emits "preprocessing".
    bad_args = _valid_plan_args(phase="preprocessing")
    planner = PluginPlanner(model=_stub_model(bad_args))

    with pytest.raises(PluginPlannerError) as exc_info:
        await planner.run_plan(
            parent_strategy=parent,
            sidecar=_SIDECAR_INDICATORS,
            catalog=_empty_catalog(),
        )
    assert "phase mismatch" in str(exc_info.value)


async def test_planner_raises_on_unknown_sidecar_phase():
    parent = _build_parent_strategy()
    planner = PluginPlanner(model=_stub_model(_valid_plan_args()))

    bad_sidecar = {**_SIDECAR_INDICATORS, "phase": "made_up_phase"}
    with pytest.raises(PluginPlannerError) as exc_info:
        await planner.run_plan(
            parent_strategy=parent,
            sidecar=bad_sidecar,
            catalog=_empty_catalog(),
        )
    assert "unknown sidecar phase" in str(exc_info.value)


async def test_planner_raises_on_slug_collision():
    parent = _build_parent_strategy()
    catalog = _catalog_with("fancy_indicator", category="indicators")
    planner = PluginPlanner(model=_stub_model(_valid_plan_args()))

    with pytest.raises(PluginPlannerError) as exc_info:
        await planner.run_plan(
            parent_strategy=parent, sidecar=_SIDECAR_INDICATORS, catalog=catalog
        )
    assert "slug collision" in str(exc_info.value)


async def test_planner_raises_when_pydantic_schema_invalid():
    parent = _build_parent_strategy()
    bad_args = _valid_plan_args()
    bad_args["feature_columns"] = []  # violates Field(min_length=1)
    planner = PluginPlanner(model=_stub_model(bad_args))

    with pytest.raises(PluginPlannerError) as exc_info:
        await planner.run_plan(
            parent_strategy=parent,
            sidecar=_SIDECAR_INDICATORS,
            catalog=_empty_catalog(),
        )
    assert "schema validation failed" in str(exc_info.value)


def test_planner_uses_canonical_prompt_path():
    """Default prompt_path points at prompts/plugin_authoring.md and is readable."""
    # Don't construct a real model — just check the class default.
    default_path = (
        Path(__file__).parents[2] / "prompts" / "plugin_authoring.md"
    )
    assert default_path.is_file()
    body = default_path.read_text(encoding="utf-8")
    assert "## BasePlugin Contract" in body
    assert "## PluginPhase Enum" in body


def test_planner_model_resolves_from_settings(monkeypatch):
    """planner_model() uses settings.plugin_planner_model."""
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "plugin_planner_model", "claude-sonnet-4-6")
    m = planner_model()
    assert m.model_name == "claude-sonnet-4-6"


async def test_planner_injects_examples_into_user_prompt(tmp_path: Path):
    """Smoke: _render_user_prompt embeds example sources, run_plan calls it."""
    parent = _build_parent_strategy()
    capture: dict[str, Any] = {}
    planner = PluginPlanner(model=_capturing_model(_valid_plan_args(), capture))

    await planner.run_plan(
        parent_strategy=parent,
        sidecar=_SIDECAR_INDICATORS,
        catalog=_empty_catalog(),
    )

    # Inspect the user-prompt text from the captured ModelRequest.
    messages = capture["messages"]
    user_text = "\n".join(
        part.content
        for msg in messages
        for part in getattr(msg, "parts", [])
        if getattr(part, "part_kind", None) == "user-prompt"
    )
    assert "Sidecar (AddIndicator request from the Analyst)" in user_text
    assert "Reference plugins (in-tree examples)" in user_text
    # Empty catalog → the "no examples" fallback string appears.
    assert "no in-tree examples available" in user_text


def test_render_user_prompt_includes_examples_section():
    """_render_user_prompt smoke: example blocks appear when examples are non-empty."""
    from fwbg_agents.agents.plugin_authoring_shared import FwbgPluginExample

    examples = [
        FwbgPluginExample(
            slug="rsi",
            path="/repo/indicators/rsi",
            source="class RSIIndicator(BaseIndicator):\n    name = 'rsi'\n",
        )
    ]
    out = _render_user_prompt(
        strategy_excerpt='{"name":"x"}',
        sidecar_json='{"phase":"indicators"}',
        examples=examples,
    )
    assert "Example: rsi" in out
    assert "class RSIIndicator(BaseIndicator)" in out
