"""Secrets management endpoints — GET/PUT /agents/secrets.

Keys are stored in ``data/secrets.json`` (file-backed, no DB migration).
The API never returns actual key values, only set/not-set status.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fwbg_agents.tools.secrets import KNOWN_KEYS, list_key_status, set_secret

router = APIRouter(tags=["secrets"])


class SecretsStatus(BaseModel):
    """Response model for GET /agents/secrets — set/not-set flags per key."""

    keys: dict[str, dict[str, bool]]


class SecretsUpdate(BaseModel):
    """Request body for PUT /agents/secrets — values to store or clear."""

    tavily: str | None = None
    brave: str | None = None


@router.get("/agents/secrets", response_model=SecretsStatus)
def get_secrets() -> SecretsStatus:
    """Return set/not-set status for all known API keys.

    Values are never returned — only whether each key is configured.
    """
    return SecretsStatus(keys=list_key_status())


@router.put("/agents/secrets", response_model=SecretsStatus)
def put_secrets(body: SecretsUpdate) -> SecretsStatus:
    """Store or clear one or more API keys.

    Pass an empty string or null to clear a key (restores env-var fallback).
    Changes take effect for the next agent run without a service restart.
    """
    updates = body.model_dump(exclude_none=False)
    errors: list[str] = []
    for key, value in updates.items():
        if key not in KNOWN_KEYS:
            continue
        try:
            set_secret(key, value)
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    return SecretsStatus(keys=list_key_status())
