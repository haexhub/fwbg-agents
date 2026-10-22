# Plan 028 — Research register: run path (fwbg register step 6a)

Execution: task by task; each task gets a spec-compliance and a code-quality review.

**Goal:** fwbg gates every backtest run with its research register (card per idea, every run a
counted trial, holdout looked at once). fwbg-agents runs are refused today: they send no
`research_id`/`note`, time out on the up-to-120 s preflight, mis-handle refusals, and run the
promote-gate holdout as an ordinary iteration after the card's cutoff. This plan makes the
Runner and the promote gate register-compliant. Out of scope (step 6b, later): querying the
register for prior art before proposing, and mapping abandon decisions to register verdicts.

**fwbg contract (origin/develop):**
- `POST /api/runs/start` body adds `research_id` (`S-NNNN`), `note` (non-empty, single line),
  `reason` (only for deliberate repeats: `duplicate_fingerprint`, `holdout_repeat`), `holdout`
  (bool). An iteration without `end_date` is clamped to the card's cutoff; an iteration ending
  after it is refused (`after_cutoff`). A holdout look develops on `[start, cutoff)` and
  evaluates the frozen setting once on `[cutoff, end)`.
- The preflight takes up to 120 s (`PREFLIGHT_TIMEOUT`); refusal → 409 (any denial with status
  409) or 422 with `{"detail": {"message", "denials": [{"code", "status", "message"}]}}`;
  CLI failure before the gate → 422 `{"detail": {"message", "output"}}`; timeout → 504.
  During the preflight the run's status is `starting` (`ACTIVE_STATUSES = {starting, running}`).
- `POST /api/research` creates a card: `title`, `tags` (≥ 1), `hypothesis`, `mechanism` (non-blank),
  optional `cutoff` (ISO date), `criteria`; 201 `{"card": {...}, "warning"?}` — a `warning`
  means written but not committed: never retry. `GET /api/research/{id}` →
  `{card, trials, …}` (trials carry `kind`: `iteration` | `holdout` | …).
- A `no_edge` symbol's `unified_metrics.sharpe` is `null`.

**Decisions (operator, 2026-10-22):** step 6 split into 6a (this plan) and 6b; a card is created
automatically per hypothesis (lineage root), children inherit it; a `null` Sharpe counts as 0
in the cross-asset median; a strategy may be created with an existing card id (D8).

## Design

- **D1 card per lineage:** the card id lives only on the lineage root,
  `Strategy.metadata_json["research_id"]`; every strategy resolves it via `lineage_root`.
  Created lazily on the lineage's first run from the root's
  `strategy_dir(root.slug)/iteration_001/hypothesis.json`, validated with a minimal card model
  rather than `ResearcherHypothesis` (files written before Plan 009 fail the full model on
  `strategy_family`/`edge_mechanism`): `title`, `tags`, `hypothesis`, `edge_mechanism` →
  `mechanism`, falling back to the pre-Plan-009 `expected_edge_explanation` when
  `edge_mechanism` is absent or blank; `cutoff = get_or_freeze_boundary(...)`: B is the
  cutoff of an auto-created card. The card's cutoff is the source of truth for the split
  (iterations `[…, cutoff)`, holdout `[cutoff, today)`), see D6. No criteria: A keeps its own gate. A missing or invalid
  `hypothesis.json` raises a specific error (no invented card). A `warning` in the response is
  logged and the id stored anyway. An `asyncio.Lock` per root slug prevents two cards for one
  lineage (`ponytail:` process-local; a multi-process deployment would need a DB-level claim).
- **D2 note:** one function builds the single-line note: root → `initial backtest: <title>`;
  child → `<kind> from <parent slug>: <reasoning>` from the parent's
  `analyst_recommendation.json` (whitespace collapsed, truncated to 240 chars); the universe
  attempt label and its assets are appended for fallback attempts; promote gate →
  `promote gate holdout look` / `promote gate cost stress x<m>`. Never empty.
- **D3 reason:** only explicit. `POST /strategies/{id}/run` takes an optional `reason`, passed
  through `Runner.run(strategy, reason=...)` to every start. The promote gate sends
  `reason="promote gate: further holdout look (<slug>)"` when the card already has a holdout
  trial (`GET /api/research/{id}` trials with `kind == "holdout"`).
- **D4 run start:** `start_run` waits up to 150 s for fwbg's answer (preflight 120 s + margin;
  a module constant with that why-comment); connect/write/pool and other calls keep the
  client's timeouts.
  `_acquire_run` adopts active runs with status `starting` or `running`.
- **D5 refusals:** `ResearchGateRefusedError(RunnerError)` (`agents/research_gate.py`) carries
  `status` and the list of `(code, message)` denials (or the preflight `message`/`output`);
  raised by the Runner for 409/422 responses of `/api/runs/start` whose body has the fwbg
  shape, and for 504.
  Other non-2xx stay `FwbgClientError`. Being a `RunnerError`, a refusal ends that universe
  attempt (the next attempt has another fingerprint) and, if all fail, the AgentRun fails with
  every attempt's reason (its denial codes included) in its message.
- **D6 windows:** iterations send no `end_date`; fwbg clamps an open-ended iteration to the
  card's cutoff before fingerprinting it. The card's cutoff is the source of truth; B is the
  cutoff of an auto-created card. (Sending `end_date = B` would make every iteration of an
  operator-linked card whose cutoff lies before B fail as `after_cutoff`.) The promote-gate holdout becomes a register
  holdout look: `holdout=True`, no `start_date`/`end_date` (fwbg develops up to the cutoff and
  evaluates once after it). Cost stress stays a development run (`cost_multiplier` is part of
  the fingerprint; no `end_date` → clamped to the cutoff).
- **D7 null Sharpe:** `median_metrics_across_assets` counts a `sharpe` of `None` as `0.0` for an
  asset that has `unified_metrics`; other keys unchanged. The same rule (`metrics.asset_sharpe`)
  feeds the interventions digest's median Sharpe and the lineage's per-asset Sharpe series. The
  Analyst's per-asset criteria lines stay as they are: a no-edge symbol failing its criteria is
  the correct verdict.
- **D8 card given at creation:** `POST /strategies` accepts an optional `research_id`
  (`S-` + 4 digits, else 422) and stores it on the new root's `metadata_json`;
  `ensure_research_id` returns a stored id before building a card, and that card's cutoff
  bounds the iterations (D6), whether it lies before or after B. A root with neither a
  `research_id` nor a usable `hypothesis.json` fails its run with a `ResearchCardError` that
  asks for a valid `hypothesis.json` and names the `research_id` route for new strategies.
- **D9 holdout DSR (operator, 2026-10-22):** a register holdout look writes no
  `fold_results.json`, so the promote gate's DSR reads the look's raw per-trade P&L from each
  symbol's `grid_details/<sym>/trades.json` `tr_trace` with a dedicated reader (not a fallback in
  `pnl_series`, so the look never enters the census). No readable series (missing or unreadable
  files, no trades, too short or flat for the moments) fails the DSR check closed.
- **D10 census (operator, 2026-10-22):** the holdout look is not recorded with
  `record_trial_stat` (a look selects nothing; fwbg keeps looks out of its trial count too). The
  cost-stress run is recorded as before.
- A lineage without a card at the promote gate (`ResearchCardError`) is a configuration error,
  not a failed gate run: the AgentRun fails, the error propagates, the attempt budget is untouched.
- **D11 holdout median (operator, 2026-10-22):** in the promote gate's holdout look, a symbol
  without holdout metrics (empty `unified_metrics` in fwbg's `get_run`: development not ok, e.g.
  `no_edge`, or no trades after the cutoff) counts with `sharpe` 0.0; its other metrics stay left
  out. Iterations and the cost-stress run keep D7.
- The holdout DSR reads only symbols whose `config.json` has `holdout.evaluated == true`.

**Known limitations:**
- A gate retry on the very same strategy repeats the identical cost-stress configuration; fwbg
  refuses it as `duplicate_fingerprint` (the result would be identical anyway). That counts as a
  failed gate run and uses an attempt. No automatic reason is sent.
- A holdout look still pending in fwbg (started, not yet recorded) is not among the card's
  trials, so no reason is sent; fwbg refuses it as `holdout_repeat`, a failed gate run.
- A Runner failure on A's side after fwbg recorded the trial as ok (e.g. polling gave up, or a
  step after the run raised): the auto-runner's retry repeats the same configuration, fwbg
  refuses it as `duplicate_fingerprint`, and the Runner falls back to the next, broader
  universe rung. No automatic reason is sent.
- D3's automatic reason lets every later holdout look in a lineage pass fwbg's
  `holdout_repeat` guard, and with D10 those looks are not counted in A's census. Explicit
  decision: no worse than before (the old gate re-ran its holdout on every gate run); the
  lineage's gate attempt budget (`promote_max_attempts`, Plan 014) still bounds failed gate runs.
- An existing root without `hypothesis.json` cannot be attached to an existing card except by
  writing `hypothesis.json` (which then creates a new card). Follow-up: an endpoint that sets
  `research_id` on an existing root.

## Deployment

- Needs fwbg with the research register (origin/develop) and its research directory
  configured; otherwise every run start is refused.
- A must be able to read fwbg's `test_results` (`fwbg_test_results_dir`), or every holdout DSR
  fails closed as unreadable.
- Any proxy between A and fwbg must allow more than 150 s for `POST /api/runs/start`
  (preflight up to 120 s, D4).
- No DB migration: the card id lives in `Strategy.metadata_json`.

## Tasks

### Task 1: Client — research fields, card endpoints, start timeout
- `tools/fwbg_client.py`: `start_run(..., research_id, note, reason=None, holdout=False)` sends
  them (omit `reason` when None, `holdout` only when True); 150 s timeout on that POST.
  New `create_research_card(title, tags, hypothesis, mechanism, cutoff) -> dict` and
  `get_research_card(research_id) -> dict`.
- Tests (`tests/…/test_fwbg_client*.py`, follow existing style with `httpx.MockTransport`):
  body fields; timeout applied to start only; card create/get shapes; non-2xx → `FwbgClientError`.
- Commit: `feat(client): send research register fields to fwbg`

### Task 2: Card per lineage and run note
- New `orchestrator/research_card.py`: `ensure_research_id(session, strategy, fwbg) -> str`
  (D1) and `run_note(session, strategy, *, attempt_label=None, assets=None) -> str` (D2), plus the specific error
  type for a missing/invalid hypothesis.
- Tests: creates once and stores on the root; child reuses the root's id without a call;
  concurrent calls for one lineage create one card; missing hypothesis → error, no call;
  `warning` → id stored, logged; notes for root/child/fallback are single-line, non-empty,
  truncated.
- Commit: `feat(orchestrator): one research card per lineage`

### Task 3: Runner — fields, adoption, refusals, reason
- `agents/runner.py`: `run(strategy, *, reason=None)` resolves `research_id` once, builds the
  note per attempt, passes `research_id/note/reason` through `execute_backtest` →
  `_acquire_run` → `start_run`; adopts `starting` runs; maps refusals to
  `ResearchGateRefusedError` (D5). `api/runs.py` run endpoint accepts optional `reason`. Other
  `Runner.run` callers unchanged. `start_run` requires `research_id` and `note`; the
  start timeout covers only the read wait. `POST /strategies` takes `research_id` (D8).
- Tests: fields reach `start_run`; a `starting` run is adopted; 409 refusal on the first
  attempt falls back to the next; all attempts refused → AgentRun failed with the codes;
  504 → `ResearchGateRefusedError`; 429 behaviour unchanged.
- Commit: `feat(runner): run through the research register gate`

### Task 4: Promote gate — holdout look, notes, reason
- `orchestrator/promote_gate.py`: holdout spec → `holdout=True` without window; notes per D2;
  reason per D3 when the card already has a holdout trial; cost stress unchanged apart from
  research fields. A refusal counts as a failed gate run (existing fail-closed path).
- Tests: holdout kwargs; reason only when a holdout trial exists; refusal → failed `GateRun`.
- Commit: `fix(promote-gate): look at the holdout through the register`

### Task 5: Null Sharpe in the median
- `orchestrator/metrics.py` per D7; docstring states it.
- Tests: mixed null/number; all null → 0.0; missing key unchanged.
- Commit: `fix(metrics): count a no-edge sharpe as zero`

### Final
- Update `plans/README.md` (row 028) and the docs that describe the Runner/promote gate windows.
- `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src`, `uv run pytest -q`
  (known worktree-only failures that need a sibling fwbg checkout are listed, not hidden).
- No data from the private research register (card contents, ids beyond test fixtures) in code,
  docs, tests or commit messages: both repos are public.
- `agents/runner.py` was already over 500 LoC before this plan. Its split is a follow-up plan
  along three seams: the error family (done here: `agents/runner_errors.py`); universe and
  data resolution (`_resolve_assets`, `_ensure_data_ready`); the start/poll loop
  (`_acquire_run`, `execute_backtest`, also used by the promote gate). `Runner.run` then keeps
  only the attempt choreography and the lifecycle transition.
