# fwbg Plugin-Authoring Conventions

> Canonical reference for the **PluginPlanner** and **PluginImplementer** agents.
> Loaded into the Planner's system-prompt at runtime. Updated in lock-step with the
> `fwbg_sdk` base classes — keep this file in sync when `fwbg-sdk/src/fwbg_sdk/`
> changes a public contract.

## BasePlugin Contract

Every fwbg plugin is a subclass of `fwbg_sdk.base.BasePlugin`. The contract:

**Required class attributes (must be set on the subclass):**
- `name: str` — unique slug identifier, snake_case, must match the directory name.
- `phase: PluginPhase` — which pipeline phase this plugin belongs to (enum, see below).

**Optional class attributes (defaults shown):**
- `version: str = "0.1.0"` — semantic version, plain string.
- `stateful: bool = False` — whether the plugin maintains state across calls (preprocessors with `fit()` are stateful; pure indicators are not).
- `cacheable: bool = True` — whether the runner may cache results.
- `depends_on: list[str] = []` — slugs of other plugins that must run first.

**Required methods (per phase, see below — at least one is abstract).**

**Optional class methods:**
- `get_default_params(cls) -> dict[str, Any]` — default values for the plugin's parameters. Empty dict if the plugin takes no params.
- `get_param_schema(cls) -> dict[str, dict[str, Any]]` — rich UI/validation schema (see "Parameters" section).
- `get_feature_columns(self) -> list[str]` — list of column names this plugin writes to the DataFrame. Empty list if it doesn't add columns.

The `__init_subclass__` hook validates that `name` (str) and `phase` (`PluginPhase` enum member) are set; subclasses without these raise `TypeError` at import time.

## PluginPhase Enum

Exact enum values from `fwbg_sdk.base.PluginPhase`:

```
DATA_LOADING      = "data_loading"
PREPROCESSING     = "preprocessing"
INDICATORS        = "indicators"
FEATURE_SELECTION = "feature_selection"
EXIT_STRATEGIES   = "exit_strategies"
RISK_MANAGEMENT   = "risk_management"
LABELING          = "labeling"
MODEL             = "model"
VALIDATION        = "validation"
```

Use the `PluginPhase` enum value, not a string literal. Import: `from fwbg_sdk.base import PluginPhase`.

## Phase-Specific Subclasses

### BaseIndicator (`PluginPhase.INDICATORS`)

`from fwbg_sdk.indicators import BaseIndicator, shift_features, safe_divide`

Computes technical features from OHLCV data. Required abstract method:
```python
@abstractmethod
def compute(self, df: pd.DataFrame, **params) -> pd.DataFrame:
    """Compute indicator columns and append them to df. Return the augmented df."""
```

**Critical rules:**
- **Lookahead-bias prevention is MANDATORY.** All new feature columns MUST be shifted by 1 bar before being returned. Use `shift_features({col_name: series, ...}, df.index)` and concat with the input df.
- **Safe division.** Use `safe_divide(numerator, denominator)` for all divisions — guarantees consistent NaN-handling for zero denominators.
- Add optional class attr `group: str` for UI categorization. Default `"custom"`.
- Add optional class attr `benefits_from_stationary: bool` — `True` if the indicator should run on preprocessed (stationary) data inside each CV fold.

### BasePreprocessor (`PluginPhase.PREPROCESSING`)

`from fwbg_sdk.preprocessors import BasePreprocessor`

Transforms OHLC data (e.g., fractional differentiation for stationarity). Required abstract method:
```python
@abstractmethod
def transform(self, df: pd.DataFrame, **params) -> pd.DataFrame:
    """Apply learned parameters to df. Raises RuntimeError if fit() wasn't called first."""
```

**Critical rules:**
- Follows the sklearn `fit() → transform()` pattern. `fit()` learns parameters ONLY from train data; `transform()` applies them to train/test/OOS.
- `stateful = True` is implied (the base class sets `fitted_: bool = False` and toggles it in `fit()`).
- `transform()` MUST raise `RuntimeError` if called before `fit()` — the base class does this automatically when subclasses call `super().transform(df)`.
- Optional `inverse_transform()` for reversible preprocessors.
- Class attr `order: int = 100` controls execution order when multiple preprocessors run.

### BaseFeatureSelector (`PluginPhase.FEATURE_SELECTION`)

`from fwbg_sdk.feature_selectors import BaseFeatureSelector`

Picks the most important features for the ML model. Required abstract method:
```python
@abstractmethod
def select_features(
    self,
    X: pd.DataFrame,
    y: np.ndarray,
    max_features: int | None = None,
    **params,
) -> tuple[list[str], dict]:
    """Returns (selected_feature_names, metadata_dict with e.g. feature importances)."""
```

### BaseRiskManager (`PluginPhase.RISK_MANAGEMENT`)

`from fwbg_sdk.risk_managers import BaseRiskManager`

Computes position-sizing and risk controls. Used for plugins whose Analyst-recommended phase is `"filter"` — they map to `RISK_MANAGEMENT` in the SDK enum. Required abstract method:
```python
@abstractmethod
def compute_risk_params(
    self,
    trades: list[float],
    win_rate: float,
    rrr: float,
    **params,
) -> dict[str, Any]:
    """Returns dict with at minimum: risk_per_trade, trade_returns, circuit_breaker, risk_adjustment."""
```

## Parameters: `get_default_params` + `get_param_schema`

Two paired classmethods control how the runner discovers and validates parameters.

`get_default_params()` returns a flat dict:
```python
@classmethod
def get_default_params(cls) -> dict:
    return {"period": 14, "smoothing": 2.0, "use_log": False}
```

`get_param_schema()` returns rich metadata for UI rendering and validation:
```python
@classmethod
def get_param_schema(cls) -> dict[str, dict[str, Any]]:
    return {
        "period": {
            "type": "int",
            "default": 14,
            "description": "Lookback window in bars",
            "min": 2,
            "max": 200,
            "step": 1,
            "required": True,
        },
        "smoothing": {
            "type": "float",
            "default": 2.0,
            "description": "Exponential smoothing factor",
            "min": 0.1,
            "max": 10.0,
            "step": 0.1,
        },
        "use_log": {
            "type": "bool",
            "default": False,
            "description": "Apply log-transform to inputs",
        },
    }
```

**Allowed `type` strings:**
- `"int"`, `"float"`, `"bool"`, `"string"`
- `"list[int]"`, `"list[float]"`, `"list[string]"`
- `"choice"` — requires `choices: list[str]` alongside.

**Schema rules:**
- Every key in `get_default_params()` MUST appear in `get_param_schema()` if the latter is defined.
- `description` is non-empty (UI displays it).
- `min`/`max`/`step` apply to numeric types only.
- `required` defaults to `True`. Set to `False` for optional params.
- If `get_param_schema()` is omitted, `BasePlugin` auto-infers a basic schema from defaults — but explicit schemas are strongly preferred.

## Feature Columns: `get_feature_columns`

For plugins that add columns to the DataFrame (indicators, some preprocessors):

```python
def get_feature_columns(self) -> list[str]:
    """List the column names this plugin writes to df."""
    return ["my_indicator_value", "my_indicator_signal"]
```

**Naming rules:**
- `snake_case` only — no whitespace, no hyphens, no camelCase.
- Recommended: prefix with the plugin slug (e.g., `rsi_value` for the `rsi` indicator) to avoid collisions across plugins.
- The list MUST exactly match the columns actually produced by `compute()`/`transform()`. The runner uses this list for downstream column selection.
- For preprocessors that don't add columns (only transform existing ones): return `[]`.

## Tests Convention (`tests.py`)

Every plugin directory contains a `tests.py` file with pytest-compatible test functions.

**Mandatory standards:**
- **Minimum 3 tests** per plugin. The M5b PluginEvaluator counts and rejects plugins with fewer.
- Function names: `test_<behaviour>` — lowercase, snake_case, descriptive. Bad: `test1`, `test_works`. Good: `test_constant_price_yields_zero_returns`.
- Use `pytest` patterns: plain functions, `assert ...`, fixtures via `@pytest.fixture`. No `unittest.TestCase`.
- **No-lookahead test for indicators (REQUIRED):** assert that `df[col].iloc[i]` does not depend on `df.iloc[i+1:]`. Standard pattern:
  ```python
  def test_no_lookahead_bias():
      df_full = create_ohlc(200)
      df_partial = df_full.iloc[:100].copy()
      out_full = MyIndicator().compute(df_full)
      out_partial = MyIndicator().compute(df_partial)
      # Values up to bar 99 must be identical regardless of future bars
      assert (out_full["my_col"].iloc[:100] == out_partial["my_col"]).all()
  ```
- **Edge-case tests are encouraged:** empty df, single-row df, all-NaN inputs, constant prices.
- **Parameter-variation test:** exercise at least two non-default parameter combinations.

## Contract `test_scenarios` (deterministic Evaluator)

The `contract.test_scenarios[*].name` values are NOT free text. The
PluginEvaluator only knows five deterministic, seeded OHLCV generators and
rejects the whole verification run on the first unknown name:

- `trending_up`, `trending_down`, `sideways`, `high_vola`, `sparse_data`

Rules:
- Every scenario name MUST be one of the five above. Do NOT invent
  plugin-specific scenario names.
- `data_path` MUST be `"test_scenarios/<name>.parquet"` — the Evaluator
  generates and writes the data itself; never point at hand-made fixtures.
- Omit `expected_outputs` — the deterministic Evaluator checks structural
  invariants (length parity, finite values, dtypes), not expected values.
- Mark event-style outputs that only carry values at signal bars (e.g. a
  `*_lag` or `*_age` column) with `sparse: true` in `contract.outputs`. The
  synthetic scenarios contain no upstream indicator columns, so composite
  plugins legitimately produce zero signals there — a sparse output may then
  be all-NaN without failing verification. Dense outputs (a moving average,
  a score defined on every bar) must NOT be marked sparse.

## File Layout

```
<plugin_slug>/
├── __init__.py         # exports the plugin class
├── tests.py            # ≥3 pytest tests
└── docs/               # optional
    └── README.md
```

- `__init__.py` MUST contain the `BasePlugin` subclass. The class name is `<PascalCaseSlug>Indicator`/`Preprocessor`/`Selector`/`RiskManager` matching the phase (e.g. slug `my_zscore`, phase `indicators` → class `MyZscoreIndicator`).
- The plugin's `name` class attr MUST equal the directory slug.
- `docs/README.md` is optional. If present, no path-traversal: keep all relative paths inside the plugin dir.

## Worked Examples

Reference plugins per phase (the PluginCatalog auto-injects 2-3 actual source samples
at runtime via `get_fwbg_plugin_examples(catalog, category=<phase>, n=3)`):

- **indicators:** `rsi`, `adx`, `ema`, `momentum` (all in `fwbg-core/indicators/`)
- **preprocessing:** `fractional_diff` (in `fwbg-premium/preprocessing/`)
- **feature_selection:** `boruta`, `correlation_filter`, `plateau`, `stability`
- **risk_management:** `kelly`, `vol_targeted_kelly` (in `fwbg-core/risk_management/`)

Study the runtime-injected examples for current naming, structure, and test
patterns before emitting code.
