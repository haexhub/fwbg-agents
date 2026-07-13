"""Analyst agent — LLM-driven recommendation engine.

Reads a backtested strategy's results + criteria YAML and emits one of:
  Promote | Abandon | TuneParams | ChangeExit | ModifyPlugins | AddIndicator

Structured output is enforced by pydantic-ai. The Analyst can recommend
anything, but the orchestrator's `validate_and_apply` then runs hard rules
(criteria check, post-mortem requirement, ...) before any state changes —
the LLM cannot bypass safety guards.

Context given to the model (M8 analyst upgrade):
- per-asset metrics for EVERY backtested symbol (not just the best one),
  plus a per-asset evaluation against the asset-class criteria YAML,
- the full family history of the iteration chain (which change produced
  which metrics), so the model can judge whether its last recommendation
  actually improved anything,
- the plugin catalog with descriptions + default params (live from fwbg
  when a client is provided), so change_exit / modify_plugins can emit
  concrete, valid replacement specs.

Token usage is recorded per call in `llm_call`. Cost is left null since the
default model goes through haex-claude-proxy (subscription pricing); future
work can plug in an estimator that infers USD from input/output tokens.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, field_validator, model_validator
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.instrumented import run_instrumented
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import check_backtest_criteria, strategy_dir
from fwbg_agents.orchestrator.lineage import render_family_history
from fwbg_agents.orchestrator.live_catalog import LiveCatalog, fetch_live_catalog
from fwbg_agents.orchestrator.plugin_catalog import PluginCatalog
from fwbg_agents.persistence.agent_runs import (
    fail_agent_run,
    finish_agent_run,
    start_agent_run,
)
from fwbg_agents.persistence.models import (
    AgentRunStatus,
    LlmCall,
    Strategy,
)
from fwbg_agents.tools.fwbg_client import FwbgClient
from fwbg_agents.tools.llm import model_for, prompt_path_for

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recommendation schema (typed union — pydantic-ai will validate the LLM
# output against this and retry if the model returns malformed JSON).
# ---------------------------------------------------------------------------


class _RecBase(BaseModel):
    """Fields common to every recommendation kind.

    `confidence` and `reasoning` are defaulted so an occasional model omission
    degrades gracefully instead of exhausting pydantic-ai's output retries and
    crashing the whole auto-runner pass (the models regularly drop these two
    when focused on kind-specific fields). Both are advisory — logging and the
    post-mortem only; the hard promotion gates live in validate_and_apply.
    """

    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasoning: str = ""


class _IterBase(_RecBase):
    """Base for recommendation kinds that spawn a child iteration."""

    target_assets: list[str] = Field(
        default_factory=list,
        description=(
            "Symbols the child iteration should focus on (subset of the "
            "backtested assets). Use this to drop assets where the strategy "
            "consistently fails and concentrate on the ones showing an edge. "
            "Empty = keep the parent's universe."
        ),
    )


class Promote(_RecBase):
    """Decision to promote the strategy to the next phase."""

    kind: Literal["promote"] = "promote"


class Abandon(_RecBase):
    """Decision to abandon the strategy with post-mortem details."""

    kind: Literal["abandon"] = "abandon"
    post_mortem_summary: str
    lessons: list[str]


class ParamTune(BaseModel):
    """One parameter to retune with a list of candidate values."""

    param: str
    new_range: list[float | int] = Field(
        description="3-7 candidate values for a grid search over this parameter."
    )


class TuneParams(_IterBase):
    """Decision to retune one to three strategy parameters."""

    kind: Literal["tune_params"] = "tune_params"
    params: list[ParamTune] = Field(
        min_length=1,
        max_length=3,
        description="The 1-3 most impactful parameters to re-tune together.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_single_param(cls, data: object) -> object:
        """Accept the pre-M8 single-param shape {param, new_range}."""
        if (
            isinstance(data, dict)
            and "params" not in data
            and "param" in data
            and "new_range" in data
        ):
            data = dict(data)
            data["params"] = [{"param": data.pop("param"), "new_range": data.pop("new_range")}]
        return data


class ChangeExit(_IterBase):
    """Decision to swap the strategy's exit plugin for a different catalog entry."""

    kind: Literal["change_exit"] = "change_exit"
    from_exit: str
    to_exit: str
    new_exit_strategy: dict
    """Concrete replacement spec: {"name": <catalog slug>, "params": {...}}.

    Required — the Translator swaps the parent's exit_strategies wholesale
    with this entry and validates it against the live catalog."""


class PluginOp(BaseModel):
    """One deterministic edit to the strategy's plugin composition."""

    action: Literal["add", "remove", "replace"]
    section: Literal["indicators", "preprocessing", "feature_selection", "extra_filters"]
    slug: str = Field(
        description=(
            "Catalog slug to add / replace with; for action=remove, the slug "
            "to remove. MUST exist in the catalog below."
        )
    )
    params: dict = Field(
        default_factory=dict,
        description="Plugin params (inline-pipeline sections only; start from the defaults).",
    )
    replaces: str | None = Field(
        default=None,
        description="For action=replace: the existing slug being replaced.",
    )


class ModifyPlugins(_IterBase):
    """Re-compose the strategy from EXISTING catalog plugins.

    Unlike add_indicator (which requests a brand-new plugin), this swaps,
    adds or removes plugins that are already in the catalog. The Translator
    applies the ops deterministically and validates the result.
    """

    kind: Literal["modify_plugins"] = "modify_plugins"
    ops: list[PluginOp] = Field(min_length=1, max_length=3)


# ---------------------------------------------------------------------------
# AddIndicator enum robustness.
#
# Models routinely copy the *plural* category key straight out of the catalog
# snapshot ("indicators") while the schema wants the singular enum member
# ("indicator"), and they occasionally invent a phase the prompt never lists
# ("entry"). Either near-miss used to exhaust pydantic-ai's output retries and
# fail the whole analyst run. We keep one canonical mapping here, used both to
# render valid category labels into the snapshot *and* to coerce the model's
# output before validation — mirroring the tolerance the downstream
# plugin_planner already applies (see its _PHASE_MAPPING). Unknown values fall
# back to the most common valid member (with a warning) rather than crash.
# ---------------------------------------------------------------------------

_CATEGORY_VALUES: tuple[str, ...] = (
    "indicator",
    "model",
    "exit_strategy",
    "risk_management",
    "entry_modifier",
    "preprocessing",
    "feature_selection",
    "data_loading",
)
_CATEGORY_ALIASES: dict[str, str] = {
    "indicators": "indicator",
    "models": "model",
    "exit_strategies": "exit_strategy",
    "entry_modifiers": "entry_modifier",
}

_PHASE_VALUES: tuple[str, ...] = ("feature_selection", "indicators", "preprocessing", "filters")
_PHASE_ALIASES: dict[str, str] = {
    "entry": "indicators",
    "indicator": "indicators",
    "filter": "filters",
}


def _normalise_category(value: str) -> str:
    """Normalise a raw category string to a valid AddIndicator.category enum member."""
    key = value.strip().lower()
    if key in _CATEGORY_VALUES:
        return key
    mapped = _CATEGORY_ALIASES.get(key)
    if mapped:
        return mapped
    log.warning("AddIndicator.category %r not recognised; defaulting to 'indicator'", value)
    return "indicator"


def _normalise_phase(value: str) -> str:
    """Normalise a raw phase string to a valid AddIndicator.phase enum member."""
    key = value.strip().lower()
    if key in _PHASE_VALUES:
        return key
    mapped = _PHASE_ALIASES.get(key)
    if mapped:
        return mapped
    log.warning("AddIndicator.phase %r not recognised; defaulting to 'indicators'", value)
    return "indicators"


class AddIndicator(_RecBase):
    """Request a brand-new plugin via PluginAuthor (M5b).

    The Analyst emits this ONLY when no entry in the catalog snapshot covers
    what the strategy needs. The orchestrator does NOT transition the strategy
    — it persists a sidecar JSON that the M5b PluginAuthor agent picks up to
    write a fresh plugin.
    """

    kind: Literal["add_indicator"] = "add_indicator"
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

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, v: object) -> object:
        """Coerce the raw category value before field validation."""
        return _normalise_category(v) if isinstance(v, str) else v

    @field_validator("phase", mode="before")
    @classmethod
    def _coerce_phase(cls, v: object) -> object:
        """Coerce the raw phase value before field validation."""
        return _normalise_phase(v) if isinstance(v, str) else v


AnalystRecommendation = Annotated[
    Promote | Abandon | TuneParams | ChangeExit | ModifyPlugins | AddIndicator,
    Discriminator("kind"),
]


# ---------------------------------------------------------------------------


_PROMPT_PATH = Path(__file__).parent / "prompts" / "analyst.md"


def _best_symbol_metrics_from_results(run: dict) -> dict:
    """Return unified_metrics for the symbol with the highest Sharpe in a backtest run."""
    assets = run.get("assets") or {}
    best: tuple[float, dict] = (float("-inf"), {})
    for sym in assets.values():
        m = sym.get("unified_metrics") or {}
        sh = m.get("sharpe")
        if isinstance(sh, (int, float)) and sh > best[0]:
            best = (float(sh), m)
    return best[1]


def _median_metrics_across_assets(run: dict) -> dict:
    """Per-metric median across every asset that produced unified_metrics.

    The promotion gate judges a strategy over its whole universe rather than
    its single best symbol (which invites selection bias — a strong result on
    one asset carried the whole gate). For a single-asset universe the median
    equals that asset's metrics, so single-asset strategies are unaffected.
    """
    per_metric: dict[str, list[float]] = {}
    for sym in (run.get("assets") or {}).values():
        m = sym.get("unified_metrics") or {}
        for k, v in m.items():
            if isinstance(v, (int, float)):
                per_metric.setdefault(k, []).append(float(v))
    return {k: float(statistics.median(vs)) for k, vs in per_metric.items() if vs}


def _per_asset_metrics_from_results(run: dict) -> dict[str, dict]:
    """symbol → unified_metrics for every backtested asset."""
    return {
        sym: (data.get("unified_metrics") or {}) for sym, data in (run.get("assets") or {}).items()
    }


def _render_per_asset_criteria(asset_class: str | None, per_asset: dict[str, dict]) -> str:
    """PASS/FAIL per symbol against the asset-class criteria YAML."""
    if not per_asset:
        return "(no per-asset results)"
    lines: list[str] = []
    for sym in sorted(per_asset):
        numeric = {k: float(v) for k, v in per_asset[sym].items() if isinstance(v, (int, float))}
        ok, failures = check_backtest_criteria(
            asset_class=asset_class or "unknown", metrics=numeric
        )
        lines.append(f"- {sym}: {'PASS' if ok else 'FAIL — ' + '; '.join(failures)}")
    return "\n".join(lines)


def _render_prompt(
    template: str,
    *,
    strategy: Strategy,
    iteration: int,
    max_iterations: int,
    strategy_json: dict,
    metrics: dict,
    median_metrics: dict,
    per_asset_metrics: dict[str, dict],
    per_asset_criteria: str,
    family_history: str,
    criteria_yaml: str,
    trade_diagnostics: str,
    catalog_snapshot: str,
) -> str:
    """Tiny mustache-style replacer — we don't need Jinja for a handful of variables."""
    out = template
    out = out.replace("{{ strategy.slug }}", strategy.slug or "")
    out = out.replace("{{ strategy.asset_class }}", strategy.asset_class or "unknown")
    out = out.replace("{{ strategy.strategy_family }}", strategy.strategy_family or "unknown")
    out = out.replace("{{ iteration }}", str(iteration))
    out = out.replace("{{ max_iterations }}", str(max_iterations))
    out = out.replace("{{ strategy_json }}", json.dumps(strategy_json, indent=2))
    out = out.replace("{{ metrics }}", json.dumps(metrics, indent=2))
    out = out.replace("{{ median_metrics }}", json.dumps(median_metrics, indent=2))
    out = out.replace("{{ per_asset_metrics }}", json.dumps(per_asset_metrics, indent=2))
    out = out.replace("{{ per_asset_criteria }}", per_asset_criteria)
    out = out.replace("{{ family_history }}", family_history)
    out = out.replace("{{ criteria_yaml }}", criteria_yaml or "(no criteria YAML present)")
    out = out.replace("{{ trade_diagnostics }}", trade_diagnostics)
    out = out.replace("{{ catalog_snapshot }}", catalog_snapshot)
    return out


def _render_catalog_snapshot(catalog: PluginCatalog) -> str:
    """One line per (category, slug). Empty categories are skipped.

    Category labels are normalised to the AddIndicator.category enum spelling
    (singular) so that, when the model decides an add_indicator request is
    warranted, it copies a *valid* category token — the raw catalog keys are
    plural ("indicators") and used to make the model emit an invalid category.
    """
    lines: list[str] = []
    for category in sorted(catalog.by_category):
        slugs = catalog.all_slugs_for(category)
        if not slugs:
            continue
        lines.append(f"- {_normalise_category(category)}: {', '.join(slugs)}")
    if not lines:
        return "(catalog empty — only suggest add_indicator if you genuinely have no other option)"
    return "\n".join(lines)


def _render_catalog_details(live: LiveCatalog) -> str:
    """Catalog with descriptions + default params, so the model can emit
    concrete specs for change_exit / modify_plugins. Falls back to the plain
    slug snapshot when no details are available (offline catalog scan)."""
    if not any(live.plugin_details.values()):
        return _render_catalog_snapshot(live.catalog)

    lines: list[str] = []
    for category in sorted(live.catalog.by_category):
        slugs = live.catalog.all_slugs_for(category)
        if not slugs:
            continue
        details: dict[str, dict[str, Any]] = {
            d.get("name", ""): d for d in live.plugin_details.get(category, [])
        }
        lines.append(f"### {_normalise_category(category)}")
        for slug in slugs:
            d = details.get(slug) or {}
            desc = d.get("description") or ""
            defaults = d.get("default_params") or {}
            suffix = f" (default params: {json.dumps(defaults)})" if defaults else ""
            lines.append(f"- {slug}{': ' + desc if desc else ''}{suffix}")
    if not lines:
        return "(catalog empty — only suggest add_indicator if you genuinely have no other option)"
    return "\n".join(lines)


class Analyst:
    """LLM-driven agent that reads backtest results and emits a typed recommendation."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        model: Model | None = None,
        prompt_path: Path | None = None,
        fwbg_client: FwbgClient | None = None,
    ):
        """Initialize."""
        self.session = session
        self.model = model if model is not None else model_for("analyst")
        self.prompt_path = prompt_path or prompt_path_for("analyst", _PROMPT_PATH)
        self.fwbg_client = fwbg_client

    async def analyze(self, strategy: Strategy) -> AnalystRecommendation:
        """Run the analyst agent on a backtested strategy and return a typed recommendation."""
        ar = await start_agent_run(self.session, agent_name="analyst", strategy_id=strategy.id)

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
            median_metrics = _median_metrics_across_assets(results)
            per_asset = _per_asset_metrics_from_results(results)
            per_asset_criteria = _render_per_asset_criteria(strategy.asset_class, per_asset)

            criteria_path = settings.criteria_dir / f"{strategy.asset_class}.yaml"
            criteria_yaml = criteria_path.read_text() if criteria_path.is_file() else ""

            diagnostics_path = iteration_dir / "trade_diagnostics.md"
            trade_diagnostics = (
                diagnostics_path.read_text()
                if diagnostics_path.is_file()
                else "(no trade diagnostics available)"
            )

            live = await fetch_live_catalog(self.session, self.fwbg_client)
            catalog_snapshot = _render_catalog_details(live)

            depth, family_history = await render_family_history(self.session, strategy)

            template = self.prompt_path.read_text()
            system_prompt = _render_prompt(
                template,
                strategy=strategy,
                iteration=depth,
                max_iterations=settings.reiterate_max_depth,
                strategy_json=strategy_json,
                metrics=metrics,
                median_metrics=median_metrics,
                per_asset_metrics=per_asset,
                per_asset_criteria=per_asset_criteria,
                family_history=family_history,
                criteria_yaml=criteria_yaml,
                trade_diagnostics=trade_diagnostics,
                catalog_snapshot=catalog_snapshot,
            )

            agent = Agent(  # type: ignore[call-overload]  # pydantic-ai union output_type not matched by overloads
                self.model,
                output_type=AnalystRecommendation,
                system_prompt=system_prompt,
                retries={"output": 3},
            )
            t0 = time.monotonic()
            result = await run_instrumented(
                agent, "Emit your recommendation now.", agent_run_id=ar.id
            )
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
                f"# Analyst recommendation — {strategy.slug} (iteration {depth})\n\n"
                f"```json\n{result.output.model_dump_json(indent=2)}\n```\n"
            )

            await finish_agent_run(
                self.session,
                ar,
                status=AgentRunStatus.DONE,
                output_artifact_path=str(report_path),
            )

            return result.output
        except Exception as exc:
            await fail_agent_run(self.session, ar, exc)
            raise
