# Agent-driven spec-kit workflow for plugins/indicators

Date: 2026-07-07
Status: proposed

## Goal

Make plugin/indicator creation spec-driven (adapted from GitHub spec-kit), run
autonomously by the agents, and produce a spec document for **every**
plugin/indicator. The primary payoff: **no more duplicate plugins** — a new
capability is matched against existing specs before anything is built.

This is not the GitHub spec-kit CLI (interactive, human-driven, no Python API).
We adopt its *methodology and artifacts* and drive them from our own pipeline.

## Confirmed decisions

1. **Scope: lean.** Per plugin: a structured `spec.md` + `plan.md`, plus one
   project-level `constitution.md`. No per-plugin `tasks.md` (ceremony for a
   single-file plugin).
2. **Storage: co-located.** Native plugins get their `spec.md` next to their
   source in the **fwbg** repo; agent-authored plugins under
   `fwbg-agents/data/plugins/<slug>/v1/`. Spec is versioned with the plugin.
3. **Backfill now.** A one-time agent generates specs for all existing plugins
   (37 catalogued + ~28 on-disk-but-unregistered — count to be reconciled),
   so dedup can match against the current catalog.

## spec-kit → plugin mapping

| spec-kit | Adapted | Today |
|---|---|---|
| `constitution.md` | Plugin contract + conventions + **one** canonical category/phase vocabulary | scattered in `prompts/plugin_authoring.md` + ~5 mapping tables |
| `spec.md` | capability, inputs/params, outputs/feature-columns, acceptance invariants, edge cases → **dedup anchor** | free-text `spec_md` (`min_length=80`, unstructured, never read) |
| `plan.md` | phase/category, base class, algorithm sketch, deps, test scenarios | already the structured `PluginPlan` (transient `plan.json`) |
| `tasks.md` | dropped | — |

## Phases

### Phase 0 — Foundation (fwbg-agents)
- Populate the empty `src/fwbg_agents/speckit/` package:
  - `PluginSpec` Pydantic model (validated) + a markdown renderer. Replaces the
    free-text `spec_md: str` on `PluginAuthorResult`
    (`agents/plugin_authoring_shared.py:65`).
  - `plugin-constitution.md`: the MUST-rules distilled from
    `prompts/plugin_authoring.md` + the plugin contract; declares the single
    canonical category/phase vocabulary.
- Verify: unit tests for `PluginSpec` schema + renderer; existing plugin-author
  tests still green.

### Phase 1 — Backfill spec corpus
- One-time agent: read each existing plugin source (fwbg-core + fwbg-premium),
  emit a `PluginSpec` + `spec.md`. Native specs are written into the **fwbg**
  repo, co-located with source (separate PR, branch from `develop`).
- Build a compact **spec index** (slug → category → one-line capability) for
  dedup and the analyst snapshot.
- Verify: every catalogued plugin has a spec; index loads; spot-check a few
  specs against source.

### Phase 2 — Dedup gate
- `find_existing_plugin_for_capability(spec, corpus)`: LLM gate over
  category-filtered candidates (category narrows first, LLM decides semantic
  match). Returns an existing slug or none.
- Wire into `orchestrator/plugin_flow.py::author_plugin_from_strategy`: match
  first → **reuse** via `reiterate_with_plugin(existing_slug)`, skip authoring.
- Feed capability summaries into the Analyst catalog snapshot
  (`agents/analyst.py:454-455`) so it prefers `ModifyPlugins` over
  `AddIndicator` when a capability already exists.
- Verify: a duplicate `add_indicator_request` for an existing capability
  reuses instead of authoring; a genuinely new capability still authors.

### Phase 3 — Reshape authoring into spec-kit phases
- Planner: consume/emit the structured `PluginSpec` (`/specify`) and persist
  `plan.md` (`/plan`) with a constitution check.
- Implementer: write code conforming to spec + plan; `spec.md` is now
  authoritative from `/specify` (not a free-text afterthought).
- Persist the full doc set per plugin (constitution ref + spec.md + plan.md).
- Verify: full author→evaluate chain green end-to-end; docs present and valid.

## Cross-repo note
Native plugin specs live in the **fwbg** repo → Phases 1 and 3 touch two repos,
each via its own PR off `develop` (Gitflow).

## Risks / open items
- Reconcile authoritative plugin count (bundle manifests declare 37; ~65 slug
  dirs on disk).
- Category/phase vocabulary is normalized in ~5 places today; the constitution
  fixes one canonical set — migrate the mapping tables toward it *surgically*,
  not a big-bang rewrite.
- Backfill is ~37+ LLM calls; the pipeline currently hits Anthropic timeout
  errors (tracked separately — see the api_errors.py hardening). Run backfill
  when the API is stable.
