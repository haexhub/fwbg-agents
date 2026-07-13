"""StrategySpec — the structured "what" of a trading strategy (Plan 009 WP5).

Mirrors `PluginSpec` (speckit/spec.py): where a plugin's dedup anchor is its
one-line `capability`, a strategy's dedup anchor is its one-line
`edge_mechanism`. Together with a CONTROLLED-VOCABULARY `strategy_family`
(free-text families let near-identical ideas slip past the same-family
anti-redundancy bypass — observed in the DB), this makes semantic equality of
strategies detectable.

The remaining fields are the differentiation dimensions researcher.md already
asks for as prose; they are optional here so the spec can be generated from a
hypothesis without forcing a large schema expansion.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Distinct from the Translator's human-readable `spec.md` (agents/translator.py):
# this is the structured, dedup-anchored StrategySpec artifact.
STRATEGY_SPEC_FILENAME = "strategy_spec.md"

# Controlled vocabulary. Extend deliberately — every new value weakens the
# same-family dedup bypass, so prefer `other` over a one-off string.
STRATEGY_FAMILIES: tuple[str, ...] = (
    "ORB",
    "mean_reversion",
    "momentum",
    "breakout",
    "carry",
    "seasonality",
    "liquidity_sweep",
    "volatility",
    "pairs",
    "other",
)

StrategyFamilyLit = Literal[STRATEGY_FAMILIES]  # type: ignore[valid-type]


class StrategySpec(BaseModel):
    """Structured specification for a single strategy hypothesis."""

    model_config = ConfigDict(extra="forbid")

    strategy_family: StrategyFamilyLit
    # One-line statement of WHY the edge exists — the dedup anchor. Short on purpose.
    edge_mechanism: str = Field(min_length=10, max_length=240)
    entry_logic: str = ""
    exit_mechanism: str = ""
    regime_assumption: str = ""
    filters: list[str] = []
    timeframe: str = ""
    universe: list[str] = []
    asset_specific: bool = False

    @field_validator("edge_mechanism")
    @classmethod
    def _edge_is_single_line(cls, v: str) -> str:
        if "\n" in v:
            raise ValueError("edge_mechanism must be a single line")
        return v


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- _none_"


def render_strategy_spec_md(spec: StrategySpec) -> str:
    """Render a StrategySpec to markdown (the on-disk spec.md)."""
    sections = [
        "# Strategy Spec",
        "",
        f"**Family**: {spec.strategy_family}"
        f"  •  **Asset-specific**: {spec.asset_specific}"
        f"  •  **Timeframe**: {spec.timeframe or 'n/a'}",
        "",
        "## Edge mechanism (dedup anchor)",
        "",
        spec.edge_mechanism,
        "",
        "## Entry logic",
        "",
        spec.entry_logic or "_unspecified_",
        "",
        "## Exit mechanism",
        "",
        spec.exit_mechanism or "_unspecified_",
        "",
        "## Regime assumption",
        "",
        spec.regime_assumption or "_unspecified_",
        "",
        "## Filters",
        "",
        _bullets(spec.filters),
        "",
        "## Universe",
        "",
        _bullets(spec.universe),
    ]
    return "\n".join(sections) + "\n"
