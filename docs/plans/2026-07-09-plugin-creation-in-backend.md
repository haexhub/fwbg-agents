# Step 3 — plugin creation lands in the fwbg backend

Date: 2026-07-09
Status: proposed (design agreed with user)
Scope: fwbg + fwbg-agents (two repos, two PRs)

## Goal

Runtime-authored plugins become part of the **one** registry (fwbg), not a
second source of truth in the fwbg-agents DB. The agent stays the builder; only
the *landing* moves. Retire `merge_with_db` (client-side DB→catalog injection).

Non-goal: moving any LLM/authoring into fwbg. fwbg is the registry/execution
engine; fwbg-agents keeps Planner→Implementer→Evaluator.

## Why this shape (recap)

- The autonomous loop needs the plugin **immediately** (`reiterate_with_plugin`),
  so git-PR-then-redeploy can't be the primary path.
- The `agents` service mounts the fwbg repo **read-only** (`.:/fwbg-src:ro`), so
  agents *cannot* write plugins into fwbg's tree — the API write path is the only
  option (this validates the design).
- Security: agent-generated plugin code already executes in the `api` process
  during backtests today — the register endpoint adds no new trust boundary.

## Current end-state (what changes)

Today `author_plugin_from_strategy` (plugin_flow.py) writes
`fwbg-agents/data/plugins/<slug>/v1/{plugin.py, contract.yaml, spec.md}`, creates
a `Plugin` DB row (SPECIFIED→AUTHORED→VERIFIED), and `merge_with_db`
(plugin_catalog.py) injects VERIFIED DB plugins into the catalog client-side.
`reiterate_with_plugin` requires the DB plugin VERIFIED.

fwbg's `PluginRegistry` (pipeline/registry.py) discovers plugins by scanning
`get_core_plugins_dir()` (src/fwbg/plugins, in the image), **`get_user_plugins_dir()`
(`~/.fwbg/plugins/`)**, and entry-point packages — each `<bundle>/<category>/<slug>/`
needs `__init__.py` + `manifest.json`. `get_registry()` is a process singleton;
`reset_registry()` exists.

## Phase 3.1 — fwbg: register endpoint + persistence + refresh

1. **Writable, persistent plugin location.** `~/.fwbg/plugins/` is scanned but is
   NOT on a volume today (workspace volume is at `/root/fwbg`, not `/root/.fwbg`).
   Fix one of:
   - add a volume mount for `/root/.fwbg` (or `/root/.fwbg/plugins`) to the `api`
     (and `ig-bot`) services in docker-compose, or
   - make the user-plugins dir configurable (env, e.g. `FWBG_USER_PLUGINS_DIR`)
     pointing at a path on an existing volume.
   Recommended: a dedicated `fwbg-agent-plugins` volume mounted on both `api` and
   `ig-bot` so live trading can use authored plugins too.
   **Explicit decision required:** mounting on `ig-bot` lets agent-generated code
   (machine-verified only, no human review) execute in the live-trading process —
   the "no new trust boundary" argument above covers only the backtest `api`
   process. Either accept this consciously, or initially mount only on `api` and
   gate the `ig-bot` mount on Phase 3.4 promotion.
2. **`POST /api/plugins`** — accept a plugin payload: `slug`, `python_code`,
   `contract` (yaml/json), `spec_md`, `tests_code`. The endpoint:
   - derives category from `contract.kind`; writes
     `<user_plugins>/agent-authored/<category>/<slug>/{__init__.py, manifest.json,
     tests.py, spec.md, contract.yaml}`. **Transforms:** `python_code`→`__init__.py`;
     generate `manifest.json` (`{name, version, phase, description}`) from the
     contract (fwbg discovery requires a per-plugin manifest.json).
   - **re-validates before committing**: contract parses; module imports as a
     `BasePlugin` subclass with `name == slug` and a valid `phase`; runs the
     plugin's `tests.py` (reuse the `/tests/run` machinery) incl. the mandatory
     no-lookahead test for indicators. Reject (4xx) with a clear reason on failure
     — fwbg is the gatekeeper of its own registry.
   - refreshes discovery so the plugin is immediately in `/api/plugins`
     (`reset_registry()` + re-scan, or a targeted `discover_package`).
   - returns the `fqn` (`agent-authored:<slug>`).
   - idempotency: 409 (or overwrite semantics) if the slug already exists.
3. Tests: register a valid plugin → appears in `GET /api/plugins` and
   `/{fqn}/source` + `/{fqn}/spec`; register invalid code → 4xx, not registered.

## Phase 3.2 — fwbg-agents: ship verified plugins to fwbg

1. `FwbgClient.register_plugin(payload)` → `POST /api/plugins`.
2. In `author_plugin_from_strategy`, after the plugin VERIFIES, **register it with
   fwbg** (send code + contract + spec + tests). Keep the `Plugin` DB row for
   authoring lineage/state, but it is no longer the catalog source.
3. `reiterate_with_plugin` references the fwbg-registered plugin (fqn/slug now in
   `/api/plugins`); precondition becomes "registered in fwbg" instead of
   "DB VERIFIED + merged".
4. The Evaluator stays as the agent-side pre-check; fwbg re-validates as the
   gatekeeper (defense in depth).

## Phase 3.3 — retire the second source of truth

- Remove `merge_with_db` from the catalog build (`live_catalog`): the catalog is
  purely `/api/plugins`. Keep `Plugin`/DB for lineage only.
- Delete `merge_with_db` + `_VISIBLE_PLUGIN_STATES` + `_KIND_TO_CATEGORY` if
  nothing else uses them after the switch (grep first).

## Phase 3.4 — promotion (later, optional, human-gated)

A runtime plugin that proves itself in real backtests can be promoted into the
fwbg **git repo** as a permanent, versioned, CI-checked plugin via a PR. This is
the only place git-level creation belongs — a promotion of the proven, not the
authoring path. Out of scope for the initial implementation.

## Open questions / risks

- **Two fwbg processes** (`api`, `ig-bot`) each have their own registry singleton.
  Registering in `api` covers backtests (the loop). Live trading (`ig-bot`) needs
  the plugin too — with the shared volume it picks it up on next restart; a
  cross-process refresh signal is a later nicety.
- **Persistence** hinges on the user-plugins dir being on a volume (Phase 3.1).
- **Cleanup**: authored-but-superseded plugins accumulate on the volume — a
  retention/GC policy is a follow-up.
- `exit_modifiers` has no `PluginKind` (1 plugin has no spec today) — unrelated
  vocab gap, fix separately if needed.

## Deliverables
- fwbg PR: register endpoint + docker-compose volume + validation + tests.
- fwbg-agents PR: `register_plugin` client + author-flow wiring + drop
  `merge_with_db`.
- fwbg-agents PR also updates the CLAUDE.md critical safety rule ("Generated
  plugins live in `data/plugins/` only"): generated plugins live in
  `data/plugins/` (authoring lineage) **and** the fwbg agent-plugins volume
  (runtime registry); they are still never auto-committed to the fwbg git repo —
  promotion to git remains a human-reviewed PR (Phase 3.4).
