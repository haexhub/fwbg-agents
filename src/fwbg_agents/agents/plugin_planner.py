"""M5d PluginPlanner — emits a structured PluginPlan via the stronger model
(default `claude-opus-4-8`, env-overridable via PLUGIN_PLANNER_MODEL).

Pure callable: takes (parent_strategy, sidecar, catalog) → returns a
PlannerRunResult bundle (plan + on-disk plan.json path + LlmCall telemetry).
AgentRun + LlmCall persistence is the orchestrator's responsibility (Task 5).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from fwbg_agents.agents.plugin_author import (
    FwbgPluginExample,
    get_fwbg_plugin_examples,
)
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.plugin_catalog import PluginCatalog
from fwbg_agents.persistence.models import Strategy

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parents[3] / "prompts" / "plugin_authoring.md"

# Plural sidecar phase (from AddIndicator.phase) → fwbg_sdk.base.PluginPhase value.
# "filters" routes to RISK_MANAGEMENT in the SDK enum.
_PHASE_MAPPING: dict[str, str] = {
    "indicators": "indicators",
    "feature_selection": "feature_selection",
    "preprocessing": "preprocessing",
    "filters": "risk_management",
}

PluginPhaseLit = Literal[
    "data_loading",
    "preprocessing",
    "indicators",
    "feature_selection",
    "exit_strategies",
    "risk_management",
    "labeling",
    "model",
    "validation",
]


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal[
        "int",
        "float",
        "bool",
        "string",
        "list[int]",
        "list[float]",
        "list[string]",
        "choice",
    ]
    default: int | float | bool | str | list[Any] | None
    description: str = Field(min_length=1)
    min: int | float | None = None
    max: int | float | None = None
    step: int | float | None = None
    choices: list[str] | None = None
    required: bool = True


class PluginPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    class_name: str = Field(min_length=2, pattern=r"^[A-Z][A-Za-z0-9]+$")
    phase: PluginPhaseLit
    version: str = Field(default="0.1.0", min_length=1)
    stateful: bool = False
    depends_on: list[str] = []
    params: list[ParamSpec] = []
    feature_columns: list[str] = Field(min_length=1)
    algorithm_sketch: str = Field(min_length=120)
    edge_cases: list[str] = Field(min_length=1)
    expected_test_names: list[str] = Field(min_length=3)


class PluginPlannerFailed(RuntimeError):
    """PluginPlanner cannot produce a valid PluginPlan (phase mismatch, slug
    collision, schema validation, or wrapper errors)."""


@dataclass(frozen=True)
class LlmCallMeta:
    model_name: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class PlannerRunResult:
    plan: PluginPlan
    plan_path: Path
    llm: LlmCallMeta


def planner_model() -> Model:
    """Build the PluginPlanner's Anthropic model from settings."""
    provider = AnthropicProvider(
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
    )
    return AnthropicModel(
        model_name=settings.plugin_planner_model,
        provider=provider,
    )


def _slug_in_catalog(slug: str, catalog: PluginCatalog) -> bool:
    return any(slug in slugs for slugs in catalog.by_category.values())


def _render_strategy_excerpt(parent: Strategy) -> str:
    """Render an excerpt of the parent's strategy.json.

    Duplicated from plugin_author._render_strategy_excerpt to avoid importing a
    private symbol; will be consolidated in Task 5 (plugin_authoring_shared).
    """
    latest_dir = settings.data_dir / "strategies" / parent.slug / "iteration_001"
    strategy_path = latest_dir / "strategy.json"
    if not strategy_path.is_file():
        return "(no strategy.json on disk)"
    try:
        data = json.loads(strategy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(unreadable strategy.json)"
    excerpt_keys = ("name", "pipeline", "model", "filters", "validation", "exit_strategies")
    excerpt = {k: data.get(k) for k in excerpt_keys if k in data}
    return json.dumps(excerpt, indent=2)


def _render_user_prompt(
    *,
    strategy_excerpt: str,
    sidecar_json: str,
    examples: list[FwbgPluginExample],
) -> str:
    examples_block = (
        "\n\n".join(
            f"## Example: {ex.slug} ({ex.path})\n```python\n{ex.source}\n```"
            for ex in examples
        )
        or "(no in-tree examples available for this category)"
    )
    return (
        "## Parent Strategy excerpt\n"
        f"```json\n{strategy_excerpt}\n```\n\n"
        "## Sidecar (AddIndicator request from the Analyst)\n"
        f"```json\n{sidecar_json}\n```\n\n"
        "## Reference plugins (in-tree examples)\n"
        f"{examples_block}\n\n"
        "Emit the PluginPlan now."
    )


class PluginPlanner:
    """Stronger-model agent that emits a structured PluginPlan from a sidecar.

    The caller (orchestrator) wraps this in an AgentRun(kind='plugin_plan') and
    persists the returned LlmCallMeta as an LlmCall row.
    """

    def __init__(
        self,
        *,
        model: Model | None = None,
        prompt_path: Path | None = None,
    ) -> None:
        self.model = model if model is not None else planner_model()
        self.prompt_path = prompt_path or _PROMPT_PATH

    async def run_plan(
        self,
        *,
        parent_strategy: Strategy,
        sidecar: dict[str, Any],
        catalog: PluginCatalog,
    ) -> PlannerRunResult:
        sidecar_phase = sidecar.get("phase")
        if sidecar_phase not in _PHASE_MAPPING:
            raise PluginPlannerFailed(
                f"unknown sidecar phase: {sidecar_phase!r}; "
                f"expected one of {sorted(_PHASE_MAPPING)}"
            )
        expected_phase = _PHASE_MAPPING[sidecar_phase]

        try:
            system_prompt = self.prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PluginPlannerFailed(
                f"prompt-doc not readable at {self.prompt_path}: {exc}"
            ) from exc

        strategy_excerpt = _render_strategy_excerpt(parent_strategy)
        category = sidecar.get("category") or sidecar_phase
        examples = get_fwbg_plugin_examples(catalog, category=category, n=3)
        user_prompt = _render_user_prompt(
            strategy_excerpt=strategy_excerpt,
            sidecar_json=json.dumps(sidecar, indent=2, default=str),
            examples=examples,
        )

        agent: Agent[None, PluginPlan] = Agent(
            self.model,
            output_type=PluginPlan,
            system_prompt=system_prompt,
        )

        t0 = time.monotonic()
        try:
            result = await agent.run(user_prompt)
        except (ValidationError, UnexpectedModelBehavior) as exc:
            raise PluginPlannerFailed(f"plan schema validation failed: {exc}") from exc
        latency_ms = int((time.monotonic() - t0) * 1000)

        plan = result.output

        if plan.phase != expected_phase:
            raise PluginPlannerFailed(
                f"phase mismatch: sidecar phase {sidecar_phase!r} maps to "
                f"{expected_phase!r}, plan emitted {plan.phase!r}"
            )

        if _slug_in_catalog(plan.slug, catalog):
            raise PluginPlannerFailed(
                f"slug collision: {plan.slug!r} already exists in the catalog"
            )

        plan_dir = settings.data_dir / "plugin-runs" / plan.slug
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plan_dir / "plan.json"
        plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

        usage = result.usage
        # pydantic-ai exposes RunUsage as an attribute (not callable).
        # M5b's PluginAuthor uses input_tokens/output_tokens — match that.
        meta = LlmCallMeta(
            model_name=getattr(self.model, "model_name", "unknown"),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            latency_ms=latency_ms,
        )

        log.info(
            "plugin_planner.run_plan_ok slug=%s phase=%s latency_ms=%d",
            plan.slug,
            plan.phase,
            latency_ms,
        )

        return PlannerRunResult(plan=plan, plan_path=plan_path, llm=meta)
