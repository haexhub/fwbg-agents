"""Plugin catalog types.

The catalog of fwbg-known plugin slugs is built live from the fwbg HTTP API
(see `live_catalog.fetch_live_catalog`); fwbg is the single source of truth.
Agent-authored plugins appear here after being registered via POST /api/plugins
(Phase 3.2) — no DB merge needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
