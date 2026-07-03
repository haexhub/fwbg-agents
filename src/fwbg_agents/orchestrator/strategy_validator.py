"""Lightweight structural validator for fwbg strategy.json.

The Translator emits strategy.json; this validator catches obvious LLM
mistakes BEFORE we hand the file to fwbg. The full source of truth is
`fwbg.core.config.StrategyConfig` (pydantic), but pulling fwbg in as a
runtime dependency of this repo would be heavy — the Runner remains the
ultimate validator when fwbg starts the backtest.

M5a refactor: `validate_strategy_json` accepts an optional `catalog`
(PluginCatalog) kwarg. When provided AND the relevant catalog category has
at least one entry, plugin names are looked up in the catalog. Otherwise the
M4 frozenset fallback applies — existing call sites and tests work unchanged.
M5b's PluginAuthor extends the catalog without touching this file.

Inline composition (M7): `pipeline`, `model` and `filters` are composed
inline by the Translator from the live plugin catalog — a `pipeline` dict
holds per-phase plugin entries, `model` is a ModelConfig-shaped dict,
`filters` a FilterConfig-shaped dict. Legacy string preset refs remain valid
(checked against `presets` when provided, else the frozensets) so strategies
written before M7 still re-validate during reiterate. `validation` and
`resources` stay preset-string-only by design: the validation protocol is
operator policy, deliberately NOT per-strategy agent output.
"""

from __future__ import annotations

from difflib import get_close_matches
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fwbg_agents.orchestrator.plugin_catalog import PluginCatalog


KNOWN_PIPELINES: frozenset[str] = frozenset({"orb_simple_v1"})
KNOWN_MODELS: frozenset[str] = frozenset({"signal_orb_v1"})
KNOWN_FILTERS: frozenset[str] = frozenset({"orb_scalping_v1"})
KNOWN_VALIDATIONS: frozenset[str] = frozenset({"walk_forward_intraday_v1"})
KNOWN_RESOURCES: frozenset[str] = frozenset({"standard_v1"})

REQUIRED_TOP_LEVEL: tuple[str, ...] = (
    "name",
    "datasource",
    "pipeline",
    "model",
    "filters",
    "validation",
    "resources",
    "timeframe",
    "exit_strategies",
    "tags",
    "hypothesis",
)

# Preset-section fallbacks for legacy string refs. When a live `presets`
# mapping is provided (fetched from fwbg's workspace), it wins.
_PRESET_STRING_FIELDS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("pipeline", "pipelines", KNOWN_PIPELINES),
    ("model", "models", KNOWN_MODELS),
    ("filters", "filters", KNOWN_FILTERS),
    ("validation", "validations", KNOWN_VALIDATIONS),
    ("resources", "resources", KNOWN_RESOURCES),
)

# fwbg pipeline phases valid inside an inline pipeline dict, mapped to the
# catalog category their plugin names are validated against.
_PIPELINE_PHASES: tuple[tuple[str, str], ...] = (
    ("data_loading", "data_loading"),
    ("preprocessing", "preprocessing"),
    ("indicators", "indicators"),
    ("feature_selection", "feature_selection"),
)

_MODEL_ARCHITECTURES = frozenset({"unified", "long_short_separate"})
_TRADE_DIRECTIONS = frozenset({"long", "short"})


class StrategyValidationError(ValueError):
    """Raised when a strategy.json payload fails structural validation."""


def _suggest(value: str, allowed: list[str]) -> str:
    """Render a 'did you mean ...' hint for the error message, or empty."""
    if not allowed:
        return ""
    matches = get_close_matches(value, allowed, n=3, cutoff=0.4)
    if not matches:
        matches = [s for s in allowed if value.lower() in s.lower()][:3]
    if not matches:
        return ""
    return f" did you mean: {matches}?"


def _check_field_with_catalog(
    field: str,
    value: str,
    *,
    catalog: PluginCatalog | None,
    catalog_category: str,
    frozen_fallback: frozenset[str],
) -> None:
    """Catalog-first, frozenset-fallback membership check.

    Catalog wins when it has at least one slug for `catalog_category`; otherwise
    fall back to the M4 frozenset so existing call sites without a catalog (and
    tests) keep working unchanged.
    """
    if catalog is not None and catalog.all_slugs_for(catalog_category):
        allowed = catalog.all_slugs_for(catalog_category)
        if value not in allowed:
            raise StrategyValidationError(
                f"{field}={value!r} is not in the catalog category "
                f"{catalog_category!r}.{_suggest(value, allowed)}"
            )
        return
    if value not in frozen_fallback:
        raise StrategyValidationError(
            f"{field}={value!r} is not in the known catalog "
            f"({sorted(frozen_fallback)}). The Translator must keep the strategy in "
            "PROPOSED and emit a 'needs_plugin' note instead of inventing slugs."
        )


# M5c: plugin-slot list-fields. Unlike `model`, these have NO frozen fallback —
# they're 100% plugin-authored. When no catalog is passed or the catalog category
# is empty, membership is unchecked (lax, M4-compatible).
# Field-name → catalog-category mapping (note: `extra_filters` routes to `filters`).
_PLUGIN_LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("indicators", "indicators"),
    ("feature_selection", "feature_selection"),
    ("preprocessing", "preprocessing"),
    ("extra_filters", "filters"),
)


def _check_plugin_list_field(
    field: str, value: Any, *, catalog: PluginCatalog | None, catalog_category: str
) -> None:
    """Validate one optional plugin-slot list-field.

    Shape rules apply unconditionally:
      - must be a list
      - each entry must be a non-empty str
    Catalog membership is checked only when the catalog has entries for
    `catalog_category` — otherwise lax.
    """
    if not isinstance(value, list):
        raise StrategyValidationError(f"{field} must be a list of strings")
    for i, entry in enumerate(value):
        if not isinstance(entry, str) or not entry:
            raise StrategyValidationError(
                f"{field}[{i}] must be a non-empty string"
            )
    if catalog is None:
        return
    allowed = catalog.all_slugs_for(catalog_category)
    if not allowed:
        return
    for entry in value:
        if entry not in allowed:
            raise StrategyValidationError(
                f"{field} slug {entry!r} is not in the catalog category "
                f"{catalog_category!r}.{_suggest(entry, allowed)}"
            )


def _check_exit_strategies(items: Any, *, catalog: PluginCatalog | None) -> None:
    if not isinstance(items, list) or not items:
        raise StrategyValidationError("exit_strategies must be a non-empty list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise StrategyValidationError(f"exit_strategies[{i}] must be an object")
        if "name" not in item or not isinstance(item["name"], str):
            raise StrategyValidationError(f"exit_strategies[{i}].name is required (str)")
        if "params" not in item or not isinstance(item["params"], dict):
            raise StrategyValidationError(f"exit_strategies[{i}].params is required (object)")
        # Catalog check only kicks in when the catalog has entries — keeps M4
        # shape lax for the no-catalog path.
        if catalog is not None and catalog.all_slugs_for("exit_strategies"):
            allowed = catalog.all_slugs_for("exit_strategies")
            if item["name"] not in allowed:
                raise StrategyValidationError(
                    f"exit_strategies[{i}].name={item['name']!r} is not in the "
                    f"catalog.{_suggest(item['name'], allowed)}"
                )


def _check_preset_string(
    field: str,
    value: str,
    *,
    section: str,
    presets: dict[str, list[str]] | None,
    frozen_fallback: frozenset[str],
) -> None:
    """Legacy preset-ref check: live workspace preset list first, frozenset
    fallback when none was provided (offline / M4 call sites)."""
    allowed = (presets or {}).get(section) or sorted(frozen_fallback)
    if value not in allowed:
        raise StrategyValidationError(
            f"{field}={value!r} is not an available preset in section "
            f"{section!r} ({allowed}).{_suggest(value, list(allowed))}"
        )


def _check_inline_pipeline(value: dict, *, catalog: PluginCatalog | None) -> None:
    """Inline pipeline dict: per-phase lists of {name, params} plugin entries."""
    valid_phases = {phase for phase, _ in _PIPELINE_PHASES}
    unknown = set(value) - valid_phases
    if unknown:
        raise StrategyValidationError(
            f"pipeline has unknown phase keys {sorted(unknown)}; "
            f"valid: {sorted(valid_phases)}"
        )
    if not value.get("indicators"):
        raise StrategyValidationError(
            "pipeline.indicators must contain at least one plugin entry"
        )
    for phase, category in _PIPELINE_PHASES:
        entries = value.get(phase)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise StrategyValidationError(f"pipeline.{phase} must be a list")
        allowed = catalog.all_slugs_for(category) if catalog is not None else []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise StrategyValidationError(
                    f"pipeline.{phase}[{i}] must be an object with name/params"
                )
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise StrategyValidationError(
                    f"pipeline.{phase}[{i}].name is required (str)"
                )
            if "params" in entry and not isinstance(entry["params"], dict):
                raise StrategyValidationError(
                    f"pipeline.{phase}[{i}].params must be an object"
                )
            if allowed and name not in allowed:
                raise StrategyValidationError(
                    f"pipeline.{phase}[{i}].name={name!r} is not in the catalog "
                    f"category {category!r}.{_suggest(name, allowed)}"
                )


def _check_inline_model(value: dict, *, catalog: PluginCatalog | None) -> None:
    """Inline model dict, shaped like fwbg's ModelConfig."""
    mtype = value.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise StrategyValidationError("model.type is required (str)")
    if catalog is not None:
        allowed = catalog.all_slugs_for("models")
        if allowed and mtype not in allowed:
            raise StrategyValidationError(
                f"model.type={mtype!r} is not in the catalog category "
                f"'models'.{_suggest(mtype, allowed)}"
            )
    arch = value.get("architecture")
    if arch is not None and arch not in _MODEL_ARCHITECTURES:
        raise StrategyValidationError(
            f"model.architecture={arch!r} must be one of {sorted(_MODEL_ARCHITECTURES)}"
        )
    directions = value.get("trade_directions")
    if directions is not None and (
        not isinstance(directions, list)
        or not directions
        or not set(directions) <= _TRADE_DIRECTIONS
    ):
        raise StrategyValidationError(
            "model.trade_directions must be a non-empty subset of "
            f"{sorted(_TRADE_DIRECTIONS)}"
        )
    if "hyperparameters" in value and not isinstance(value["hyperparameters"], dict):
        raise StrategyValidationError("model.hyperparameters must be an object")


def _check_tags(tags: Any) -> None:
    if not isinstance(tags, list) or not tags:
        raise StrategyValidationError("tags must be a non-empty list")
    for t in tags:
        if not isinstance(t, str):
            raise StrategyValidationError("tags entries must be strings")


def validate_strategy_json(
    data: dict,
    *,
    catalog: PluginCatalog | None = None,
    presets: dict[str, list[str]] | None = None,
    datasources: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> None:
    """Structural validation. Pass `catalog` to route plugin-name lookups
    through the runtime PluginCatalog, `presets` (section → names, from the
    fwbg workspace) for preset-string refs, and `datasources` (names actually
    configured in fwbg) for the datasource ref; without them the M4 frozenset
    fallbacks apply.
    """
    if not isinstance(data, dict):
        raise StrategyValidationError("payload must be a JSON object")

    missing = [k for k in REQUIRED_TOP_LEVEL if k not in data]
    if missing:
        raise StrategyValidationError(f"missing required keys: {missing}")

    for field in ("datasource", "validation", "resources", "timeframe"):
        if not isinstance(data[field], str):
            raise StrategyValidationError(f"{field} must be a string")

    # Datasource membership is checked ONLY against the live fwbg list —
    # there is deliberately no hardcoded fallback (a frozen name like the old
    # 'forexsb' default guarantees instantly-failing runs on other machines).
    # Without a live list (offline) the ref stays unchecked; the Runner is
    # the ultimate validator.
    if datasources and data["datasource"] not in datasources:
        raise StrategyValidationError(
            f"datasource={data['datasource']!r} is not configured in fwbg "
            f"({datasources}).{_suggest(data['datasource'], list(datasources))}"
        )
    # Timeframe membership only against the live fwbg list (no hardcoded set:
    # the old frozen one silently forbade MINUTE_1/HOUR_4/DAY_1 which fwbg
    # supports). Offline the ref stays unchecked — fwbg 422s bad values.
    if timeframes and data["timeframe"] not in timeframes:
        raise StrategyValidationError(
            f"timeframe={data['timeframe']!r} is not supported by fwbg "
            f"({timeframes}).{_suggest(data['timeframe'], list(timeframes))}"
        )

    # pipeline/model/filters: inline composition (dict) or legacy preset ref
    # (string). validation/resources: preset string only — operator policy.
    if isinstance(data["pipeline"], dict):
        _check_inline_pipeline(data["pipeline"], catalog=catalog)
    elif isinstance(data["pipeline"], str):
        _check_preset_string(
            "pipeline", data["pipeline"],
            section="pipelines", presets=presets, frozen_fallback=KNOWN_PIPELINES,
        )
    else:
        raise StrategyValidationError("pipeline must be an object or a preset name")

    if isinstance(data["model"], dict):
        _check_inline_model(data["model"], catalog=catalog)
    elif isinstance(data["model"], str):
        if catalog is not None and catalog.all_slugs_for("models"):
            _check_field_with_catalog(
                "model", data["model"],
                catalog=catalog, catalog_category="models",
                frozen_fallback=KNOWN_MODELS,
            )
        else:
            _check_preset_string(
                "model", data["model"],
                section="models", presets=presets, frozen_fallback=KNOWN_MODELS,
            )
    else:
        raise StrategyValidationError("model must be an object or a preset name")

    if isinstance(data["filters"], str):
        _check_preset_string(
            "filters", data["filters"],
            section="filters", presets=presets, frozen_fallback=KNOWN_FILTERS,
        )
    elif not isinstance(data["filters"], dict):
        raise StrategyValidationError("filters must be an object or a preset name")

    _check_preset_string(
        "validation", data["validation"],
        section="validations", presets=presets, frozen_fallback=KNOWN_VALIDATIONS,
    )
    _check_preset_string(
        "resources", data["resources"],
        section="resources", presets=presets, frozen_fallback=KNOWN_RESOURCES,
    )

    _check_exit_strategies(data["exit_strategies"], catalog=catalog)
    _check_tags(data["tags"])

    # M5c: optional plugin-slot list-fields. Omitted == empty == valid.
    for field, category in _PLUGIN_LIST_FIELDS:
        if field in data:
            _check_plugin_list_field(
                field, data[field], catalog=catalog, catalog_category=category,
            )

    if not isinstance(data["name"], str) or not data["name"]:
        raise StrategyValidationError("name must be a non-empty string")
    if not isinstance(data["hypothesis"], str) or not data["hypothesis"]:
        raise StrategyValidationError("hypothesis must be a non-empty string")
