# Plan 025: Establish dashboard identity, authorization, and service boundaries

> **Executor instructions**: This is a cross-repository plan for
> `fwbg-dashboard`, `fwbg-agents`, and `fwbg`. Follow it step by step and keep
> one branch/PR per repository. Run every verification command and confirm the
> expected result before moving on. If anything in "STOP conditions" occurs,
> stop and report; do not invent an identity provider, role mapping, or rollout
> policy. Update this plan's row in `fwbg-agents/plans/README.md` only after all
> three PRs and the deployment checks are complete.
>
> **Drift check (run first)**:
>
> Run each command from the named repository; do not assume sibling paths:
>
> ```bash
> # fwbg-dashboard
> git diff --stat 8e26fc2..HEAD -- package.json bun.lock nuxt.config.ts app.vue middleware components pages server types tests playwright.config.ts
> # fwbg-agents (origin/develop snapshot, before stacked Plans 014/015)
> git diff --stat 39cf73d..HEAD -- src/fwbg_agents tests .env.example README.md
> # fwbg (Plan 011/PR #146 snapshot)
> git diff --stat 7c56c82..HEAD -- src/fwbg tests docker-compose.yml .env.example README.md
> ```
>
> The source snapshots intentionally differ by repository. If an in-scope file
> changed, compare the live code with "Current state" before continuing. A
> semantic mismatch is a STOP condition.

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 011; this plan is the concrete completion design for the
  blocked Plan 024, not a dependency on Plan 024
- **Category**: security, architecture, migration
- **Planned at**: plan-index commit `371eb50`, 2026-07-20; audited source
  snapshots: fwbg-dashboard `8e26fc2`, fwbg-agents `39cf73d`, fwbg `7c56c82`

## Why this matters

The dashboard currently exposes trading state, broker credentials, research
controls, emergency-stop actions, and live-promotion actions without a user
identity or server-side authorization boundary. The two backend services also
cannot distinguish the dashboard from another caller, and secret-bearing
objects are returned to browser code. A single compromised browser session or
reachable port can therefore become a credential disclosure or money-moving
incident.

This plan establishes one reusable security architecture rather than adding
ad-hoc checks to individual endpoints: OIDC login and a sealed local session in
Nuxt, permission-based route policy, CSRF/origin checks, secret-safe DTOs, and
separate service principals for dashboard and agents. The local sealed session
is deliberate: an already authenticated operator must still be able to invoke
the emergency stop during a temporary identity-provider outage.

## Decisions fixed by this plan

These are implementation constraints, not open choices for the executor:

1. `fwbg-dashboard` owns browser authentication. Use OpenID Connect
   Authorization Code flow with PKCE through `openid-client`. Use
   `nuxt-auth-utils` only for its sealed HttpOnly session machinery. Do not use
   an OIDC helper that merely decodes an ID token; signature, issuer, audience,
   expiry, state, nonce, and PKCE validation are mandatory.
2. Authorization is permission-based. Roles are an input mapping, not the API
   abstraction. The fixed permissions are `view`, `operate_research`,
   `emergency_stop`, `manage_config`, `manage_secrets`, and `promote_live`.
3. The built-in roles are:
   - `viewer`: `view`
   - `operator`: `view`, `operate_research`, `emergency_stop`
   - `admin`: all six permissions
4. All dashboard API, SSE, and WebSocket routes require `view` unless an exact
   policy rule requires a stronger permission. Unknown routes deny by default.
   Only login, callback, session bootstrap, and a minimal process-liveness
   endpoint may be public.
5. Unsafe browser requests (`POST`, `PUT`, `PATCH`, `DELETE`) require both a
   session-bound CSRF token and an exact trusted `Origin`. WebSocket upgrades
   require a valid session and trusted `Origin`. SSE uses the normal session.
6. Browser code never receives broker passwords/tokens, datasource API keys or
   connection strings, service API keys, or LLM provider keys. Redacted DTOs
   expose only `configured: boolean` per secret field. Patch semantics are:
   omitted = preserve, `null` = clear, non-empty string = replace.
7. Service credentials are distinct. The dashboard and agents must not share a
   fwbg key, and the agents key must not authorize a future human-only plugin
   adoption endpoint.
8. Steady-state production has no unauthenticated bypass. The only exception is
   Step 7's time-bounded internal-only rollout `compat` mode after host-port
   removal; it has an explicit removal gate. Development bypasses are
   local-only and rejected when the process detects production mode.
9. All three services use one validated `DEPLOYMENT_ENV` enum
   (`development|test|production`). Production startup rejects dev/fake-broker
   flags, missing canonical HTTPS configuration, known placeholder keys, keys
   shorter than 32 characters, and equal dashboard/agents/legacy service keys.

Reference documentation:

- `openid-client`: <https://github.com/panva/openid-client>
- Nuxt Auth Utils: <https://nuxt.com/modules/auth-utils>

## Current state

### Dashboard

- `fwbg-dashboard/nuxt.config.ts:8-20` registers UI/Pinia/MDC only. Runtime
  config contains backend URLs but no OIDC issuer, client, session secret, or
  server-side service key.
- `fwbg-dashboard/server/utils/fwbg-api.ts:8-41` and
  `server/utils/fwbg-agents-api.ts:8-41` duplicate transport code, read
  `process.env` at module load, accept caller-supplied headers after defaults,
  and forward full upstream error bodies.
- `fwbg-dashboard/server/api/settings/[account]/info.get.ts:21-29` returns the
  entire account object. Its credentials are therefore serialized to the
  browser even when the UI visually masks them.
- `fwbg-dashboard/server/api/settings/[account]/emergency-stop.post.ts:22-52`
  closes live positions and disables an account without authentication,
  authorization, CSRF, or audit attribution.
- `fwbg-dashboard/server/api/agents/strategies/[id]/promote-live.post.ts:7-13`
  forwards a live-trading promotion without an identity check.
- `fwbg-dashboard/components/ai/AiChat.vue:146-159,197-218,264` reads and writes
  provider keys in `localStorage` and sends them in the request body.
- `fwbg-dashboard/server/api/ai/chat.post.ts:386-400` accepts that browser key
  as a fallback to server configuration.
- `fwbg-dashboard/server/routes/ws/chart.ts:100-160` and
  `server/routes/ws/positions.ts:130-203` accept unauthenticated upgrades; the
  latter exposes balances, positions, and P&L.
- There are roughly 139 `server/api/**` handlers. A manual one-by-one policy
  without an inventory test will drift as new routes are added.

### fwbg-agents

- `src/fwbg_agents/main.py:55-80` installs permissive localhost CORS and mounts
  all routers without incoming authentication.
- `src/fwbg_agents/config.py:100-105` has only the outbound fwbg API key.
- `src/fwbg_agents/api/secrets.py` and the strategy/config/research endpoints
  are reachable under the same unauthenticated application.
- Only exact `/healthz` is suitable for public liveness. Database/proxy health
  reveals dependencies and must be authenticated.

### fwbg

- PR #146 adds a fail-closed single `FWBG_API_KEY` boundary and is the exemplar
  for constant-time key comparison. A single undifferentiated key cannot
  express that dashboard admins may approve a plugin while agents may only
  submit one for validation.
- `src/fwbg/api/datasources.py:85-106` serializes `DataSourceConfig.to_dict()`;
  `src/fwbg/core/data_sources.py:405-424` shows that source objects can contain
  REST API keys, WebSocket headers, and database connection strings.

### Deployment

- `fwbg/docker-compose.yml` publishes the dashboard and agents API, uses a
  shared internal network, and currently permits rollout combinations in which
  one service requires a key that its caller does not send.
- Existing plans use additive compatibility first and fail-closed activation
  second. Follow that pattern; do not turn on strict enforcement before every
  caller is ready.

## Commands you will need

Run commands from the named repository root.

| Repository | Purpose | Command | Expected on success |
|---|---|---|---|
| dashboard | Install | `bun install --frozen-lockfile` | exit 0 |
| dashboard | Typecheck | `bun run nuxi typecheck` | exit 0, no errors |
| dashboard | Unit/integration | `bun run test:run -- --passWithNoTests` | all pass |
| dashboard | Build | `bun run build` | exit 0 |
| dashboard | Browser E2E | `bun run test:e2e` | all selected auth/RBAC tests pass |
| dashboard | Dependency audit | `bun audit --audit-level high` | no high/critical reachable advisory |
| agents | Focused tests | `uv run pytest tests/test_main.py tests/api/test_auth.py tests/api/test_secrets.py tests/tools/test_fwbg_client.py -q` | all pass; create the two auth/main files if absent |
| agents | Full tests | `uv run pytest` | all pass apart from explicitly proven pre-existing worktree fixture issues |
| agents | Lint | `uv run ruff check src tests` | exit 0 |
| agents | Format check | `uv run ruff format --check src tests` | exit 0 |
| agents | Types | `uv run mypy src` | exit 0 |
| fwbg | Focused tests | `uv run pytest tests/test_api.py tests/test_api_auth.py tests/test_datasources.py -q` | all pass; create `test_api_auth.py` if absent |
| fwbg | Full tests | `uv run pytest` | all pass apart from documented skips |
| fwbg | Lint | `uv run ruff check src packages tests` | exit 0 |
| fwbg | Format | `uv run ruff format --check src packages tests` | exit 0 |
| deployment | Render | `docker compose config --no-interpolate --quiet` | exit 0 without printing interpolated values |

Do not put credentials on command lines or in committed fixtures. Tests use
obviously fake placeholders generated inside the test process.

## Scope

### In scope

`fwbg-dashboard`:

- `package.json`, `bun.lock`, `nuxt.config.ts`, `.env.example`, `README.md`
- new `shared/auth.ts`, `server/utils/auth/**`, `server/middleware/auth.ts`
- new `server/utils/security-audit.ts` and a global client migration plugin that
  removes historical AI-key localStorage entries without reading them
- new auth/session/CSRF routes under `server/routes/auth/**` and
  `server/api/auth/**`
- `server/utils/backend-client.ts`; existing `fwbg-api.ts` and
  `fwbg-agents-api.ts` become compatibility delegates, then may be removed when
  no imports remain
- every `server/api/**` handler only as needed to map policy, use the central
  transport, validate patch DTOs, or remove secret fields
- `server/routes/ws/chart.ts`, `server/routes/ws/positions.ts`
- `components/ai/AiChat.vue`, account/datasource forms and their types/tests
- auth and route-policy unit/integration tests plus focused Playwright fixtures

`fwbg-agents`:

- `src/fwbg_agents/main.py`, `config.py`, a new `api_auth.py`
- API response models/handlers that expose secrets or internal filesystem paths
- `tests/**` for API authentication, redaction, and outbound principal behavior
- `.env.example`, `README.md`

`fwbg`:

- the API-key middleware/config introduced by PR #146
- datasource/account response DTOs and mutation schemas
- authentication/redaction tests, `.env.example`, `README.md`
- `docker-compose.yml` and a hermetic local-build security-test override with
  fake credentials, fake broker, and project-scoped temporary volumes

### Out of scope

- Encrypting existing secrets at rest. This plan prevents browser/API
  disclosure and restricts access; storage encryption and key management need
  a separate threat model.
- Replacing the broker client, changing trading semantics, or changing strategy
  lifecycle rules.
- Automatic refresh-token use in the browser. Provider tokens stay server-side
  and should not be persisted unless a documented IdP requirement forces it.
- Per-tenant data isolation. This deployment is treated as one trusted trading
  workspace with role separation, not as a multi-tenant SaaS.
- The isolated plugin worker and human plugin approval endpoint; Plan 027 uses
  the identities created here.

## Git workflow

- Branches: `advisor/025-dashboard-identity`,
  `advisor/025-agents-service-auth`, and `advisor/025-fwbg-service-principals`.
- Start the fwbg branch from PR #146, not from a branch without Plan 011.
- Use small conventional commits, for example `feat(auth): add OIDC session`
  and `fix(secrets): return redacted datasource DTOs`.
- Keep separate PRs linked as one rollout unit. Do not merge or deploy the
  strict mode until the compatibility and rollout step below is green.

## Steps

### Step 1: Record deployment identity inputs and route policy before coding

Create `fwbg-dashboard/docs/security-boundary.md` containing only names and
rules, never secret values:

- canonical external HTTPS origin;
- OIDC issuer URL and client ID variable names;
- callback URI `/auth/callback`;
- exact trusted origin list;
- the claim used for groups plus an explicit group-to-role allowlist;
- private runtime variable name/format for an optional exact `(issuer,
  subject)` break-glass admin allowlist; never commit real subject values;
- permission matrix from "Decisions fixed by this plan";
- session policy: HttpOnly, Secure in production, SameSite=Lax, 30-minute idle
  and 8-hour absolute lifetime;
- production rule that email is never an authorization key unless the provider
  marks it verified;
- public-route list and deployment order.

Add a machine-readable `shared/auth.ts` that defines `Role`, `Permission`, the
role-to-permission mapping, and no secret configuration. Persist only the
normalized role in a session and derive permissions from this mapping on every
authorization; never persist a second permissions authority. Both middleware
and UI visibility helpers import it.

**Verify**:

```bash
rg -n "OIDC|viewer|operator|admin|emergency_stop|promote_live" docs/security-boundary.md shared/auth.ts
rg -n "password|clientSecret|api[_-]?key\s*[:=]\s*['\"]" docs/security-boundary.md shared/auth.ts
```

Expected: the first command finds all contract terms; the second finds no
embedded value.

### Step 2: Implement validated OIDC login and sealed local sessions

In `fwbg-dashboard`:

1. Add compatible current versions of `openid-client` and `nuxt-auth-utils`.
   Register the auth-utils module in `nuxt.config.ts`.
2. Configure auth-utils through its real contract:
   `NUXT_SESSION_PASSWORD` / `runtimeConfig.session.password` (at least 32
   characters), HttpOnly, SameSite=Lax, Secure in production, and the stated
   lifetime. Add private runtime config names for issuer, client ID, client
   secret when required, canonical origin, trusted origins, role mapping,
   break-glass mapping, and backend keys. Nothing except a non-sensitive
   login-enabled flag belongs under `runtimeConfig.public`. A startup validator
   rejects invalid `DEPLOYMENT_ENV`, HTTP production origin/issuer, mismatched
   callback URI, short/placeholder session/key values, invalid mappings, or
   production dev/fake flags before the server listens.
3. Implement `server/utils/auth/oidc.ts` using `openid-client` discovery,
   `randomPKCECodeVerifier`, a SHA-256 challenge, random state and nonce,
   `buildAuthorizationUrl`, and `authorizationCodeGrant` with expected state,
   nonce, issuer, audience, and an expected ID token. Store only short-lived
   flow state in a sealed HttpOnly cookie and clear it on both success and
   failure.
4. Implement `server/routes/auth/login.get.ts` (`GET /auth/login`) and
   `server/routes/auth/callback.get.ts` (`GET /auth/callback`). Use auth-utils'
   generated `GET /api/_auth/session` only for logged-in/logged-out bootstrap;
   it must not expose secure data. Implement custom authenticated
   `POST /api/auth/logout` so logout is covered by CSRF/Origin. If auth-utils
   exposes a DELETE session route, protect it identically or disable it. Reject open redirects: the
   post-login target must be an internal path beginning with one `/` and no
   scheme/host. Regenerate the session and CSRF value on login.
5. Split the sealed session into a browser-visible user projection
   `{displayName, role}` and auth-utils' server-only `secure`
   section `{issuer, subject, csrfSecret, issuedAt, lastSeenAt,
   absoluteExpiresAt}`. Confirm the session endpoint never serializes the secure
   section. Do not store provider access/refresh/ID tokens anywhere in the
   session.
6. Add logout that clears the local session. Authentication of an existing
   session must perform no live IdP call.
7. On each protected request, enforce both idle and absolute expiry from the
   secure timestamps. At most once per five minutes, use auth-utils'
   session-replacement/re-sealing API to update `lastSeenAt`; do not rewrite the
   cookie on every asset/SSE poll and never extend `absoluteExpiresAt`.
8. Add test-only OIDC fixture support without a production bypass. Tests must
   exercise locally signed tokens and discovery; they must not call the
   internet.

Unit/integration tests must reject wrong state, wrong nonce, wrong issuer,
wrong audience, invalid signature, expired token, missing configured role,
unverified-email authorization, and external return URLs. Assert cookie flags,
idle/absolute expiry, and that secure session fields are absent from the client
session response.

**Verify**:

```bash
bun run test:run -- auth
bun run nuxi typecheck
```

Expected: all auth tests pass and typecheck exits 0.

### Step 3: Add a complete, deny-by-default permission policy

Implement `server/utils/auth/policy.ts` as the single route-policy table.
Patterns must be anchored and method-specific. Apply this deterministic matrix:

- exact public routes: `GET /auth/login`, `GET /auth/callback`, `GET
  /api/_auth/session`, and `GET /healthz`; pass framework assets under
  `/_nuxt/**`, favicon, and explicitly enumerated static public files without
  treating them as application API routes;
- all SSR page requests except auth endpoints require `view`; anonymous page
  navigation redirects to `/auth/login`, while API/SSE/WS requests return
  401/403 and never redirect;
- authenticated safe API reads/HEAD, SSE, chart WS, and positions WS require
  `view` after secret DTOs are redacted;
- research, backtest, analyze, retry/cancel, queue, sync, uploads/ETL/data jobs,
  discovery/custom signals, bot restart/toggle, account activation, and
  `POST /api/ai/chat` require `operate_research`;
- exact emergency-stop route requires `emergency_stop`;
- criteria, agent configuration, non-secret account/datasource settings,
  plugin tests, and destructive DELETE operations other than a run cancel
  require `manage_config`;
- credential replacement/clearing, search/LLM/provider secret configuration,
  and any raw-secret migration endpoint require `manage_secrets`;
- exact paper-to-live promotion requires `promote_live`;
- Plan 027's quarantine source/review/adoption routes require its new
  `adopt_plugin` permission;
- every other unsafe route/method denies until explicitly classified.

Add `server/middleware/auth.ts` to resolve the session and enforce the policy
for SSR pages and HTTP/SSE. It must answer stable `401` or `403` JSON without
upstream error bodies. Add a test that inventories every `server/api/**` and
`server/routes/**` method file plus framework-generated auth routes and the SSR
page/static allowlists, converts each to method/path, and asserts exactly one
policy match. Duplicate or unmapped application routes fail the test. Include
auth-utils' generated `/api/_auth/session` explicitly so middleware cannot
accidentally block bootstrap or expose an unprotected DELETE.

Expose a server-generated session-bound CSRF value through an authenticated
same-origin endpoint. Add one client mutation wrapper that sends
`X-CSRF-Token`; migrate every dashboard mutation to it. The server compares in
constant time and also rejects a missing/mismatched `Origin`. Do not require
CSRF on safe methods.

Authenticate both WebSocket upgrades in the Nitro `upgrade` hook—not after
`open`—and validate the exact trusted origin before subscriptions or broker
connections are created. Close unauthorized sockets with a non-sensitive
policy error. SSE and WS handlers set a server timer to end the connection no
later than the session's current idle or absolute expiry; reconnection performs
a fresh session check/idle refresh. Do not treat stream traffic as user
activity.

Create structured mutation audit events with: timestamp, issuer+subject, role,
permission, method, normalized route template, outcome/status, and request ID.
Write JSONL through an `O_APPEND` owner-only (`0600`) file sink on a dedicated
persistent audit path, rotate daily, and retain 90 days. Never log request
bodies, query strings, authorization headers, cookies, source code, or
credentials. Audit failure blocks config/secret/live-promotion mutations before
the upstream call; emergency stop remains available and emits a critical
secret-free stderr fallback because failing safe means closing risk, not
blocking it.

**Verify**:

```bash
bun run test:run -- policy csrf websocket audit
```

Expected: inventory coverage is 100%; anonymous, wrong-role, CSRF, and origin
negative cases pass; audit snapshots contain no fixture secrets.

### Step 4: Consolidate dashboard-to-backend transport

Create `server/utils/backend-client.ts` with named clients for `fwbg` and
`agents`. The helper must:

- read private runtime config per request rather than module-level
  `process.env` constants;
- inject the correct service key after sanitizing caller headers so a route
  cannot override `X-API-Key`;
- support JSON, raw/multipart upload, SSE streaming, and abort/timeout behavior
  without buffering streams;
- propagate or generate a bounded request ID;
- map upstream failures to stable local errors and log only status, service,
  route template, and request ID;
- never return an upstream traceback/body to the browser.

Make `fwbgFetch` and `fwbgAgentsFetch` thin delegates during migration. Migrate
the direct-fetch upload, discovery SSE, agents event SSE, and AI tool paths as
well as ordinary handlers. Then remove the old wrappers only if `rg` proves no
imports remain.

**Verify**:

```bash
rg -n "fetch\(.*FWBG|process\.env\.FWBG|X-API-Key" server --glob '*.ts'
bun run test:run -- backend-client proxy stream
```

Expected: backend URLs/keys occur only in runtime config and the central
helper; tests prove caller override is impossible and SSE/multipart remain
functional.

### Step 5: Replace secret-bearing responses with redacted contracts

Implement explicit response and patch schemas at the service that owns each
secret:

- dashboard account/broker settings;
- fwbg datasource settings, including REST key, WebSocket headers, and database
  connection strings;
- fwbg-agents search/LLM secret settings;
- dashboard AI provider configuration.

Responses use non-secret metadata and `configured` flags only. They never use
`to_dict()` on an internal object containing credentials. Patch handlers
validate omit/preserve, `null`/clear, and non-empty/replace semantics and never
echo submitted values. Add a regression test that recursively walks every JSON
response and fails if the fixture secret appears in a key or value.

Centralize account/datasource file resolution: validate route IDs against the
existing slug convention, resolve against the configured root, and require
`is_relative_to(root)` before reads or writes. All secret-bearing file writes
use a same-directory temporary file, flush/fsync, atomic replace, and owner-only
directory/file modes (`0700`/`0600`). At-rest encryption remains out of scope;
world-readable or partially overwritten plaintext does not.

Remove the provider key input/storage/send flow from `AiChat.vue`; provider
credentials come from private server runtime config. Add a one-time global
client migration plugin—not an AiChat-only mount hook—that removes the known
historical localStorage key names without reading or logging their values. The
UI may show only provider availability.

Protect or remove endpoints returning internal credential/code filesystem
paths. Render any source text as text, never `v-html`.

**Verify**:

```bash
# run from fwbg-dashboard
bun run test:run -- settings datasource ai secrets
# run from fwbg-agents
uv run pytest tests/api/test_secrets.py -q
# run from fwbg
uv run pytest tests/test_datasources.py -q
```

Expected: all tests pass; fixture secrets occur in request fixtures only, never
response snapshots or logs.

### Step 6: Give both Python APIs scoped, fail-closed service identities

In fwbg, generalize PR #146's middleware to accept distinct configured keys and
attach a typed principal to request state:

- `FWBG_API_DASHBOARD_KEY` -> principal `dashboard`;
- `FWBG_API_AGENTS_KEY` -> principal `agents`.

Use constant-time comparisons and reject empty keys. Retain `FWBG_API_KEY` only
as a documented one-release compatibility input; it grants only the common
existing API surface, never a principal-specific admin capability. Add a
dependency such as `require_service("dashboard")` for endpoints which must be
human initiated. Plan 027 will use it for plugin adoption.

Validate production service keys at startup: minimum 32 characters, no known
placeholder/repeated trivial values, pairwise different dashboard/agents/legacy
keys, and secret-redacted settings representations. Ignore/remove any
caller-supplied service-principal or actor header before deriving request state;
only a successfully matched configured key creates the principal.

In fwbg-agents, add `FWBG_AGENTS_API_KEY` and an explicit
`FWBG_AGENTS_AUTH_MODE=compat|required`. `required` fails closed at application
creation when the key is absent and authenticates everything except exact
`/healthz`. `compat` exists for one internal-only rollout interval, emits a
warning/metric for unauthenticated calls, and is rejected unless the agents
host port has already been removed; delete it after Step 7. A local-only
`FWBG_AGENTS_ALLOW_UNAUTHENTICATED_API=1` may exist for tests/development but
must raise under `DEPLOYMENT_ENV=production`. Remove browser CORS because only
the Nuxt server calls this API.

Apply the same all-routes rule to fwbg: add exact public `GET /healthz`; protect
all other paths, not only `/api`; disable `/docs`, `/redoc`, and
`/openapi.json` in production or require a service principal. Database/proxy
health remains protected.

Use these deployment mappings:

- fwbg `FWBG_API_DASHBOARD_KEY` = dashboard private `FWBG_API_KEY`;
- fwbg `FWBG_API_AGENTS_KEY` = agents outbound `FWBG_API_KEY`;
- agents `FWBG_AGENTS_API_KEY` = dashboard private `FWBG_AGENTS_API_KEY`.

The names above describe wiring only; never commit the values.

**Verify**:

```bash
# from fwbg
uv run pytest tests/test_api.py tests/test_api_auth.py -q
# from fwbg-agents
uv run pytest tests/test_main.py tests/api -q
```

Expected: missing/invalid keys get 401 or fail-closed startup as specified;
dashboard and agents principals are distinguishable; exact `/healthz` remains
public; database/proxy health is protected.

### Step 7: Wire and stage the production rollout without an outage

Update `fwbg/docker-compose.yml` and examples:

1. Add all three distinct secret variables through deployment-secret/env-file
   references; do not put values in Compose.
2. Remove the agents host-port publication before any compatibility mode.
   Keep it reachable only on the
   internal Compose network. Keep dashboard as the only browser-facing service.
3. Add a dedicated persistent dashboard audit volume/path. Make production
   reject all unauthenticated development/fake flags.
4. Replace uncoordinated `latest`/automatic partial rollout for these three
   services with one explicit release identifier, or otherwise disable
   Watchtower for them and document the coordinated order.
5. Use this exact rollout: provision distinct secrets and remove the agents
   host port; deploy additive fwbg principals with legacy compatibility and
   internal-only agents `compat`; deploy the OIDC dashboard that sends both new
   keys; verify authenticated-call metrics; switch agents to `required` and
   fwbg to scoped-required; remove legacy/compat code and key in the next
   release. Never expose agents while compat is active.

Create `tests/integration/run_auth_compose.py` and a security-test Compose
override. The harness takes explicit fwbg/agents/dashboard worktree paths,
builds those local sources (the production Compose's `image: ...:latest` is not
sufficient), uses a unique `-p fwbg-auth-test-<id>` project, an explicit fake
env file, a local fake OIDC provider, a dependency-injected fake broker, and
fresh project-scoped volumes. It must never mount or address production
account/data volumes. `DEPLOYMENT_ENV=test` permits the fake broker; production
startup rejects it. The harness always tears down only its exact project and
volumes in `finally`.

Before strict activation, run its smoke matrix through the test dashboard
origin: anonymous read/mutation denied, viewer read allowed,
operator research and emergency stop allowed, operator live promotion denied,
admin allowed, bad CSRF denied, bad WS Origin denied, and backends unreachable
through host ports. The fake broker asserts the emergency-close call without
ever connecting to or reading a real account.

**Verify**:

```bash
# from fwbg; arguments are the executor's three checked-out branches/worktrees
docker compose config --no-interpolate --quiet
uv run python tests/integration/run_auth_compose.py \
  --fwbg-repo /absolute/path/to/fwbg-worktree \
  --agents-repo /absolute/path/to/fwbg-agents-worktree \
  --dashboard-repo /absolute/path/to/fwbg-dashboard-worktree
```

Expected: local branch images build; all test services are healthy; only the
test dashboard has a host port; the complete RBAC/fake-emergency matrix passes;
the harness removes only its project/volumes.

### Step 8: Run full gates and prepare linked PRs

Run the complete repository gates from "Commands you will need". In each PR,
include the same rollout checklist and links to the other two PRs. The dashboard
PR is not independently deployable until both backend compatibility PRs are
deployed.

**Verify**:

```bash
# run in each repository separately
git status --short
```

Expected: only files listed in Scope are changed; no `.env`, session dumps,
tokens, generated runtime data, or credential-bearing snapshots are tracked.

## Test plan

- OIDC: successful Authorization Code + PKCE; state, nonce, signature, issuer,
  audience, expiry, role mapping, return URL, and cookie-flag negatives.
- Session: idle/absolute expiry, logout, regeneration on login, and continued
  emergency-stop authorization while the test IdP is unavailable; open SSE/WS
  closes at session expiry and must reauthenticate on reconnect.
- Policy: every route maps exactly once; anonymous/viewer/operator/admin matrix;
  unknown route denies; safe reads do not require CSRF.
- Request authenticity: missing/wrong CSRF, missing/wrong Origin, valid unsafe
  request, WS origin/session, SSE session.
- Secrets: account, datasource, agents secrets, and AI response/log recursive
  leak tests; patch preserve/clear/replace semantics; safe path containment,
  atomic replacement, and `0600`/`0700` modes.
- Transport: JSON, multipart, SSE, timeout, request ID, caller header override,
  and sanitized upstream error.
- Service auth: distinct principals, legacy compatibility cannot call
  dashboard-only endpoint, caller principal-header override ignored, weak/equal
  keys rejected, exact public liveness, docs protected, production bypass
  rejection.
- Deployment E2E: use fake broker/IdP; never exercise live money endpoints in
  automated tests.

## Done criteria

- [ ] Dashboard OIDC validates state, nonce, PKCE, signature, issuer, audience,
  and expiry and stores only a sealed local session.
- [ ] Every dashboard HTTP/SSE/WS route is covered exactly once by the central
  permission policy, including SSR and generated auth routes; the inventory
  test passes and expired streams close.
- [ ] All unsafe browser mutations enforce permission, CSRF, and Origin.
- [ ] Browser response/log tests prove broker, datasource, service, and LLM
  fixture secrets are absent.
- [ ] Dashboard-to-backend calls use one server-only transport abstraction and
  distinct keys.
- [ ] fwbg and fwbg-agents fail closed and expose only exact `/healthz` without
  service authentication; production rejects weak/equal keys and all bypasses.
- [ ] The agents service is not host-published; the coordinated deployment
  smoke matrix passes.
- [ ] All full dashboard, agents, and fwbg verification commands pass.
- [ ] No secret value or `.env` file appears in any diff.
- [ ] `plans/README.md` is updated after the linked PRs and rollout are complete.

## STOP conditions

Stop and report instead of improvising if:

- the canonical external HTTPS origin, reverse-proxy trust rules, OIDC issuer,
  client registration, group claim, or group-to-role mapping is unavailable;
- the chosen provider cannot satisfy signed ID-token issuer/audience validation
  with Authorization Code + PKCE;
- immediate server-side session revocation is required. A sealed stateless
  session does not provide it; design a server-side session store first;
- external clients call fwbg-agents or raw fwbg credential endpoints directly;
  inventory and migrate them before removing host access or response fields;
- production is currently remotely reachable without authentication. Restrict
  ingress and rotate any exposed broker/datasource/service/provider credential
  before treating the code change as sufficient;
- a real credential is already tracked in git. Stop, report only its type and
  file/line (never its value), remove it from the supported configuration path,
  and require rotation; do not copy it into tests, plans, logs, or PR text;
- a browser feature genuinely requires a raw secret after the DTO migration;
  obtain a product/security decision rather than adding an exception;
- in-scope code no longer matches the current-state evidence or a verification
  gate fails twice after a reasonable correction.

## Maintenance notes

- Every new dashboard route must update the policy inventory and its permission
  test in the same PR. Treat an unmapped route as a CI failure.
- Reviewers should inspect header merge order, redirect validation, session
  contents, and logs—not just happy-path login.
- Rotate service keys independently and support dual values briefly if zero
  downtime is required; never reintroduce one shared super-key.
- Plan 027 adds `adopt_plugin` as a new admin permission and a fwbg endpoint
  restricted specifically to the dashboard service principal.
- At-rest secret encryption remains a separately planned improvement.
