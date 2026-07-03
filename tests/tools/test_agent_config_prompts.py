"""default_prompt() must degrade gracefully when a bundled prompt file is
missing (broken image packaging) instead of 500ing /agents/config."""

from pathlib import Path

from fwbg_agents.tools import agent_config


def test_default_prompt_missing_file_returns_empty(monkeypatch):
    monkeypatch.setitem(
        agent_config.DEFAULT_PROMPT_PATHS,
        "plugin_planner",
        Path("/nonexistent/prompts/plugin_authoring.md"),
    )
    assert agent_config.default_prompt("plugin_planner") == ""


def test_default_prompt_reads_bundled_file():
    text = agent_config.default_prompt("researcher")
    assert "Researcher" in text
