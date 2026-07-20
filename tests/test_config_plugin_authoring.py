"""M5d: tests for the plugin-authoring fields on Settings (Planner/Implementer split)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fwbg_agents.config import Settings


def test_defaults_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh Settings() returns documented defaults for plugin-authoring fields."""
    for var in ("PLUGIN_PLANNER_MODEL", "PLUGIN_IMPLEMENTER_MODEL", "PLUGIN_IMPL_MAX_ROUNDS"):
        monkeypatch.delenv(var, raising=False)

    s = Settings(_env_file=None)
    assert s.plugin_planner_model == "claude-opus-4-8"
    assert s.plugin_implementer_model == "claude-sonnet-5"
    assert s.plugin_impl_max_rounds == 5


def test_env_override_planner_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLUGIN_PLANNER_MODEL", "claude-sonnet-4-6")
    s = Settings(_env_file=None)
    assert s.plugin_planner_model == "claude-sonnet-4-6"


def test_env_override_implementer_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLUGIN_IMPLEMENTER_MODEL", "claude-haiku-4-5-20251001")
    s = Settings(_env_file=None)
    assert s.plugin_implementer_model == "claude-haiku-4-5-20251001"


def test_max_rounds_overridable_in_valid_range(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "5", "10", "20"):
        monkeypatch.setenv("PLUGIN_IMPL_MAX_ROUNDS", value)
        s = Settings(_env_file=None)
        assert s.plugin_impl_max_rounds == int(value)


def test_max_rounds_below_one_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLUGIN_IMPL_MAX_ROUNDS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_max_rounds_above_twenty_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLUGIN_IMPL_MAX_ROUNDS", "21")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
