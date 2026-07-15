# Portfolio/correlation risk layer before live capital

**Status:** Design spike (Plan 017) — no build yet.
**Context:** Every gate in the pipeline (backtest criteria, promote gate,
paper criteria, human approval at `post_strategy_promote_live`,
`src/fwbg_agents/api/strategies.py:452-539`) evaluates one strategy in
isolation. Nothing today diversifies the *live book* across strategies, and
there is no portfolio-level position-sizing policy. This doc proposes the
missing layer and records what Plan 017's feasibility script
(`scripts/spike_portfolio_correlation.py`) found about the data it would run on.

## Problem

N strategies that individually clear every gate (backtest → paper → human
approval) can share one edge mechanism and have highly correlated returns.
Approving them one at a time is locally correct and globally wrong: capital
concentrates in one bet with an extra layer of false diversification (it
*looks* like N independent strategies on the dashboard). There is also no
policy for how much capital/risk each live strategy should get relative to
the others — today every strategy's position size comes from its own
`risk_management` plugin (kelly / vol_targeted_kelly), computed purely from
that strategy's own trade history. See "The gate" and "Sizing policy
options" below.

## 1. Correlation input decision

**What Step 1 actually found, on this installation, right now:**

- `data/state.db` has exactly 3 strategies, all in state `proposed`. None
  has ever reached `backtested`, `paper_trading`, or `live_trading` — there
  are zero `transition` rows with `to_state='backtested'` (or later) in the
  whole DB, and zero `account-trades/<slug>/trades.jsonl` files under the
  resolved `fwbg_data_dir` (`~/Projekte/fwbg/data`, which only contains an
  unrelated `forexsb/` directory).
- Running `scripts/spike_portfolio_correlation.py --db data/state.db
  --test-results-dir ~/fwbg/test_results --fwbg-data-dir ~/Projekte/fwbg/data
  --states paper_trading,live_trading,backtested` against the real MAIN-tree
  data returns immediately: `no eligible strategies: none of the strategies
  are in states (...)`. Widening `--states` to include `proposed` still
  yields 0 usable series — every strategy is skipped for both reasons
  (`no paper trades.jsonl`, `no strategy->backtested transition found`).
- `~/fwbg/test_results/` does contain four run directories with real
  `grid_details/<symbol>/fold_results.json` shape (`diag_orb_003`,
  `hist_run_a`, `hist_run_b`, `job_1`) — but none of them is referenced by
  any transition payload in `data/state.db`, so they are orphaned fixtures
  (from manual/dev testing), not live pipeline output. They are useful only
  as a schema sanity check, not as correlation input.

This is a harder finding than the plan's own STOP condition ("fewer than 2
strategies have any per-trade data") — the honest number today is **zero**.
Per the plan's instruction, this is a downgrade to a documented finding, not
an abort: the design below is still worth having, but **the gate proposal in
section 2 is currently blocked on data density** — there is nothing to
diversify against yet. Revisit once the first strategy reaches
`paper_trading` (expected once Plan 016, fidelity-filtered paper series,
plus real backtests land).

**Correctness of the mechanism itself was verified separately**, against a
synthetic fixture (30 days of two deliberately-correlated paper series +
one 10-day backtest series from a fabricated `fold_results.json`, all built
under the scratchpad, never touching real data): the script correctly (a)
prefers `trades.jsonl` over backtest data per strategy, (b) resolves the
backtest fallback via the strategy's latest `strategy->backtested`
transition payload (`fwbg_run_id` + `universe.assets`), reusing
`orchestrator/trade_diagnostics.py::_load_symbol_trades` rather than
re-parsing `fold_results.json`, (c) computed Pearson correlation 0.995 for
the two correlated series over their 30 overlapping days, (d) correctly
reported 0 overlapping days and fired the "correlation not estimable"
warning for the backtest series (disjoint date range from the paper pair,
well under the 20-day floor), and (e) surfaced the unit-mismatch note
(`pnl_pct` vs `pnl_raw`) whenever a pair mixes sources.

**Input decision, once data exists:**

- **Prefer paper trades over holdout/backtest trades** when both are
  available for a strategy. Paper trades reflect current live execution
  (slippage, actual fills, current regime); holdout trades are the
  strategy's own out-of-sample test, already used once for the promote gate.
  A strategy freshly promoted to paper will only have holdout data for a
  while — that's fine, and expected to be the common case early on.
- **Minimum overlap: 20 trading days**, matching the script's default and
  roughly a month of daily P&L — below this a Pearson correlation is not a
  meaningful estimate (too few points, dominated by a handful of days).
  This number is a starting point, not derived from real overlap data (none
  exists yet); revisit once real paired daily series exist.
- **Unit normalization**: `pnl_raw` (backtest/holdout, absolute quote-currency
  P&L per trade) and `pnl_pct` (paper, % of account equity per trade) are on
  different scales. Pearson correlation is invariant to each series' own
  affine scaling, so a raw-vs-pct pairwise correlation is still a valid
  co-movement signal — but the two series must never be combined additively
  (e.g. summed into a blended "portfolio P&L") without first normalizing
  both to % of equity. The eventual gate should normalize backtest data to
  a %-of-notional basis at load time (needs the position size used in that
  backtest, available in `fold_results.json` per trade) so all series share
  one unit even before correlating.

## 2. The gate

Proposal: a `portfolio_check` step inside `post_strategy_promote_live`
(`src/fwbg_agents/api/strategies.py`), run **after** the existing three gates
(human_approval, `paper_analyst_promote_recommended`, state==`PAPER_TRADING`)
pass, before the state transition commits:

1. Load the candidate's daily P&L series (paper preferred, per section 1).
2. Load the same for every strategy currently `LIVE_TRADING`.
3. Compute pairwise Pearson correlation, candidate vs each live strategy,
   over overlapping days only.
4. **Reject** (HTTP 422, mirroring the existing gate's error style) if:
   - max pairwise correlation vs any live strategy exceeds a ceiling —
     propose **0.6** as a first default. Rationale: 0.6 is a widely-used
     "meaningfully correlated" threshold in portfolio construction (above it,
     diversification benefit drops off sharply); it is deliberately
     conservative for a first live-capital-adjacent gate, and it is a
     dashboard-configurable `Setting` (like `dsr_min`), not a hard-coded
     constant, so the maintainer can tune it once real numbers exist.
   - OR the live book already holds **≥ 2** strategies sharing the
     candidate's `strategy_family × asset_class` (also configurable) — this
     catches concentration the correlation check might miss when overlap is
     too short to estimate (section 1's 20-day floor) but the family/asset
     match is a cheap, always-available proxy signal.
   - If overlap with a given live strategy is < 20 days, that pair cannot be
     evaluated by correlation — fall back to the family×asset_class check
     alone for that pair, and surface a warning in the response (not a
     silent pass).
5. On any reject, return a structured 422 body naming the offending live
   strategy/strategies and the measured correlation/count, so the operator
   sees *why* — this is a human-in-the-loop gate, the human still decides
   whether to override (see open question 3).

**This must be a deterministic code gate, not an LLM judgment** — same
reasoning as the existing promote gate and DSR check: the inputs are
numeric, the threshold is a policy choice the maintainer sets, and the
gate's job is reproducible refusal, not interpretation. An LLM (e.g. the
paper Analyst) has already had its say via
`paper_analyst_promote_recommended`; this gate is a second, independent,
mechanical check like `human_approval` and the state check that precede it.

## 3. Sizing policy options

Two concrete positions, not mutually exclusive (a phased build could do the
first now and the second later):

1. **Equal risk-budget per strategy, scaled by realized vol.** Each live
   strategy gets `account_risk / N` as its risk-per-trade budget, further
   scaled down for strategies with higher realized volatility (so a jumpy
   strategy doesn't eat a bigger share of the drawdown budget than a calm
   one for the same nominal allocation). Simple, explainable, no covariance
   matrix to estimate or invert — appropriate given section 1's data
   density problem (a covariance-based method needs *more* overlapping data
   than a single pairwise correlation check, which will be even harder to
   satisfy early on).
2. **Correlation-penalized weights.** Scale each strategy's budget down by
   its average pairwise correlation with the rest of the live book (e.g.
   `weight_i ∝ 1 / (1 + mean_correlation_i)`), or go further to a proper
   minimum-variance/risk-parity allocation over the live book's covariance
   matrix. More correct, but needs enough overlapping history across *all*
   pairs simultaneously — strictly harder than option 1's per-strategy
   estimate, and the covariance matrix becomes ill-conditioned with few
   strategies/short histories. Treat as a v2, not the first build.

**Who enforces it, and what each would touch:**

- **fwbg's `risk_management` plugin phase** (`kelly` / `vol_targeted_kelly`,
  `fwbg/src/fwbg/plugins/fwbg-core/risk_management/`) computes
  `risk_per_trade` from *that strategy's own* `trades`/`win_rate`/`rrr` only
  (confirmed by reading `kelly/__init__.py::compute_risk_params`) — it has
  no visibility into sibling strategies or account-wide exposure, and
  giving it that would mean changing the plugin interface's contract
  (`BaseRiskManager.compute_risk_params`) to accept portfolio state, which
  ripples into every existing risk-management plugin and its tests. This is
  the wrong layer for a cross-strategy signal.
- **agents-side account config** is the natural enforcement point instead:
  the orchestrator already has the cross-strategy view (`state.db` knows
  every strategy's state) that fwbg's per-strategy plugin cannot see. The
  concrete mechanism would be a per-strategy risk multiplier written into
  whatever config fwbg's paper/live account setup reads (today
  `paper_account_id` is an opaque pointer to a fwbg-side
  `accounts/<slug>.yaml`, per `persistence/models.py:174-179` — agents never
  reads it back, only writes the pointer). **Open question**: does fwbg's
  account YAML already expose a risk-multiplier / position-size-scale
  field agents could set, or does that need a fwbg-side change first? This
  needs a read of the fwbg account-config schema before scoping the build —
  out of scope for this spike.

## 4. Failure modes

- **Thin/overlapping-window data.** Section 1 confirms this is not
  theoretical — it is the *current* state of the real DB (0 eligible
  strategies). A correlation ceiling with a 20-day floor and a
  family×asset_class fallback (section 2) degrades gracefully to "not
  estimable, use the proxy signal" rather than silently passing or
  hard-blocking every promotion.
- **Regime-dependent correlation.** Two strategies can be uncorrelated in
  a trending regime and highly correlated in a risk-off event (the exact
  scenario the gate exists to prevent) — a single trailing-window Pearson
  correlation will underestimate tail co-movement. Mitigation is out of
  scope for a first build (would need conditional/tail correlation, e.g.
  correlation computed on the worst-N-days subset) but should be a named v2
  follow-up, not silently assumed away.
- **Candidate has only backtest/holdout data.** The common case early in a
  strategy's life (freshly promoted to paper, no paper trades yet). The
  gate still runs — correlation vs backtest data, with the unit-normalization
  caveat from section 1 — but confidence in the number is lower than a
  paper-vs-paper comparison; the response should flag which side(s) of a
  given pair came from backtest data so the operator can weigh that.
- **Survivorship in the live book itself.** As strategies are abandoned
  (never deleted, per the no-DELETE-endpoints rule) their correlation
  history stops updating; the gate only ever compares against *currently*
  `LIVE_TRADING` strategies, so this is self-correcting, but worth stating
  explicitly since the append-only Transition log could tempt someone into
  querying "all strategies that were ever live."

## 5. Open questions for the maintainer

1. **Correlation ceiling default (0.6) and family×asset_class ceiling
   (≥2)** — both proposed above with no real data to calibrate against yet.
   *Recommendation:* ship both as `Setting` rows (like `dsr_min`), default
   as proposed, revisit numerically once ≥2 strategies have ≥20 days of
   overlapping paper data.
2. **Reject vs warn.** Section 2 proposes a hard reject (422). An
   alternative is warn-and-require-explicit-override (a second checkbox in
   the promote-live dashboard flow, similar to `human_approval` itself).
   *Recommendation:* warn-and-override for the first build — a hard reject
   with a miscalibrated threshold (see Q1, no real data yet) risks blocking
   legitimate promotions on a default nobody has validated. Tighten to a
   hard reject once the threshold has real evidence behind it.
3. **Backtest-only candidates** (failure mode 3) — should the gate be
   stricter (lower ceiling) when comparing against backtest-only data on
   either side, given the added unit/regime uncertainty? *Recommendation:*
   yes, e.g. apply the ceiling at 0.5 instead of 0.6 when either side of a
   pair is backtest-sourced; simple enough to implement as a parameter, no
   new mechanism.
4. **Sizing enforcement point** (section 3) — does fwbg's account-config
   YAML already support a per-account risk multiplier, or does that need a
   fwbg-side change first? *Recommendation:* spend a short research pass on
   the fwbg account-config schema before scoping the sizing build — this
   spike deliberately did not go deep into fwbg's account YAML.
5. **Which sizing option first** (equal-risk-budget vs correlation-penalized,
   section 3)? *Recommendation:* equal-risk-budget-scaled-by-vol first (no
   covariance matrix, works with the same thin data the correlation gate
   has to tolerate); correlation-penalized weights only once the live book
   is large enough (5+ strategies) for a covariance estimate to be
   meaningful.

## 6. Build estimate

Assuming Plan 016 (fidelity-filtered paper series) has landed and at least a
couple of strategies have reached `paper_trading` with real overlapping
history — otherwise this build has no input to test against (per section 1).

- **Correlation-input loader** (a production version of this spike's
  loading logic, reusing `_load_symbol_trades`, with unit normalization
  added per section 1): **S**. Mostly moving read-only logic already
  prototyped here into `orchestrator/`, plus the normalization step this
  spike deferred.
- **`portfolio_check` gate wired into `post_strategy_promote_live`**,
  including the two new `Setting` rows (ceiling, family×asset_class limit)
  and the structured-422 response: **M**. New deterministic code path, new
  API response shape, new dashboard surface to show the rejection reason.
- **Sizing policy (equal-risk-budget-scaled-by-vol only, per open question
  5)**, including the fwbg account-config research spike (open question 4)
  and whatever fwbg-side change that research surfaces: **M–L** — the range
  reflects that the fwbg-side portion is currently unscoped (open question
  4 is explicitly unresolved).
- **Correlation-penalized weights / regime-conditional correlation (v2)**:
  **L**, and should be its own later plan, not bundled into the first build.

Suggested split: one plan for the gate (S+M above), a second plan for
sizing once open question 4 is answered (M–L), a third (later, only if the
live book grows) for v2 sizing and regime-conditional correlation.

## Maintenance notes carried from Plan 017

- Revisit this doc after Plan 016 (fidelity-filtered paper series) lands —
  that is the better correlation input than raw `trades.jsonl`.
- The eventual build must reuse
  `orchestrator/trade_diagnostics.py::_load_symbol_trades` for the backtest
  fallback rather than re-parsing `fold_results.json` — this spike's script
  already does so; carry that forward, don't re-derive a parser.
