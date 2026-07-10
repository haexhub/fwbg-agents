# fwbg backend = single source of truth for plugins

Date: 2026-07-09
Status: proposed (aligned with user)

## Problem

There are **two divergent plugin-discovery mechanisms**:

- **fwbg runtime registry** (`src/fwbg/pipeline/registry.py`) scans the filesystem
  + each plugin's own `manifest.json` → knows **all 66** plugins; exposed via
  `GET /api/plugins`.
- **fwbg-agents `discover_fwbg_plugins`** reads only the hand-maintained **bundle
  manifest** `plugins:` list → **37** (stale; 29 real plugins undeclared, incl.
  `liquidity_sweep`, `fair_value_gap`, `market_structure`, `vwap`).

Validator + PluginPlanner use the stale filesystem path directly; the Analyst
uses the live API but falls back to the stale path when the API is unreachable
(cf. "fwbg API not served in Docker", fwbg#20). Result: the pipeline can't see
~29 real plugins → the Analyst requests building duplicates.

## Decision

The **fwbg backend is the single source of truth** for plugins.

1. **All fwbg-agents agents access plugins ONLY via the fwbg HTTP API.** No agent
   scans the filesystem or reads manifests. `discover_fwbg_plugins` (bundle-scan)
   is removed.
2. **New plugins are created/registered in the fwbg backend, not in the agents.**
   Today agent-authored plugins live in the fwbg-agents DB and are merged
   client-side (`merge_with_db`) — a second source of truth that must go away.
3. **Backend serves data; agents provide judgment.** The duplicate-detection
   (dedup) decision is LLM judgment and stays in fwbg-agents, operating over the
   plugin specs the API serves. The backend does not do semantic matching.

## Backend API surface (plugins resource)

| Purpose | Endpoint | Status |
|---|---|---|
| Find/list (filter by phase/category) | `GET /api/plugins?phase=…` | exists (registry-backed, all 66) |
| Access/detail (params, contract, description) | `GET /api/plugins/{fqn}` | exists |
| Docs | `GET /api/plugins/{fqn}/docs` | exists |
| Spec (speckit spec) | `GET /api/plugins/{fqn}/spec` | to add (Step 2) |
| Create/register a new plugin | `POST /api/plugins` (or authoring flow) | to design (Step 3) |

## Steps

### Step 1 — fwbg-agents API-only catalog (now)
- Validator + PluginPlanner consume the API-sourced catalog (via
  `fetch_live_catalog` / a shared catalog client), not `discover_fwbg_plugins`.
- Remove the bundle-manifest scan. All agents then see all 66 plugins.
- Verify: catalog returns all on-disk plugins; a strategy referencing a
  previously-undeclared plugin (e.g. `liquidity_sweep`) passes validation.

### Step 2 — backend serves specs + agents consume
- `GET /api/plugins/{fqn}/spec` in fwbg; speckit specs (co-located in the fwbg
  repo) served through it. Resume the spec backfill to populate them.
- Dedup gate matches a new capability against API-served specs.

### Step 3 — plugin creation lands in the backend (agreed design)
The agent stays the builder; only the *landing* moves. Clear split:
- **fwbg-agents = LLM/authoring** — keep the existing PluginPlanner (opus-4-8) →
  PluginImplementer (opus-4-7) → deterministic PluginEvaluator (the real quality
  gate). No LLM moves into fwbg.
- **fwbg backend = registry / source of truth** — accepts the *verified* plugin.

Mechanism (fast path): a fwbg register endpoint (e.g. `POST /api/plugins`) writes
the verified plugin into a writable "agent-authored" bundle on the
**fwbg-workspace volume** (survives redeploys), re-validates (runs the plugin's
own tests), and refreshes the registry so it appears in `/api/plugins`
immediately. This retires the fwbg-agents DB `merge_with_db` second source and
keeps the autonomous loop fast (no git/PR/redeploy stall).

Promotion (slow path, later, human-gated): a runtime plugin that proves itself in
real backtests can be promoted into the fwbg **git repo** as a permanent,
versioned, CI-checked plugin (a PR) — this is where git-level creation belongs,
as a promotion of the proven, not the primary authoring path.

Tradeoff: runtime plugins live only on the volume (not in git/image) until
promoted — acceptable given a stable volume + the promotion step.

Note: this changes the CLAUDE.md critical safety rule "Generated plugins live in
`data/plugins/` only" — the implementing PR must update it (see
`2026-07-09-plugin-creation-in-backend.md`, Deliverables); the "never
auto-committed to the fwbg git repo" clause stays true.

## Consequences
- **Hard dependency on a reachable fwbg API.** No local fallback → fwbg#20
  (API reliably served in Docker) becomes a prerequisite.
- Supersedes the filesystem-centric parts of
  `2026-07-07-speckit-plugin-workflow.md`: spec *content* + co-located storage
  stay; *consumption* is via the API, not fwbg-agents scanning the fwbg repo.

## Parked
- Spec backfill: 25 specs written then paused (uncommitted in the fwbg working
  tree). Preserve for Step 2.
