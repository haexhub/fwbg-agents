"""Shared helpers for the M5d plugin-authoring agents (Planner + Implementer).

Holds the symbols both agents need: SyntaxCheck, validate_python_syntax,
FwbgPluginExample, get_fwbg_plugin_examples, and the strategy-excerpt
renderer. Extracted from the retired M5b plugin_author module.
"""

from __future__ import annotations

import ast
import json
import logging

from pydantic import BaseModel, ConfigDict, Field

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.live_catalog import LiveCatalog
from fwbg_agents.orchestrator.plugin_contract import PluginContract, PluginKindLit
from fwbg_agents.persistence.models import Strategy
from fwbg_agents.tools.fwbg_client import FwbgClient

log = logging.getLogger(__name__)

_PLUGIN_EXAMPLES_HARD_CAP = 5
_SOURCE_TRUNCATE_CHARS = 4000

# PluginContract.kind / AddIndicator.category are singular; the live catalog's
# plugin_details are keyed by the plural API category (see
# live_catalog._PHASE_TO_CATEGORY). The mapping is hand-curated because English
# plurals aren't algorithmically reliable. Filter/risk kinds route to `filters`
# to match the API category (fwbg has no distinct `filters` phase).
_CATEGORY_TO_BUNDLE_DIR: dict[str, str] = {
    "indicator": "indicators",
    "model": "models",
    "exit_strategy": "exit_strategies",
    "risk_management": "filters",
    "filter": "filters",
    "filters": "filters",
    "entry_modifier": "entry_modifiers",
    "preprocessing": "preprocessing",
    "feature_selection": "feature_selection",
    "data_loading": "data_loading",
}


class SyntaxCheck(BaseModel):
    """Result of a Python syntax validation check."""

    model_config = ConfigDict(frozen=True)
    ok: bool
    line: int | None = None
    msg: str = ""


class FwbgPluginExample(BaseModel):
    """One fwbg plugin source example fetched from the live catalog."""

    model_config = ConfigDict(frozen=True)
    slug: str
    path: str
    source: str


class PluginAuthorResult(BaseModel):
    """Output schema for the PluginImplementer (M5d) — kept compatible with
    M5b's PluginAuthor.run_fresh return shape so M5c reiterate-flow downstream
    is unaffected by the split."""

    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=2, max_length=64)
    python_code: str = Field(min_length=10)
    contract: PluginContract
    spec_md: str = Field(min_length=80)


def validate_python_syntax(code: str) -> SyntaxCheck:
    """Run ast.parse on `code`. Deterministic — no LLM."""
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return SyntaxCheck(ok=False, line=exc.lineno or 1, msg=str(exc))
    return SyntaxCheck(ok=True)


async def get_fwbg_plugin_examples(
    live: LiveCatalog,
    client: FwbgClient,
    *,
    category: PluginKindLit,
    n: int = 3,
) -> list[FwbgPluginExample]:
    """Return up to `min(n, 5)` plugin source samples for the given singular
    category, fetched over HTTP from fwbg (the single source of truth).

    The LiveCatalog's `plugin_details` for a category come straight from
    GET /api/plugins, so they only ever contain fwbg-registered (never
    agent-authored) plugins. Each carries an `fqn`; we fetch its source via
    `client.get_plugin_source(fqn)`. Values above the hard cap of 5 are silently
    clamped (with a warning). Per-plugin fetch failures are logged and skipped.
    Unknown category returns []."""
    if n > _PLUGIN_EXAMPLES_HARD_CAP:
        log.warning(
            "get_fwbg_plugin_examples: clamping n=%d to hard cap %d",
            n,
            _PLUGIN_EXAMPLES_HARD_CAP,
        )
        n = _PLUGIN_EXAMPLES_HARD_CAP
    if n <= 0:
        return []

    bundle_dir = _CATEGORY_TO_BUNDLE_DIR.get(category)
    if bundle_dir is None:
        return []

    entries = sorted(live.plugin_details.get(bundle_dir, []), key=lambda e: e.get("name", ""))
    out: list[FwbgPluginExample] = []
    for entry in entries:
        if len(out) >= n:
            break
        fqn = entry.get("fqn")
        if not fqn:
            continue
        try:
            data = await client.get_plugin_source(fqn)
        except Exception as exc:
            log.warning("could not fetch source for plugin %s: %s", fqn, exc)
            continue
        source = (data.get("source") or "")[:_SOURCE_TRUNCATE_CHARS]
        out.append(
            FwbgPluginExample(
                slug=entry.get("name", "") or fqn,
                path=data.get("filename") or fqn,
                source=source,
            )
        )
    return out


def render_strategy_excerpt(parent: Strategy) -> str:
    """Render an excerpt of the parent's strategy.json for LLM context."""
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
