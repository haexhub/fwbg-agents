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
            raise StrategyValidationError(f"{field}[{i}] must be a non-empty string")
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
    """Validate the exit_strategies list structure and catalog membership."""
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
            _check_exit_params_against_schema(i, item, catalog=catalog)


def _check_exit_params_against_schema(i: int, item: dict, *, catalog: PluginCatalog) -> None:
    """Validate choice-typed exit params against the plugin's param schema.

    An invalid choice value is exactly the class of error that silently
    corrupted run 20260715_042300_0b19fe: `sl_level: "range"` (not a valid
    choice for orb_based) made fwbg resolve a range-HEIGHT column as an
    absolute SL PRICE — shorts exited instantly at a phantom price and booked
    the full entry price as profit. fwbg now rejects such values at
    simulation time; this check moves the failure to translation time so the
    Translator gets immediate feedback instead of a dead backtest run.

    Only params the schema declares with `choices` are checked; everything
    else stays lax (fwbg's pydantic config is the ultimate validator). An
    empty schema (older fwbg without param_schema in GET /api/plugins)
    disables the check for that plugin.
    """
    manifest = catalog.get("exit_strategies", item["name"])
    if manifest is None or not manifest.param_schema:
        return
    for key, value in item["params"].items():
        spec = manifest.param_schema.get(key)
        if not isinstance(spec, dict):
            continue
        choices = spec.get("choices")
        if choices and value not in choices:
            raise StrategyValidationError(
                f"exit_strategies[{i}].params.{key}={value!r} is not a valid "
                f"choice for exit strategy {item['name']!r} "
                f"(allowed: {list(choices)})."
                f"{_suggest(str(value), [str(c) for c in choices])}"
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
            f"pipeline has unknown phase keys {sorted(unknown)}; valid: {sorted(valid_phases)}"
        )
    if not value.get("indicators"):
        raise StrategyValidationError("pipeline.indicators must contain at least one plugin entry")
    phase_names: dict[str, set[str]] = {}
    for phase, category in _PIPELINE_PHASES:
        entries = value.get(phase)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise StrategyValidationError(f"pipeline.{phase} must be a list")
        allowed = catalog.all_slugs_for(category) if catalog is not None else []
        names: set[str] = set()
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise StrategyValidationError(
                    f"pipeline.{phase}[{i}] must be an object with name/params"
                )
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise StrategyValidationError(f"pipeline.{phase}[{i}].name is required (str)")
            if "params" in entry and not isinstance(entry["params"], dict):
                raise StrategyValidationError(f"pipeline.{phase}[{i}].params must be an object")
            if allowed and name not in allowed:
                raise StrategyValidationError(
                    f"pipeline.{phase}[{i}].name={name!r} is not in the catalog "
                    f"category {category!r}.{_suggest(name, allowed)}"
                )
            names.add(name)
        phase_names[phase] = names

    # fwbg's PipelineRunner resolves depends_on by short name against the
    # OTHER plugins configured in the SAME phase — catch an incomplete chain
    # here, at translation time, instead of one missing dependency per failed
    # backtest run.
    if catalog is not None:
        for phase, category in _PIPELINE_PHASES:
            names = phase_names.get(phase, set())
            for name in names:
                manifest = catalog.get(category, name)
                if manifest is None:
                    continue
                missing = [dep for dep in manifest.depends_on if dep not in names]
                if missing:
                    raise StrategyValidationError(
                        f"pipeline.{phase} plugin {name!r} depends on {missing!r}, "
                        f"which {'is' if len(missing) == 1 else 'are'} not in "
                        f"pipeline.{phase}. Add {missing!r} to pipeline.{phase}."
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
            f"model.trade_directions must be a non-empty subset of {sorted(_TRADE_DIRECTIONS)}"
        )
    if "hyperparameters" in value and not isinstance(value["hyperparameters"], dict):
        raise StrategyValidationError("model.hyperparameters must be an object")


def signal_model_has_source(data: dict) -> bool:
    """Whether an inline `type: "signal"` model has a usable entry-signal source.

    Callers MUST only apply this to inline signal models (model is a dict with
    type == "signal"); it is meaningless for preset-string models whose source
    is baked into the preset.

    Mirrors fwbg's signal-fold pool logic: a signal model only ever trades when
    it has signal_rules with conditions, a non-empty model.required_features, or
    an allowed_hours/allowed_days time filter — those are the only things that
    populate the feature pool the model reads its entry column from. A
    signal-emitting plugin in pipeline.indicators is NOT a source on its own
    unless its output column is named in model.required_features.

    Without any of these the backtest skips every walk-forward fold with an
    empty pool and "completes" in seconds as no_successful_folds (the exact bug
    that plagues index/seasonality strategies).

    Returns True (lax) when a section is a preset string we cannot introspect —
    fwbg's own pre-flight guard stays the ultimate validator.
    """
    signal_rules = data.get("signal_rules")
    if isinstance(signal_rules, dict) and any(
        (signal_rules.get(d) or {}).get("conditions") for d in ("long", "short")
    ):
        return True
    model = data.get("model")
    if isinstance(model, dict) and model.get("required_features"):
        return True
    filters = data.get("filters")
    if isinstance(filters, str):
        # Opaque preset string — cannot rule out a time filter. Stay lax.
        return True
    if not isinstance(filters, dict):
        # No (or null) filters section: no time-filter source here.
        return False
    return bool(filters.get("allowed_hours") or filters.get("allowed_days"))


def _check_tags(tags: Any) -> None:
    """Validate that tags is a non-empty list of strings."""
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
            "pipeline",
            data["pipeline"],
            section="pipelines",
            presets=presets,
            frozen_fallback=KNOWN_PIPELINES,
        )
    else:
        raise StrategyValidationError("pipeline must be an object or a preset name")

    if isinstance(data["model"], dict):
        _check_inline_model(data["model"], catalog=catalog)
    elif isinstance(data["model"], str):
        if catalog is not None and catalog.all_slugs_for("models"):
            _check_field_with_catalog(
                "model",
                data["model"],
                catalog=catalog,
                catalog_category="models",
                frozen_fallback=KNOWN_MODELS,
            )
        else:
            _check_preset_string(
                "model",
                data["model"],
                section="models",
                presets=presets,
                frozen_fallback=KNOWN_MODELS,
            )
    else:
        raise StrategyValidationError("model must be an object or a preset name")

    # A signal model with no entry-signal source is unrunnable: fwbg skips every
    # fold with an empty feature pool and the run silently "completes" in
    # seconds. Reject it here, at translation time, the same way depends_on
    # rejects references to plugins that don't exist.
    if (
        isinstance(data["model"], dict)
        and data["model"].get("type") == "signal"
        and not signal_model_has_source(data)
    ):
        raise StrategyValidationError(
            "model.type='signal' has no entry-signal source. Add signal_rules "
            "with conditions, a non-empty model.required_features, or a "
            "filters.allowed_hours/allowed_days time filter. NOTE: a "
            "signal-emitting plugin in pipeline.indicators is not a source on "
            "its own unless its output column is listed in "
            "model.required_features. If the required capability has no "
            "plugin yet, keep the strategy in PROPOSED with a needs_plugin "
            "note instead of emitting an unrunnable signal model."
        )

    if isinstance(data["filters"], str):
        _check_preset_string(
            "filters",
            data["filters"],
            section="filters",
            presets=presets,
            frozen_fallback=KNOWN_FILTERS,
        )
    elif not isinstance(data["filters"], dict):
        raise StrategyValidationError("filters must be an object or a preset name")

    _check_preset_string(
        "validation",
        data["validation"],
        section="validations",
        presets=presets,
        frozen_fallback=KNOWN_VALIDATIONS,
    )
    _check_preset_string(
        "resources",
        data["resources"],
        section="resources",
        presets=presets,
        frozen_fallback=KNOWN_RESOURCES,
    )

    _check_exit_strategies(data["exit_strategies"], catalog=catalog)
    _check_tags(data["tags"])

    # M5c: optional plugin-slot list-fields. Omitted == empty == valid.
    for field, category in _PLUGIN_LIST_FIELDS:
        if field in data:
            _check_plugin_list_field(
                field,
                data[field],
                catalog=catalog,
                catalog_category=category,
            )

    if not isinstance(data["name"], str) or not data["name"]:
        raise StrategyValidationError("name must be a non-empty string")
    if not isinstance(data["hypothesis"], str) or not data["hypothesis"]:
        raise StrategyValidationError("hypothesis must be a non-empty string")
