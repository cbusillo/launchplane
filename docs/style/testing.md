---
title: Testing Style
---

- Add targeted unit tests for storage, contracts, and workflow mapping.
- Prefer deterministic file-system tests using `TemporaryDirectory`.
- Test fail-closed behavior explicitly.
- Keep fixtures small and inline unless they are reused heavily.
- Default local full-suite entrypoint is
  `uv run launchplane ci unittest-shard local`.

## Local test loop

Run a focused unittest module or test method before the full suite:

```bash
uv run python -m unittest tests.test_module_name
uv run python -m unittest tests.test_module_name.TestCaseName.test_behavior
```

Before review, run the official local full-suite gate:

```bash
uv run launchplane ci unittest-shard local
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
LAUNCHPLANE_TEST_POSTGRES_URL=postgresql+psycopg://... uv run launchplane ci postgres-integration
```

The URL is a temporary/root test service URL, not a Launchplane runtime
credential. The harness creates and drops isolated databases, upgrades each from
empty schema through Alembic `head`, verifies the exact checked-in schema head
and critical indexes/types, and runs focused two-connection concurrency tests
for idempotency conflicts, operation claims, stale lease owners, lease recovery,
and active-operation partial uniqueness. Same-repo CI provides the URL via a
PostgreSQL service container; fork PRs keep the SQLite/unittest path only. Keep
the integration module focused: target runtime is under 2 minutes in CI, and any
flake should be treated as a storage or harness bug rather than hidden with a
retry loop.

The lower-level CI shard commands remain available for diagnosis:

```bash
uv run launchplane ci unittest-shard plan --shard-count 12 --timings-file .ci-cache/unittest-timings/history.json --max-tests-per-target 20 --max-seconds-per-target 30
uv run launchplane ci unittest-shard run --shard-count 12 --shard-index 0 --timings-file .ci-cache/unittest-timings/history.json --max-tests-per-target 20 --max-seconds-per-target 30 --timings-output tmp/shard-0.json
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
