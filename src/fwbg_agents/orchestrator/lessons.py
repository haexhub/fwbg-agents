"""Lessons digest — global memory of abandoned-strategy lessons (Plan 009 WP5).

After every abandon the post-mortem's `lessons` are aggregated (deterministic,
no LLM) into `data/lessons.md`, grouped by `strategy_family`, newest first, with
date + slug. The Researcher gets a length-capped view of this file as the
`{{ lessons_digest }}` prompt slot so it does not re-propose ideas that already
failed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from fwbg_agents.config import settings

log = logging.getLogger(__name__)

LESSONS_FILENAME = "lessons.md"
DIGEST_MAX_CHARS = 4000


def _lessons_path() -> Path:
    return settings.data_dir / LESSONS_FILENAME


def regenerate_lessons_digest() -> Path:
    """Rebuild `data/lessons.md` from every strategy's post_mortem.yaml.

    Grouped by family; within a family, newest first. Best-effort — a malformed
    post-mortem is skipped, never raised.
    """
    root = settings.data_dir / "strategies"
    # (written_at, family, slug, lesson) — written_at as sortable ISO string.
    rows: list[tuple[str, str, str, str]] = []
    if root.is_dir():
        for pm in sorted(root.glob("*/post_mortem.yaml")):
            try:
                data = yaml.safe_load(pm.read_text()) or {}
            except (OSError, yaml.YAMLError):
                continue
            family = str(data.get("strategy_family") or "unknown")
            slug = str(data.get("slug") or pm.parent.name)
            written = str(data.get("written_at") or "")
            lessons = data.get("lessons")
            if not isinstance(lessons, list):
                continue
            for lesson in lessons:
                rows.append((written, family, slug, str(lesson)))

    by_family: dict[str, list[tuple[str, str, str, str]]] = {}
    for row in rows:
        by_family.setdefault(row[1], []).append(row)

    lines = ["# Lessons from abandoned strategies", ""]
    if not rows:
        lines.append("_none yet_")
    for family in sorted(by_family):
        lines.append(f"## {family}")
        # newest first
        for written, _fam, slug, lesson in sorted(
            by_family[family], key=lambda r: r[0], reverse=True
        ):
            date = written[:10] if written else "????-??-??"
            lines.append(f"- [{date}] `{slug}`: {lesson}")
        lines.append("")

    path = _lessons_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n")
    return path


def lessons_digest(max_chars: int = DIGEST_MAX_CHARS) -> str:
    """Return the current lessons digest (length-capped), or a placeholder."""
    path = _lessons_path()
    if not path.is_file():
        return "(no abandoned-strategy lessons yet)"
    text = path.read_text()
    return text[:max_chars]
