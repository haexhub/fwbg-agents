"""Lessons-digest tests (Plan 009 WP5)."""

from __future__ import annotations

import yaml

from fwbg_agents.orchestrator.lessons import (
    lessons_digest,
    regenerate_lessons_digest,
)


def _write_pm(settings, slug, family, lessons, written_at):
    d = settings.data_dir / "strategies" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "post_mortem.yaml").write_text(
        yaml.safe_dump(
            {"slug": slug, "strategy_family": family, "lessons": lessons, "written_at": written_at}
        )
    )


def test_regenerate_groups_by_family_newest_first(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    _write_pm(settings, "orb_a", "ORB", ["ORB fails on JPY crosses"], "2026-01-01T00:00:00")
    _write_pm(settings, "mr_a", "mean_reversion", ["MR needs a vol filter"], "2026-02-01T00:00:00")
    _write_pm(
        settings, "mr_b", "mean_reversion", ["MR dead in trending regimes"], "2026-03-01T00:00:00"
    )

    path = regenerate_lessons_digest()
    text = path.read_text()

    assert "## ORB" in text
    assert "## mean_reversion" in text
    assert "ORB fails on JPY crosses" in text
    # newest mean_reversion lesson appears before the older one
    assert text.index("MR dead in trending regimes") < text.index("MR needs a vol filter")
    assert "[2026-03-01]" in text


def test_digest_placeholder_when_empty(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    assert "no abandoned-strategy lessons" in lessons_digest()


def test_digest_is_length_capped(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    _write_pm(settings, "x", "ORB", ["y" * 10000], "2026-01-01T00:00:00")
    regenerate_lessons_digest()
    assert len(lessons_digest(max_chars=500)) == 500


def test_regenerate_skips_malformed_post_mortem(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    d = settings.data_dir / "strategies" / "broken"
    d.mkdir(parents=True, exist_ok=True)
    (d / "post_mortem.yaml").write_text("{not: valid: yaml:")
    _write_pm(settings, "ok", "ORB", ["real lesson"], "2026-01-01T00:00:00")
    text = regenerate_lessons_digest().read_text()
    assert "real lesson" in text
