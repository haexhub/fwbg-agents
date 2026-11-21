"""Backfill speckit spec.md for the existing fwbg plugin corpus (Phase 1).

Reads each catalogued plugin's source, generates a validated PluginSpec via the
LLM, and writes a co-located spec.md next to the plugin in the fwbg repo. The
resulting specs are the corpus the dedup gate (Phase 2) matches new capabilities
against.

Idempotent: skips a plugin that already has a spec.md unless --force. Run it
where the haex-claude-proxy is reachable (it makes one LLM call per plugin).

Usage:
    uv run python scripts/backfill_plugin_specs.py --dry-run --limit 1   # verify one
    uv run python scripts/backfill_plugin_specs.py                       # write all missing
    uv run python scripts/backfill_plugin_specs.py --force               # regenerate all
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Iterator, Sequence
from importlib.metadata import entry_points
from pathlib import Path

from fwbg_agents.config import settings
from fwbg_agents.speckit import SPEC_FILENAME, render_spec_md
from fwbg_agents.speckit.spec_generator import (
    CATEGORY_TO_KIND,
    generate_spec_from_source,
)

log = logging.getLogger("backfill_plugin_specs")

# This dev-time tool legitimately reads the fwbg repo off disk (unlike the
# runtime catalog, which is API-only). Plugins live at
# <root>/<bundle>/<category>/<slug>/__init__.py under every root.
_FWBG_CORE_REL = Path("src/fwbg/plugins")
# fwbg-premium is a separately installed package. Like fwbg itself, this tool
# finds it through the entry point group below; each entry point resolves to a
# callable returning that package's plugins directory.
_PLUGIN_PACKAGES_GROUP = "fwbg.plugin_packages"


def installed_plugin_roots() -> list[Path]:
    """Plugin directories contributed by installed packages (entry points).

    Returns an empty list when no such package (e.g. fwbg-premium) is installed;
    the backfill then covers the core plugins only.
    """
    roots: list[Path] = []
    for ep in entry_points(group=_PLUGIN_PACKAGES_GROUP):
        try:
            plugins_dir = Path(ep.load()()).resolve()
        except (ImportError, AttributeError, TypeError, OSError) as exc:
            log.warning("skip plugin package %r: cannot resolve its plugins dir: %s", ep.name, exc)
            continue
        if not plugins_dir.is_dir():
            log.warning("skip plugin package %r: %s is not a directory", ep.name, plugins_dir)
            continue
        if plugins_dir not in roots:
            roots.append(plugins_dir)
    if not roots:
        log.info(
            "no installed plugin package found via entry point group %r "
            "(fwbg-premium not installed): processing core plugins only",
            _PLUGIN_PACKAGES_GROUP,
        )
    return roots


def _read_plugin_source(plugin_dir: Path) -> str:
    """Concatenate the plugin's .py source (excluding tests) for the generator."""
    parts: list[str] = []
    for py in sorted(plugin_dir.rglob("*.py")):
        if py.name == "tests.py" or py.name.startswith("test_"):
            continue
        try:
            parts.append(f"# --- {py.relative_to(plugin_dir)} ---\n{py.read_text()}")
        except OSError as exc:
            log.warning("cannot read %s: %s", py, exc)
    return "\n\n".join(parts)


def _iter_plugins(
    fwbg_root: Path, extra_roots: Sequence[Path] | None = None
) -> Iterator[tuple[str, str, Path]]:
    """Yield (slug, category, plugin_dir) for EVERY plugin on disk under the
    core root and the installed plugin packages — not just those declared in a
    bundle manifest.

    ``extra_roots`` are plugin directories of installed packages; None resolves
    them through the ``fwbg.plugin_packages`` entry point group.

    Layout: <root>/<bundle>/<category>/<slug>/__init__.py.
    """
    if extra_roots is None:
        extra_roots = installed_plugin_roots()
    for root in (fwbg_root / _FWBG_CORE_REL, *extra_roots):
        if not root.is_dir():
            continue
        for bundle_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for category_dir in sorted(p for p in bundle_dir.iterdir() if p.is_dir()):
                for slug_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
                    if (slug_dir / "__init__.py").is_file():
                        yield slug_dir.name, category_dir.name, slug_dir


async def backfill(*, fwbg_root: Path, dry_run: bool, force: bool, limit: int | None) -> int:
    done = 0
    for slug, category, plugin_dir in _iter_plugins(fwbg_root):
        if limit is not None and done >= limit:
            break
        kind = CATEGORY_TO_KIND.get(category)
        if kind is None:
            log.warning("skip %s: no PluginKind for category %r", slug, category)
            continue
        if not plugin_dir.is_dir():
            log.warning("skip %s: plugin dir missing at %s", slug, plugin_dir)
            continue
        spec_path = plugin_dir / SPEC_FILENAME
        if spec_path.exists() and not force:
            log.info("skip %s: spec.md already exists (use --force to regenerate)", slug)
            continue
        source = _read_plugin_source(plugin_dir)
        if not source.strip():
            log.warning("skip %s: no source found in %s", slug, plugin_dir)
            continue

        log.info("generating spec for %s (%s)...", slug, kind)
        spec = await generate_spec_from_source(slug=slug, kind=kind, source_code=source)
        md = render_spec_md(spec)
        if dry_run:
            print(f"\n===== {spec_path} =====\n{md}")
        else:
            spec_path.write_text(md, encoding="utf-8")
            log.info("wrote %s", spec_path)
        done += 1

    log.info("backfill %s: %d plugin(s) processed", "dry-run" if dry_run else "done", done)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fwbg-root", type=Path, default=settings.fwbg_repo_root)
    parser.add_argument("--dry-run", action="store_true", help="print specs, don't write")
    parser.add_argument("--force", action="store_true", help="regenerate existing specs")
    parser.add_argument("--limit", type=int, default=None, help="process at most N plugins")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(
        backfill(
            fwbg_root=args.fwbg_root,
            dry_run=args.dry_run,
            force=args.force,
            limit=args.limit,
        )
    )


if __name__ == "__main__":
    main()
