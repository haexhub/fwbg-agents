# Plan 018: Make the factory's USD economics answerable (cost estimator + rollup endpoint)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. On
> any STOP condition, stop and report. When done, update the status row in
> `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 75123b0..HEAD -- src/fwbg_agents/persistence/models.py src/fwbg_agents/config.py src/fwbg_agents/api/ src/fwbg_agents/agents/ src/fwbg_agents/orchestrator/plugin_flow.py`
> On mismatch with the "Current state" excerpts, STOP.

## Status

- **Priority**: P2 (cheap, high diagnostic value)
- **Effort**: S–M
- **Risk**: LOW (additive; only reporting)
- **Depends on**: none
- **Category**: dx / direction
- **Planned at**: commit `75123b0`, 2026-07-16

## Why this matters

Token usage per LLM call is already persisted, but USD economics are not
answerable: `LlmCall.cost_usd` has been nullable-and-empty since M3 ("future
infra plugs in a USD estimator" — never built), and no aggregation exists.
So the founding question of an autonomous strategy factory — *what does one
promoted strategy cost, and is the hit rate improving per dollar?* — cannot
be answered, while expensive knobs (researcher fan-out width, reiterate depth
of 12, critic passes) are tuned blind. The raw data model already supports
attribution: `LlmCall → AgentRun → strategy_id/parent_run_id`. This plan adds
a static price table, fills `cost_usd` on write, backfills history, and
exposes one rollup endpoint.

Precision note: the proxy uses subscription pricing, so USD figures are
**estimates at list price** — label them as such in the API docstring; they
are for relative comparison (cost per lineage, per outcome), not billing.

## Current state

- `src/fwbg_agents/persistence/models.py:299-318`:

  ```python
  class LlmCall(Base):
      """One LLM round-trip inside an agent run.

      Cost is nullable because the haex-claude-proxy uses subscription pricing —
      M3 records tokens, future infra plugs in a USD estimator.
      """
      ...
      model: Mapped[str] = mapped_column(String(128), nullable=False)
      input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
      output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
      cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
  ```

- `LlmCall(` is constructed in 6 places (verify with
  `grep -rn "LlmCall(" src/fwbg_agents/`; the class definition line in
  `models.py` also matches textually):
  - 5 real LLM calls that get `cost_usd`: `agents/critic.py:116`,
    `agents/researcher.py:211`, `agents/analyst.py:577`,
    `agents/translator.py:434`, `orchestrator/plugin_flow.py:115`.
  - 1 search-quota pseudo-call that stays UNCHANGED:
    `tools/search/tavily.py:108` (`_log_quota`, `model="<provider>-search"`,
    0 tokens — not an LLM call; its rows intentionally stay `cost_usd NULL`
    and show up in the summary's `unpriced_calls`). Add a one-line comment
    there noting the exclusion.
- `AgentRun` (`models.py:242-271`) carries `strategy_id`, `parent_run_id`,
  `agent_name`, `status` — the join keys for attribution.
- `Strategy.current_state` distinguishes outcomes (`StrategyState`:
  `LIVE_TRADING`, `PAPER_TRADING`, `ABANDONED`, `BACKTESTED`, …) and
  `parent_strategy_id` links lineages (root = `parent_strategy_id IS NULL`).
- Settings pattern: `src/fwbg_agents/config.py` — pydantic-settings `Field`s
  with descriptions; env-overridable.
- Router exemplar: `src/fwbg_agents/api/trials.py` (small read-only router,
  `APIRouter(tags=[...])`, response `BaseModel`, `Depends(get_session)`).
  Routers are registered in `src/fwbg_agents/main.py` — find the
  `include_router` block and match it.
- Script exemplar: `scripts/backfill_plugin_specs.py`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `uv run pytest -q` | all pass |
| Lint | `uv run ruff check src tests scripts && uv run ruff format --check src tests scripts` | exit 0 |
| Types | `uv run mypy src` | exit 0 |

## Scope

**In scope**:
- `src/fwbg_agents/tools/llm_pricing.py` (create)
- `src/fwbg_agents/config.py` (price-table setting)
- The 5 `LlmCall(` construction sites (one-line change each)
- `src/fwbg_agents/api/economics.py` (create) + router registration in `main.py`
- `scripts/backfill_llm_costs.py` (create)
- tests

**Out of scope**:
- No schema change (`cost_usd` exists), no migration.
- The proxy, `tools/llm.py` transport, model selection logic.
- Dashboard work (the endpoint is the contract; UI is a separate task).

## Git workflow

- Branch `advisor/018-llm-cost-telemetry` off `develop`. Conventional
  commits; no Claude/Anthropic references. No push/PR unless instructed.

## Steps

### Step 1: price table + estimator

New `src/fwbg_agents/tools/llm_pricing.py`:

```python
def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """List-price estimate; None for unknown models (never guess)."""
```

- Price table: a `dict[str, tuple[float, float]]` of
  `model-substring -> (usd_per_1M_input, usd_per_1M_output)`, matched by
  longest matching key against the recorded `model` string (model strings may
  carry date suffixes). Seed it from the model names actually present in the
  DB (`SELECT DISTINCT model FROM llm_call` — check via a quick script or
  read what `settings` configures as models in `config.py` /
  `tools/agent_config.py`). Prices: take current Anthropic list prices for
  exactly the model families found; leave a `# prices as of 2026-07 — update
  when models change` comment. Unknown model → `None`, never 0.
- Make the table overridable via a `Settings` field
  (`llm_price_table_json: str | None` parsed at startup, or a
  `dict[str, tuple[float, float]]` field if pydantic-settings handles it
  cleanly — match existing complex-field patterns in `config.py`).

**Verify**: unit tests — known model exact + suffixed match, unknown → None,
zero tokens → 0.0.

### Step 2: fill on write

At each of the 5 `LlmCall(` sites, pass
`cost_usd=estimate_cost_usd(model, input_tokens, output_tokens)` using the
same variables already passed to the constructor. Keep the change one line
per site; import at module top.

**Verify**: `grep -rn "LlmCall(" src/fwbg_agents/` → same sites as before;
the 5 LLM-call sites pass `cost_usd=`, the tavily quota site does not (has
the exclusion comment instead); one integration-style test (pick
the agent with the simplest existing test double, likely the critic or
researcher tests) asserts a persisted `LlmCall` row has non-null `cost_usd`
when the model is in the table.

### Step 3: backfill

`scripts/backfill_llm_costs.py` — idempotent: `UPDATE`s only rows where
`cost_usd IS NULL` and the model matches the table; prints
`updated N, skipped M (unknown model)`. Structure modeled on
`scripts/backfill_plugin_specs.py`.

**Verify**: test with 3 seeded rows (known model null cost, unknown model,
already-priced) → exactly 1 update; second run → 0.

### Step 4: rollup endpoint

New `src/fwbg_agents/api/economics.py`, registered like the trials router:

`GET /economics/summary` returning:

```python
class EconomicsSummary(BaseModel):
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float          # sum over priced rows
    unpriced_calls: int            # rows with cost_usd NULL — honesty counter
    by_agent: dict[str, CostBucket]        # agent_name -> tokens/cost
    by_outcome: dict[str, CostBucket]      # strategy current_state -> tokens/cost (calls without strategy_id under "unattributed")
    cost_per_promoted_strategy: float | None  # total_cost / count(state in PAPER_TRADING|LIVE_TRADING); None if none
    lineage_top: list[LineageCost]          # top 10 most expensive lineages (root slug, total cost, outcome)
```

Implementation: SQL aggregation (`func.sum`, group-by joins over
`llm_call → agent_run → strategy`), no Python-side row loops. Lineage rollup:
resolve each strategy to its root via a recursive CTE if SQLite supports it
here (SQLAlchemy `cte(recursive=True)`), else a Python walk over the
(small) strategy table only — never over llm_call rows.

**Verify**: endpoint test via the ASGI test pattern used by the existing API
tests (find with `grep -rl "trials/summary" tests/`): seed 2 strategies (one
promoted, one abandoned) with runs+calls → assert totals, buckets,
`cost_per_promoted_strategy`.

### Step 5: full gates

**Verify**: `uv run pytest -q && uv run ruff check src tests scripts && uv run ruff format --check src tests scripts && uv run mypy src` → all exit 0.

## Test plan

Covered per step; place API tests next to the existing trials-endpoint tests,
pricing tests in a new `tests/tools/test_llm_pricing.py`.

## Done criteria

- [ ] Tests/lint/format/mypy exit 0
- [ ] All 5 LLM-call `LlmCall(` sites pass `cost_usd=`; the tavily quota site is unchanged apart from the exclusion comment
- [ ] `GET /economics/summary` returns the model above with correct seeded-data numbers
- [ ] Backfill script idempotent (test proves)
- [ ] `unpriced_calls` surfaces unknown models instead of hiding them
- [ ] No files outside the in-scope list modified
- [ ] `plans/README.md` status row updated

## STOP conditions

- More or fewer than 6 `LlmCall(` construction sites (5 LLM + 1 tavily quota)
  exist at execution time — re-enumerate and report the delta before
  proceeding (a missed site undercounts forever). *(Resolved 2026-07-16: the
  6th site is the tavily quota pseudo-call; it stays unchanged by decision.)*
- The `model` strings in the real DB don't match any current Anthropic model
  family you can price — report the distinct model list instead of guessing
  prices.
- `config.py` has no existing pattern for dict-valued settings and pydantic
  parsing fights you — fall back to the JSON-string field and say so.

## Maintenance notes

- Prices go stale: the table carries an as-of comment; the `unpriced_calls`
  counter in the summary is the tripwire for new/renamed models.
- When a proper cost source appears (proxy-side billing export), it should
  overwrite `cost_usd` and this estimator becomes the fallback.
- The dashboard can consume `/economics/summary` as-is; if per-time-window
  economics are wanted later, add `from`/`to` query params rather than a new
  endpoint.
