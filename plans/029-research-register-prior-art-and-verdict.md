# Plan 029 — Research register: prior art and abandon verdicts (fwbg register step 6b)

**Goal:** the Researcher sees ideas the register already holds (including ones that never ran
through fwbg-agents), and abandoning a whole lineage leaves a verdict on its card instead of an
`active` card nobody works on. Builds on plan 028 (card per lineage root).

**fwbg contract:** `GET /api/research?detail=true` (fwbg #194) lists cards with `hypothesis`,
`mechanism` and `verdict` (`reason`, `reopen_if`; `null` while open). `POST
/api/research/{id}/status` takes `{status, reason, actor, reopen_if}`.

**Decisions (operator, 2026-10-25):**
- Register matches are shown, never blocking. The hard duplicate rejection stays limited to the
  strategy table.
- An abandon parks the card. Only an Analyst abandon that is confident
  (`register_reject_min_confidence`, default 0.8) and at least `min_iterations_before_abandon`
  deep rejects it.

## Design

- **Prior art:** `lookup_register_prior_art` applies the existing tag-Jaccard rule to the cards.
  Cards that a lineage root already carries (`own_research_ids`) are skipped, the strategy lookup
  covers them. The Researcher gets a second tool, `lookup_register_tool`; the prompt asks for it
  and says a rejected or parked idea is not proposed again unless its reopen condition applies.
  A register outage yields no matches, it never stops a research run.
- **Verdict:** `record_abandon_verdict` runs after the transition to ABANDONED at three sites:
  Analyst abandon, auto-runner cap, operator abandon. Auto-repair (parent replaced by its child)
  records nothing. It writes only when no member of the lineage is left that is not abandoned
  (a live strategy counts), only for a lineage that has a card, and never raises: the abandon is
  done, a failed verdict is logged, a `warning` is not retried.
- **Attach a card:** `PUT /strategies/{id}/research` sets an existing card on a lineage root that has
  none (the follow-up named in plan 028). Only a root, only while it has no other card; fwbg must
  know the card (404 if not, 502 if fwbg is unreachable). `research_id` is part of the strategy
  response, so the dashboard can show it.

## Tasks

1. `FwbgClient.list_research_cards`, `set_research_status`.
2. `lookup_register_prior_art`, `own_research_ids`; Researcher tool, prompt rule, `register_client`
   threaded through `research_flow`.
3. `record_abandon_verdict` and the three call sites; `register_reject_min_confidence`.
4. `PUT /strategies/{id}/research`, `research_id` in the strategy response.
