"""Criteria endpoints — list / fetch / edit per-asset-class success thresholds.

Calibration is exposed via POST /calibrate which kicks off a background pass
and returns the persisted calibration_run id immediately. The Calibrator
itself is pure Python (no LLM), see agents.calibrator.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select

from fwbg_agents.agents.calibrator import CalibrationResult, calibrate
from fwbg_agents.config import settings
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import CalibrationRun
from fwbg_agents.tools.fwbg_client import FwbgClient, FwbgClientError

log = logging.getLogger(__name__)

router = APIRouter(tags=["criteria"])


VALID_ASSET_CLASS_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ_")


def _validate_asset_class(asset_class: str) -> str:
    """Enforce upper-case alphanumeric. Prevents path traversal in file lookup."""
    if not asset_class or not all(c in VALID_ASSET_CLASS_CHARS for c in asset_class):
        raise HTTPException(status_code=400, detail="invalid asset_class")
    return asset_class


def _criteria_path(asset_class: str) -> Path:
    p: Path = settings.criteria_dir / f"{asset_class}.yaml"
    # Guard: ensure resolved path stays inside criteria_dir.
    resolved = p.resolve()
    if not str(resolved).startswith(str(settings.criteria_dir.resolve())):
        raise HTTPException(status_code=400, detail="invalid asset_class")
    return p


@router.get("/criteria")
async def list_criteria() -> dict[str, Any]:
    """List all asset classes that currently have a criteria YAML on disk."""
    if not settings.criteria_dir.is_dir():
        return {"asset_classes": [], "baseline": None}
    asset_classes = sorted(
        p.stem for p in settings.criteria_dir.glob("*.yaml") if not p.stem.startswith("_")
    )
    baseline_path = settings.criteria_dir / "_calibration_baseline.json"
    baseline: dict[str, Any] | None = None
    if baseline_path.is_file():
        try:
            baseline = json.loads(baseline_path.read_text())
        except (OSError, json.JSONDecodeError):
            baseline = None
    return {"asset_classes": asset_classes, "baseline": baseline}


@router.get("/criteria/{asset_class}")
async def get_criteria(asset_class: str) -> dict[str, Any]:
    """Retrieve the criteria YAML for a given asset class."""
    asset_class = _validate_asset_class(asset_class)
    path = _criteria_path(asset_class)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no criteria for {asset_class}")
    yaml_text = path.read_text()
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"corrupt YAML: {exc}") from exc
    # Both shapes: parsed dict for programmatic clients, raw yaml_text for the
    # editor (lets the dashboard avoid carrying a YAML serializer in the bundle).
    return {
        "asset_class": asset_class,
        "criteria": data,
        "yaml_text": yaml_text,
        "path": str(path),
    }


class CriteriaUpdate(BaseModel):
    """Payload for PUT /criteria/{asset_class}. We accept either parsed JSON
    or a raw YAML string — the dashboard sends YAML text from a textarea."""

    criteria: dict[str, Any] | None = None
    yaml_text: str | None = None


def _validate_criteria_schema(data: Any) -> dict[str, Any]:
    """Minimal structural check on the section-6.1 schema.

    We do NOT type-check threshold strings (the YAML supports human-edited
    comparator expressions like '>= 0.8' which evaluators parse downstream).
    """
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="criteria root must be a mapping")
    for required in ("backtest_to_paper", "paper_to_live"):
        if required not in data:
            raise HTTPException(status_code=422, detail=f"missing top-level key: {required}")
        if not isinstance(data[required], dict):
            raise HTTPException(status_code=422, detail=f"{required} must be a mapping")
    btp = data["backtest_to_paper"]
    for section in ("required_all", "required_any", "hard_blockers"):
        if section in btp and not isinstance(btp[section], list):
            raise HTTPException(
                status_code=422, detail=f"backtest_to_paper.{section} must be a list"
            )
    return data


@router.put("/criteria/{asset_class}")
async def put_criteria(asset_class: str, body: CriteriaUpdate = Body(...)) -> dict[str, Any]:
    """Create or replace the criteria YAML for a given asset class."""
    asset_class = _validate_asset_class(asset_class)
    if body.criteria is None and body.yaml_text is None:
        raise HTTPException(status_code=400, detail="provide either criteria or yaml_text")

    if body.yaml_text is not None:
        try:
            parsed = yaml.safe_load(body.yaml_text)
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=422, detail=f"invalid YAML: {exc}") from exc
    else:
        parsed = body.criteria

    validated = _validate_criteria_schema(parsed)
    path = _criteria_path(asset_class)
    settings.criteria_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(validated, sort_keys=False, allow_unicode=True))
    return {"asset_class": asset_class, "path": str(path), "ok": True}


class CalibrationRunOut(BaseModel):
    """Response schema for a completed calibration run."""

    id: int
    ran_at: datetime
    runs_scanned: int
    runs_with_elite: int
    asset_classes_processed: dict[str, int]
    baseline_path: str


async def _persist_calibration(result: CalibrationResult) -> int:
    """Insert a calibration_run row and return its id."""
    async with SessionLocal() as session:
        row = CalibrationRun(
            ran_at=result.ran_at,
            runs_scanned=result.runs_scanned,
            runs_with_elite=result.runs_with_elite,
            asset_classes_processed=dict(result.asset_classes),
            baseline_path=str(result.baseline_path),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


@router.post("/calibrate")
async def trigger_calibration() -> dict[str, Any]:
    """Run a calibration pass. M1 runs it inline (sync stats are fast);
    we still return immediately because the body is small and the work is
    deterministic — there is no progress to stream yet."""
    started_at = datetime.now(UTC)
    log.info("calibrate: starting at %s", started_at.isoformat())

    # Fetch symbol→asset_class map from fwbg (single source of truth).
    # If fwbg is unreachable we log a warning and proceed with an empty map
    # (all symbols fall back to "FOREX") so calibration still runs.
    symbol_asset_class: dict[str, str] = {}
    client = FwbgClient(base_url=settings.fwbg_api_url, api_key=settings.fwbg_api_key)
    try:
        assets = await client.get_assets()
        symbol_asset_class = {
            a["symbol"].upper(): a["asset_class"]
            for a in assets
            if "symbol" in a and "asset_class" in a
        }
        log.info("calibrate: loaded %d symbol→class mappings from fwbg", len(symbol_asset_class))
    except FwbgClientError as exc:
        log.warning(
            "calibrate: could not reach fwbg asset registry (%s); classifying all as FOREX",
            exc,
        )
    finally:
        await client.aclose()

    # Calibrator does plain file I/O; run it on the threadpool to keep the
    # event loop free if the test_results dir is large.
    result = await asyncio.to_thread(calibrate, symbol_asset_class=symbol_asset_class)
    run_id = await _persist_calibration(result)
    log.info(
        "calibrate: id=%d runs_scanned=%d runs_with_elite=%d asset_classes=%s",
        run_id,
        result.runs_scanned,
        result.runs_with_elite,
        result.asset_classes,
    )
    return {
        "id": run_id,
        "ran_at": result.ran_at.isoformat(),
        "runs_scanned": result.runs_scanned,
        "runs_with_elite": result.runs_with_elite,
        "asset_classes_processed": dict(result.asset_classes),
        "baseline_path": str(result.baseline_path),
        "seeded_criteria_files": [str(p) for p in result.seeded_criteria_files],
        "preserved_criteria_files": [str(p) for p in result.preserved_criteria_files],
    }


@router.get("/calibrate/runs")
async def list_calibration_runs(limit: int = 20) -> dict[str, Any]:
    """Most-recent calibration runs — for dashboard history view."""
    limit = max(1, min(limit, 200))
    async with SessionLocal() as session:
        stmt = select(CalibrationRun).order_by(desc(CalibrationRun.ran_at)).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
    return {
        "runs": [
            {
                "id": r.id,
                "ran_at": r.ran_at.isoformat(),
                "runs_scanned": r.runs_scanned,
                "runs_with_elite": r.runs_with_elite,
                "asset_classes_processed": r.asset_classes_processed,
                "baseline_path": r.baseline_path,
            }
            for r in rows
        ]
    }
