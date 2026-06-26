"""Analyst agent — LLM-driven recommendation engine.

Reads a backtested strategy's results + criteria YAML and emits one of:
  Promote | Abandon | TuneParams | ChangeExit

Structured output is enforced by pydantic-ai. The Analyst can recommend
anything, but the orchestrator's `validate_and_apply` then runs hard rules
(criteria check, post-mortem requirement, ...) before any state changes —
the LLM cannot bypass safety guards.

Token usage is recorded per call in `llm_call`. Cost is left null since the
default model goes through haex-claude-proxy (subscription pricing); future
work can plug in an estimator that infers USD from input/output tokens.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.plugin_catalog import PluginCatalog, load_catalog
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Strategy,
)
from fwbg_agents.tools.llm import default_model

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recommendation schema (typed union — pydantic-ai will validate the LLM
# output against this and retry if the model returns malformed JSON).
# ---------------------------------------------------------------------------


class Promote(BaseModel):
    kind: Literal["promote"] = "promote"
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class Abandon(BaseModel):
    kind: Literal["abandon"] = "abandon"
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    post_mortem_summary: str
    lessons: list[str]


class TuneParams(BaseModel):
    kind: Literal["tune_params"] = "tune_params"
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    param: str
    new_range: list[float | int]


class ChangeExit(BaseModel):
    kind: Literal["change_exit"] = "change_exit"
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    from_exit: str
    to_exit: str
    new_exit_strategy: dict | None = None
    """Optional concrete replacement (name, params, ct, ...).

    If set, the Translator (M4 reiterate-mode) swaps the parent's
    exit_strategies wholesale. If not set, reiterate-mode raises — the
    LLM-driven Analyst is expected to fill this in for ChangeExit
    recommendations from M4 onward."""


class AddIndicator(BaseModel):
    """Request a brand-new plugin via PluginAuthor (M5b).

    The Analyst emits this ONLY when no entry in the catalog snapshot covers
    what the strategy needs. The orchestrator does NOT transition the strategy
    — it persists a sidecar JSON that the M5b PluginAuthor agent picks up to
    write a fresh plugin.
    """

    kind: Literal["add_indicator"] = "add_indicator"
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    phase: Literal["feature_selection", "indicators", "preprocessing", "filters"]
    capability: str = Field(
        description=(
            "Free-text description of the missing capability. Must NOT be a "
            "slug that already exists in the catalog snapshot."
        )
    )
    category: Literal[
        "indicator",
        "model",
        "exit_strategy",
        "risk_management",
        "entry_modifier",
        "preprocessing",
        "feature_selection",
        "data_loading",
    ]


AnalystRecommendation = Annotated[
    Promote | Abandon | TuneParams | ChangeExit | AddIndicator, Discriminator("kind")
]


# ---------------------------------------------------------------------------


_PROMPT_PATH = Path(__file__).parent / "prompts" / "analyst.md"


def _best_symbol_metrics_from_results(run: dict) -> dict:
    assets = run.get("assets") or {}
    best: tuple[float, dict] = (float("-inf"), {})
    for sym in assets.values():
        m = sym.get("unified_metrics") or {}
        sh = m.get("sharpe")
        if isinstance(sh, (int, float)) and sh > best[0]:
            best = (float(sh), m)
    return best[1]


def _render_prompt(
    template: str,
    *,
    strategy: Strategy,
    iteration: int,
    strategy_json: dict,
    metrics: dict,
    criteria_yaml: str,
    catalog_snapshot: str,
) -> str:
    """Tiny mustache-style replacer — we don't need Jinja for five variables."""
    out = template
    out = out.replace("{{ strategy.slug }}", strategy.slug)
    out = out.replace("{{ strategy.asset_class }}", strategy.asset_class)
    out = out.replace("{{ strategy.strategy_family }}", strategy.strategy_family)
    out = out.replace("{{ iteration }}", str(iteration))
    out = out.replace("{{ strategy_json }}", json.dumps(strategy_json, indent=2))
    out = out.replace("{{ metrics }}", json.dumps(metrics, indent=2))
    out = out.replace("{{ criteria_yaml }}", criteria_yaml or "(no criteria YAML present)")
    out = out.replace("{{ catalog_snapshot }}", catalog_snapshot)
    return out


def _render_catalog_snapshot(catalog: PluginCatalog) -> str:
    """One line per (category, slug). Empty categories are skipped."""
    lines: list[str] = []
    for category in sorted(catalog.by_category):
        slugs = catalog.all_slugs_for(category)
        if not slugs:
            continue
        lines.append(f"- {category}: {', '.join(slugs)}")
    if not lines:
        return "(catalog empty — only suggest add_indicator if you genuinely have no other option)"
    return "\n".join(lines)


class Analyst:
    def __init__(
        self,
        session: AsyncSession,
        *,
        model: Model | None = None,
        prompt_path: Path | None = None,
    ):
        self.session = session
        self.model = model if model is not None else default_model()
        self.prompt_path = prompt_path or _PROMPT_PATH

    async def analyze(self, strategy: Strategy) -> AnalystRecommendation:
        now = datetime.now(UTC)
        ar = AgentRun(
            agent_name="analyst",
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
            strategy_path = iteration_dir / "strategy.json"
            results_path = iteration_dir / "fwbg_results.json"
            if not results_path.is_file():
                raise FileNotFoundError(f"missing fwbg_results.json at {results_path}")

            ar.input_artifact_path = str(results_path)

            strategy_json = json.loads(strategy_path.read_text()) if strategy_path.is_file() else {}
            results = json.loads(results_path.read_text())
            metrics = _best_symbol_metrics_from_results(results)

            criteria_path = settings.criteria_dir / f"{strategy.asset_class}.yaml"
            criteria_yaml = criteria_path.read_text() if criteria_path.is_file() else ""

            catalog = await load_catalog(self.session)
            catalog_snapshot = _render_catalog_snapshot(catalog)

            template = self.prompt_path.read_text()
            system_prompt = _render_prompt(
                template,
                strategy=strategy,
                iteration=1,
                strategy_json=strategy_json,
                metrics=metrics,
                criteria_yaml=criteria_yaml,
                catalog_snapshot=catalog_snapshot,
            )

            agent = Agent(
                self.model, output_type=AnalystRecommendation, system_prompt=system_prompt
            )
            t0 = time.monotonic()
            result = await agent.run("Emit your recommendation now.")
            latency_ms = int((time.monotonic() - t0) * 1000)

            usage = result.usage
            self.session.add(
                LlmCall(
                    agent_run_id=ar.id,
                    model=getattr(self.model, "model_name", "unknown"),
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                    latency_ms=latency_ms,
                    created_at=datetime.now(UTC),
                )
            )

            # Write a Markdown report alongside the JSON output.
            report_path = iteration_dir / "analyst_report.md"
            report_path.write_text(
                f"# Analyst recommendation — {strategy.slug} (iteration 1)\n\n"
                f"```json\n{result.output.model_dump_json(indent=2)}\n```\n"
            )

            ar.status = AgentRunStatus.DONE.value
            ar.ended_at = datetime.now(UTC)
            ar.output_artifact_path = str(report_path)
            await self.session.commit()

            return result.output
        except Exception as exc:
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await self.session.commit()
            raise
