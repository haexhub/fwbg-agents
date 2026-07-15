"""Plugin catalog types.

The catalog of fwbg-known plugin slugs is built live from the fwbg HTTP API
(see `live_catalog.fetch_live_catalog`); fwbg is the single source of truth.
Agent-authored plugins appear here after being registered via POST /api/plugins
(Phase 3.2) — no DB merge needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Provenance = Literal["fwbg-core", "fwbg-premium", "agent-authored"]


class PluginManifest(BaseModel):
    """Immutable descriptor for a single registered fwbg plugin."""

    model_config = ConfigDict(frozen=True)

    name: str
    category: str
    provenance: Provenance
    version: str
    source_path: Path
    # Parameter schema as served by fwbg (param name → spec with type/
    # default/choices/...). Empty when fwbg didn't provide one; validation
    # is then lax for this plugin.
    param_schema: dict[str, Any] = Field(default_factory=dict)


class PluginCatalog(BaseModel):
    """Immutable snapshot of all fwbg-known plugins indexed by category."""

    model_config = ConfigDict(frozen=True)

    by_category: dict[str, dict[str, PluginManifest]]

    def has(self, category: str, slug: str) -> bool:
        """Return True if the catalog contains a plugin with the given category and slug."""
        return slug in self.by_category.get(category, {})

    def get(self, category: str, slug: str) -> PluginManifest | None:
        """Return the manifest for a plugin, or None if not found."""
        return self.by_category.get(category, {}).get(slug)

    def all_slugs_for(self, category: str) -> list[str]:
        """Return a sorted list of all plugin slugs in the given category."""
        return sorted(self.by_category.get(category, {}).keys())
