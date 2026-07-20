"""Regression tests for deployment-critical contents of the agents image."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_image_copies_and_runs_trial_stat_backfill() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()

    copy = "COPY scripts/backfill_trial_stats.py scripts/backfill_trial_stats.py"
    migrate = "alembic upgrade head"
    backfill = "python scripts/backfill_trial_stats.py"
    serve = "uvicorn fwbg_agents.main:app"

    assert copy in dockerfile
    assert "scripts" not in dockerignore
    assert "!scripts/backfill_trial_stats.py" in dockerignore
    assert dockerfile.index(migrate) < dockerfile.index(backfill) < dockerfile.index(serve)
