"""File-backed secrets store for runtime API keys.

Mirrors the agent_config.py pattern: persists to ``data/secrets.json``,
reads at call-time (not import-time), env-vars are the fallback when no
file entry exists. Key values are never returned over the API — only
set/not-set status is exposed.

Env-variable fallback mapping:
    tavily → TAVILY_API_KEY
    brave  → BRAVE_API_KEY
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fwbg_agents.config import settings

KNOWN_KEYS: tuple[str, ...] = ("tavily", "brave")

_ENV_FALLBACK: dict[str, str] = {
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_API_KEY",
}


def _secrets_file() -> Path:
    return settings.data_dir / "secrets.json"


def _load() -> dict[str, str]:
    path = _secrets_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, str]) -> None:
    path = _secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def get_secret(key: str) -> str | None:
    """Return the secret for *key*, reading from file first then env fallback.

    Returns None if not configured anywhere. Reads from disk on every call
    so updates via PUT /agents/secrets take effect without a restart.
    """
    stored = _load().get(key)
    if stored:
        return stored
    env_var = _ENV_FALLBACK.get(key)
    return os.environ.get(env_var) if env_var else None


def set_secret(key: str, value: str | None) -> None:
    """Store or clear *key* in the secrets file.

    Passing None or an empty string removes the key so the env fallback
    is used again.
    """
    if key not in KNOWN_KEYS:
        raise ValueError(f"unknown secret key {key!r}; valid keys: {KNOWN_KEYS}")
    data = _load()
    if value and value.strip():
        data[key] = value.strip()
    else:
        data.pop(key, None)
    _save(data)


def list_key_status() -> dict[str, dict[str, bool]]:
    """Return set/not-set status for all known keys without exposing values."""
    stored = _load()
    result: dict[str, dict[str, bool]] = {}
    for key in KNOWN_KEYS:
        env_var = _ENV_FALLBACK.get(key)
        is_set = bool(
            stored.get(key)
            or (env_var and os.environ.get(env_var))
        )
        result[key] = {"set": is_set}
    return result
