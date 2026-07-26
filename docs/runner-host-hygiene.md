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

The report is intentionally conservative. Missing required warm builders, low
free disk, Docker reclaimable bytes over budget, runner work-directory bytes over
budget, and orphan BuildKit artifacts all produce `attention` unless policy
explicitly permits that condition.
Reports also carry the typed observation counters used for evaluation, so audit
records preserve free disk, Docker reclaimable bytes, runner work-directory
bytes, Docker toolchain evidence, warm builders, and orphan BuildKit counts
instead of relying only on raw operator notes. Executor-written reports also
include read-only resource
inventory for Docker images and volumes. Image rows preserve repository, tag,
image ID, size, creation timestamp, dangling status, in-use hints, and whether
the image is one of the retained warm builders. Volume rows preserve name,
driver, mountpoint, labels, size when Docker exposes it, reference count when
Docker exposes it, and dangling status. These inventory fields are evidence for
operator review only; aggregate reclaimable totals are not enough to approve
destructive volume or image pruning.

Docker toolchain evidence is preserved for operator review and runner-readiness
follow-up. The hygiene report records Docker Engine version, Docker CLI version,
Docker Buildx CLI plugin version, plugin path/package/source, and BuildKit
version when the read-only probe can observe them. Hygiene does not decide lane
admission from those versions; [runner-lane-baseline.md](runner-lane-baseline.md)
owns the fail-closed Buildx minimum policy.

## CLI

Evaluate a saved observation without touching the host:

```bash
uv run launchplane work-graph runner-host-hygiene-report \
  --observation-file runner-host-observation.json \
  --minimum-free-disk-bytes 10737418240 \
  --maximum-docker-reclaimable-bytes 21474836480 \
  --required-warm-builder odoo-docker-chris-testing \
  --required-warm-builder odoo-enterprise-chris-testing
```

The command prints `mode: report-only` with the normalized observation, policy,
and report. It does not call Docker, inspect live services, contact GitHub, prune
images, or restart runners.

Operators can provide a policy JSON instead of CLI flags:

```bash
uv run launchplane work-graph runner-host-hygiene-report \
  --observation-file runner-host-observation.json \
  --policy-file runner-host-policy.json
```

## Apply Planning

Apply planning records the safety boundary a future host adapter must satisfy,
but it still does not execute cleanup:

```bash
uv run launchplane work-graph runner-host-hygiene-apply-plan \
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
`--mutate` records operator intent in the request so the boundary can be tested;
this CLI still prints a dry-run JSON plan and does not invoke Docker, systemd,
SSH, GitHub, or a host adapter.

When an audit key is provided, the CLI also emits a planned audit-record payload.
That record is a contract for a future Launchplane-owned storage write, not a
write performed by this local command.

Launchplane storage supports durable runner-host hygiene audit records keyed by
`audit_record_key`. Shared-service storage promotes the key, host, action,
status, and mutate intent into columns, while the full typed request, plan,
pre/post reports, retained warm-builder evidence, and operator message remain in
the JSON payload. `POST /v1/evidence/runner-host-hygiene/audits` is native
FastAPI evidence ingress for bearer-token callers. It accepts planned,
completed, and failed audit records for product/context `launchplane/launchplane`
under `runner_host_hygiene_audit.write`, preserves the `Idempotency-Key`
replay/conflict contract, and returns the accepted record key plus audit result
details. The local planner only prints the planned record; the approved executor
calls the service route after it captures the required pre/post host evidence.
The executor also keeps a host-persistent delivery envelope outside the Actions
workspace. It atomically records planned intent before mutation, records
`action_started` immediately before the privileged command, and records terminal
evidence before remote delivery. Transient delivery failures retain the exact
idempotency key and block another mutation for the same host/action until the
pending envelope is reconciled. The current-run envelope is copied to a redacted
workflow artifact; bearer tokens and raw authorization headers are never part of
the envelope. A permanently rejected planned audit (for example, authorization,
route, or idempotency conflict) blocks mutation; a retryable transport/service
failure may proceed only because the planned intent is already durable locally.

## Approved Ops-Lane Executor

The first live executor is `.github/workflows/runner-host-hygiene.yml`. It runs
on a dedicated self-hosted ops lane selected by the operator-managed
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
uses separate keys for default cache, dangling images, and each runtime-approved
Buildx builder. The workflow attempts every scheduled action even when an
earlier action is blocked or fails, then fails the aggregate job after all
per-action audits have been recorded. Default Docker cache keeps 30 days of
reuse history:

```bash
flock -n /tmp/launchplane-runner-host-hygiene.lock \
  docker builder prune --force --filter until=720h
```

Operators can override the age bound through the workflow's `prune_until` input,
but the executor enforces a minimum of `168h` and does not expose an unbounded
default-builder prune. Named Buildx builders use one audited command per
builder, a seven-day age floor, and the runtime-configured retained-space
budget. The executor addresses the deterministic, allowlisted BuildKit
container directly so the runner can retain its per-job isolated Docker config:

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

Launchplane's PostgreSQL integration job mounts `/var/lib/postgresql/data` as a
bounded tmpfs. The official PostgreSQL image declares that path as a volume;
overriding it keeps ephemeral test data out of durable anonymous Docker volumes
while preserving the existing Actions service health check and dynamic port.

It also supports `remove_buildkit_state_volumes`, implemented as a named
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
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_RETAINED_WARM_BUILDERS`
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_RUNNER_WORKDIR_ROOTS`, as comma-separated
  public-key/absolute-path pairs such as `legacy=/srv/runners`. The keys appear
  in evidence; absolute paths remain executor-local runtime input.
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_ALLOWED_BUILDKIT_STATE_VOLUMES` for any
  approved BuildKit state-volume retirement target. Leave it empty when no
  named volume cleanup has been reviewed.
- `LAUNCHPLANE_RUNNER_HOST_HYGIENE_BUILDKIT_CACHE_BUDGETS`, as comma-separated
  `builder=bytes` entries. Builder identities and byte budgets are runtime
  authority; production code contains no real builder names or capacities.
  Each builder may appear only once. The byte value is a pressure target for
  cache eligible under the age filter, not permission to delete recent or
  in-use cache merely to force the observed total below the target.

The executor fails closed unless the process user matches the requested service
user, the GitHub repository matches the requested repository scope, retained
warm builders are present in pre-apply evidence, the apply plan is ready, no
active Docker build client or another Actions `Runner.Worker` process is observed
in two consecutive local samples. Manual mutations additionally require
`mutate=true`. Monday-through-Saturday scheduled runs stop at the
`mutate_not_requested` blocker, while the Sunday maintenance schedule supplies
explicit mutation intent only for the bounded cache and dangling-image actions.
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
runner service restart, builder deletion, or automatic rollback. Operators
should use the captured image and volume inventory to decide any later
phase-two cleanup lane.
Runner work-directory evidence covers every operator-supplied root and records
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
config directory and opened config file owner, group, mode, exact binding,
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

The executor excludes only its own ancestor `Runner.Worker` from the idle gate;
any sibling worker still blocks a shared-host mutation. If a prior run stopped
after the durable `action_started` marker, an operator may manually dispatch with
`resolve_action_started=true`. That path captures current post evidence and
writes a terminal failed audit without repeating the privileged action.

Deploy the Launchplane service revision that understands the expanded audit
contract before installing the matching helper or dispatching this workflow.
Only after one audited dry run and each replacement mutation succeed should the
legacy host-local Docker timers be disabled. A service still running the prior
strict audit schema rejects the new evidence with HTTP 422; that blocks mutation
by design rather than silently operating without accepted evidence.

Cache and image prune requests accept whole-hour age filters no shorter than
`168h`. The scheduled default-builder action is deliberately more conservative
at `720h`; named warm builders and dangling images use `168h`.

Runner lane registration uses a separate manual ops-lane workflow,
`.github/workflows/runner-lane-registration.yml`. It shares the same approved
host, execution-lane, and service-user variables, but it does not prune Docker
state or restart existing services. Its first slice creates a new repo-scoped
Actions runner lane under an allowlisted registration root, starts only the
matching `launchplane-runner@<lane>.service` supervisor, and verifies the lane
through GitHub inventory before writing a completed registration audit.
Existing-lane adoption, stale-lane removal, and generic runner service restarts
remain outside this slice.

## Host Replacement Runbook

`chris-testing` is no longer just a basic self-hosted runner. It also carries the
Launchplane ops-gate lane used for runner-host hygiene, warm-builder evidence,
and shared Docker state observation. Treat replacement as a controlled lane
cutover, not as an interchangeable runner registration swap.

Use this sequence when replacing `chris-testing` or standing up a parallel host:

1. Provision the replacement host with Docker, GitHub Actions runner service
   management, the constrained runner service user, and the same filesystem
   permissions needed for Docker evidence and approved Docker cache or named
   volume mutations. Do not copy host-level Docker credentials from the old
   host.
2. Register the replacement runner with the common Launchplane labels required
   by [runner-lane-baseline.md](runner-lane-baseline.md), but keep the
   `chris-testing-ops-gate` label and any production-shared lane label off until
   the host passes readiness and hygiene evidence.
3. Seed or rebuild the retained warm-builder images on the replacement host.
   The hygiene executor must be able to observe every value configured in
   `LAUNCHPLANE_RUNNER_HOST_HYGIENE_RETAINED_WARM_BUILDERS` before the host can
   receive the ops-gate label.
4. Run `runner-baseline-observe` from a job on the replacement host and evaluate
   the baseline readiness. The result must show required labels, the expected
   service user/home-root constraints when configured, positive isolated Docker
   credential evidence, and Docker toolchain evidence that satisfies the
   configured Buildx minimum.
5. Run the runner-host hygiene workflow manually with `mutate=false`, no target
   volumes, and a replacement-specific audit key. The run must write an
   accepted planned audit, block only on `mutate_not_requested`, observe the
   retained warm builders, and include image/volume inventory for the new host.
6. Update the repository variables that define the approved hygiene host,
   execution lane, service user, and retained warm builders only after the new
   host has passing baseline and hygiene dry-run evidence. Keep the old values
   recoverable in the issue or runbook comment used for the cutover.
7. Add the `chris-testing-ops-gate` label to the replacement runner and remove
   it from the old runner in the same maintenance window. Avoid a period where
   two hosts carry the ops-gate label unless the workflow concurrency group and
   repository variables intentionally name the replacement host.
8. Re-run the hygiene workflow with `mutate=false` after the label and variable
   cutover. Treat any host mismatch, missing warm-builder evidence, missing
   inventory, or unexpected active-build failure as a rollback signal.
9. Leave the old host registered without shared or ops-gate labels until one
   scheduled dry-run has succeeded on the replacement host. Then drain/remove
   the old runner through the runner-control planning boundary and archive its
   final hygiene audit evidence.

Rollback is to remove the ops-gate label from the replacement runner, restore
the previous hygiene repository variables, and rerun the workflow with
`mutate=false` on the old host. Do not run ad hoc Docker cleanup on either host
as part of rollback; use the Launchplane-owned hygiene workflow or add a reviewed
service endpoint first.

## Adapter Boundary Planning

Before a real host mutation adapter is implemented, operators can review the
privileged execution boundary against a ready apply plan:

```bash
uv run launchplane work-graph runner-host-hygiene-adapter-boundary-plan \
  --adapter-type remote_host_executor \
  --host-name chris-testing \
  --execution-lane chris-testing-ops-gate \
  --service-user launchplane-runner-hygiene \
  --repository-scope cbusillo/launchplane \
  --privileged-scope docker_cache \
  --audit-record-key runner-host-hygiene/2026-05-23/chris-testing \
  --rollback-plan "Stop before mutation if retained builders are absent." \
  --pre-apply-evidence df \
  --pre-apply-evidence docker_summary \
  --pre-apply-evidence warm_builders \
  --post-apply-evidence df \
  --post-apply-evidence docker_summary \
  --post-apply-evidence warm_builders \
  --approved-host chris-testing \
  --allowed-adapter-type remote_host_executor \
  --allowed-execution-lane chris-testing-ops-gate \
  --allowed-service-user launchplane-runner-hygiene \
  --allowed-repository-scope cbusillo/launchplane \
  --allowed-privileged-scope docker_cache \
  --required-pre-apply-evidence df \
  --required-pre-apply-evidence docker_summary \
  --required-pre-apply-evidence warm_builders \
  --required-post-apply-evidence df \
  --required-post-apply-evidence docker_summary \
  --required-post-apply-evidence warm_builders \
  --apply-plan-file runner-host-apply-plan.json
```

This command is still a planning surface. It fails closed unless the apply plan
is ready, the host is approved, the proposal names an allowed adapter type,
execution lane, service user, repository scope, narrow privileged scope, audit
record key prefix, pre/post evidence set, and rollback or stop condition. It
does not call Docker, systemd, SSH, GitHub runner registration APIs, or a host
executor; the dedicated ops-lane executor is the separate apply surface.

The privileged scope must match the planned action exactly:

- `prune_docker_cache` requires `docker_cache`.
- `prune_dangling_images` requires `docker_cache` and never includes tagged
  images.
- `remove_buildkit_state_volumes` requires `docker_volume` and may only remove
  one explicitly requested, allowlisted, zero-link `buildx_buildkit_*_state`
  named volume observed in the pre-apply report.
- `prune_runner_workdir` requires `runner_workdir`.
- `restart_runner_service` requires `runner_service`.

Extra privileged scopes block the boundary plan so a Docker-cache prune cannot
quietly grow service-restart or work-directory powers.

## Future Apply Requirements

Before Launchplane grows additional host mutation powers, each apply design must
name:

- the disposable or explicitly approved host target
- the runner lane and repository scope the host adapter is allowed to affect
- the evidence snapshot captured before and after cleanup
- the warm builder retention budget
- the rollback or stop condition when cleanup cannot be completed safely
- the audit record written back to Launchplane-owned storage

Until those are present for another action, that action remains report-only or
dry-run planning only.
