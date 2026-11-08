---
title: Runner Lane Baseline
---

Launchplane owns the safety contract for self-hosted GitHub Actions runner lanes
used by product publish, preview, and promotion workflows. Product repositories
may request work, but runner host readiness and Docker credential hygiene belong
to Launchplane rather than repo-by-repo workflow workarounds.
Shared host hygiene reporting lives in
[runner-host-hygiene.md](runner-host-hygiene.md); lane readiness and lane control
planning stay here.

## Baseline Policy

Every Launchplane-controlled runner lane must satisfy the baseline before it is
treated as ready for shared product automation:

- The lane is discoverable through GitHub's self-hosted runner inventory.
- The lane carries the required identity labels, including `self-hosted` and
  `launchplane` unless a narrower policy overrides them.
- Docker registry credentials are isolated per job. The readiness contract fails
  closed unless there is positive evidence that jobs do not share a mutable
  host-level Docker config.
- Shared product lanes that run Docker image builds record Docker Engine, Docker
  CLI, Docker Buildx CLI plugin, plugin path/package/source, and BuildKit
  version evidence. Policies can require that evidence and can enforce a
  minimum Buildx CLI plugin version for workflows that depend on modern
  `docker/build-push-action` behavior.
- Optional host checks may constrain the service user and home-directory roots
  used by managed runner lanes.

The baseline does not grant permission to mutate product or provider state. It
only says whether a runner lane is safe enough to receive work that other
Launchplane policies already authorized.

## Readiness Contract

The typed contract in `control_plane.contracts.runner_lane_baseline` separates
policy from observation:

- `RunnerLaneBaselinePolicy` records required labels, whether Docker credential
  isolation is mandatory, optional Docker toolchain evidence requirements,
  minimum Docker Buildx CLI version, and optional service-user or home-root
  constraints.
- `RunnerLaneBaselineObservation` records observed lane facts from a host or
  runner bootstrap check, including Docker toolchain evidence when supplied.
- `evaluate_runner_lane_baseline(...)` returns `RunnerLaneBaselineReadiness`
  with a fail-closed summary and structured violations.

No observation means not ready. Unknown Docker credential isolation means not
ready. Missing labels or configured host guardrails also make the lane not ready.
Home-directory checks canonicalize observed and allowed paths before comparing
roots, so traversal segments such as `..` cannot satisfy a configured root by
string prefix alone. Readiness counts lanes by unique runner name, so duplicate
observations for the same runner do not inflate the observed or compliant lane
totals.

When a policy requires Docker toolchain observation, missing toolchain evidence
or missing Buildx CLI version evidence is not ready. When it sets a
`minimum_docker_buildx_version`, an unparsable Buildx version is not ready, and
a stale Buildx CLI plugin is not ready. For example, a `0.13.1+ds1`
Debian-packaged Buildx plugin fails a `0.23.0` minimum even when the workflow's
BuildKit builder container is newer; the readiness gate is about the host-side
Docker CLI plugin used by `docker buildx build --load`.

## Operations

### One-job addon runners

Private product addon jobs can use fresh runner containers through the retained
Runner Lane Lifecycle workflow. Production image builds remain GitHub-hosted.
The product owns its container recipe and test commands. Before queuing the
addon job, its workflow uploads `addon-runner-recipe-{run_attempt}` containing
exactly `Dockerfile`, `entrypoint.sh`, and `source-commit` (the run's head SHA).
The job requests `self-hosted` plus `lp-addon-{run_id}-{run_attempt}`; its name
and commands belong to the product. Fork runs, public repositories,
`pull_request_target`, old attempts, and mismatched recipe sources are refused.

The existing outbox worker scans queued addon jobs every minute. It uses the
Runner Lifecycle App's existing repository-scoped Actions read permission to
verify the job and artifact digest, then queues the retained lifecycle workflow
once per job through the existing outbox. Its dispatch source comes from the
existing exact lifecycle authorization rule; no second source binding or grant
is needed. Missing or ambiguous authority remains unavailable. Existing outbox
reconciliation rules apply to dispatches whose provider effect is uncertain.
Dispatch observation matches the original job's unique workflow run title.
The scan verifies job/source identity; only the executor downloads the recipe.

The scan observes outstanding launcher records even after the tenant run finishes.
The existing outbox record projects terminal launcher failure into failed
operations; its stored external URL retains the original run link. The inspection
surface supplies the tenant job/run IDs in routing inputs. It does not silently
register a replacement. Inspect that original run and its cleanup receipt:
after correcting the failure and confirming cleanup, use the existing failed-run
recovery on that same workflow run. An uncertain cleanup must first reconcile
the original receipt through its always-step or `--cleanup-only`. A successful
recovery of that original run clears the outbox failure. The queued tenant job
can still be waiting while its launcher is failed; the operations record is the
failure signal, not a claim that the tenant test ran.
If dispatch failed before any launcher run existed, correct the reported
dispatch failure, then cancel and **Re-run all jobs** on the tenant workflow.
That explicit new workflow attempt has new job identities. Never delete the
original outbox row, change its marker or blindly retry an unknown dispatch.

The lifecycle executor builds the same-attempt recipe, creates one fresh
container with private filesystem and Docker configuration, and supplies the
short-lived registration token on standard input. The recipe must use native
`config.sh --ephemeral`, report its native runner ID and ephemeral flag as its
first stdout JSON line, and start `run.sh`. The token must stay out of process
arguments and listener environment. A successful listener exit alone does not
prove the addon job passed: the executor reads the actual job conclusion.

The container has its own Docker daemon and anonymous data volume, with no host
socket, source, credential, or cache mounts. It is privileged and therefore does
not claim guest or host-administrator isolation. The executor limits its CPU,
memory and processes. Container, registration and image identities are recorded
before subsequent effects. Normal completion, failure and cancellation remove
the original container and its volumes, retire any remaining exact runner
registration, and remove the recipe image. Cleanup failures remain failed,
never inferred absent from a Docker error. The workflow's always-step can
reconcile the saved original receipt with `runner-lane-ephemeral --cleanup-only`;
it does not register another runner. Receipts and redacted runner logs are
retained as workflow artifacts. A host loss can leave cleanup unconfirmed and
requires reconciliation of those original identities.

This implements the container choice recorded in
[infrastructure issue 613](https://github.com/cbusillo/shiny-infra-ops/issues/613#issuecomment-6724167129).
Revisit the executor architecture when outside contributors gain push or PR
rights, production image builds leave hosted GitHub, or tenant count outgrows
the selected host. Host Docker-access housekeeping remains a separate track.
The retained operations lane remains occupied until the addon job and cleanup
finish, so its existing runner count bounds concurrency. The minute-based scan
uses the existing installation's API budget. Observe actual queue waits and
rate-limit evidence during delivery; do not infer spare capacity or expand the
host/lane/App configuration from this recipe.

Use the existing read-only inventory command before adding or controlling runner
lanes:

```bash
uv run launchplane runner-inventory --repository owner/name
```

That command reads GitHub runner metadata only. Host-level observation and future
reconciliation must run through a Launchplane-owned runner lane flow, not through
product workflow edits and not through ad hoc retries after a shared Docker
credential race.

Before increasing a shared host's general lane count, confirm that workflows do
not rely on shared mutable Docker names. Buildx builder names and loaded image
tags should be unique per run or per job so two admitted jobs cannot race on the
same host-local builder state or image tag.

To inspect whether recent workflow jobs look runner-capacity constrained from
GitHub Actions timing evidence, run:

```bash
uv run launchplane runner-queue-wait --repository owner/name
```

The queue-wait command is also read-only. It reads recent workflow runs and their
job metadata, computes `started_at - created_at` only when GitHub exposes both
timestamps, and reports missing or malformed timestamps as `unknown` rather than
as a zero-second wait. By default it also reads current runner inventory so the
JSON summary can show queue-wait evidence next to current lane-capacity status.
Use `--skip-runner-inventory` for fixture-only or permission-limited reads.

To emit baseline observation evidence from a self-hosted runner job, run:

```bash
uv run launchplane runner-baseline-observe \
  --allowed-service-user gha \
  --allowed-home-root /opt/actions-runners \
  --observe-docker-toolchain \
  --docker-toolchain-timeout-seconds 30 \
  --require-docker-toolchain-observation \
  --minimum-docker-buildx-version 0.23.0
```

The observation command is read-only. By default it reads `RUNNER_NAME`,
`RUNNER_TRACKING_ID`, `RUNNER_LABELS`, `USER`, `HOME`, `DOCKER_CONFIG`, and
`LAUNCHPLANE_ISOLATED_DOCKER_CONFIG` from the current job environment, then
evaluates the readiness contract in the same JSON payload. Docker credential
isolation is considered present only when the job exposes matching
`DOCKER_CONFIG` and `LAUNCHPLANE_ISOLATED_DOCKER_CONFIG` values, or when an
admin passes explicit `--docker-config-isolated` evidence. Missing evidence
for labels or Docker isolation still fails closed.

Docker toolchain observation is read-only. `--observe-docker-toolchain` collects
Docker Engine and CLI versions, `docker buildx version`, standard Buildx plugin
paths, Debian/RPM package evidence when available, and `docker buildx inspect`
BuildKit evidence. Admins can also pass those values explicitly with
`--docker-engine-version`, `--docker-cli-version`, `--docker-buildx-version`,
`--docker-buildx-plugin-path`, `--docker-buildx-package`,
`--docker-buildx-source`, and `--buildkit-version` for fixture-driven or
permission-limited checks. Use `--docker-toolchain-timeout-seconds` to raise the
per-command timeout on cold or busy Docker daemons.

For live products, use a non-production runner or a read-only observation path
until the Director explicitly approves host changes. VeriReel production must
not be used as a runner-baseline test surface without explicit Director
permission.

For host provisioning and replacement, use the existing private
[infrastructure runbook](https://github.com/cbusillo/shiny-infra-ops/blob/main/docs/infrastructure/remote-build-host.md).
Do not copy host-level Docker credentials. Before routing a replacement onto a
shared or ops-gate lane, require passing baseline evidence and an accepted hygiene
dry run with the configured retained images. Keep the prior settings recoverable
and the old host available until a scheduled dry run succeeds; rollback restores
that routing and configuration through their supported paths.

The local `runner-control-plan` command evaluates saved inventory and baseline
readiness without contacting GitHub or mutating hosts. Its `--mutate` flag
records intent only. Use the lifecycle executors below for supported registration
and retirement.

## Existing-runner relabel

For authorized runner label changes, use the catalog's
[`gh-owner-admin.py`](https://github.com/cbusillo/codex-skills/blob/main/skills/github/references/cli-reference.md#verified-repository-owner-administration)
using the Director's verified GitHub login: save and review the exact runner's plan,
apply its digest, then independently read it back. Preserve prior custom labels
for undo.

`control_plane.workflows.runner_lane_relabel` remains the existing custom-label
library and typed audit contract.

The engine takes an explicit repository/host/root policy, exact runner name/ID,
reviewed prior custom labels and desired custom labels. It preserves automatic
labels and caller-supplied protected operations labels/names. It reads identity
and label types before planning, requires accepted pre-action audit before
mutation, refreshes the prior state after that audit, attempts one label PUT,
and reads identity and labels back independently. Dry-run never writes labels;
no registration, service, filesystem or host operation is part of this engine.

GitHub offers no conditional label replacement. The prior-state check is
optimistic, not atomic compare-and-swap with an unrelated administrator. A
caller must coordinate external label writers. A job can start after an
idle observation; relabeling never interrupts it. `mutation_reconciliation_required`
means outcome/readback is uncertain: read the runner back and do not retry the
write automatically. For an inverse, review the verified current custom labels
as expected and the saved original custom labels as desired, with fresh identity
checks. Empty custom-label sets are valid inverses.

## Lifecycle Executors

The narrow host adapters for creating and retiring a repo-scoped runner lane
share the manual ops-lane workflow
`.github/workflows/runner-lane-registration.yml`. The filename remains stable so
the existing exact GitHub OIDC workflow identity and service-backed audit grant
do not broaden during the lifecycle expansion. Admins select `register` or
`retire`; both operations share one non-canceling per-repository/per-lane
concurrency group and the runner-host hygiene lock.

The registration CLI entrypoint is:

```bash
uv run launchplane runner-lane-registration-executor \
  --repository owner/name \
  --host-name chris-testing \
  --execution-lane chris-testing-ops-gate \
  --service-user launchplane-runner-hygiene \
  --lane-name product-runner-1 \
  --registration-root /home/launchplane-runner-hygiene/actions-runners \
  --runner-package-url "$ACTIONS_RUNNER_TARBALL_URL" \
  --label self-hosted \
  --label launchplane \
  --label launchplane-managed \
  --audit-record-key runner-lane-registration/2026-06-08/product/dry-run \
  --allowed-repository owner/name \
  --approved-host chris-testing \
  --allowed-registration-root /home/launchplane-runner-hygiene/actions-runners \
  --inventory-file runner-inventory.json
```

The command is dry-run by default. A dry-run writes only local JSON evidence and
does not request a GitHub registration token. When `--mutate` is explicitly set
and the registration plan is ready, the executor performs a create-only
supervised registration: it requests a short-lived GitHub registration token,
prepares a new lane directory below the approved registration root, downloads
the admin-supplied GitHub Actions runner package, runs `config.sh`, starts the
root-owned `launchplane-runner@<lane>.service` unit through the reviewed narrow
privilege boundary, and verifies that GitHub inventory reports exactly one
online lane with the expected labels before writing a `completed` audit. If any
step fails, the executor writes a `failed` audit. The raw registration token must
never be written to the audit record, command output, or JSON result.
The persistent registration executor passes the token as a command-line option,
so its ops lane must treat same-user process inspection on the host as privileged
and keep unrelated workloads off the constrained service user during mutation.

The retirement CLI entrypoint is:

```bash
uv run launchplane runner-lane-retirement-executor \
  --repository owner/name \
  --host-name chris-testing \
  --execution-lane chris-testing-ops-gate \
  --service-user launchplane-runner-hygiene \
  --lane-name product-runner-1 \
  --registration-root /home/launchplane-runner-hygiene/actions-runners \
  --audit-record-key runner-lane-retirement/2026-07-26/product/dry-run \
  --allowed-repository owner/name \
  --approved-host chris-testing \
  --allowed-registration-root /home/launchplane-runner-hygiene/actions-runners \
  --required-label launchplane \
  --required-label launchplane-managed \
  --inventory-file runner-inventory.json
```

Retirement is dry-run by default and mutating CLI use requires service-backed
audit delivery. A ready retirement requires one exact idle GitHub lane with the
managed labels, no active repository workflow runs, no target `Runner.Worker`,
an approved host/root, and an audit key. The mutating executor verifies the
canonical runner directory and its ownership, stops and disables only the exact
root-authorized systemd unit, verifies target processes stopped, then re-reads
repository runs and GitHub inventory before deleting the unchanged runner ID.
It verifies GitHub no longer lists the lane and removes the inactive runner
directory only when `lsof` finds no open files. Any pre-delete failure restores
the supervised service when the GitHub registration remains present. If the
terminal service audit cannot be delivered after mutation, the executor returns
`audit_delivery_pending` and embeds the complete terminal audit in the uploaded
workflow result so the job fails visibly without discarding the outcome record.

All lifecycle roots must be scoped absolute paths rather than bare `/` and
reject explicit parent-directory (`..`)
components. Benign `.` components, duplicate separators, and trailing separators
remain supported when the normalized path stays scoped. Malformed roots fail
model validation before a planned audit is written.

Before creating an absent registration root, registration proves its nearest
existing ancestor is a directory whose exact canonical path matches the approved
lexical path. Symlinked ancestors, including dangling links, are refused before
directory creation can follow them. The resulting root and new runner directory
must be directories, non-symlinks, exactly canonical, owned by the requested
service user, and within the approved root. Existing runner entries, including
dangling symlinks, are refused rather than adopted or overwritten. Valid absent
roots remain supported without privileged ownership changes.

Registration validates the target without filesystem writes before acquiring its
short-lived GitHub token, then repeats that validation before creating directories.
A token-acquisition failure therefore leaves an absent root and lane absent, so
the next dispatch can retry. Registration repeats the shared registration/retirement
identity checks before and after extraction, before configuration, before service
start, and before completed audit publication. It extracts and
configures from a verified physical working directory. A detected path change
fails the operation; these boundary checks are not a guarantee against arbitrary
concurrent filesystem replacement. Failed command evidence redacts the known
registration token before truncating or persisting it. File-backed lifecycle
audits use bounded ASCII filenames below the filesystem component limit, retain
ordinary short-key filename identity, and preserve the original audit key in the
record. PostgreSQL key semantics are unchanged.

Existing-lane adoption, remove/recreate, generic service restarts, and automatic
scaling are not supported by these executors. The runner must be supervised
outside the Actions job process tree.

The manual workflow defaults `registration_root` to `auto`, which resolves to
`LAUNCHPLANE_RUNNER_REGISTRATION_ALLOWED_ROOT` when configured, otherwise to
`$HOME/actions-runners` for the constrained service user. An explicit root must
match that resolved approved root exactly.

The manual workflow calls Launchplane's service-backed
[runner lifecycle App](secrets.md#runner-lifecycle-app-credential) for runner
inventory, registration tokens, active-run reads and retirement. It holds
neither the App key nor an installation token and does not read the old PAT.
The service uses one repository-scoped token per call and revokes it afterward.
Local rehearsal mode still accepts an explicitly supplied fixture token; service
audit mode always uses Launchplane and has no GitHub-token fallback.

Mutating registrations require a `runner_package_url` workflow input
(passed as `--runner-package-url`) that
points to an `actions/runner` release tarball on GitHub, plus a preinstalled
`launchplane-runner@.service` template and a narrow sudo rule or equivalent
root helper for `systemctl enable --now` and `systemctl is-active` on that
template. Durable service-backed audit
persistence is available through
`POST /v1/evidence/runner-lane-registration/audits` under
`runner_lane_registration_audit.write`; descriptor-backed admin routing for
registration planning remains a later slice. Retirement reuses the same route
with `operation: retire`, preserving the exact authorized workflow identity and
persisted audit table while distinguishing lifecycle intent in the typed record.
The workflow still uploads the JSON artifact so admins can inspect the exact
plan/result packet.

An audit key belongs to its first persisted, normalized lifecycle request,
including its operation and all request inputs. PostgreSQL and local filesystem
storage reject a different request under that key, even when its HTTP
`Idempotency-Key` differs or is absent. The evidence route returns 409 for this
conflict without replacing the original audit or recording an accepted upload.
Use a fresh audit key for a different lifecycle request. The original request
can still advance from planned to terminal evidence, update its observations
and message, or deliver a retained terminal packet even if no planned packet
was persisted. Audit recovery posts that original evidence; it never reruns a
provider mutation. This binding preserves request identity, not authenticated
actor provenance or a general terminal-status precedence policy.

The current host bootstrap uses a root-owned template that runs the packaged
service wrapper rather than the interactive runner script:

```ini
[Unit]
Description=Launchplane GitHub Actions Runner (%i)
After=network-online.target
Wants=network-online.target

[Service]
User=launchplane-runner-hygiene
WorkingDirectory=/home/launchplane-runner-hygiene/actions-runners/%i
ExecStart=/home/launchplane-runner-hygiene/actions-runners/%i/bin/runsvc.sh
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`bin/runsvc.sh` is part of the official GitHub Actions runner tarball and traps
service termination for the runner listener. `run.sh` remains the interactive
script and must not be the long-lived systemd entrypoint for this supervised
path. Validate the template with `systemd-analyze verify` before enabling any
lane service.

The sudo boundary should validate the lane-shaped unit argument rather than use
a broad shell glob. On sudo versions that support command-argument regular
expressions, the installed shape is:

<!-- markdownlint-disable MD013 -->

```sudoers
Cmnd_Alias LAUNCHPLANE_RUNNER_REG_ENABLE = /usr/bin/systemctl ^enable --now launchplane-runner@[a-z0-9][a-z0-9._-]{0,127}\.service$
Cmnd_Alias LAUNCHPLANE_RUNNER_REG_ACTIVE = /usr/bin/systemctl ^is-active --quiet launchplane-runner@[a-z0-9][a-z0-9._-]{0,127}\.service$
launchplane-runner-hygiene ALL=(root) NOPASSWD: LAUNCHPLANE_RUNNER_REG_ENABLE, LAUNCHPLANE_RUNNER_REG_ACTIVE
```

<!-- markdownlint-enable MD013 -->

Validate sudoers changes with `visudo -cf`. If a host cannot support sudoers
argument regex, use a tiny root-owned helper that validates the lane name before
calling `systemctl`; do not broaden the rule to arbitrary `systemctl`, shell,
file-write, or generic restart authority.

Retirement uses the reviewed root-owned
`/usr/local/sbin/launchplane-runner-service-retire` helper. Install the checked-in
`scripts/runner-lane-service-retire.sh` at that path with root ownership and mode
`0755`. Its root-owned mode-`0600`
`/etc/launchplane/runner-lane-retirement-targets` file contains tab-separated
`repository`, `lane`, `registration_root`, and `service_user` bindings. The
helper rejects symlinked or non-root-owned configuration, verifies the invoking
sudo user, canonical root, systemd unit user, and the exact `ExecStart`
executable `<registration_root>/<lane>/bin/runsvc.sh` (not a substring of its
path or arguments), then stops
and disables only that exact unit. A sudoers entry may grant only this helper;
the root-owned target file remains the runtime authority for each approved
retirement:

```sudoers
launchplane-runner-hygiene ALL=(root) NOPASSWD: NOSETENV: /usr/local/sbin/launchplane-runner-service-retire *
```

Validate the helper, target file, and candidate sudoers policy before the
mutating lifecycle dispatch. Remove a completed one-time target binding after
post-retirement verification so stale host authority does not accumulate.
