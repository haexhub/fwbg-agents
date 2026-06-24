"""Translator agent — turns ResearcherHypothesis into a runnable fwbg strategy.json (M4).

Two modes:
- `run_fresh(strategy)` — LLM-driven. Reads iteration_001/hypothesis.json,
  emits strategy.json + spec.md. Strategy stays PROPOSED — the Runner picks
  it up next.
- `run_reiterate(parent)` — deterministic (no LLM). Reads the parent's
  analyst_recommendation.json sidecar and produces a child Strategy with
  parent_strategy_id set, iteration_count=1, and the recommendation
  applied. Stays PROPOSED.

Both paths run `validate_strategy_json` on the result and refuse to commit
if the structural check fails — better to mark the AgentRun failed than to
write a broken file that the Runner would only catch later.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.strategy_validator import (
    KNOWN_DATASOURCES,
    KNOWN_FILTERS,
    KNOWN_MODELS,
    KNOWN_PIPELINES,
    KNOWN_RESOURCES,
    KNOWN_TIMEFRAMES,
    KNOWN_VALIDATIONS,
    StrategyValidationError,
    validate_strategy_json,
)
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Strategy,
)
from fwbg_agents.tools.llm import default_model

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "translator.md"


class TranslatorFailed(RuntimeError):
    """Raised when the Translator output fails structural validation."""


class _TranslatorOutput(BaseModel):
    """Loose envelope for the LLM's strategy.json so pydantic-ai can hand us
    structured output. The downstream validator does the strict check."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    hypothesis: str = ""
    expected_outcome: str = ""
    datasource: str
    pipeline: str
    model: str
    filters: str
    validation: str
    resources: str
    timeframe: str
    exit_strategies: list[dict] = []
    tags: list[str] = []
    optimization: dict = {}


def _render_prompt(template: str, *, hypothesis_json: str) -> str:
    return template.replace("{{ hypothesis_json }}", hypothesis_json)


def _known_plugins_dict() -> dict[str, list[str]]:
    return {
        "datasource": sorted(KNOWN_DATASOURCES),
        "pipeline": sorted(KNOWN_PIPELINES),
        "model": sorted(KNOWN_MODELS),
        "filters": sorted(KNOWN_FILTERS),
        "validation": sorted(KNOWN_VALIDATIONS),
        "resources": sorted(KNOWN_RESOURCES),
        "timeframe": sorted(KNOWN_TIMEFRAMES),
    }


def _write_spec_md(path: Path, *, strategy_slug: str, hypothesis: dict, strategy_json: dict) -> None:
    plugins = ", ".join(
        f"{k}={strategy_json.get(k)}"
        for k in ("pipeline", "model", "filters", "validation", "resources")
    )
    text = (
        f"# Spec — {strategy_slug}\n\n"
        "## Goal\n\n"
        f"{hypothesis.get('hypothesis', '').strip()}\n\n"
        "## Inputs\n\n"
        f"- asset_class: `{hypothesis.get('asset_class')}`\n"
        f"- strategy_family: `{hypothesis.get('strategy_family')}`\n"
        f"- timeframe: `{strategy_json.get('timeframe')}`\n"
        f"- key_indicators: {hypothesis.get('key_indicators', [])}\n\n"
        "## Outputs\n\n"
        f"- fwbg `strategy.json` with {plugins}\n"
        f"- Backtest run (via Runner) producing `fwbg_results.json`\n"
        f"- Analyst report and recommendation\n\n"
        "## Acceptance Criteria\n\n"
        f"- {hypothesis.get('expected_edge_explanation', '').strip()}\n"
        f"- Promotion gates per `criteria/{hypothesis.get('asset_class')}.yaml`\n\n"
        "## Implementation Notes\n\n"
        f"- Tags: {strategy_json.get('tags', [])}\n"
        f"- Exit strategies: {len(strategy_json.get('exit_strategies', []))} configured\n"
    )
    path.write_text(text)


class Translator:
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

    async def run_fresh(self, strategy: Strategy) -> Path:
        now = datetime.now(UTC)
        ar = AgentRun(
            agent_name="translator",
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
            iteration_dir.mkdir(parents=True, exist_ok=True)
            hypothesis_path = iteration_dir / "hypothesis.json"
            if not hypothesis_path.is_file():
                raise FileNotFoundError(f"missing hypothesis.json at {hypothesis_path}")

            ar.input_artifact_path = str(hypothesis_path)
            hypothesis_data = json.loads(hypothesis_path.read_text())

            template = self.prompt_path.read_text()
            system_prompt = _render_prompt(
                template, hypothesis_json=json.dumps(hypothesis_data, indent=2)
            )

            agent: Agent[None, _TranslatorOutput] = Agent(
                self.model, output_type=_TranslatorOutput, system_prompt=system_prompt
            )

            @agent.tool_plain
            def get_known_plugins() -> dict[str, list[str]]:
                """Return the catalog of plugin slugs the Translator may pick from."""
                return _known_plugins_dict()

            t0 = time.monotonic()
            result = await agent.run("Emit the strategy.json now.")
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
            await self.session.commit()

            payload = result.output.model_dump()
            payload["name"] = strategy.slug  # canonical slug wins

            try:
                validate_strategy_json(payload)
            except StrategyValidationError as exc:
                raise TranslatorFailed(str(exc)) from exc

            strategy_path = iteration_dir / "strategy.json"
            strategy_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

            spec_path = iteration_dir / "spec.md"
            _write_spec_md(
                spec_path,
                strategy_slug=strategy.slug,
                hypothesis=hypothesis_data,
                strategy_json=payload,
            )
            strategy.spec_path = str(spec_path)
            strategy.updated_at = datetime.now(UTC)

            ar.status = AgentRunStatus.DONE.value
            ar.ended_at = datetime.now(UTC)
            ar.output_artifact_path = str(strategy_path)
            await self.session.commit()

            return strategy_path
        except Exception as exc:
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await self.session.commit()
            raise
