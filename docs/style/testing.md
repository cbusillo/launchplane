---
title: Testing Style
---

- Add targeted unit tests for storage, contracts, and workflow mapping.
- Prefer deterministic file-system tests using `TemporaryDirectory`.
- Test fail-closed behavior explicitly.
- Keep fixtures small and inline unless they are reused heavily.
- Default local full-suite entrypoint is
  `uv run --extra dev launchplane ci unittest-shard local`.

## Local test loop

Run a focused unittest module or test method before the full suite:

```bash
uv run --extra dev python -m unittest tests.test_module_name
uv run --extra dev python -m unittest tests.test_module_name.TestCaseName.test_behavior
```

Before review, run the official local full-suite gate:

```bash
uv run --extra dev launchplane ci unittest-shard local
```

The local gate computes one deterministic plan, runs every shard in an isolated
Python subprocess, reports all failing shard indexes in one result, and writes
the next local timing history only after every shard passes. Its defaults match
same-repo CI: 12 shards with a 20-test/30-second split threshold. `--jobs`
controls only local concurrency and defaults to the detected CPU count capped at
12; it does not change the shard topology. Local timing history is stored under
the ignored `.ci-cache/` directory and remains a balancing hint rather than test
authority. A failed run leaves the previous timing history unchanged instead of
learning from a partial suite.

Storage tests that instantiate `PostgresRecordStore` with `sqlite+pysqlite://`
are portability and local rehearsal tests. They prove ORM mapping, payload round
trips, and SQLite-compatible migration mechanics, but they do not prove
PostgreSQL row locks, `SKIP LOCKED`, JSONB column types, Alembic head adoption,
or partial unique index predicates.

The real PostgreSQL storage proof is an explicit integration gate:

```bash
LAUNCHPLANE_TEST_POSTGRES_URL=postgresql+psycopg://... uv run --extra dev launchplane ci postgres-integration
```

The URL is a temporary/root test service URL, not a Launchplane runtime
credential. The harness creates and drops isolated databases, upgrades each from
empty schema through Alembic `head`, verifies the exact checked-in schema head
and critical indexes/types, and runs focused two-connection concurrency tests
for mutation reservation/replay/conflict, reconciliation-key fencing, atomic
business-write completion and rollback, operation claims, stale lease owners,
lease recovery, and active-operation partial uniqueness. Same-repo CI provides
the URL via a PostgreSQL service container; fork PRs keep the SQLite/unittest
path only. Keep the integration module focused: target runtime is under 2
minutes in CI, and any flake should be treated as a storage or harness bug
rather than hidden with a retry loop.

The lower-level CI shard commands remain available for diagnosis:

```bash
uv run --extra dev launchplane ci unittest-shard plan --shard-count 12 --timings-file .ci-cache/unittest-timings/history.json --max-tests-per-target 20 --max-seconds-per-target 30
uv run --extra dev launchplane ci unittest-shard run --shard-count 12 --shard-index 0 --timings-file .ci-cache/unittest-timings/history.json --max-tests-per-target 20 --max-seconds-per-target 30 --timings-output tmp/shard-0.json
```

Shard planning discovers `tests/test*.py` dynamically. Small files run as whole
modules. Large or timing-known-slow files may be split into unittest target IDs,
preferably `tests.test_module.TestCase` and only down to
`tests.test_module.TestCase.test_behavior` when a single test case is still too
large. This lets hot modules distribute across shards without physical file
moves. Timing files are balancing hints only; discovered tests remain the source
of truth.

Same-repo CI currently uses 12 unittest shards with a 20-test/30-second split
threshold to keep large app and service targets under the tool wall-clock
ceiling. CI restores timing history once per workflow run and distributes that
immutable snapshot to every shard and the aggregate job. Shards must not restore
timing caches independently because timing-dependent target splitting requires
one shared plan input. Snapshot and shard artifacts remain available for the
full workflow rerun window and may be overwritten safely by rerun jobs. The
shard plan includes per-target timing-source diagnostics:

- `exact`: the timing file has a direct record for that unittest target.
- `parent`: the estimate is derived from the parent module timing and spread
  across discovered child targets.
- `default`: no timing history exists yet, so the planner uses the conservative
  default estimate until a shard timing artifact teaches it better.

GitHub Actions remains the source of truth for required pull-request gates. The
local command is the official pre-review full-suite proof; the PostgreSQL
integration command is the official production storage-semantics proof when a
local or CI PostgreSQL service is available. Neither local command replaces CI's
runner isolation, artifact retention, or required status checks.

## Browser smoke

Run the deterministic operator-journey smoke separately from the frontend unit,
type, OpenAPI, and production-build gate:

```bash
pnpm --dir frontend exec playwright install chromium
pnpm --dir frontend test:browser
```

The Playwright suite starts the Vite development server and uses only repo-local
development fixtures. Product-detail journeys use `?fixture=products`; product
inventory boundaries use `?fixture=empty` and `?fixture=error`. Anonymous
authentication is simulated by intercepting only `/v1/auth/session`; no product
or runtime mutation reaches a deployed Launchplane service. The suite exercises
eight journeys: the rendered authentication prompt, honest empty and error
product inventories, product workspace, recent activity, environment
diagnostics, an honestly blocked action, and a dry-run confirmation without
submitting the apply. Each journey runs at desktop and narrow widths, checks
route-heading and keyboard focus behavior, rejects duplicate document IDs and
horizontal overflow, and fails on unexpected mutation requests, console errors,
uncaught page errors, failed requests, or HTTP error responses.

Screenshots, traces, and failure evidence are written under
`tmp/browser-smoke/`. CI uploads that directory from the dedicated hosted browser
job. Every run clears prior evidence first and verifies the complete desktop and
narrow screenshot set before passing. These screenshots are review evidence
rather than pixel-diff baselines, so intentional visual changes do not require
snapshot churn.

Development fixtures remain excluded from production authority. `pnpm --dir
frontend validate` still runs the independent production build and
`assert-production-fixtures.mjs` scan; browser smoke does not weaken or replace
that proof. Deployed OIDC smoke remains a separate non-destructive evidence
layer because it validates service authentication and deployment wiring rather
than deterministic UI behavior.

Workflow contract tests should parse workflow YAML through
`tests/support/workflows.py` and assert named invariants instead of mirroring
large YAML snippets, substring counts, or indentation-sensitive job fragments.
Use semantic checks for event shape, fork-hosted versus same-repository
self-hosted runner isolation, OIDC and permission requirements,
`launchplane-request` inputs, artifact retention, immutable timing snapshots,
and aggregate gate dependencies. Keep direct text assertions only for embedded
script behavior that is not represented as YAML structure, and make invariant
failures name both the workflow file and the violated invariant.

## HTTP and ASGI contracts

- Use `tests.support.http.lifespan_client` or `tests.support.http.request` for
  ordinary HTTP contracts, including GET/POST, cookies, redirects, and OpenAPI.
  These helpers enter the application's lifespan through the supported async
  HTTP client path; do not construct HTTP ASGI scopes in contract tests.
- `tests.support.raw_asgi.request` is reserved for duplicate, absent, or
  deliberately malformed framing headers that a supported client cannot emit.
  Streaming, body-limit, cancellation, and `http.disconnect` tests may use a
  bespoke scripted ASGI receive sequence when the shared helper cannot express
  the transport condition. Keep every raw-scope test explicit about the
  exceptional ASGI behavior being exercised.
- When moving a test between these paths, retain the behavior assertion and
  document any intentional coverage replacement in the change description; do
  not delete behavior tests merely because the support implementation changed.
