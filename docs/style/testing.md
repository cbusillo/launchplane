---
title: Testing Style
---

- Add targeted unit tests for storage, contracts, and workflow mapping.
- A test stays only if it fails when the product is broken and passes when
  someone makes an intended change. A version, toolchain, or dependency bump,
  or an edit to a workflow step's wording, should need no test edit.
- Do not assert a literal defined elsewhere (versions, pins, image digests,
  hashes, counts, operation tables). Check agreement with the one source, or
  keep a byte-exact gate only on a real generated artifact or immutable
  evidence.
- Do not assert workflow, action, config, or docs text. Run the embedded script
  with inputs and check what it does, or enforce a rule that holds for every
  workflow (fork isolation, pinned actions, permissions).
- CI's `repository_pins` job runs the shared
  [change-detector scanner](https://github.com/cbusillo/codex-skills/blob/main/skills/references/test-quality.md)
  as warnings only. Triage its annotations on tests you add or change.
- Verification code must not depend on working-tree state; read live state
  only on the path that acts on it.
- Prefer deterministic file-system tests using `TemporaryDirectory`.
- When a fixture follows a record written with the current clock, derive its
  timestamps from that persisted record. A distant future date only delays the
  failure; ordering assertions should also hold after that date passes.
- Test fail-closed behavior explicitly.
- Lease-renewal success fixtures should synchronize on committed background
  heartbeats and control the database clock. A short wall-clock sleep cannot
  distinguish a renewal defect from thread starvation or SQLite write-lock
  contention under parallel execution. Retain separate expired/stolen-lease
  refusal coverage with the real storage implementation.
- Keep fixtures small and inline unless they are reused heavily.

## Local test loop

During the one-week [Q326 trial](https://github.com/cbusillo/codex-skills/issues/1884#issuecomment-6194586961)
approved October 15, 2026 and refined by [Q375](https://github.com/cbusillo/codex-skills/issues/1884#issuecomment-6215129557)
on October 16, routine changes use focused local behavior tests for
the changed code, plus lint, type checks, and IDE inspection. Choose the
relevant modules or test cases; substitute them in the targeted command rather
than running an unselected unittest discovery:

```bash
uv run --extra dev python -m unittest tests.test_module_name
uv run --extra dev python -m unittest tests.test_module_name.TestCaseName.test_behavior
```

The Supervisor owns the measured comparison and brings the keep-or-revert
decision to the Director on the linked trial issue. The rule becomes permanent
only if the numbers support it. Q375 explicitly supersedes the older full-local
safety exception for Launchplane during this trial, including older catalog or
global test guidance. Matching catalog guidance updates are tracked on the
[trial issue](https://github.com/cbusillo/codex-skills/issues/1884#issuecomment-6215760222).

Use `.github/github.json` for the lint and type-check commands and inspection
configuration. `qualityGate.test.local` is the focused command template;
`qualityGate.test.localSafety` takes whole safety module names, and
`qualityGate.test.ci` names the complete suite for CI only. For a documentation
or metadata change with no affected code, record why behavior tests or IDE inspection do not apply; do not add tests that
assert instruction wording or config text.

For frontend changes, use `qualityGate.build.frontend` for frontend unit, type,
and build validation, with browser smoke for UI changes as described below.

The full sharded suite remains CI's required gate before merge. Wait for green
current-head CI before the checked `gh-pr.py ready` handoff; a local test pass
does not replace that evidence. The trial does not change CI workflows or
required checks.

Safety, authorization, approval-rule, destructive-helper, and shared-infrastructure
changes keep their safety classification. Credential handling falls under safety
and authorization; shared test helpers, shard tooling, and CI runner selection
are shared infrastructure. For these changes, run focused behavior tests plus
the **whole test modules** covering the touched behavior within each safety
area: authorization,
secrets, recovery, and credentials. Select modules by the changed behavior and
its callers, including relevant tests in broadly named modules; a filename
keyword search alone is not a complete selection. Pass module names, not
individual classes or methods, to `qualityGate.test.localSafety`:

```bash
uv run --extra dev python -m unittest tests.test_operation_authorization tests.test_product_secret_copy
```

These are selection examples, not a mandatory inventory for every change.
Record the classification, touched areas, selected modules and results in the
PR. When no executable safety area is touched (for example, this test guidance
itself), record that reason and obtain the required model review. Do not run
unrelated safety modules just because the repository is a control plane.
Shared-infrastructure changes touching none of those safety areas run their
focused tests locally and record why no additional safety modules apply.
Broaden local coverage within the affected areas for shared contracts, storage,
generated surfaces or observed failures; the complete 12-shard suite runs only
in CI, including for safety-classified changes.

`qualityGate.test.ci` names the all-shard equivalent of CI's plan/run/aggregate
sequence; CI does not invoke that exact command. Same-repository CI plans once
and runs the complete inventory across 12 shards, with the coverage aggregate
required by `ci-gate`; fork PRs run the complete inventory through unsharded
unittest discovery. Both require green CI before `ready` and merge. Reproduce
an individual shard using the frozen plan and the diagnostic commands below,
rather than substituting a new host-local plan.

The historical `unittest-shard local` CLI remains implemented for compatibility;
it is not a developer-host review, ready or closeout gate under this trial.
It computes one deterministic plan, runs shards in isolated Python subprocesses,
and reports failing shard indexes. Its `--jobs` option controls concurrency;
timing history under ignored `.ci-cache/` is a balancing hint and updates only
when all shards pass. This describes existing mechanics, not a local full-suite
requirement or an exception to Q375.

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
credential. Percent-encoded URL components, including PostgreSQL connection
options, are preserved through the harness's Alembic configuration.
The harness creates and drops isolated databases. It upgrades one
empty database through Alembic `head` per process and clones it as a template
for each test that only needs a head schema (`_head_postgres_database`); tests
that prove a migration upgrade their own database from the revision they need.
It verifies the exact checked-in schema head
and critical indexes/types, and runs focused two-connection concurrency tests
for mutation reservation/replay/conflict, reconciliation-key fencing, atomic
business-write completion and rollback, operation claims, stale lease holders,
lease recovery, and active-operation partial uniqueness. Same-repo CI provides
the URL via a PostgreSQL service container with an IPv4-only dynamic host port,
preventing the runner from selecting an IPv6 mapping for an IPv4 connection;
fork PRs keep the SQLite/unittest path only. Same-repo CI partitions the existing
integration inventory into three parallel jobs using the unittest shard planner.
Each job discovers the CLI's canonical module/class inventory, runs its
disjoint test IDs in one process, and retains one migrated template database in
its own PostgreSQL service. `fail-fast: false` lets every shard finish after a
failure; `ci-gate` requires the whole matrix to succeed. Each log records selected
test IDs and elapsed test time. The default local command still runs the complete
serial inventory; reproduce one CI job with `--shard-count 3 --shard-index 0`
(or index 1 or 2). Invalid, empty, or unloadable selections fail before execution.
Keep the integration module focused: target runtime is under 2 minutes in CI,
and any flake should be
treated as a storage or harness bug rather than hidden with a retry loop.

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

CI plans once. The `test_timing_snapshot` job writes `plan.json` next to the
frozen timings. Each shard runs its slice with `unittest-shard run --plan-file`,
and `aggregate --plan-file` checks coverage against that same plan. Every job
creates fresh temporary directories for snapshot and shard-result artifacts.
Downloading into a reused directory leaves files absent from the artifact in
place: residual `history.json` changed target splitting on some self-hosted
runners, so shards that planned for themselves overlapped or missed tests
(#2618). Fresh directories also prevent a failed shard from uploading an earlier
result and keep aggregate inputs limited to the downloaded artifacts. Without
`--plan-file`, `run` and `aggregate` still discover and plan locally.

CI also runs daily against the default branch. Scheduled runs always execute
fresh checks, including Python dependency audits, image scans and PostgreSQL
tests; they never reuse an earlier successful tree. The `ci-gate` summary lists
each job's result and the Actions run remains failed when a gate fails.
Inspect scheduled runs in Actions to diagnose calendar-driven failures before
an unrelated pull request encounters them. Email notification delivery depends
on the schedule actor and that account's settings; delivery to the Director is
not guaranteed. Reporting uses the existing read-only workflow token; it does
not create issues or dependency updates.

For pushes to `main` and `launchplane/train/**`, the `verified-tree` job can
reuse a completed, successful GitHub Actions `ci-gate` on the exact pushed
commit. Its check suite must identify that same commit on main without a PR
merge context; conflicting pending or failed gates veto reuse. This avoids full
work when an all-no-op batch publishes the already-tested base, without trusting
a fork or retargeted PR's merge-ref checks. Native construction uses
`launchplane/construct/**`, outside the required workflows' push filters, and
publishes the canonical train ref only after every entry is verified. The
controller's narrow single-entry exception is defined in
[candidate construction](../merge-train-policy.md#candidate-construction).

Main retains its existing PR-tree reuse, tightened to require that the base is
an ancestor of the PR head as well as matching trees and a successful gate.
The workflow does not extend that shortcut: a historical PR gate alone cannot
identify the merge-ref tree tested before a retarget. Ordinary PR events, unrelated
branches, and missing API evidence run the full suite. Construction branches
must remain outside required workflow triggers. Concurrency and required checks
are unchanged, and every published final candidate SHA still receives its own
gate. Generated same-repository train PRs delegate to that exact candidate's
push CI rather than running a second suite; the evidence and failure contract
is owned by [candidate validation](../merge-train-policy.md#candidate-validation).

The `ci-gate` job reports the run's wall time against a 10-minute pull-request
budget in the job summary and raises a warning annotation when a run exceeds
it. The report never fails the gate; an exceeded budget is a signal to measure
and fix the slow lane. Only pushes to `main` save the uv dependency cache:
pull-request caches are scoped to that pull request, so saving one re-uploads
hundreds of megabytes that main and other pull requests cannot restore, and
self-hosted runners already keep a local uv cache.

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

Train candidate CI and Security use the reserved `launchplane-candidate`
self-hosted label for both push and generated candidate PR events, including
bootstrap, browser, and summary jobs. The reserved lanes and their live
qualification are owned by [shiny-infra-ops#488](https://github.com/cbusillo/shiny-infra-ops/issues/488).
Ordinary jobs keep their existing hosted or configured self-hosted routing.
Forks stay hosted. The shared selector in `scripts/select-ci-runner.sh` fails
closed when ordinary self-hosted work has no configured label. Dynamic jobs
retain independent same-repository guards because a fork controls its checkout;
bootstrap and summary runner expressions enforce that boundary before checkout.
Every existing gate still runs. Browser isolation is described in
[Browser smoke](#browser-smoke). CodeQL retains its separate hosted workflow.
Existing PR-number concurrency cancels superseded PR runs; candidate creation
and reset pushes retain the SHA-isolated concurrency documented in
[merge-train policy](../merge-train-policy.md#candidate-construction).

The test aggregate and summary gates use `!cancelled()` so they still report
failed prerequisites but do not remain queued after a superseding push cancels
the run. An `always()` aggregate can retain the old workflow's concurrency slot
while it waits for a runner, preventing the new head's CI from starting.

GitHub Actions remains the source of truth for required pull-request gates.
Follow the safety module selection in [Local test loop](#local-test-loop).
Local validation uses targeted behavior proof plus whole modules for touched
safety areas. Add PostgreSQL integration, browser, generated-contract or other
affected-area gates when the boundary or observed risk requires them; the
complete suite stays CI-only.
When PostgreSQL storage semantics require proof, the PostgreSQL integration
command is the official production storage-semantics proof with a local or CI
PostgreSQL service.
Do not add tests that merely restate a copied value or mirror the implementation.
No local command replaces CI's runner isolation, artifact retention, or required
status checks.

Python dependency audits and runtime-image vulnerability scans run on every
CI path. They block changes to dependency manifests, lockfiles, image recipes
or the scan workflow; otherwise their outcome is reported in the job summary
and failed scans raise warning annotations with details in the scan logs.
Python inputs are `pyproject.toml`, `uv.lock` and requirements files. Image
inputs also include Dockerfiles, `.dockerignore` and frontend dependency
manifests. Application-source edits alone do not make an inherited advisory
block an unrelated fix. Lint, types, image builds and Compose qualification
remain blocking. Fork PRs use the same classification on hosted runners.

Classification uses the PR diff from its base, a main push from its previous
commit, and a train candidate from main. Renames include both old and new paths.
Schedules and missing diff evidence retain absolute blocking audits, and a
failed classification job fails `ci-gate`. Tree reuse still applies to push
events. Daily fresh CI is supplied separately by PR #2893; this reporting
change must land after or with that schedule so newly discovered advisories
have a blocking signal independent of unrelated pull requests.

CI image builds use the job-specific Buildx builder without importing or
exporting shared build caches. `scripts/build-ci-runtime-image.sh` retries once
without build cache only when cache-key computation reports the missing-layer
`httpReadSeeker` error; unrelated failures and a failed retry remain blocking.
The loaded image still runs every Compose qualification and vulnerability scan.

CI container and unittest jobs run `scripts/setup-ci-compose.sh`; the workflow
owns the Compose version. Installation copies directly from the runner's tool
cache (or downloads the official release on a miss) into the job's Docker plugin
directory, avoiding the setup action's shared per-user staging file. Validated
downloads refill the runner-local cache atomically. The self-hosted runner's
Docker-config isolation hook remains required. The service Compose test renders
synthetic image and network inputs without starting containers. It requires
Docker in CI and checks image propagation before runtime qualification can
overwrite those settings. Dockerless local runs skip that rendering test.
Local reproduction requires a compatible
`docker compose` command, GNU `timeout`, the Docker engine, and built image.
The fixture uses bounded detached `up` calls and explicit running, absence, and
container-identity checks because Compose v5.3.1 `--wait` incorrectly requires
zero-replica services.

## Browser smoke

Run the deterministic admin-journey smoke separately from the frontend unit,
type, OpenAPI, and production-build gate:

```bash
pnpm --dir frontend exec playwright install chromium
pnpm --dir frontend test:browser
pnpm --dir frontend exec node --test tests/browser-support/server-isolation.test.mjs
```

The Playwright suite starts its own Vite development server on an OS-assigned
loopback port. Playwright captures the listener URL from Vite's startup output
and passes it to workers; overlapping runner jobs never reuse another job's
server. The isolation test holds two focused browser probes open together using
the smoke configuration and checks that their origins differ. It runs as part
of `test:browser`; the standalone command above is for focused diagnosis.
It writes only task-local test artifacts.

CI uses a bounded worker count in `frontend/playwright.config.ts`, including
self-hosted candidate lanes. Shared-host CPU count is not a per-job browser
budget. Local runs retain Playwright's default concurrency; use its `--workers`
option for a measured diagnostic comparison.

CI divides the unchanged suite into two jobs with Playwright's built-in
`--shard` option. Each job retains four workers, zero retries, the isolation
probe and browser diagnostics. `BROWSER_SMOKE_SHARD=1/2` (or `2/2`) selects a
partition through `pnpm --dir frontend test:browser`; without that variable,
the local command still runs the whole browser suite and checks all screenshots.
Sharded runs write Playwright JSON reports under `tmp/browser-smoke/reports`.
After both jobs succeed, CI downloads their evidence into a freshly cleared
directory, independently discovers the complete current suite and requires
each discovered test to pass exactly once across the reports. Missing,
duplicated, skipped, retried or failed tests and report errors fail the gate.
The combined evidence must also pass the existing complete screenshot check.
Artifact names identify the shard within the current workflow run. Re-running
failed jobs replaces that shard's evidence and retains successful siblings
from the same immutable head; re-running only evidence verification can reuse
both successful artifacts. Downloads remain scoped to the current workflow
run and use a freshly cleared directory, so another head's proof cannot enter.
`ci-gate` requires both browser execution and evidence verification, including
on forks. This reduces the browser critical path without reducing coverage;
it adds one runner job and an evidence job, so shared runner contention and
the other CI lanes still bound the whole-run saving.

Browser smoke runs in every CI run that needs fresh proof, including main pushes. The verified-tree
shortcut above can reuse a previously successful tree; it does not selectively
skip browser coverage. Keep that reuse rather than duplicating a full browser
run for the same tree.

Every browser spec imports `test` and `expect` from `tests/browser/state.ts`.
Its context fixture installs and pauses Playwright's clock before navigation,
including subsequent tabs, so slow host scheduling cannot expire evidence or
fire app timers between assertions. Persistent-profile tests use its
`launchControlledProfile` helper for each new context. Use `setClockTime` for a fixture's
recorded date and `page.clock.runFor` or `fastForward` to exercise timer-driven
behavior. Do not install a running clock or pause it within a short host-time
window in an individual test.

Locator assertions wait for rendered app state. Use `waitForState` for a route
handler's request count or release gate. Pass the page as its second argument
when a development fixture intentionally delays completion: each probe advances
the controlled clock until the requested state is observed. This form proves
fixture completion; timer and freshness assertions advance a known interval
explicitly, then await rendered state. Observe the rendered response before
changing a fixture's mode or advancing to the next refresh; a request counter
alone does not prove React consumed its response. Hold asynchronous fixtures
behind an explicit release gate until the test observes the intermediate
screen, rather than consuming one response and assuming only one request.
Development remounts can read a session more than once.

`pnpm --dir frontend check:browser-timing` rejects sleeps, timer aliases,
test-specific timeouts/retries and unmanaged clock/context creation. Both
frontend validation and browser smoke run this check. Shared Playwright
failure deadlines live in `playwright-deadlines.mjs`: they bound a broken test,
not successful synchronization. Do not add a sleep, retry or larger timeout
to hide a flake.

The server-isolation harness uses a watchdog derived from the shared server
startup and test deadlines. Its peer wait includes the peer's cold server
startup; it cannot assume both independent servers become ready together.
Ordinary browser assertion, test and server deadlines are unchanged.

Use Playwright's built-in repetition with zero retries to qualify the complete
suite under representative sibling host load. Record the revision, worker
count, load samples and pass/fail totals on the owning issue or PR. For example:

```bash
CI=true pnpm --dir frontend exec playwright test --repeat-each=5 --workers=4 --retries=0
```

Use a focused repetition for diagnosis:

```bash
CI=true pnpm --dir frontend exec playwright test sidebar-freshness --repeat-each=30
```

The suite uses only repo-local
development fixtures. Product-detail journeys use `?fixture=products`; product
inventory boundaries use `?fixture=empty` and `?fixture=error`. Anonymous
authentication is simulated by intercepting only `/v1/auth/session`; no product
or runtime mutation reaches a deployed Launchplane service. Journeys cover the
rendered authentication prompt, honest empty and error product inventories,
product workspace, recent activity, environment diagnostics, blocked and
dry-run action boundaries, plus mixed, ready, unsupported, denied, failed,
empty, and loading operational-readiness states. Desktop runs every test; narrow
width runs only tests explicitly tagged `@layout` or `@responsive`. Mark tests
whose purpose includes layout, overflow, touch-target spacing, or responsive
behavior using Playwright test details, for example:

```typescript
test("readiness controls fit the viewport", { tag: "@layout" }, async ({ page }) => {
  // Exercise the controls and assert the layout behavior.
});
```

Use `@responsive` for behavior that changes with viewport size. Tagging is an
explicit coverage choice: a screenshot or an incidental overflow guard alone
does not require tagging every functional journey. The screenshot journeys
required by `frontend/scripts/assert-browser-smoke-artifacts.mjs` are retained
as representative layout tests, alongside focused responsive and spacing cases.
Unmarked journeys retain desktop coverage; narrow-width defects in those
journeys are an accepted coverage tradeoff (Q175, #3384). Check actual selection with
`pnpm --dir frontend exec playwright test --list --project=desktop` and
`pnpm --dir frontend exec playwright test --list --project=narrow`.

The core admin journeys check route-heading and keyboard focus behavior, reject
duplicate document IDs and horizontal overflow, and fail on unexpected
mutation requests, console errors, uncaught page errors, failed requests, or
HTTP error responses.

Screenshots, traces, and failure evidence are written under
`tmp/browser-smoke/`. CI uploads that directory from the dedicated browser
job. Every run clears prior evidence first and verifies the complete desktop and
narrow screenshot set before passing. These screenshots are review evidence
rather than pixel-diff baselines, so intentional visual changes do not require
snapshot churn.

The browser job runs Vite and Chromium together inside a per-job Docker container
on both hosted and self-hosted runners. Its private network namespace prevents
sibling jobs' Docker bridge lifecycle from reaching Chromium's network-change
observer; OS-assigned Vite ports still isolate overlapping suite processes.
The digest-pinned Playwright image supplies browser system libraries. Browser
execution runs inside the container, with private shared memory and the job's
actual UID/GID so artifacts remain writable by subsequent runner jobs. The host
installs frontend dependencies; the container uses the job's Node executable
and the image's Chromium. Every frontend CI job selects a pnpm store under its
own checkout before cache restore and install. The helper publishes a relative
cache target so runner slots share cache versions and archives; each restore
still writes only its own checkout. The new cache-key namespace resets the old
shared-home entries. Sibling restores cannot overwrite an install's objects. Checkout cleanup
owns this generated directory; jobs never clear the shared home store. The same
setup applies to validation, fork validation, both browser shards and evidence
verification. The wrapper verifies that its namespace differs from
the host and removes only its captured container ID, including after failures.
The workflow owns the image pin. Match the
image's Playwright version to the frontend dependency when updating it. Preserve
the same suite, diagnostics, timeouts and zero retries when qualifying changes
under normal sibling CI load.

Development fixtures remain excluded from production authority. `pnpm --dir
frontend validate` still runs the independent production build and
`assert-production-fixtures.mjs` scan; browser smoke does not weaken or replace
that proof. Deployed OIDC smoke remains a separate non-destructive evidence
layer because it validates service authentication and deployment wiring rather
than deterministic UI behavior.

Workflows are checked where they execute: CI runs them, `security.yml` runs
actionlint over every workflow, and `tests/test_github_actions_security.py`
holds the rules that apply to all of them. Do not add a test that re-reads one
workflow to check that it says what it says. Workflow tests take one of three
forms:

- Run an embedded script step with inputs (through `tests/support/workflows.py`
  `load_workflow` and `step_named`) and assert its output or exit code.
- Enforce a rule over every workflow, such as fork pull requests never reaching
  self-hosted runners, remote actions being pinned to a full commit SHA, or
  `run:` scripts reading inputs and event fields through `env` instead of
  `${{ }}`, and name the workflow file and violated rule on failure.
- Check that two real files agree, such as an admin wrapper's inputs and the
  pinned worker it forwards to.

## HTTP and ASGI contracts

- Use `tests.support.http.lifespan_client` or `tests.support.http.request` for
  ordinary HTTP contracts, including GET/POST, cookies, redirects, and OpenAPI.
  These helpers enter the application's lifespan through the supported async
  HTTP client path; do not construct HTTP ASGI scopes in contract tests.
  Their lifespan deadline only catches a startup or shutdown that never
  finishes. Production uvicorn sets no startup deadline, and a tight one fails
  when a loaded host stalls the test process. Do not test startup latency
  through it.
- `tests.support.raw_asgi.request` is reserved for duplicate, absent, or
  deliberately malformed framing headers that a supported client cannot emit.
  Streaming, body-limit, cancellation, and `http.disconnect` tests may use a
  bespoke scripted ASGI receive sequence when the shared helper cannot express
  the transport condition. Keep every raw-scope test explicit about the
  exceptional ASGI behavior being exercised.
- When moving a test between these paths, retain the behavior assertion and
  document any intentional coverage replacement in the change description; do
  not delete behavior tests merely because the support implementation changed.
