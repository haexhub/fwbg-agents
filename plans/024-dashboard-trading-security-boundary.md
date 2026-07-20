# Plan 024: Protect dashboard credentials and trading mutations

> **Executor**: Work in a disposable `fwbg-dashboard` branch. Preserve emergency-stop reliability, run every gate, and never print/copy credential values. The reviewer maintains the index.
>
> **Drift check**: `git -C ../fwbg-dashboard diff --stat 8e26fc2..HEAD -- server/api/settings server/api/agents server/utils/settings-types.ts server/middleware tests package.json`.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: 011
- **Category**: security
- **Planned at**: dashboard `8e26fc2`, 2026-07-20

## Why this matters

Account GET responses include broker credentials. Live promotion, emergency stop, and agent configuration have no server-side user authorization. A reachable dashboard therefore exposes money-adjacent actions and stored secrets.

## Current state

- `server/api/settings/[account]/info.get.ts:21-29` returns complete `AccountInfo`.
- `server/utils/settings-types.ts:20-25` includes `credentials` in that response.
- `server/api/agents/strategies/[id]/promote-live.post.ts:7-12` forwards promotion without authorization.
- `server/api/settings/[account]/emergency-stop.post.ts:4-53` closes positions/deactivates an account without identity checks.

## Commands

- `cd ../fwbg-dashboard && bun x vue-tsc --noEmit && bun run test:run` → pass.
- `cd ../fwbg-dashboard && bun run test:e2e` → pass against controlled fixtures.
- `cd ../fwbg-dashboard && bun run build` → pass.

## Scope

In scope: central session/auth middleware; viewer/operator/admin authorization; sanitized read/write DTOs; service-auth forwarding; critical route tests and CI scripts. Out of scope: broker API changes, browser credential storage, weakening emergency stop, public runtime service keys, or automatic real-secret rotation.

## Steps

1. Define permissions: viewer reads; operator research/backtests/emergency stop; admin credentials/config/live promotion. Use server-side identity with HttpOnly, Secure-in-production, SameSite session and CSRF protection for mutations.
2. Add deny-by-default route authorization for settings, mutating agent routes, and trading routes. Emergency stop requires operator/admin but must not depend on nonessential upstream services.
3. Split persistence/write and public account DTOs. GET returns metadata plus `configured` flags only—never values or reversible masks. Updates are write-only and preserve omitted values.
4. Update UI to configured/replace/remove semantics. Replace tests expecting credentials and recursively assert responses/logs contain no secret fields/values. If remotely exposed previously, report operator-led rotation without reading values.
5. Consolidate backend transport/auth/error handling and forward service keys from private runtime config only; never `runtimeConfig.public`.
6. Test anonymous/viewer/operator/admin across reads, credential writes, agent config, live promotion, and emergency stop, including CSRF negatives and secret-free audit records.
7. Add stable `typecheck`, `test:unit`, `test:contract`, and `check` scripts and use them in CI. Repair paginated-runs expectations and never accept HTTP 500 as success.

## Done criteria

- [ ] GET responses contain no broker credential values.
- [ ] Protected reads/mutations reject anonymous users.
- [ ] Role matrix and CSRF behavior are tested.
- [ ] Live promotion/credentials require admin; emergency stop operator/admin.
- [ ] Service keys remain server-private and reach both backends.
- [ ] Typecheck, tests and build pass in CI.

## STOP conditions

- Deployment identity/session strategy is unknown; request a maintainer choice rather than inventing one.
- Emergency stop would depend on an unavailable service during incidents.
- External clients require raw credential GET responses.
- A tracked real secret is found; report type/location and request rotation only.

## Maintenance

UI visibility is not authorization. Every new money-adjacent endpoint needs an explicit permission and negative authorization tests.
