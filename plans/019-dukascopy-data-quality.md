# Plan 019: Data-quality report at Dukascopy ingest (distinguish "no edge" from "broken data")

> **Executor instructions**: This plan is executed in the **fwbg** repo
> (`/home/haex/Projekte/fwbg`). The local checkout there sits on a stale
> feature branch 25 commits behind `origin/main` — run `git fetch origin` and
> branch from `origin/main`. Follow the steps; run every verification; on any
> STOP condition, stop and report. When done, update the status row in
> fwbg-agents' `plans/README.md` (the cross-repo index).
>
> **Drift check (run first, in fwbg)**:
> `git diff --stat 8ab08f7..origin/main -- src/fwbg/data/dukascopy.py`
> → expect empty (plan written against origin/main `8ab08f7`). On mismatch,
> re-read the file and compare against the excerpts below; STOP on
> contradiction.

## Status

- **Priority**: P2 (cheap lever, high diagnostic value)
- **Effort**: M
- **Risk**: LOW (warn-only; no behavior change to backtests)
- **Depends on**: none
- **Category**: direction / correctness (data validity)
- **Planned at**: fwbg commit `8ab08f7` (origin/main), 2026-07-16

## Why this matters

The whole strategy factory stands on one data source: Dukascopy bars written
to CSV at download time. The downloader aligns bid/ask, writes mid-OHLC and a
static spread — but nothing checks the result: no gap detection against the
expected bar frequency, no zero/broken-volume ratio, no monotonic-timestamp
or duplicate check, no coverage-vs-requested-range check. A symbol-timeframe
with silent holes (weekend rollovers, DST shifts, thin-liquidity gaps, a
partial download) produces a plausible-looking backtest that the entire
agent loop then trusts — bad data becomes an invisible, systematic source of
false edges, and the loop cannot distinguish "no edge" from "broken data".
After this plan, every download writes a machine-readable quality report next
to the CSV, surfaces WARN-level anomalies in the download result (API/CLI),
and downstream consumers (the agents' Analyst) have a stable artifact to
read. Warn-only by design — no hard gate yet.

## Current state

- `src/fwbg/data/dukascopy.py` — the only ingest path.
  - `download(...)` (line ~230 on origin/main): docstring documents mid-OHLC +
    90th-percentile spread; returns
    `[{symbol, file, rows, spread[, warning]}, ...]`.
  - The write block (lines ~316–360):

    ```python
    bid, ask = bid.align(ask, join="inner", axis=0)
    ...
    ts = pd.DatetimeIndex(bid.index)
    ts = ts.tz_convert("UTC") if ts.tz is not None else ts.tz_localize("UTC")
    ...
    out = pd.DataFrame({"T": ts.strftime(...), "O": ..., "H": ..., "L": ..., "C": ..., "V": volume})
    out.to_csv(dest, index=False)
    gap = ask["close"].to_numpy() - bid["close"].to_numpy()
    ...
    spread = float(np.percentile(gap, 90)) if gap.size else 0.0
    ```

  - Existing partial checks worth keeping distinct: "no data in range" and
    "no overlapping bid/ask bars" warnings (lines ~310–324).
  - `TIMEFRAMES` maps timeframe strings to Dukascopy intervals (top of file) —
    the source for "expected bar spacing".
- There is **no** validation module under `src/fwbg/data/` (verify:
  `ls src/fwbg/data/`).
- Forex market-hours reality the checks must respect: bars exist ~24/5;
  weekends (Fri ~21/22:00 UTC → Sun ~21/22:00 UTC, DST-dependent) have no
  bars. Gap detection must therefore ignore weekend gaps rather than flag
  them (heuristic: a gap whose interior lies entirely within
  Friday-evening→Sunday-evening UTC is expected).
- Callers of `download`: find them with `grep -rn "dukascopy" src/fwbg/api/
  src/fwbg/cli/ --include='*.py' -l` — the API data router and the CLI pass
  the result dicts through to the user; adding keys to the dict is additive
  and safe.

## Commands you will need

| Purpose | Command (in fwbg) | Expected |
|---|---|---|
| Tests | `uv run pytest -q` | all pass |
| Lint | `uv run ruff check src/ packages/` | exit 0 |
| Focused tests | `uv run pytest -q tests/ -k data_quality` | new tests pass |

## Scope

**In scope**:
- `src/fwbg/data/quality.py` (create — pure functions, no I/O except by caller)
- `src/fwbg/data/dukascopy.py` (call the check + write the report + extend the result dict)
- `tests/data/test_quality.py` (create; if `tests/data/` doesn't exist, put it where the existing dukascopy tests live — find with `grep -rln dukascopy tests/`)

**Out of scope**:
- No hard gate: never refuse to write the CSV, never fail the download.
- The backtest loader (`optimization/process.py`) — consuming the report at
  backtest time is the explicit follow-up, not this plan.
- fwbg-agents repo entirely (the Analyst can read the report later).
- Spread modeling changes.

## Git workflow

- In fwbg: `git fetch origin`, branch `advisor/019-data-quality` off
  `origin/main`. Conventional commits; no Claude/Anthropic references.
  No push/PR unless instructed.

## Steps

### Step 1: pure quality checks

`src/fwbg/data/quality.py` with one entry point:

```python
def assess_bars(df: pd.DataFrame, *, timeframe: str, requested_start: datetime,
                requested_end: datetime) -> dict:
    """Quality report for a mid-OHLC bar frame (columns T,O,H,L,C,V or a
    DatetimeIndex + OHLCV). Pure — no I/O."""
```

Report fields (all computed, none optional):

- `n_bars`, `first_bar`, `last_bar` (ISO strings)
- `coverage`: `(last_bar - first_bar) / (requested_end - requested_start)` clamped to [0,1]
- `expected_spacing_seconds` (from the timeframe), `n_gaps`: count of
  consecutive-bar deltas > 1.5× expected spacing **excluding** weekend gaps
  (heuristic from "Current state"), `max_gap_seconds` (same exclusion),
  `weekend_gaps_ignored`: count
- `non_monotonic`: count of timestamp deltas ≤ 0; `duplicate_timestamps`: count
- `zero_volume_ratio`: share of bars with `V == 0` (note: the writer stores
  `volume = 0` scalar when either side lacks volume — a 1.0 ratio therefore
  means "volume unavailable", document that in the field's docstring)
- `ohlc_violations`: count of bars where not `L <= min(O,C) <= max(O,C) <= H`
- `nan_bars`: rows with any non-finite OHLC
- `warnings`: list[str] — one entry per threshold breach; thresholds as
  module constants with comments: `coverage < 0.95`, `n_gaps > 0`,
  `non_monotonic > 0`, `duplicate_timestamps > 0`, `ohlc_violations > 0`,
  `nan_bars > 0`. (Deliberately no threshold on `zero_volume_ratio` — forex
  volume is unreliable; report only.)

**Verify**: `uv run pytest -q tests/ -k data_quality` → unit tests from the
Test plan pass.

### Step 2: wire into the downloader

In `dukascopy.py` after `out.to_csv(dest, index=False)`:

- `report = assess_bars(...)` on the just-built frame (it has `ts` and the
  mid arrays in scope — pass a frame with a DatetimeIndex, don't re-read the CSV).
- Add the assumed spread into the report: `report["spread_p90"] = spread`.
- Write `dest.with_suffix(".quality.json")` (i.e.
  `EURUSD_H1.quality.json` next to `EURUSD_H1.csv`),
  `json.dumps(report, indent=2)`.
- Extend the per-symbol result dict: `"quality": {"warnings": report["warnings"], "coverage": report["coverage"], "n_gaps": report["n_gaps"]}`
  (keep the dict small — the full report is on disk); `log.warning` each
  warning string prefixed with the symbol.

**Verify**: integration test — build a small synthetic bid/ask fixture the way
existing dukascopy tests do (monkeypatched fetch; find the pattern via
`grep -rln "dukascopy" tests/`), run `download`, assert the `.quality.json`
exists and the result dict carries `quality.warnings`.

### Step 3: full gates

**Verify**: `uv run pytest -q && uv run ruff check src/ packages/` → exit 0.

## Test plan

Unit tests (`test_quality.py`), each on small synthetic frames:

- clean hourly week → zero warnings, `weekend_gaps_ignored ≥ 1`, coverage ≈ 1
- a 5-hour hole on a Wednesday → `n_gaps == 1`, warning present
- weekend-only gap → `n_gaps == 0`, `weekend_gaps_ignored` counts it
- duplicate + backwards timestamp → both counters fire
- `H < L` bar → `ohlc_violations == 1`
- NaN close → `nan_bars == 1`
- requested range twice the data range → coverage ≈ 0.5 + warning
- Integration test from Step 2.

## Done criteria

- [ ] `uv run pytest -q` and `uv run ruff check src/ packages/` exit 0 in fwbg
- [ ] Every fresh download writes `<SYMBOL>_<TF>.quality.json`
- [ ] Download result dicts contain `quality.warnings` (API/CLI see them without further changes)
- [ ] A download is never blocked by the checks (warn-only — test proves CSV written even with warnings)
- [ ] No files outside the in-scope list modified
- [ ] fwbg-agents `plans/README.md` status row updated

## STOP conditions

- The write block in `dukascopy.py` no longer matches the excerpt (upstream
  moved past `8ab08f7` in that area) — re-anchor or report.
- Existing dukascopy tests have no monkeypatch pattern for the fetcher and a
  real network call would be needed — report; do not hit the network in tests.
- The weekend heuristic misfires on the fixture (symbol conventions differ) —
  report the observed bar pattern instead of loosening thresholds silently.

## Maintenance notes

- Explicit follow-ups, deferred: (a) consume the report at backtest time
  (refuse/flag runs whose data has warnings) — that's the actual gate;
  (b) surface `quality.warnings` in the fwbg-agents Runner so the Analyst can
  tell data failure from strategy failure; (c) a re-validation pass over
  already-downloaded CSVs (`assess_bars` is pure, so a small script can batch
  it later).
- Thresholds are constants by design — when the maintainer tunes them,
  they're one place, commented.
- Plan 016's fidelity ratio is the live-side counterpart of `spread_p90`
  recorded here.
