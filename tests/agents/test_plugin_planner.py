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
from fwbg_agents.orchestrator.live_catalog import LiveCatalog
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


def _empty_live() -> LiveCatalog:
    return LiveCatalog(catalog=PluginCatalog(by_category={}), plugin_details={})


def _live_with(slug: str, category: str = "indicators") -> LiveCatalog:
    manifest = PluginManifest(
        name=slug,
        category=category,
        provenance="fwbg-core",
        version="0.1.0",
        source_path=Path("/nonexistent"),
    )
    return LiveCatalog(
        catalog=PluginCatalog(by_category={category: {slug: manifest}}),
        plugin_details={},
    )


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
    planner = PluginPlanner(model=_stub_model(_valid_plan_args()))

    result = await planner.run_plan(
        parent_strategy=parent, sidecar=_SIDECAR_INDICATORS, live=_empty_live()
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
        parent_strategy=parent, sidecar=_SIDECAR_INDICATORS, live=_empty_live()
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
            live=_empty_live(),
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
            live=_empty_live(),
        )
    assert "unknown sidecar phase" in str(exc_info.value)


def test_plugin_phase_literal_matches_sdk_enum():
    """PluginPhaseLit is the LLM-facing mirror of fwbg_sdk.PluginPhase —
    pinned so the two vocabularies cannot drift apart."""
    from typing import get_args

    from fwbg_sdk.base import PluginPhase

    from fwbg_agents.agents.plugin_planner import PluginPhaseLit

    assert set(get_args(PluginPhaseLit)) == {p.value for p in PluginPhase}


async def test_planner_raises_on_slug_collision():
    parent = _build_parent_strategy()
    planner = PluginPlanner(model=_stub_model(_valid_plan_args()))

    with pytest.raises(PluginPlannerError) as exc_info:
        await planner.run_plan(
            parent_strategy=parent,
            sidecar=_SIDECAR_INDICATORS,
            live=_live_with("fancy_indicator", category="indicators"),
        )
    assert "slug collision" in str(exc_info.value)


async def test_planner_raises_on_unknown_depends_on():
    parent = _build_parent_strategy()
    plan_args = _valid_plan_args()
    plan_args["depends_on"] = ["atr_quintile_filter"]
    planner = PluginPlanner(model=_stub_model(plan_args))

    with pytest.raises(PluginPlannerError) as exc_info:
        await planner.run_plan(
            parent_strategy=parent,
            sidecar=_SIDECAR_INDICATORS,
            live=_live_with("adx", category="indicators"),
        )
    assert "depends_on" in str(exc_info.value)
    assert "atr_quintile_filter" in str(exc_info.value)


async def test_planner_accepts_known_depends_on():
    parent = _build_parent_strategy()
    plan_args = _valid_plan_args()
    plan_args["depends_on"] = ["adx"]
    planner = PluginPlanner(model=_stub_model(plan_args))

    result = await planner.run_plan(
        parent_strategy=parent,
        sidecar=_SIDECAR_INDICATORS,
        live=_live_with("adx", category="indicators"),
    )
    assert result.plan.depends_on == ["adx"]


async def test_planner_lax_depends_on_when_catalog_empty():
    """Offline / no live catalog: depends_on membership is unchecked (M4 lax fallback)."""
    parent = _build_parent_strategy()
    plan_args = _valid_plan_args()
    plan_args["depends_on"] = ["whatever_not_registered"]
    planner = PluginPlanner(model=_stub_model(plan_args))

    result = await planner.run_plan(
        parent_strategy=parent, sidecar=_SIDECAR_INDICATORS, live=_empty_live()
    )
    assert result.plan.depends_on == ["whatever_not_registered"]


async def test_planner_raises_when_pydantic_schema_invalid():
    parent = _build_parent_strategy()
    bad_args = _valid_plan_args()
    bad_args["feature_columns"] = []  # violates Field(min_length=1)
    planner = PluginPlanner(model=_stub_model(bad_args))

    with pytest.raises(PluginPlannerError) as exc_info:
        await planner.run_plan(
            parent_strategy=parent,
            sidecar=_SIDECAR_INDICATORS,
            live=_empty_live(),
        )
    assert "schema validation failed" in str(exc_info.value)


def test_planner_uses_canonical_prompt_path():
    """Default prompt_path points at prompts/plugin_authoring.md and is readable."""
    # Don't construct a real model — just check the class default.
    default_path = Path(__file__).parents[2] / "prompts" / "plugin_authoring.md"
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
        live=_empty_live(),
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


# ---------------------------------------------------------------------------
# get_fwbg_plugin_examples — async, HTTP-fetched source
# ---------------------------------------------------------------------------


def _live_with_details(details: dict[str, Any]) -> LiveCatalog:
    return LiveCatalog(catalog=PluginCatalog(by_category={}), plugin_details=details)


class _FakeSourceClient:
    """Returns source for any fqn; raises for fqns in `fail`."""

    def __init__(self, fail: set[str] | None = None):
        self._fail = fail or set()

    async def get_plugin_source(self, fqn: str) -> dict[str, Any]:
        if fqn in self._fail:
            raise RuntimeError("boom")
        return {"fqn": fqn, "filename": f"{fqn.rsplit('.', 1)[-1]}.py", "source": f"# src {fqn}"}


async def test_get_fwbg_plugin_examples_fetches_source_over_http():
    from fwbg_agents.agents.plugin_authoring_shared import get_fwbg_plugin_examples

    live = _live_with_details(
        {
            "indicators": [
                {
                    "name": "ema",
                    "fqn": "core.indicators.ema",
                    "description": "",
                    "default_params": {},
                },
                {
                    "name": "adx",
                    "fqn": "core.indicators.adx",
                    "description": "",
                    "default_params": {},
                },
            ]
        }
    )

    examples = await get_fwbg_plugin_examples(live, _FakeSourceClient(), category="indicator", n=3)

    # sorted by name; source + filename carried through from the API response
    assert [e.slug for e in examples] == ["adx", "ema"]
    assert examples[0].source == "# src core.indicators.adx"
    assert examples[0].path == "adx.py"


async def test_get_fwbg_plugin_examples_skips_fetch_failures():
    from fwbg_agents.agents.plugin_authoring_shared import get_fwbg_plugin_examples

    live = _live_with_details(
        {
            "indicators": [
                {"name": "ema", "fqn": "core.indicators.ema"},
                {"name": "adx", "fqn": "core.indicators.adx"},
            ]
        }
    )
    client = _FakeSourceClient(fail={"core.indicators.adx"})

    examples = await get_fwbg_plugin_examples(live, client, category="indicator", n=3)

    assert [e.slug for e in examples] == ["ema"]


async def test_get_fwbg_plugin_examples_unknown_category_is_empty():
    from fwbg_agents.agents.plugin_authoring_shared import get_fwbg_plugin_examples

    examples = await get_fwbg_plugin_examples(
        _empty_live(), _FakeSourceClient(), category="not_a_category", n=3
    )
    assert examples == []
