"""LLM model factory.

Uses pydantic-ai for provider-neutral agent definitions. The default model is
Anthropic Claude routed through haex-claude-proxy (subscription pricing).
Gemini models are also selectable per-agent, called directly against Google's
API (own billing, own GOOGLE_API_KEY) rather than through haex-claude-proxy.
"""

import logging
import time
from pathlib import Path

from anthropic import Anthropic, AsyncAnthropic
from google import genai
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider

from fwbg_agents.config import settings
from fwbg_agents.tools.secrets import get_secret

log = logging.getLogger(__name__)

# Claude models selectable per agent via /agents/config. All route through the
# same haex-claude-proxy, so no extra API keys are needed to switch between
# them — listed live from the proxy's own GET /v1/models (see
# list_claude_models) rather than duplicated here. Anthropic has no live
# model-listing endpoint reachable via OAuth, so haex-claude-proxy maintains
# and validates that list itself; this just relays it.
_CLAUDE_MODELS_CACHE_TTL_SECONDS = 300
_claude_models_cache: tuple[float, list[str]] | None = None


def list_claude_models() -> list[str]:
    """Claude models haex-claude-proxy currently advertises and accepts.

    Cached briefly since this runs on every GET /agents/config. Falls back to
    the last successful list (or `[]`) if the proxy is unreachable.
    """
    global _claude_models_cache
    now = time.monotonic()
    if _claude_models_cache is not None:
        cached_at, cached_models = _claude_models_cache
        if now - cached_at < _CLAUDE_MODELS_CACHE_TTL_SECONDS:
            return cached_models

    try:
        client = Anthropic(
            base_url=settings.anthropic_base_url,
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        models = sorted(m.id for m in client.models.list())
    except Exception as exc:
        log.warning("Claude ListModels call failed: %s", exc)
        return _claude_models_cache[1] if _claude_models_cache else []

    _claude_models_cache = (now, models)
    return models


# Gemini models are not hardcoded — listed live from Google's API (see
# list_gemini_models) so newly released models show up without a code change.
_GEMINI_MODELS_CACHE_TTL_SECONDS = 300
_gemini_models_cache: tuple[float, str, list[str]] | None = None

# Model-name prefixes Google's API serves. Not all of them start with "gemini-"
# (Gemma, Deep Research, Antigravity, and the media generators are all listed by
# the same ListModels call), and every one of them must be called against
# Google's API directly — haex-claude-proxy only serves Claude, so routing a
# Google name there fails with HTTP 404.
_GOOGLE_MODEL_PREFIXES = (
    "gemini-",
    "gemma-",
    "deep-research-",
    "antigravity-",
    "lyria-",
    "nano-banana-",
    "veo-",
)

# Google advertises `generateContent` for its image/video/music/speech
# generators and its robotics/computer-use previews too, but none of them can
# drive an agent (text in, structured text out). Excluded from the per-agent
# model picker so an operator cannot select one.
_NON_CHAT_MODEL_MARKERS = (
    "-image",
    "-tts",
    "-computer-use",
    "robotics",
    "lyria-",
    "nano-banana-",
    "veo-",
)


def is_google_model(model_name: str) -> bool:
    """True if Google's API serves this model (never haex-claude-proxy)."""
    return model_name.startswith(_GOOGLE_MODEL_PREFIXES)


def _is_chat_capable(model_name: str) -> bool:
    """True if the model generates text, i.e. can back an agent."""
    return not any(marker in model_name for marker in _NON_CHAT_MODEL_MARKERS)


def list_gemini_models() -> list[str]:
    """Gemini models the configured API key can actually call right now.

    Queries Google's ListModels endpoint directly instead of keeping a
    hand-maintained list in sync with what Google ships. Returns `[]` if no
    "google" secret is configured (GET/PUT /agents/secrets, env fallback
    GOOGLE_API_KEY) or if the listing call fails. Cached briefly since this
    runs on every GET /agents/config.

    Media generators and robotics/computer-use previews are dropped even though
    Google advertises `generateContent` for them — see `_is_chat_capable`.
    """
    global _gemini_models_cache
    api_key = get_secret("google")
    if api_key is None:
        return []

    now = time.monotonic()
    if _gemini_models_cache is not None:
        cached_at, cached_key, cached_models = _gemini_models_cache
        if cached_key == api_key and now - cached_at < _GEMINI_MODELS_CACHE_TTL_SECONDS:
            return cached_models

    try:
        client = genai.Client(api_key=api_key)
        models = []
        for model in client.models.list():
            if not model.name or "generateContent" not in (model.supported_actions or []):
                continue
            model_id = model.name.removeprefix("models/")
            if _is_chat_capable(model_id):
                models.append(model_id)
        models.sort()
    except Exception as exc:
        log.warning("Gemini ListModels call failed: %s", exc)
        return _gemini_models_cache[2] if _gemini_models_cache else []

    _gemini_models_cache = (now, api_key, models)
    return models


def _build_model(model_name: str) -> AnthropicModel:
    """Construct an AnthropicModel with project-configured timeout and retry settings."""
    # Own the Anthropic client so we control both the per-request timeout and
    # the retry budget. The SDK default (max_retries=2 = 3 attempts) turned a
    # too-short 120s timeout into ~6min stacked failures on every long Opus
    # call. A generous timeout lets a legitimately long generation finish;
    # llm_max_retries bounds a wedged-proxy hang to a small multiple of it.
    client = AsyncAnthropic(
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    provider = AnthropicProvider(anthropic_client=client)
    return AnthropicModel(
        model_name=model_name,
        provider=provider,
        settings=AnthropicModelSettings(timeout=settings.llm_timeout_seconds),
    )


def _build_google_model(model_name: str) -> GoogleModel:
    """Construct a GoogleModel calling Gemini's API directly (own API key, own billing)."""
    api_key = get_secret("google")
    if api_key is None:
        raise RuntimeError(
            "No Gemini API key configured; set it via PUT /agents/secrets "
            "(key 'google') or the GOOGLE_API_KEY environment variable."
        )
    provider = GoogleProvider(api_key=api_key)
    return GoogleModel(
        model_name=model_name,
        provider=provider,
        settings=GoogleModelSettings(timeout=settings.llm_timeout_seconds),
    )


def _build_model_for_name(model_name: str) -> Model:
    """Dispatch to the right provider's model factory based on the model name."""
    if is_google_model(model_name):
        return _build_google_model(model_name)
    return _build_model(model_name)


def role_default_model(agent_name: str) -> str:
    """Built-in default model for an agent, before any runtime override.

    Preserves the historical per-role split (Planner stronger, Implementer
    weaker); everything else falls back to the global ``anthropic_model``.
    """
    if agent_name == "plugin_planner":
        return settings.plugin_planner_model
    if agent_name == "plugin_implementer":
        return settings.plugin_implementer_model
    return settings.anthropic_model


def model_name_for(agent_name: str) -> str:
    """Effective model name: runtime override if set, else the role default."""
    from fwbg_agents.tools import agent_config

    return agent_config.get_model_override(agent_name) or role_default_model(agent_name)


def model_for(agent_name: str) -> Model:
    """Model for a given agent, honoring its runtime override (Claude or Gemini)."""
    return _build_model_for_name(model_name_for(agent_name))


def tool_callback_headers(agent_run_id: int) -> dict[str, str]:
    """Extra headers that opt an LLM call into haex-claude-proxy's MCP tool
    bridge (see api/internal_tools.py + orchestrator/tool_registry.py).

    Returns `{}` — inert, byte-for-byte today's behavior — when
    ``internal_tool_exec_key`` is unset (the default). When set, the proxy
    forwards these as `X-Tool-Callback-Url` / `X-Tool-Callback-Token` to the
    spawned MCP bridge, which POSTs tool calls back to
    `{self_base_url}/internal/tool-exec/{agent_run_id}`.
    """
    if settings.internal_tool_exec_key is None:
        return {}
    return {
        "X-Tool-Callback-Url": f"{settings.self_base_url}/internal/tool-exec/{agent_run_id}",
        "X-Tool-Callback-Token": settings.internal_tool_exec_key,
    }


def prompt_path_for(agent_name: str, default_path: Path) -> Path:
    """Override persona file if one exists on disk, else the bundled default."""
    from fwbg_agents.tools import agent_config

    override = agent_config.prompt_override_path(agent_name)
    return override if override.is_file() else default_path


def default_model() -> AnthropicModel:
    """Claude via haex-claude-proxy (global default model)."""
    return _build_model(settings.anthropic_model)


async def ping() -> dict[str, object]:
    """Minimal round-trip to verify the proxy is reachable and routing."""
    from pydantic_ai import Agent

    agent = Agent(default_model(), system_prompt="Reply with exactly one word.")
    result = await agent.run("Reply with the single word: pong")
    text = result.output.strip()
    # pydantic-ai 2.0: usage is a property with input_tokens/output_tokens
    # (was a callable returning request_tokens/response_tokens pre-2.0).
    usage = result.usage
    return {
        "ok": "pong" in text.lower(),
        "model": settings.anthropic_model,
        "reply": text,
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
    }
