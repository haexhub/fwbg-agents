# Plan 021: Repair the IG broker adapter's rotted unit tests (surfaced by Plan 020)

> **Executor instructions**: Follow this plan step by step in the `fwbg` repo
> (`/home/haex/Projekte/fwbg`). Run every verification command and confirm the
> expected result before moving to the next step. If anything in the "STOP
> conditions" section occurs, stop and report; do not improvise. When done,
> update the status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**: `cd /home/haex/Projekte/fwbg && git log --oneline -5 -- src/fwbg/adapters/broker/ig/adapter.py src/fwbg/adapters/broker/__init__.py` —
> confirm the credential-wrapping (`_IGCredentials`) and reconnect/session-refresh
> logic described below are still present before trusting this plan's specifics.

## Status

- **Priority**: P2 (correctness of test coverage on a broker adapter that
  touches real money/orders; not user-facing until someone acts on IG broker
  data)
- **Effort**: M
- **Risk**: LOW–MED (touches credential handling and connection logic in the
  IG adapter; no production behavior change expected, but verify carefully)
- **Depends on**: none
- **Category**: test-debt / correctness
- **Planned at**: `fwbg` branch `fix/broker-timeframe-mapping`, commit adding
  `src/fwbg/adapters/broker/ig` to `testpaths` (2026-07-17)

## Why this matters

Plan 020 (WP3, IG/yfinance timeframe-mapping fail-loud fix) found that
`pyproject.toml`'s `testpaths` never included
`src/fwbg/adapters/broker/ig/` — the legacy IG adapter's own
`test_adapter.py`/`test_mappings.py`/`test_streaming.py` (65 tests) have
therefore **never run in CI**. Worse: CI's dependency install
(`.github/workflows/deploy.yml`) never installed the `ig` extra
(`trading-ig`, `yfinance`), so even collecting that directory would have
hard-failed at `IGBrokerAdapter.__init__` (`ImportError`).

Plan 020 fixed both gaps (testpaths entry + CI installs `.[dev,api,ig]`).
Doing so immediately surfaced **5 pre-existing, unrelated failures** — real
test/production drift from an adapter refactor (credential wrapping +
connect retry/session-refresh) that shipped after these tests were last
exercised. They are currently marked `xfail`/`skip` with a reference to this
plan so CI stays green; this plan is the actual repair.

## Current state (found while diagnosing)

1. **`test_init_with_required_params` (xfail, strict)** — the adapter no
   longer exposes `.username`/`.password`/`.api_key` as plain attributes;
   credentials are wrapped in a `_IGCredentials` object
   (`src/fwbg/adapters/broker/ig/adapter.py:51`) with dict-style
   `__getitem__` access only. The test (and possibly other callers) still
   expect direct attribute access.

2. **`test_get_historical_bars_from_ig` (skip, not xfail)** — the test sets
   `adapter._ig = MagicMock()` directly, which used to be enough. Since the
   reconnect/session-refresh refactor added `_ensure_session_valid()`
   (checks `self._connected` *and* `self._ig`, and calls a real `connect()`
   otherwise), the mock is bypassed: `connect()` fails for real (three
   logged `IGException` retries), `_fetch_ig_historical` returns `None`, and
   `get_historical_bars` falls through to the yfinance fallback — which
   made a **real network call to Yahoo Finance** during this session's test
   run (confirmed: response contained live recent-date bars, not the two
   mocked rows the test asserts). This is marked `skip` rather than `xfail`
   specifically to stop that network call from happening on every CI run.

3. **`test_get_positions_returns_list` (xfail)**,
   **`test_get_account_info_returns_data` (xfail)**,
   **`test_processes_complete_candle` in `test_streaming.py` (xfail)** — all
   three fail on `isinstance(value, Position/AccountInfo/BarData)` despite
   the returned object's field values being exactly correct (confirmed via
   repr diff, e.g.
   `AccountInfo(balance=10000.0, equity=10500.0, margin_used=1000.0, margin_available=9000.0, currency='EUR')`
   is not an instance of `AccountInfo`). Ruled out during triage: there is
   only **one** definition site for each of `Position`/`AccountInfo`/`BarData`
   in the codebase (`src/fwbg/adapters/broker/__init__.py:74,94,104`) — no
   shadow/duplicate class. A plain `python -c` script importing
   `fwbg.adapters.broker` and `fwbg.adapters.broker.ig.adapter` side by side
   shows the **same** class object (`is` compares equal).

   **Order-dependent — important for Step 3**: running just this directory
   (`pytest src/fwbg/adapters/broker/ig`) reproduces the failure reliably,
   including with `--runxfail` on a single test in isolation. But running
   the **full** suite (`pytest -q`, which collects `tests/` — ~1683 items —
   before reaching `src/fwbg/adapters/broker/ig/` per `testpaths` order) made
   all three **unexpectedly pass**. That is why these three markers are
   plain `xfail` (no `strict=True`): with `strict=True` an unexpected pass
   is reported as a failure, and this actually happened — the first version
   of this fix used `strict=True` and turned up `3 failed` in the full-suite
   run despite being clean in an isolated run of just this directory, which
   would have made CI red. The 4th test (`test_init_with_required_params`,
   credential attributes) fails consistently in both isolation and the full
   suite, so it keeps `strict=True`.

   This strongly suggests two distinct imports of `fwbg.adapters.broker` (or
   the whole `fwbg` package) exist somewhere in pytest's collection/import
   path, producing two non-identical class objects with the same name — and
   that whichever import happens first "wins" and becomes consistent for the
   rest of the run. Root cause **not yet found** — needs real investigation
   (see Step 3), and any fix must be re-verified against **both** an
   isolated run of this directory and the full suite, since the symptom
   only shows in one of the two.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Focused tests | `cd /home/haex/Projekte/fwbg && uv run pytest -q src/fwbg/adapters/broker/ig --runxfail -rA` | see which xfails now pass/fail for real, guides Step 1/2/3 |
| Full suite | `uv run pytest -q` | all green, no unmarked failures, no unexpectedly-passing strict xfails |
| Lint | `uv run ruff check src/ packages/` | exit 0 |

## Scope

**In scope**:
- `src/fwbg/adapters/broker/ig/adapter.py` (credential access, connect/session logic)
- `src/fwbg/adapters/broker/ig/test_adapter.py`, `test_streaming.py` (remove the xfail/skip markers once each underlying issue is fixed)
- Whatever produces the duplicate-import symptom for Steps 3's tests (likely `pyproject.toml` pytest config, or how `fwbg` is installed/discovered — investigate before changing)
- The packaged copy `packages/fwbg-broker-ig/src/fwbg_broker_ig/adapter.py` **only if** the same credential/connect drift is confirmed to exist there too (it currently has zero tests, so there is nothing failing there to fix — check its `_fetch_ig_historical`/`connect()`/`_ensure_session_valid()` for the same pattern before deciding whether to touch it)

**Out of scope**:
- Consolidating the two IG adapters (package vs. legacy) into one — a separately known, larger finding (see `plans/README.md` re-audit notes on "Two divergent IG adapters").
- IG order-confirmation reliability (single-shot confirm, no dealReference idempotency) — separately known finding, not touched here.
- Any change to Plan 020's timeframe-mapping fail-loud behavior.

## Git workflow

- New worktree/branch in `fwbg`, e.g. `fix/ig-adapter-test-rot`, based on
  `origin/develop` after Plan 020's `fix/broker-timeframe-mapping` is merged
  (or rebase on top of it if it lands first — check for conflicts in
  `test_adapter.py`/`test_streaming.py` where the xfail/skip markers were
  added).
- Conventional commits, no `uv.lock`, push/PR only after operator instruction.

## Steps

### Step 1: Fix credential attribute access (`test_init_with_required_params`)

Decide: either restore plain `.username`/`.password`/`.api_key` properties on
`IGBrokerAdapter` that read through to `self._credentials` (least invasive —
keeps the leak-prevention wrapper, adds a thin read-only property layer), or
update the test to use dict-style/attribute access matching `_IGCredentials`.
Prefer the property approach unless something else in the codebase already
depends on `_credentials` being opaque.

**Verify**: `uv run pytest -q src/fwbg/adapters/broker/ig/test_adapter.py::TestIGBrokerAdapterInit -v` all green; remove the `xfail` marker.

### Step 2: Fix the historical-bars mock (`test_get_historical_bars_from_ig`)

Update the test to actually satisfy `_ensure_session_valid()` — e.g. patch
`connect()` to return `True` (or set `adapter._connected = True` alongside
`adapter._ig = MagicMock()`, matching whatever `_ensure_session_valid()`
currently checks) so the mocked IG response is used instead of falling
through to a real yfinance call. Confirm no other test in this file has the
same latent problem (grep for `adapter._ig = MagicMock()` without a
matching `_connected`/`_ensure_session_valid` patch).

**Verify**: run the test with network access disabled (or under a fixture
that fails any real HTTP call) to *prove* it no longer reaches yfinance;
remove the `skip` marker.

### Step 3: Root-cause the Position/AccountInfo/BarData identity mismatch

This is the one with a real unknown — investigate before changing anything:
- Reproduce with `python -m pytest --runxfail` on a single test and add a
  temporary debug print of `id(AccountInfo)` (test-side) vs.
  `id(type(info))` (returned instance) plus `sys.modules['fwbg.adapters.broker']`
  identity, to confirm which import path diverges.
- Check `pyproject.toml`'s `[tool.pytest.ini_options]` for any `pythonpath`
  entry, and whether `fwbg` is installed editable (`pip show -f fwbg` /
  `uv pip show fwbg`) pointing at the same `src/` this worktree uses — a
  stale editable install pointing at a *different* checkout would explain a
  second copy of the whole package tree.
- Only after finding the actual cause, fix it (likely a pytest config or
  install-state fix, not a source-code change) and remove the three `xfail`
  markers. Re-verify both ways: `pytest -q src/fwbg/adapters/broker/ig` in
  isolation AND the full `pytest -q` — the symptom only showed up in
  isolation, so isolation-only testing would miss a regression here.

**STOP** if the cause turns out to be a real duplicate-package-install
problem outside this repo's control (e.g. two conflicting editable installs
in the shared dev environment) — report instead of working around it in
test code.

### Step 4: Full verification

- `uv run pytest -q` — must show 0 failures and 0 unexpected passes (a
  `strict=True` xfail that now passes will itself fail the run, which is
  the intended signal to remove that marker).
- `uv run ruff check src/ packages/`

## Test plan

- All 65 tests in `src/fwbg/adapters/broker/ig/` pass without any
  xfail/skip markers referencing this plan.
- The historical-bars fallback test provably makes no network call.
- Full suite green.

## Done criteria

- [ ] `test_init_with_required_params` passes for real; marker removed.
- [ ] `test_get_historical_bars_from_ig` passes for real, without a network call; marker removed.
- [ ] `test_get_positions_returns_list`, `test_get_account_info_returns_data`, `test_processes_complete_candle` pass for real; markers removed; root cause documented in the commit message.
- [ ] Full suite + ruff green.
- [ ] `plans/README.md` row for 021 updated.

## STOP conditions

- The Position/AccountInfo/BarData identity issue traces to a shared dev
  environment problem (conflicting editable installs) rather than something
  fixable in this repo — report, don't paper over with `importlib.reload`
  hacks in test code.
- Fixing the credential-attribute access reveals other production callers
  depend on the current opaque `_credentials` wrapper for the leak-prevention
  guarantee it exists for — stop and ask before weakening that guarantee.
