"""Plugin catalog types + DB merge.

The catalog of fwbg-known plugin slugs is built live from the fwbg HTTP API
(see `live_catalog.fetch_live_catalog`); fwbg is the single source of truth.
This module holds the shared catalog types (`PluginManifest`, `PluginCatalog`)
and `merge_with_db`, which layers agent-authored plugins from the DB (only those
already verified/adopted, so authored-but-unverified plugins MUST NOT pass
validation) onto the API-built catalog.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from fwbg_agents.persistence.models import Plugin, PluginState

Provenance = Literal["fwbg-core", "fwbg-premium", "agent-authored"]


class PluginManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    category: str
    provenance: Provenance
    version: str
    source_path: Path


class PluginCatalog(BaseModel):
    model_config = ConfigDict(frozen=True)

    by_category: dict[str, dict[str, PluginManifest]]

    def has(self, category: str, slug: str) -> bool:
        return slug in self.by_category.get(category, {})

    def get(self, category: str, slug: str) -> PluginManifest | None:
        return self.by_category.get(category, {}).get(slug)

    def all_slugs_for(self, category: str) -> list[str]:
        return sorted(self.by_category.get(category, {}).keys())


_VISIBLE_PLUGIN_STATES: frozenset[str] = frozenset(
    {PluginState.VERIFIED.value, PluginState.ADOPTED_IN_FWBG.value}
)


# Plugin.kind is stored as a singular per PluginContract.PluginKindLit
# (indicator, model, exit_strategy, filter, ...). The fwbg API AND
# validator/runner queries use PLURAL categories (indicators, models,
# exit_strategies, filters, ...). Map at the merge boundary so DB-VERIFIED
# plugins land in the bucket the validator queries. Singular kinds without a
# known plural pass through unchanged (covers multi-word categories like
# feature_selection that don't pluralize).
_KIND_TO_CATEGORY: dict[str, str] = {
    "indicator": "indicators",
    "model": "models",
    "filter": "filters",
    "exit_strategy": "exit_strategies",
    "risk_management": "filters",
    "entry_modifier": "entry_modifier",
    "feature_selection": "feature_selection",
    "preprocessing": "preprocessing",
    "data_loading": "data_loading",
}


def merge_with_db(
    fwbg_catalog: dict[str, dict[str, PluginManifest]],
    db_plugins: list[Plugin],
) -> PluginCatalog:
    """Layer agent-authored verified plugins on top of the API catalog.

    DB-side plugins shadow fwbg-side plugins of the same slug (post-promote
    semantics: once an authored plugin makes it back into fwbg, the source-of-
    truth shifts).

    Plugin.kind is the singular PluginContract.PluginKindLit (e.g. ``indicator``,
    ``model``). The fwbg API and validator queries use the plural category
    (``indicators``, ``models``). We map singular→plural via ``_KIND_TO_CATEGORY``
    so DB-VERIFIED plugins land in the bucket the validator actually queries.
    Unknown kinds (including already-plural strings used by legacy tests) pass
    through unchanged.
    """
    merged: dict[str, dict[str, PluginManifest]] = {
        cat: dict(slugs) for cat, slugs in fwbg_catalog.items()
    }
    for p in db_plugins:
        if p.current_state not in _VISIBLE_PLUGIN_STATES:
            continue
        category = _KIND_TO_CATEGORY.get(p.kind, p.kind)
        bucket = merged.setdefault(category, {})
        spec = Path(p.spec_path) if p.spec_path else Path("")
        version = spec.parent.name if p.spec_path else "v1"
        bucket[p.slug] = PluginManifest(
            name=p.slug,
            category=category,
            provenance="agent-authored",
            version=version,
            source_path=spec,
        )
    return PluginCatalog(by_category=merged)
