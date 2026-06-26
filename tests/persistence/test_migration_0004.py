"""Migration 0004 — alembic upgrade exercises the actual SQL.

We don't round-trip (sqlite alembic downgrade has quirks); we only verify
that an `alembic upgrade head` against an empty sqlite produces a schema
that contains `verification_run` with the expected columns + indices,
and that an existing `plugin.kind = 'exit'` row gets migrated to
`'exit_strategy'`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from fwbg_agents.config import settings


def _alembic_upgrade_to(target: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path("alembic.ini").resolve()))
    command.upgrade(cfg, target)


def test_upgrade_head_creates_verification_run_table(tmp_path, monkeypatch):
    """env.py overrides sqlalchemy.url with settings.db_url; redirect the
    settings singleton's data_dir to tmp_path so the upgrade runs against a
    throw-away DB."""
    monkeypatch.chdir(Path(__file__).resolve().parents[2])  # repo root
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db_path = tmp_path / "state.db"
    _alembic_upgrade_to("head")

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    insp = inspect(engine)
    assert "verification_run" in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("verification_run")}
    assert cols == {
        "id",
        "plugin_id",
        "status",
        "scenarios_run",
        "scenarios_passed",
        "error_log_path",
        "started_at",
        "ended_at",
        "created_at",
    }
    idx_cols = {tuple(i["column_names"]) for i in insp.get_indexes("verification_run")}
    assert ("plugin_id",) in idx_cols
    assert ("created_at",) in idx_cols
    engine.dispose()


def test_existing_exit_kind_migrated_to_exit_strategy(tmp_path, monkeypatch):
    """Plugin rows written under M3 with kind='exit' must be rewritten to
    'exit_strategy' so they match the extended PluginKind enum."""
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db_path = tmp_path / "state.db"

    _alembic_upgrade_to("0003")

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plugin (slug, current_state, kind, created_at, updated_at) "
                "VALUES ('legacy-exit', 'specified', 'exit', :now, :now)"
            ),
            {"now": now},
        )
    engine.dispose()

    _alembic_upgrade_to("0004")

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT kind FROM plugin WHERE slug='legacy-exit'")
        ).one()
    engine.dispose()
    assert row[0] == "exit_strategy"
