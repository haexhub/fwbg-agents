"""Runtime, file-backed per-agent overrides (model + persona/system prompt).

Mirrors the criteria-as-files pattern (``data/criteria/*``): no DB migration,
just a JSON file plus per-agent markdown under the data dir. A missing/empty
override means "use the built-in default" — i.e. the role default model from
``settings`` and the agent's bundled prompt markdown.

Resolution is read by :mod:`fwbg_agents.tools.llm` (``model_for`` /
``prompt_path_for``) and the ``/agents/config`` API. Changes take effect for
the next agent run; in-flight runs are not hot-reloaded.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fwbg_agents.config import settings

log = logging.getLogger(__name__)

# LLM-driven roles whose model + persona are user-configurable. The
# deterministic agents (Runner, Calibrator, PluginEvaluator) are intentionally
# excluded — they make no model choice.
CONFIGURABLE_AGENTS: tuple[str, ...] = (
    "researcher",
    "translator",
    "analyst",
    "paper_analyst",
    "plugin_planner",
    "plugin_implementer",
)

# Built-in default persona files. researcher/translator/analyst/paper_analyst
# live next to the agent modules; the plugin authors share the repo-root prompt.
_AGENTS_PROMPTS = Path(__file__).resolve().parents[1] / "agents" / "prompts"
_REPO_PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
DEFAULT_PROMPT_PATHS: dict[str, Path] = {
    "researcher": _AGENTS_PROMPTS / "researcher.md",
    "translator": _AGENTS_PROMPTS / "translator.md",
    "analyst": _AGENTS_PROMPTS / "analyst.md",
    "paper_analyst": _AGENTS_PROMPTS / "paper_analyst.md",
    "plugin_planner": _REPO_PROMPTS / "plugin_authoring.md",
    "plugin_implementer": _REPO_PROMPTS / "plugin_authoring.md",
}


def _config_file() -> Path:
    return settings.data_dir / "agent_configs.json"


def _prompts_dir() -> Path:
    return settings.data_dir / "agent_prompts"


def _load() -> dict[str, dict]:
    path = _config_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict]) -> None:
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# ── Model override ─────────────────────────────────────────────────────────


def get_model_override(agent_name: str) -> str | None:
    entry = _load().get(agent_name) or {}
    return entry.get("model") or None


def set_model_override(agent_name: str, model: str | None) -> None:
    data = _load()
    entry = dict(data.get(agent_name) or {})
    if model:
        entry["model"] = model
    else:
        entry.pop("model", None)
    if entry:
        data[agent_name] = entry
    else:
        data.pop(agent_name, None)
    _save(data)


# ── Prompt / persona override ──────────────────────────────────────────────


def prompt_override_path(agent_name: str) -> Path:
    return _prompts_dir() / f"{agent_name}.md"


def get_prompt_override(agent_name: str) -> str | None:
    path = prompt_override_path(agent_name)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def set_prompt_override(agent_name: str, text: str | None) -> None:
    path = prompt_override_path(agent_name)
    if text and text.strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    elif path.is_file():
        path.unlink()


def default_prompt(agent_name: str) -> str:
    path = DEFAULT_PROMPT_PATHS[agent_name]
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        # A missing bundled prompt (broken image packaging) must degrade to an
        # empty default, not 500 the whole /agents/config endpoint.
        log.warning("default prompt for %s missing at %s", agent_name, path)
        return ""


def effective_prompt(agent_name: str) -> str:
    """The persona the agent will actually use: override if set, else default."""
    return get_prompt_override(agent_name) or default_prompt(agent_name)
