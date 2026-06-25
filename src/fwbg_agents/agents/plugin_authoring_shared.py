"""Shared helpers for the M5d plugin-authoring agents (Planner + Implementer).

Holds the symbols both agents need: SyntaxCheck, validate_python_syntax,
FwbgPluginExample, get_fwbg_plugin_examples, and the strategy-excerpt
renderer. Extracted from the retired M5b plugin_author module.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from fwbg_agents.config import settings
from fwbg_agents.orchestrator.plugin_catalog import PluginCatalog
from fwbg_agents.orchestrator.plugin_contract import PluginContract, PluginKindLit
from fwbg_agents.persistence.models import Strategy

log = logging.getLogger(__name__)

_PLUGIN_EXAMPLES_HARD_CAP = 5
_SOURCE_TRUNCATE_CHARS = 4000

# PluginContract.kind / AddIndicator.category are singular; fwbg bundle
# manifests use plural directory names. The mapping is hand-curated because
# English plurals aren't algorithmically reliable.
_CATEGORY_TO_BUNDLE_DIR: dict[str, str] = {
    "indicator": "indicators",
    "model": "models",
    "exit_strategy": "exit_strategies",
    "risk_management": "risk_management",
    "entry_modifier": "entry_modifiers",
    "preprocessing": "preprocessing",
    "feature_selection": "feature_selection",
    "data_loading": "data_loading",
}


class SyntaxCheck(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    line: int | None = None
    msg: str = ""


class FwbgPluginExample(BaseModel):
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


def _read_plugin_source(bundle_manifest: Path, plural_category: str, slug: str) -> str | None:
    """Read the most likely `plugin.py`-equivalent file for a plugin slug.

    Layout: bundle_dir/<plural_category>/<slug>/{plugin.py, <slug>.py, *.py}.
    Returns None when nothing readable is found; the caller filters those out.
    """
    bundle_dir = bundle_manifest.parent
    slug_dir = bundle_dir / plural_category / slug
    if not slug_dir.is_dir():
        return None

    for filename in ("plugin.py", f"{slug}.py"):
        candidate = slug_dir / filename
        if candidate.is_file():
            try:
                return candidate.read_text()[:_SOURCE_TRUNCATE_CHARS]
            except OSError:
                return None

    # Fall back to the first non-test python file in the slug dir.
    for candidate in sorted(slug_dir.glob("*.py")):
        if candidate.name.startswith("test_"):
            continue
        try:
            return candidate.read_text()[:_SOURCE_TRUNCATE_CHARS]
        except OSError:
            continue
    return None


def get_fwbg_plugin_examples(
    catalog: PluginCatalog,
    *,
    category: PluginKindLit,
    n: int = 3,
) -> list[FwbgPluginExample]:
    """Return up to `min(n, 5)` plugin source samples for the given singular
    category. Values above the hard cap of 5 are silently clamped (with a
    warning). Unreadable plugin dirs are skipped. Unknown category returns []."""
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

    candidates = catalog.by_category.get(bundle_dir, {})
    out: list[FwbgPluginExample] = []
    for slug in sorted(candidates):
        if len(out) >= n:
            break
        manifest = candidates[slug]
        # Only fwbg-core / fwbg-premium provenance — agent-authored plugins
        # haven't proven themselves yet.
        if manifest.provenance == "agent-authored":
            continue
        source = _read_plugin_source(manifest.source_path, bundle_dir, slug)
        if source is None:
            continue
        out.append(
            FwbgPluginExample(
                slug=slug,
                path=str(manifest.source_path.parent / bundle_dir / slug),
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
