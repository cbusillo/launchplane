---
title: Runner Host Hygiene
---

Launchplane owns shared self-hosted runner host hygiene. Product repositories
must not grow one-off Docker pruning, builder reset, or host cleanup workflows as
a workaround for shared runner pressure.

## Boundary

Runner host hygiene has two separate phases:

- Report-only evidence: collect host facts and evaluate them against a typed
  policy. This phase is safe for normal development because it does not mutate
  Docker, runner services, GitHub runner registrations, or product state.
- Apply workflow: clean Docker state, restart services, or change runner lanes
  through an approved Launchplane-owned host adapter. The first approved lanes
  are limited to bounded Docker cache pruning and explicit zero-link BuildKit
  state-volume removal; runner work-directory pruning and runner service
  restarts stay disabled.

The current Launchplane surface implements report-only evidence, local
apply-boundary planning, durable audit evidence, and a constrained self-hosted
ops executor for the approved first lane.

## Record-backed adapter qualification

The service has a source integration for the existing executor. This does not
activate a live migration or retire the Actions workflow below. Keep its
settings and credentials until supported service-only key adoption, binding
readback, isolated behavior qualification and rollback proof are complete.

The existing service runtime-setting review/apply path accepts
`LAUNCHPLANE_RUNNER_HOST_HYGIENE_CONFIGURATION` in the exact `launchplane`
context's `RuntimeEnvironmentRecord`. Its JSON value is validated by
`RunnerHostHygieneConfiguration` and the existing executor contract. It has no
file, process-environment or inherited-record fallback. The projection maps the
existing settings as follows:

| Existing hygiene setting suffix | Configuration field |
| --- | --- |
| `HOST` | `host_name` |
| `EXECUTION_LANE` | `execution_lane` |
| `SERVICE_USER` | `service_user` |
| `RETAINED_WARM_BUILDERS` | `retained_warm_builders` |
| `RUNNER_WORKDIR_ROOTS` | `runner_workdir_roots` |
| `GENERATED_RUN_CACHE_ROOTS` | `generated_run_cache_roots` |
| `GITHUB_IDLE_BINDINGS` | `github_idle_bindings` |
| `BUILDKIT_CACHE_BUDGETS` | `buildkit_cache_budgets` |
| `DEFAULT_CACHE_BUDGET_BYTES` | `default_cache_budget_bytes` |
| `GITHUB_APP_CLIENT_ID` | `github_app_client_id` |

The suffixes above share `LAUNCHPLANE_RUNNER_HOST_HYGIENE_`. The record also
names the existing scheduling `repository_scope` and the existing, separately
approved `allowed_buildkit_state_volumes`. Arrays use the executor's structured
root and binding fields rather than comma-separated workflow strings. Store
the complete host manifest; local service-unit discovery still rejects extra
or omitted services. Named budgets still select the Sunday builder actions.
Changing the budget list is therefore a scope change, not routine tuning.

`POST /v1/service/runner-host-hygiene/dispatch` returns the normalized configuration
digest, execution lane and validated executor requests. It accepts `report`,
`maintenance` and `manual` intent. Scheduled intent accepts no manual overrides;
scheduled audit keys use the service's UTC date, host and authenticated run ID.
Manual intent selects existing targets without replacing stored roots,
retained builders, budgets or allowlists. Both this route and
`POST /v1/service/runner-host-hygiene/metadata` require the existing authorized
hygiene workflow's `runner_host_hygiene_audit.write` identity, its signed run ID
and the stored scheduling repository. These reads grant no cleanup authority.

The metadata adapter uses the existing App through one exact, configured
`launchplane_service` context SecretRecord/SecretBinding under
`LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_APP_PRIVATE_KEY`. Global, instance and
ambiguous key bindings fail closed. It verifies the App client ID, installation
account, existing installation grants and coverage of every runtime-derived
repository. Every issued token is limited to Actions/Administration/Metadata read;
extra existing installation permissions require no grant change. Short-lived
read tokens stay in the service and are revoked after each metadata request,
including failed scope checks. One idle request reads the complete host sample
with one token. The executor accepts that whole sample at its observation
boundary and applies its existing per-binding provenance checks, including on
unavailable samples. Legacy local binding readers remain supported. The new
consumer retains no sample or token cache.
Only bounded typed idle samples or generated-cache run states leave it. Each
sample rechecks the configuration digest; local process, open-handle and
service-unit evidence remain separate executor observations.

The consumer entrypoint is
`uv run python -m control_plane.workflows.runner_host_hygiene_service_executor`.
It takes `--intent-file`, `--service-url`, `--execution-lane`,
`--audit-spool-root` and `--artifact-directory`; intent is the dispatch request
JSON, including the current runner name and a manual audit key when applicable.
It authenticates with the existing workflow OIDC path and verifies the local host/user and scheduling
repository before running any action. A minimal scheduler must still select
the explicit execution lane **before** starting the job. Retain that bootstrap
until a service scheduler selects it; a job-step lookup cannot set `runs-on`.

The consumer attempts every dispatched action and preserves the existing durable
audit envelopes and accepted terminal fields. Like the existing executor CLI,
it fails for invocation exceptions, pending audit delivery or a mutation that
does not complete. Run the existing `runner-host-hygiene-qualify` command on the
per-action artifact files to assess report and maintenance qualification,
including report-only and manual dry runs. No helper performs live import or
settings deletion. Until qualified activation, rollback is the retained
workflow with its existing settings; do not run both schedulers together.

Use the job's temporary directory for `--artifact-directory`. Before attempting
any action, the consumer writes `runner-host-hygiene-expected.txt` with every
expected artifact basename. Pass every listed file to qualification, including
missing files; a glob can silently omit a failed action. With
`--artifact-directory "$RUNNER_TEMP"`, the existing qualification command is:

```bash
audit_args=()
while IFS= read -r artifact; do
  audit_args+=(--audit-file "$RUNNER_TEMP/$artifact")
done < "$RUNNER_TEMP/runner-host-hygiene-expected.txt"
uv run launchplane runner-host-hygiene-qualify "${audit_args[@]}" --require-qualified
```

## Report Contract

The typed contract in `control_plane.contracts.runner_host_hygiene` separates
policy from observation:

- `RunnerHostHygienePolicy` records minimum free disk, optional Docker
  reclaimable and runner work-directory budgets, required warm builders, and
  whether orphan BuildKit artifacts are tolerated.
- `RunnerHostHygieneObservation` records facts collected by an approved
  read-only probe, including host name, free disk, Docker reclaimable bytes,
  runner work-directory bytes, Docker Engine/CLI/Buildx/BuildKit toolchain
  evidence, warm builders, read-only image and volume inventory, and orphan
  BuildKit counts.
- `evaluate_runner_host_hygiene(...)` returns a structured report with
  `healthy` or `attention` status, findings, and non-mutating next steps.

Reports preserve the observation timestamp and expose a generic
`cache_class_telemetry` read surface alongside the existing action-planning
fields. Each cache-class row carries a public key, class, scope, source,
measurement basis, availability, and the measurements that the source can
honestly provide:

- filesystem roots use apparent and allocated bytes
- Docker Engine classes use logical and reclaimable bytes
- measured zero is distinct from unavailable, partial, or stale evidence
- unavailable or partial evidence carries a bounded reason instead of silently
  becoming zero
- generated-cache rows may also carry bounded age buckets, estimated growth,
  and the latest cleanup snapshot

The generic rows do not grant cleanup authority. Existing action-specific
fields and apply-plan gates remain authoritative for the already approved
mutation lanes; new cache classes stay report-only.

The report is intentionally conservative. Missing required warm builders, low
free disk, Docker reclaimable bytes over budget, runner work-directory bytes over
budget, and orphan BuildKit artifacts all produce `attention` unless policy
explicitly permits that condition.
Reports also carry the typed observation counters used for evaluation, so audit
records preserve free disk, Docker reclaimable bytes, runner work-directory
bytes, Docker toolchain evidence, warm builders, and orphan BuildKit counts
instead of relying only on raw admin notes. Executor-written reports also
include read-only resource inventory for Docker images and volumes. The
executor sorts each inventory deterministically, retains at most 128 rows, and
records the total count plus a truncation flag. Image and volume sizes are
nullable when Docker does not expose them; absence is not converted into
measured zero. These inventory fields are evidence for Director review only;
aggregate reclaimable totals are not enough to approve destructive volume or
image pruning.

Docker toolchain evidence is preserved for Director review and runner-readiness
follow-up. The hygiene report records Docker Engine version, Docker CLI version,
Docker Buildx CLI plugin version, plugin path/package/source, and BuildKit
version when the read-only probe can observe them. Hygiene does not decide lane
admission from those versions; [runner-lane-baseline.md](runner-lane-baseline.md)
owns the fail-closed Buildx minimum policy.

## CLI

Evaluate a saved observation without touching the host:

```bash
uv run launchplane runner-host-hygiene-report \
  --observation-file runner-host-observation.json \
  --minimum-free-disk-bytes 10737418240 \
  --maximum-docker-reclaimable-bytes 21474836480 \
  --required-warm-builder odoo-docker-chris-testing \
  --required-warm-builder odoo-enterprise-chris-testing
```

The command prints `mode: report-only` with the normalized observation, policy,
and report. It does not call Docker, inspect live services, contact GitHub, prune
images, or restart runners.

Admins can provide a policy JSON instead of CLI flags:

```bash
uv run launchplane runner-host-hygiene-report \
  --observation-file runner-host-observation.json \
  --policy-file runner-host-policy.json
```

## Apply Planning

The local apply planner checks the safety boundary without executing cleanup:

```bash
uv run launchplane runner-host-hygiene-apply-plan \
  --action prune_docker_cache \
  --host-name chris-testing \
  --mutate \
  --audit-record-key runner-host-hygiene/2026-05-23/chris-testing \
  --approved-host chris-testing \
  --allow-docker-cache-prune \
  --required-retained-warm-builder odoo-docker-chris-testing \
  --retained-warm-builder odoo-docker-chris-testing \
  --report-file runner-host-report.json
```

The planner fails closed unless the host is approved, the action is enabled, the
request carries explicit mutate intent, the pre-apply report is healthy, required
warm builders are retained, and an audit record key is present. Passing
`--mutate` records admin intent in the request so the boundary can be tested;
this CLI still prints a dry-run JSON plan and does not invoke Docker, systemd,
SSH, GitHub, or a host adapter.

Launchplane storage supports durable runner-host hygiene audit records keyed by
`audit_record_key`. Shared-service storage promotes the key, host, action,
status, and mutate intent into columns, while the full typed request, plan,
pre/post reports, retained warm-builder evidence, and admin message remain in
the JSON payload. `POST /v1/evidence/runner-host-hygiene/audits` is native
FastAPI evidence ingress for bearer-token callers. It accepts planned,
completed, and failed audit records for product/context `launchplane/launchplane`
under `runner_host_hygiene_audit.write`, preserves the `Idempotency-Key`
replay/conflict contract, and returns the accepted record key plus audit result
details. The local planner only prints the planned record; the approved executor
calls the service route after it captures the required pre/post host evidence.
Every mutation requires a host-persistent delivery envelope outside the Actions
workspace. It atomically records planned intent, then stores the fresh
pre-action request, ready plan, and sanitized source evidence together with
`action_started` immediately before the privileged command. Terminal evidence
is durable before remote delivery. Transient delivery failures retain the exact
idempotency key and block another mutation for the same host/action until the
pending envelope is reconciled. The envelope also stores a SHA-256 integrity
digest over runtime-only executor inputs so recovery cannot collect post
evidence under changed roots, repositories, lanes, or targets. The current-run
envelope is copied to a redacted workflow artifact; bearer tokens, raw runtime
topology, and authorization headers are never part of persisted evidence. A
permanently rejected planned audit blocks mutation. Generated run-cache
mutation is stricter still: accepted service delivery of the planned audit is
required before the privileged helper can run. A pending or rejected planned
record leaves that action blocked.

Authorized admins and workflows can read the durable evidence through:

- `GET /v1/evidence/runner-host-hygiene/audits` for a bounded summary list,
  filtered by optional host, action, and audit status
- `GET /v1/evidence/runner-host-hygiene/audits/record` with an
  `audit_record_key` query parameter for one sanitized detail projection
- `GET /v1/evidence/runner-host-hygiene/history` with a required `host_name`
  and optional `cache_key` for bounded pre/post observation chronology

The read routes require `runner_host_hygiene_audit.read` for
`launchplane/launchplane`. List and history limits default to 25 and cannot
exceed 100. Detail and history projections omit raw image and volume inventory
rows; they expose bounded counts, truncation state, finding codes, and generic
cache-class telemetry. Legacy audit reports that predate timestamp persistence
remain readable with `chronology_state: legacy_missing`; Launchplane does not
infer observation time from the audit key. History responses expose separate
`scan_truncated` and `result_truncated` flags so callers can distinguish the
fixed 100-audit scan ceiling from the requested result limit.

## Approved Ops-Lane Executor

The protected `.github/workflows/runner-host-hygiene.yml` workflow invokes
`uv run launchplane runner-host-hygiene-executor`. It supplies
`--audit-spool-root` for host-persistent delivery state and
`--audit-artifact-file` for the redacted current-run envelope;
`--resolve-action-started` is its interrupted-action recovery option, described
below. The workflow runs
on a dedicated self-hosted ops lane selected by the admin-managed
`LAUNCHPLANE_RUNNER_HOST_HYGIENE_EXECUTION_LANE` repository variable,
authenticates back to
Launchplane with GitHub Actions OIDC, and executes on the runner host as the
constrained service user. The workflow runs daily on a schedule in dry-run mode
from Monday through Saturday and can also be manually dispatched for approved
mutations. The Sunday schedule runs audited maintenance. Every scheduled action
uses its own audit key and executor invocation, so a successful mutation cannot
be hidden by a later action failure.

The report-only schedule generates an audit key under
`runner-host-hygiene/<date>/<host>-scheduled-report-<run-id>`. Sunday maintenance
uses separate keys for default cache, dangling images, each runtime-approved
Buildx builder, and each runtime-approved generated run cache. The workflow
attempts every scheduled action even when an
earlier action is blocked or fails, then fails the aggregate job after all
per-action audits have been recorded. Default Docker cache uses an
admin-configured LRU retained-space ceiling:

```bash
flock -n /tmp/launchplane-runner-host-hygiene.lock \
  curl --silent --show-error --fail --request POST \
  --unix-socket /var/run/docker.sock \
  'http://localhost/v1.45/build/prune?all=true&keep-storage=<approved-bytes>'
```

The default daemon cache budget uses BuildKit's least-recently-used ordering and
does not combine the ceiling with an age filter. An age-only filter cannot
release old parent layers while a newer excluded child still depends on them;
the size-bounded pass can release the least-recently-used leaf first and then
collect newly unreferenced parents. The executor enforces an 8 GiB minimum for
this ceiling and uses the daemon-native Engine endpoint so per-job
Docker/Buildx client isolation cannot redirect cleanup to an empty client-local
builder view. The action cannot affect Docker images, volumes, or named Buildx
builders. Direct executor calls without a default-cache budget retain the
age-only behavior and enforce a minimum `prune_until` of `168h`.

Named Buildx builders use one audited command per builder, a seven-day age
floor, and the runtime-configured retained-space budget. The executor addresses
the deterministic, allowlisted BuildKit container directly so the runner can
retain its per-job isolated Docker config:

```bash
flock -n /tmp/launchplane-runner-host-hygiene.lock \
  docker exec buildx_buildkit_<approved-builder>0 \
  buildctl prune \
  --all \
  --keep-duration 168h \
  --keep-storage <approved-megabytes>
```

The byte budget is rounded up to BuildKit's whole-megabyte `--keep-storage`
unit. The executor first verifies that the exact container is running and that
its `buildctl` supports both bounded flags. It never reconstructs or imports
host-persistent Buildx client metadata into the isolated job configuration.

The separate `prune_dangling_images` action runs `docker image prune --force
--filter until=168h`. It never supplies `-a` or `--all`, so tagged retained warm
images remain outside that action.

The `prune_generated_run_cache` action is narrower than runner work-directory
cleanup. It addresses only top-level directories whose names match a
root-owned configured run-cache prefix and numeric GitHub Actions run id. Before
the planned audit becomes mutation authority, Launchplane records apparent and
allocated bytes, age buckets, last cleanup and growth evidence, two local
open-handle observations per run, two `Runner.Worker` counts for the configured
owning user, and the source repository's live GitHub run state. Cleanup remains
blocked unless the exact run is completed, old enough, free of open handles,
the owning user is idle in both samples, the class is above its high-water
limit, and its cooldown has expired. The helper deletes only selected
completed-run directories and records the lower post-cleanup size; it never
touches sibling Cargo, Bazel disk, package, credential, or configuration roots.

Idle safety is persisted as typed, source-attributed convergence rather than
anonymous process-count tuples. Shared Docker actions use one `full_host`
convergence containing consecutive GitHub job, GitHub runner, local process,
and runner-service observations. A runtime-only host binding manifest names
every expected repository, lane, runner, and systemd service unit. The executor
builds GitHub source requirements from that manifest and requires its service
units to match the complete local `systemctl` inventory; an omitted or extra
lane therefore blocks rather than disappearing from `full_host` evidence. Only
public hashes and counts enter persisted evidence. The executor excludes only
its own GitHub job, identified by `GITHUB_RUN_ID` and `RUNNER_NAME`, across the
current repository/lane bindings, and excludes only its own runner
registration. Sibling jobs and runner registrations remain visible. Missing
identity, unauthorized APIs, incomplete pagination, manifest disagreement,
stale timestamps, or a short or zero observation window blocks the apply plan.
Generated run-cache actions do not use full-host evidence. Each policy-owned
root instead records one `isolated_user` convergence with owning-process,
per-entry open-handle, and GitHub run-state evidence. `isolated_lane` remains a
reserved contract value and is rejected by this executor until lane-scoped
process evidence exists. Successful and blocking convergence is preserved in
planned and terminal audit reports and is exposed through the sanitized
runner-host hygiene read model.

The executor also supports `remove_buildkit_state_volumes`, implemented as a named
single-volume `docker volume rm` call under the same lock. This action is
intentionally not `docker volume prune`: the apply plan must name every target
volume, the policy must allowlist those names from the separate
`LAUNCHPLANE_RUNNER_HOST_HYGIENE_ALLOWED_BUILDKIT_STATE_VOLUMES` repository
variable, and the pre-apply report must show each target is a
`buildx_buildkit_*_state` volume with zero container links. Linked warm builder
state volumes remain blocked even when explicitly requested. Only one target
volume is accepted per mutation run so a failed removal cannot hide an earlier
successful deletion in the same command.

The workflow requires these repository variables:

- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_HOST`
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_EXECUTION_LANE`
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_SERVICE_USER`
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_RUNNER_WORKDIR_ROOTS`, as comma-separated
  public-key/absolute-path pairs such as `legacy=/srv/runners`. The keys appear
  in evidence; absolute paths remain executor-local runtime input.
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_GENERATED_RUN_CACHE_ROOTS`, as comma-separated
  `key=/absolute/path|owner/repository|high-bytes|low-bytes|min-age-hours|cooldown-hours`
  entries. Only the public key, run ids, and bounded measurements enter audit
  evidence; the absolute path and repository identity remain executor-local
  runtime authority. High water must be positive, low water must be smaller,
  and minimum age must be at least one hour.
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_IDLE_BINDINGS`, as comma-separated
  `owner/repository|lane|runner-name|systemd-service-unit` entries covering
  every runner service on the host. This is runtime topology, not a checked-in
  catalog. Shared Docker mutation is rejected unless the manifest includes the
  current `GITHUB_REPOSITORY`, execution lane, and `RUNNER_NAME`, and its exact
  service-unit set matches local discovery.
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_ALLOWED_BUILDKIT_STATE_VOLUMES` for any
  approved BuildKit state-volume retirement target. Leave it empty when no
  named volume cleanup has been reviewed.
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_BUILDKIT_CACHE_BUDGETS`, as comma-separated
  `builder=bytes` entries. Builder identities and byte budgets are runtime
  authority; production code contains no real builder names or capacities.
  Each builder may appear only once. The byte value is a pressure target for
  cache eligible under the age filter, not permission to delete recent or
  in-use cache merely to force the observed total below the target.
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_DEFAULT_CACHE_BUDGET_BYTES`, as the positive
  retained-space ceiling for the daemon-global default BuildKit cache. The
  executor rejects values below 8 GiB. Capacity remains runtime authority;
  production code contains no host-specific cache budget.
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_APP_CLIENT_ID`, as the non-secret
  client ID for the dedicated runner-host hygiene GitHub App.

`LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_APP_PRIVATE_KEY` is the repository
secret for the dedicated runner-host hygiene GitHub App, with read-only Actions
and Administration permissions. Actions reads
provide active workflow and job state, while Administration read is needed to
list self-hosted runner registrations; GitHub adds Metadata read implicitly.
The workflow derives the exact repository list from runtime inputs, rejects
cross-repository-owner scopes that cannot be represented by one installation token, and
mints a short-lived token through the commit-pinned official GitHub action. The
App installation must cover every runtime-derived repository. Additional
repositories under the same GitHub account are supported, including an
installation on all repositories in that account. The workflow validates coverage through the
installation API and reports only the required repository count and digest;
it does not pass repository names to the token
action because the action logs explicit repository inputs. The token requests
only Actions read and Administration read, and the action revokes it at job
completion. There is no PAT, default `GITHUB_TOKEN`, or runner-registration-token
fallback. Missing App configuration, missing required coverage, or an API that
cannot see a named runner fails closed.
Organization or enterprise registrations require an equivalent reader before
they can be listed in the manifest. Generated-cache run-state reads use only
Actions read. Neither the App nor its installation token grants
runner-registration write or repository-content write authority.

GitHub App installation tokens are intentionally short-lived. Keep the current
scheduled action fan-out comfortably below the one-hour token lifetime; if
maintenance grows toward that boundary, split it into independently tokenized
jobs rather than adding a longer-lived credential or refresh fallback.

The executor fails closed unless the process user matches the requested service
user, the GitHub repository matches the requested repository scope, retained
warm builders are present in pre-apply evidence, the apply plan is ready, and
the action's required idle scope converges over at least two samples separated
by a nonzero interval. Every mutation requires a durable host-local audit spool.
After planned-audit delivery, the executor recollects every required source,
replans against a new decision timestamp, and atomically stores that sanitized
authorization with `action_started` before invoking the mutation. Manual
mutations additionally require `mutate=true`.
Monday-through-Saturday scheduled runs stop at the
`mutate_not_requested` blocker, while the Sunday maintenance schedule supplies
explicit mutation intent for bounded Docker, dangling-image, named-builder, and
generated completed-run cache actions. Shared Docker actions require full-host
idle from every required GitHub and local source. Generated run-cache actions
instead require two idle observations for the configured owning user plus two
zero-handle observations for every exact target; unrelated users and lanes do
not block that isolated cache class. The default evidence age limit is five
minutes and is configurable through `--idle-max-age-seconds` without permitting
zero-second observation intervals. A failed GitHub sample retains its failure
reason when combined with a successful sample; mixed availability is incomplete
evidence, not a contradiction. Actual contradictory evidence remains distinct.
GitHub jobs in `requested` state count as active, including before queue admission.
Cache and image maintenance may proceed when the report has unrelated attention
findings so cleanup can remediate pressure; host identity, warm-image retention,
audit durability, exact builder allowlists, and the idle gate remain mandatory.
Mutating runs write a `planned` audit before mutation, then write `completed`
only when the bounded mutation command succeeds and action-specific safety
postconditions pass. Unrelated report attention remains visible in post-apply
evidence and the completion message rather than blocking the cleanup intended
to remediate it. The CLI exits nonzero when a requested mutation is blocked,
fails, or leaves audit delivery pending. It does not run `docker system prune`, `docker
image prune -a`, generic `docker volume prune`, runner work-directory deletion,
runner service restart, builder deletion, or automatic rollback. The Director
should use the captured image and volume inventory to decide any later
phase-two cleanup lane.
Runner work-directory evidence covers every admin-supplied root and records
both apparent and allocated bytes per public root key; Docker reclaimable bytes
are also split into images, containers, local volumes, and build cache while the
existing aggregate remains backward-compatible. Docker totals and volume
inventory come from one verbose Engine `/system/df` API snapshot rather than two
separate daemon-wide `docker system df` scans. The executor discovers only
depth-two `_work` registration directories through the root-owned
`/usr/local/sbin/launchplane-runner-workdir-usage` helper. Install that helper
from `scripts/runner-host-hygiene-workdir-usage.sh` only from a reviewed merged
revision. The helper performs two read-only GNU `du` measurements and emits only
the apparent and allocated byte totals for a complete measurement. It adds a
third `partial` line only when a retried traversal still races with live
workspace changes. A configured root with no matching
registration fails closed instead of silently reporting zero usage.

The helper reads exact public-key/path bindings from the root-owned mode-`0600`
file `/etc/launchplane/runner-host-hygiene-roots`. Those bindings must match
`LAUNCHPLANE_RUNNER_HOST_HYGIENE_RUNNER_WORKDIR_ROOTS`; for example:

```text
legacy=/srv/runners
```

Install the boundary from a reviewed merged checkout with equivalent ownership
and modes:

```bash
install -d -o root -g root -m 0700 /etc/launchplane
install -o root -g root -m 0755 \
  scripts/runner-host-hygiene-workdir-usage.sh \
  /usr/local/sbin/launchplane-runner-workdir-usage
install -o root -g root -m 0600 <prepared-bindings-file> \
  /etc/launchplane/runner-host-hygiene-roots
```

The sudoers snippet must keep `env_reset`, a root-owned `secure_path`, and
`NOSETENV`, and permit only the helper with one public-safe binding argument:

```sudoers
Defaults:<service-user> env_reset, secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Cmnd_Alias LAUNCHPLANE_RUNNER_HYGIENE_WORKDIR_USAGE = /usr/local/sbin/launchplane-runner-workdir-usage ^[a-z0-9][a-z0-9._-]{0,63}=/[^[:space:]]+$
<service-user> ALL=(root) NOPASSWD: NOSETENV: LAUNCHPLANE_RUNNER_HYGIENE_WORKDIR_USAGE
```

Install the snippet as root-owned mode `0440`, then validate the candidate and
the complete policy with `visudo -cf`. The helper independently verifies the
config directory and opened config file owning user, group, mode, exact binding,
canonical root path, same-filesystem traversal, and non-empty registration set.
Its stdout contract is apparent bytes, allocated bytes, and a final
optional `partial` status line; two lines therefore remain compatible with the
previous executor. A transient `du` race is retried once; a second partial
traversal remains visible in typed report findings rather than being presented
as an authoritative total. Do not grant generic passwordless
`du`, `find`, shell, or arbitrary-path sudo. The helper suppresses path-bearing
errors, and the executor treats any denied or failed privileged measurement as
incomplete evidence. A service-user-controlled helper, config directory,
config file, or sudo environment invalidates this security boundary.

Generated run-cache inventory and cleanup use the separate root-owned helper
`/usr/local/sbin/launchplane-generated-run-cache`, installed only from
`scripts/runner-host-hygiene-generated-run-cache.sh` in a reviewed merged
revision. Its mode-`0600` config file is
`/etc/launchplane/runner-host-hygiene-generated-caches`; each line binds the
runtime public key and path to the expected owning user, group, root mode, exact
top-level directory prefix, minimum age, high water, low water, and cooldown:

```text
public-key=/srv/generated-cache|runner-user|runner-group|755|run-cache-|1|21474836480|4294967296|1
```

Install the helper and prepared binding file as root, then add only the two
argument-bounded helper forms to the existing hygiene sudoers policy:

```bash
install -o root -g root -m 0755 \
  scripts/runner-host-hygiene-generated-run-cache.sh \
  /usr/local/sbin/launchplane-generated-run-cache
install -o root -g root -m 0600 <prepared-generated-cache-bindings> \
  /etc/launchplane/runner-host-hygiene-generated-caches
```

```sudoers
Cmnd_Alias LAUNCHPLANE_GENERATED_CACHE_OBSERVE = /usr/local/sbin/launchplane-generated-run-cache ^observe [a-z0-9][a-z0-9._-]{0,63}=/[^[:space:]]+ ([2-9]|10) ([1-9]|[1-5][0-9]|60)$
Cmnd_Alias LAUNCHPLANE_GENERATED_CACHE_PRUNE = /usr/local/sbin/launchplane-generated-run-cache ^prune [a-z0-9][a-z0-9._-]{0,63}=/[^[:space:]]+ ([2-9]|10) ([1-9]|[1-5][0-9]|60) [1-9][0-9]*(,[1-9][0-9]*)*$
<service-user> ALL=(root) NOPASSWD: NOSETENV: LAUNCHPLANE_GENERATED_CACHE_OBSERVE, LAUNCHPLANE_GENERATED_CACHE_PRUNE
```

Validate the candidate and complete sudoers policy with `visudo -cf`. The
helper independently revalidates the root-owned config and fixed retention
policy, canonical root,
owner/group/mode, top-level name shape, same-filesystem traversal,
non-dereferencing handling of internal symlinks, fail-closed nested-mount
detection, age floor, owning-user idle samples, and exact target open handles. It emits
only public keys, numeric run ids, counts, sizes, ages, and booleans. Deleting
completed run-scoped generated data has no byte-for-byte rollback; recovery is
regeneration in a later run, while the durable `action_started` envelope
prevents an uncertain action from being repeated automatically.

The helper never follows internal symlinks during measurement, quarantine, or
removal; only an exact canonical top-level directory can become a target. It
fails closed when mount discovery is unavailable or incomplete. A
post-quarantine owning-user worker and open-handle check restores the exact entries
before aborting when work starts during the final race window; an ambiguous
restore leaves the root-owned quarantine visible as partial evidence instead of
deleting it. A
cleanup attempt that removes data but cannot reach the configured low-water
target persists its reclaimed-byte and target receipt without advancing the
successful-cleanup cooldown, so a corrected follow-up can run immediately.
Grant the prune form only to the dedicated hygiene workflow service account;
direct interactive helper invocation is unsupported because the workflow owns
the accepted planned and terminal audit sequence.

The executor excludes only its own ancestor `Runner.Worker` from the idle gate;
any sibling worker still blocks a shared-host mutation. If a prior run stopped
after the durable `action_started` marker, an admin may manually dispatch with
`resolve_action_started=true`. That path captures current post evidence and
writes a terminal failed audit without repeating the privileged action.

Cache and image prune requests accept whole-hour age filters no shorter than
`168h`. Named warm builders and dangling images use `168h`. The scheduled
default-daemon action supplies its retained-space ceiling instead; its required
`prune_until` value is recorded with the request but is not applied when the
size budget is present.

Runner lane registration, retirement, and filesystem safety are documented in
[runner-lane-baseline.md](runner-lane-baseline.md#lifecycle-executors). Lifecycle
and hygiene mutations share the host lock. Host provisioning and replacement
belong to the existing private
[infrastructure runbook](https://github.com/cbusillo/shiny-infra-ops/blob/main/docs/infrastructure/remote-build-host.md);
its host facts do not establish Launchplane readiness.

The local `runner-host-hygiene-adapter-boundary-plan` command remains a dry-run
planner for a ready apply plan, not an execution path. It requires the exact
action's privileged scope, approved host/lane/user/repository, audit key, pre/post
evidence, and a rollback or stop condition; extra privileged scopes block it.

## Qualify an existing configuration after drift

The workflow requires qualified audit artifacts before passing. A blocked or
unqualified report, missing evidence, or requested maintenance without accepted
terminal completion fails the job; a healthy report-only plan and a recognized
not-needed action can pass without claiming maintenance ran. The workflow
summary separates report delivery,
report status, retention qualification and accepted terminal maintenance. The
qualifier uses the audit envelope's single `maintenance_completed` rule.
The latest existing audit record supplies the report and blockers; terminal
status and delivery are shown even when a failed action has a healthy report.
A run-local expected-action manifest also renders missing evidence for later
Sunday targets when the summary step can run. A job cancellation can prevent
the summary step itself. Accepted planned delivery, `blocked`, `not_needed` and pending terminal
delivery never establish completed maintenance. A completed action can still
leave cache above target or have unknown measurements; completion and retention
are separate.

Each scheduled report receives the existing default-daemon and named-builder
budgets through `--buildkit-cache-budget key=bytes`. These are read-only report
inputs. The workflow's existing runtime budget setting also selects named
Sunday maintenance targets; adding an entry there remains a maintenance-scope
change, not a way to suppress a report finding. The report-only CLI flag itself
grants no prune permission. Target actions still require their existing exact
allowlist, target, budget, idle evidence, host lock and durable audit/replay
path. The configuration projection uses the existing executor request contract;
the record-backed migration can supply that same input without a new store.

Retained warm-builder references are exact local image references, independent
of BuildKit cache names. A producer that uses run-specific verification tags
does not provide a stable image to retain. Remove obsolete image references
from the existing `RETAINED_WARM_BUILDERS` setting rather than manufacturing
tags or treating a live builder as proof that a requested image exists. An
empty list is supported when named cache budgets are configured; their measured
cache evidence remains required. The record-backed configuration still requires
the `retained_warm_builders` field, with an explicit empty array in that case.
Every image reference still configured must exist before and after cleanup.

Reports measure each configured builder using its own container's read-only
`buildctl du --format '{{json .}}'`. They retain only aggregate counters, never
raw cache descriptions. The existing complete Docker container inventory also
identifies unbudgeted named caches: these remain measured report-only rows with
an admin recovery assignment, no target and no cleanup permission. Stopped
unbudgeted containers have an explicit unmeasured report-only state and are
never probed or counted as zero. Running unbudgeted probe failures are likewise
explicit unmeasured report-only rows with recovery assignment, never measured
zero. A configured stopped or missing builder remains unknown. Existing orphan
findings still apply. Their actual producer/retention responsibility must be
reviewed independently before any budget or maintenance is proposed. The
daemon-default cache remains a distinct row from the named builders.

The report's invariant is that every configured builder has measured logical
budgeted usage within target, or measured in-use/recent-cache evidence
explaining the excess; otherwise it records a maintenance or measurement
finding with a recovery assignment. Named-builder age accounting uses the
selected action's configured retention duration, with the existing seven-day
minimum for other named rows. Shared bytes remain information only; sharing
does not protect a record from GC. The daemon and named rows preserve full
`logical_bytes` and informational `shared_bytes`, but compare the target
against non-shared `budgeted_bytes`, matching BuildKit GC. Named rows also
record the effective target after the existing adapter's megabyte rounding. The
pre-existing Docker reclaimable aggregate remains unchanged and is a separate
measurement. The default-daemon action has no age filter. Report-only budgets
never enter interrupted-action recovery identity. Missing, failed, malformed,
duplicate or truncated measurements are unknown, never zero. Active mutable BuildKit records have unknown size despite
reporting zero. Qualification deliberately requires complete logical sizes, more
strictly than GC. Engine omits mutability, so active zero-sized Engine records
are conservatively unknown in the new daemon qualification row. Empty caches and
inactive zero records remain measured zero. The legacy aggregate retains its
Engine-reported size basis and admission behavior; during an active mutable
record it is a lower bound, with known reclaimable bytes preserved. Thus weekday
qualification can need attention during a build without changing Sunday prune
admission or replacing its existing audited idle block.
[Engine cache records](https://github.com/moby/moby/blob/master/api/types/build/cache.go)
explain the API limit. Logical targets
are not physical-volume quotas, and excess is not an expected-reclaim estimate.
Overlapping protection categories are counted once.
[BuildKit's accounting implementation](https://github.com/moby/buildkit/blob/master/cmd/buildctl/diskusage.go)
and [client record format](https://github.com/moby/buildkit/blob/master/client/diskusage.go)
own the probe format.
[BuildKit GC accounting](https://github.com/moby/buildkit/blob/master/cache/manager.go)
owns the non-shared target basis and last-used retention semantics.

Evaluate the existing private audit artifact without contacting a host:

```bash
uv run launchplane runner-host-hygiene-qualify \
  --audit-file /private/evidence/runner-host-hygiene-audit.json \
  --require-qualified
```

The default report-only mode prints diagnosis without failing delivery; invalid
or absent artifacts produce fixed unqualified diagnostics. With `--summary-file`,
only bounded action/audit identifiers, snapshot time and contract codes are
rendered. Detailed JSON stays out of that job log. Summary snapshots do not claim
current-time freshness after a long Sunday batch. The explicit qualification
step allows snapshots up to the job's three-hour execution limit so earlier
Sunday actions do not expire during the same batch. The qualification CLI
mode exits nonzero for stale/missing evidence, report
findings, unresolved execution/recovery, pending delivery or action blockers other
than recognized existing not-needed outcomes and the expected
`mutate_not_requested` dry-run blocker. Legacy artifacts without builder evidence
remain unqualified. `--maximum-age-seconds` bounds review-artifact age separately
from the executor's unchanged idle freshness gate. To reproduce historical
qualification, pin `--as-of YYYY-MM-DDTHH:MM:SS+0000`; this proves only that saved
instant, never current host state. Terminal completion comes from that artifact's
own exact audit, without matching today's scheduler key to a prior run.

For a correction, first run the existing report-only path and retain its accepted
audit plus complete private service/registration and producer readbacks. Compare
the complete host fleet with the existing GitHub idle bindings; include every
service required by the complete host check. Use the existing image-inspect
results to qualify retained references, preserving Docker's normal tag/digest
resolution. A live builder container does not prove a warm image exists. If a
producer emits run-specific verification tags, an arbitrary current tag is not a
durable replacement for a fixed retained reference: qualify its production and
retention lifecycle before proposing the exact setting or producer correction.

Prepare the exact correction through the existing configuration's supported
path, with the prior settings retained privately for rollback. A live correction
requires its separately recorded Director decision, including the consequence
that an existing maintenance schedule can resume once blockers clear. Obtain a
fresh accepted report-only audit afterward. Source landing, a replayed historical
qualification and a report-only request authorize no host apply, prune, restart,
grant or legacy timer change. This report adds no train, deploy or recovery
admission gate; qualification failure stays with the hygiene recovery assignment.
