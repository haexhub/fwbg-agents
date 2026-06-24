"""PluginAuthor — writes a brand-new plugin in response to an
`add_indicator_request.json` sidecar produced by the Analyst (M5a).

Single attempt: the agent runs once, validates the output, and either
persists `data/plugins/<slug>/v1/{plugin.py, contract.yaml, spec.md}` and
transitions Plugin SPECIFIED → AUTHORED, or fails the AgentRun and raises
PluginAuthorFailed. There is no retry loop in M5b — manual retry only.
"""

from __future__ import annotations

import ast
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.lifecycle import (
    plugin_dir,
    strategy_dir,
    transition_plugin,
)
from fwbg_agents.orchestrator.plugin_catalog import PluginCatalog, load_catalog
from fwbg_agents.orchestrator.plugin_contract import (
    PluginContract,
    PluginKindLit,
    dump_contract,
)
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Plugin,
    PluginState,
    Strategy,
)
from fwbg_agents.tools.llm import default_model

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "plugin_author.md"
_PLUGIN_EXAMPLES_HARD_CAP = 5
_SOURCE_TRUNCATE_CHARS = 4000

# PluginContract.kind / AddIndicator.category are singular; fwbg bundle
# manifests use plural directory names. The mapping is hand-curated because
# English plurals aren't algorithmically reliable.
_CATEGORY_TO_BUNDLE_DIR: dict[str, str] = {
    "indicator": "indicators",
    "model": "models",
    "exit_strategy": "exit_strategies",
    "risk_management": "risk_management",
    "entry_modifier": "entry_modifiers",
    "preprocessing": "preprocessing",
    "feature_selection": "feature_selection",
    "data_loading": "data_loading",
}


class PluginAuthorFailed(RuntimeError):
    """Raised when the PluginAuthor cannot persist a valid plugin (slug
    collision, syntax check failure, or any unrecoverable error)."""


class SyntaxCheck(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    line: int | None = None
    msg: str = ""


class FwbgPluginExample(BaseModel):
    model_config = ConfigDict(frozen=True)
    slug: str
    path: str
    source: str


class PluginAuthorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=2, max_length=64)
    python_code: str = Field(min_length=10)
    contract: PluginContract
    spec_md: str = Field(min_length=80)


def validate_python_syntax(code: str) -> SyntaxCheck:
    """Run ast.parse on `code`. Deterministic — no LLM."""
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return SyntaxCheck(ok=False, line=exc.lineno or 1, msg=str(exc))
    return SyntaxCheck(ok=True)


def _read_plugin_source(bundle_manifest: Path, plural_category: str, slug: str) -> str | None:
    """Read the most likely `plugin.py`-equivalent file for a plugin slug.

    Layout: bundle_dir/<plural_category>/<slug>/{plugin.py, <slug>.py, *.py}.
    Returns None when nothing readable is found; the caller filters those out.
    """
    bundle_dir = bundle_manifest.parent
    slug_dir = bundle_dir / plural_category / slug
    if not slug_dir.is_dir():
        return None

    for filename in ("plugin.py", f"{slug}.py"):
        candidate = slug_dir / filename
        if candidate.is_file():
            try:
                return candidate.read_text()[:_SOURCE_TRUNCATE_CHARS]
            except OSError:
                return None

    # Fall back to the first non-test python file in the slug dir.
    for candidate in sorted(slug_dir.glob("*.py")):
        if candidate.name.startswith("test_"):
            continue
        try:
            return candidate.read_text()[:_SOURCE_TRUNCATE_CHARS]
        except OSError:
            continue
    return None


def get_fwbg_plugin_examples(
    catalog: PluginCatalog,
    *,
    category: PluginKindLit,
    n: int = 3,
) -> list[FwbgPluginExample]:
    """Return up to `min(n, 5)` plugin source samples for the given singular
    category. Values above the hard cap of 5 are silently clamped (with a
    warning). Unreadable plugin dirs are skipped. Unknown category returns []."""
    if n > _PLUGIN_EXAMPLES_HARD_CAP:
        log.warning(
            "get_fwbg_plugin_examples: clamping n=%d to hard cap %d",
            n,
            _PLUGIN_EXAMPLES_HARD_CAP,
        )
        n = _PLUGIN_EXAMPLES_HARD_CAP
    if n <= 0:
        return []

    bundle_dir = _CATEGORY_TO_BUNDLE_DIR.get(category)
    if bundle_dir is None:
        return []

    candidates = catalog.by_category.get(bundle_dir, {})
    out: list[FwbgPluginExample] = []
    for slug in sorted(candidates):
        if len(out) >= n:
            break
        manifest = candidates[slug]
        # Only fwbg-core / fwbg-premium provenance — agent-authored plugins
        # haven't proven themselves yet.
        if manifest.provenance == "agent-authored":
            continue
        source = _read_plugin_source(manifest.source_path, bundle_dir, slug)
        if source is None:
            continue
        out.append(
            FwbgPluginExample(
                slug=slug,
                path=str(manifest.source_path.parent / bundle_dir / slug),
                source=source,
            )
        )
    return out


def _render_strategy_excerpt(parent: Strategy) -> str:
    latest_dir = strategy_dir(parent.slug) / "iteration_001"
    strategy_path = latest_dir / "strategy.json"
    if not strategy_path.is_file():
        return "(no strategy.json on disk)"
    try:
        data = json.loads(strategy_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "(unreadable strategy.json)"
    excerpt_keys = ("name", "pipeline", "model", "filters", "validation", "exit_strategies")
    excerpt = {k: data.get(k) for k in excerpt_keys if k in data}
    return json.dumps(excerpt, indent=2)


def _render_prompt(template: str, *, strategy_excerpt: str, sidecar_json: str) -> str:
    return (
        template.replace("{{ strategy_excerpt }}", strategy_excerpt)
        .replace("{{ sidecar_json }}", sidecar_json)
    )


class PluginAuthor:
    def __init__(
        self,
        session: AsyncSession,
        *,
        model: Model | None = None,
        prompt_path: Path | None = None,
    ):
        self.session = session
        self.model = model if model is not None else default_model()
        self.prompt_path = prompt_path or _PROMPT_PATH

    async def run_fresh(
        self,
        *,
        sidecar_path: Path,
        parent_strategy: Strategy,
    ) -> int:
        """Write a new plugin from the sidecar request. Returns plugin.id."""
        now = datetime.now(UTC)
        ar = AgentRun(
            agent_name="plugin_author",
            status=AgentRunStatus.RUNNING.value,
            strategy_id=parent_strategy.id,
            input_artifact_path=str(sidecar_path),
            started_at=now,
            created_at=now,
        )
        self.session.add(ar)
        await self.session.commit()
        await self.session.refresh(ar)

        try:
            if not sidecar_path.is_file():
                raise FileNotFoundError(f"missing sidecar at {sidecar_path}")
            sidecar_data = json.loads(sidecar_path.read_text())

            template = self.prompt_path.read_text()
            system_prompt = _render_prompt(
                template,
                strategy_excerpt=_render_strategy_excerpt(parent_strategy),
                sidecar_json=json.dumps(sidecar_data, indent=2),
            )

            catalog = await load_catalog(self.session)

            agent: Agent[None, PluginAuthorResult] = Agent(
                self.model,
                output_type=PluginAuthorResult,
                system_prompt=system_prompt,
            )

            @agent.tool_plain
            def get_fwbg_plugin_examples_tool(
                category: PluginKindLit, n: int = 3
            ) -> list[FwbgPluginExample]:
                """Fetch up to N (default 3, max 5) existing fwbg plugins."""
                return get_fwbg_plugin_examples(catalog, category=category, n=n)

            @agent.tool_plain
            def validate_python_syntax_tool(code: str) -> SyntaxCheck:
                """Ast-parse the proposed plugin code."""
                return validate_python_syntax(code)

            t0 = time.monotonic()
            result = await agent.run("Emit the PluginAuthorResult now.")
            latency_ms = int((time.monotonic() - t0) * 1000)

            usage = result.usage
            self.session.add(
                LlmCall(
                    agent_run_id=ar.id,
                    model=getattr(self.model, "model_name", "unknown"),
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                    latency_ms=latency_ms,
                    created_at=datetime.now(UTC),
                )
            )
            await self.session.commit()

            output = result.output
            slug = output.slug

            # Slug collision against current catalog (verified + adopted plugins
            # + fwbg-discovered plugins of any category).
            if any(
                slug in slugs for slugs in catalog.by_category.values()
            ):
                raise PluginAuthorFailed(
                    f"slug {slug!r} already taken in the catalog; pick again"
                )

            # Also guard against any DB plugin in any state (so two parallel
            # author runs cannot land the same slug).
            from sqlalchemy import select

            existing = (
                await self.session.execute(
                    select(Plugin).where(Plugin.slug == slug)
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise PluginAuthorFailed(
                    f"slug {slug!r} already exists as plugin id={existing.id}"
                )

            # Syntax check — if the LLM emitted broken code, fail loudly.
            check = validate_python_syntax(output.python_code)
            if not check.ok:
                raise PluginAuthorFailed(
                    f"python_code failed syntax check at line {check.line}: {check.msg}"
                )

            # Persist artefacts.
            target_dir = plugin_dir(slug) / "v1"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "plugin.py").write_text(output.python_code)
            dump_contract(output.contract, target_dir / "contract.yaml")
            (target_dir / "spec.md").write_text(output.spec_md)

            # Insert Plugin row in SPECIFIED, then transition to AUTHORED so the
            # transition log shows the SPECIFIED→AUTHORED edge.
            now2 = datetime.now(UTC)
            plugin = Plugin(
                slug=slug,
                current_state=PluginState.SPECIFIED.value,
                kind=output.contract.kind,
                spec_path=str(target_dir / "spec.md"),
                contract_path=str(target_dir / "contract.yaml"),
                created_at=now2,
                updated_at=now2,
            )
            self.session.add(plugin)
            await self.session.flush()
            ar.plugin_id = plugin.id

            await transition_plugin(
                self.session,
                plugin,
                PluginState.AUTHORED,
                reason="plugin_author",
                payload={
                    "request_path": str(sidecar_path),
                    "request_strategy_id": parent_strategy.id,
                    "examples_count": _PLUGIN_EXAMPLES_HARD_CAP,
                },
                created_by="plugin_author",
            )

            ar.status = AgentRunStatus.DONE.value
            ar.ended_at = datetime.now(UTC)
            ar.output_artifact_path = str(target_dir / "contract.yaml")
            await self.session.commit()
            await self.session.refresh(plugin)
            return plugin.id

        except Exception as exc:
            ar.status = AgentRunStatus.FAILED.value
            ar.ended_at = datetime.now(UTC)
            ar.error = str(exc)
            await self.session.commit()
            raise
