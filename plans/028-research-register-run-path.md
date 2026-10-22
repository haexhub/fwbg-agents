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
  `edge_mechanism` is absent or blank; `cutoff = get_or_freeze_boundary(...)`
  (the lineage boundary B already has the register's cutoff semantics: iterations `[…, B)`,
  holdout `[B, today)`). No criteria: A keeps its own gate. A missing or invalid
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
- **D5 refusals:** `ResearchGateRefused(RunnerError)` carries `status` and the list of
  `(code, message)` denials (or the preflight `message`/`output`); raised by the Runner for
  409/422 responses of `/api/runs/start` whose body has the fwbg shape, and for 504.
  Other non-2xx stay `FwbgClientError`. Being a `RunnerError`, a refusal ends that universe
  attempt (the next attempt has another fingerprint) and, if all fail, the AgentRun fails with
  the denial codes in its message.
- **D6 windows:** iterations keep `end_date = B`. The promote-gate holdout becomes a register
  holdout look: `holdout=True`, no `start_date`/`end_date` (fwbg develops up to the cutoff and
  evaluates once after it). Cost stress stays a development run (`cost_multiplier` is part of
  the fingerprint; no `end_date` → clamped to the cutoff).
- **D7 null Sharpe:** `median_metrics_across_assets` counts a `sharpe` of `None` as `0.0` for an
  asset that has `unified_metrics`; other keys unchanged.
- **D8 card given at creation:** `POST /strategies` accepts an optional `research_id`
  (`S-` + 4 digits, else 422) and stores it on the new root's `metadata_json`;
  `ensure_research_id` returns a stored id before building a card. A root with neither a
  `research_id` nor a usable `hypothesis.json` fails its run with a `ResearchCardError` that
  names both remedies.

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
  `ResearchGateRefused` (D5). `api/runs.py` run endpoint accepts optional `reason`. Other
  `Runner.run` callers unchanged. `start_run` requires `research_id` and `note`; the
  start timeout covers only the read wait. `POST /strategies` takes `research_id` (D8).
- Tests: fields reach `start_run`; a `starting` run is adopted; 409 refusal on the first
  attempt falls back to the next; all attempts refused → AgentRun failed with the codes;
  504 → `ResearchGateRefused`; 429 behaviour unchanged.
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
