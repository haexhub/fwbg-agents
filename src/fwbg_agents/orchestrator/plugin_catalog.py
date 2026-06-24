"""Plugin discovery + DB merge — runtime catalog of every plugin slug fwbg knows.

Replaces M4's hard-coded `KNOWN_*` frozensets in strategy_validator. Scans two
manifest roots and merges in agent-authored plugins from the DB (only those
already verified, so authored-but-unverified Plugins MUST NOT pass validation).

Cache: `functools.lru_cache` keyed on the fwbg root path. Tests call
`_load_fwbg_cached.cache_clear()` in a fixture. Service restart re-scans —
deliberate: fwbg plugin sets change per deployment, not per request.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.config import settings
from fwbg_agents.persistence.models import Plugin, PluginState

log = logging.getLogger(__name__)


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


_FWBG_CORE_REL = Path("src/fwbg/plugins")
_FWBG_PREMIUM_REL = Path("packages/fwbg-premium/src/fwbg_premium/plugins")


def _read_bundle_manifest(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("skipping unreadable manifest %s: %s", path, exc)
        return None


def _expand_bundle(
    bundle_dir: Path, provenance: Provenance, into: dict[str, dict[str, PluginManifest]]
) -> None:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    data = _read_bundle_manifest(manifest_path)
    if data is None:
        return
    version = str(data.get("version", "0.0.0"))
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        log.warning("bundle %s has non-dict 'plugins' field; skipping", manifest_path)
        return
    for category, slugs in plugins.items():
        if not isinstance(slugs, list):
            continue
        bucket = into.setdefault(category, {})
        for slug in slugs:
            if not isinstance(slug, str):
                continue
            bucket[slug] = PluginManifest(
                name=slug,
                category=category,
                provenance=provenance,
                version=version,
                source_path=manifest_path,
            )


def discover_fwbg_plugins(fwbg_root: Path) -> dict[str, dict[str, PluginManifest]]:
    """Scan fwbg-core + fwbg-premium bundle manifests under `fwbg_root`.

    Missing/unreadable manifests log a warning and contribute nothing — never raises.
    """
    out: dict[str, dict[str, PluginManifest]] = {}
    if not fwbg_root.is_dir():
        log.warning("fwbg_root %s does not exist; catalog will be empty", fwbg_root)
        return out

    for rel in (_FWBG_CORE_REL, _FWBG_PREMIUM_REL):
        root = fwbg_root / rel
        if not root.is_dir():
            continue
        provenance: Provenance = "fwbg-core" if rel == _FWBG_CORE_REL else "fwbg-premium"
        for bundle_dir in sorted(root.iterdir()):
            if bundle_dir.is_dir():
                _expand_bundle(bundle_dir, provenance, out)

    return out


# Module-level cache: the second arg is just a discriminator so we can wipe.
@lru_cache(maxsize=8)
def _load_fwbg_cached(fwbg_root_str: str) -> dict[str, dict[str, PluginManifest]]:
    return discover_fwbg_plugins(Path(fwbg_root_str))


_VISIBLE_PLUGIN_STATES: frozenset[str] = frozenset(
    {PluginState.VERIFIED.value, PluginState.ADOPTED_IN_FWBG.value}
)


# Plugin.kind is stored as a singular per PluginContract.PluginKindLit
# (indicator, model, exit_strategy, filter, ...). fwbg-side bundle
# manifests AND validator/runner queries use PLURAL categories
# (indicators, models, exit_strategies, filters, ...). Map at the merge
# boundary so DB-VERIFIED plugins land in the bucket the validator queries.
# Singular kinds without a known plural pass through unchanged (covers
# multi-word categories like feature_selection that don't pluralize).
_KIND_TO_CATEGORY: dict[str, str] = {
    "indicator": "indicators",
    "model": "models",
    "filter": "filters",
    "exit_strategy": "exit_strategies",
    "risk_management": "risk_management",
    "entry_modifier": "entry_modifier",
    "feature_selection": "feature_selection",
    "preprocessing": "preprocessing",
    "data_loading": "data_loading",
}


def merge_with_db(
    fwbg_catalog: dict[str, dict[str, PluginManifest]],
    db_plugins: list[Plugin],
) -> PluginCatalog:
    """Layer agent-authored verified plugins on top of fwbg discovery.

    DB-side plugins shadow fwbg-side plugins of the same slug (post-promote
    semantics: once an authored plugin makes it back into fwbg, the source-of-
    truth shifts).

    Plugin.kind is the singular PluginContract.PluginKindLit (e.g. ``indicator``,
    ``model``). fwbg-side bundle manifests and validator queries use the plural
    bundle-manifest category (``indicators``, ``models``). We map singular→plural
    via ``_KIND_TO_CATEGORY`` so DB-VERIFIED plugins land in the bucket the
    validator actually queries. Unknown kinds (including already-plural strings
    used by legacy tests) pass through unchanged.
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


async def load_catalog(session: AsyncSession) -> PluginCatalog:
    """Top-level entry: cached fwbg discovery + fresh DB merge."""
    fwbg_root = settings.fwbg_repo_root
    fwbg_part = _load_fwbg_cached(str(fwbg_root))
    result = await session.execute(select(Plugin))
    db_plugins = list(result.scalars().all())
    return merge_with_db(fwbg_part, db_plugins)
