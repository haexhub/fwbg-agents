"""M4b: tests for the researcher_fanout_n field on Settings."""

from __future__ import annotations

import pytest

from fwbg_agents.config import Settings


def test_fanout_n_from_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCHER_FANOUT_N", "3")
    s = Settings(_env_file=None)
    assert s.researcher_fanout_n == 3
