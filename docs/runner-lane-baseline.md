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

Use the existing read-only inventory command before adding or controlling runner
lanes:

```bash
uv run launchplane work-graph runner-inventory --repository owner/name
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
uv run launchplane work-graph runner-queue-wait --repository owner/name
```

The queue-wait command is also read-only. It reads recent workflow runs and their
job metadata, computes `started_at - created_at` only when GitHub exposes both
timestamps, and reports missing or malformed timestamps as `unknown` rather than
as a zero-second wait. By default it also reads current runner inventory so the
JSON summary can show queue-wait evidence next to current lane-capacity status.
Use `--skip-runner-inventory` for fixture-only or permission-limited reads.

To emit baseline observation evidence from a self-hosted runner job, run:

```bash
uv run launchplane work-graph runner-baseline-observe \
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
operator passes explicit `--docker-config-isolated` evidence. Missing evidence
for labels or Docker isolation still fails closed.

Docker toolchain observation is read-only. `--observe-docker-toolchain` collects
Docker Engine and CLI versions, `docker buildx version`, standard Buildx plugin
paths, Debian/RPM package evidence when available, and `docker buildx inspect`
BuildKit evidence. Operators can also pass those values explicitly with
`--docker-engine-version`, `--docker-cli-version`, `--docker-buildx-version`,
`--docker-buildx-plugin-path`, `--docker-buildx-package`,
`--docker-buildx-source`, and `--buildkit-version` for fixture-driven or
permission-limited checks. Use `--docker-toolchain-timeout-seconds` to raise the
per-command timeout on cold or busy Docker daemons.

For live products, use a non-production runner or a read-only observation path
until the operator explicitly approves host changes. VeriReel production must
not be used as a runner-baseline test surface without explicit operator
permission.

## Control Planning

Runner control starts with a dry-run plan, not a host mutation. The typed
contract in `control_plane.contracts.runner_lane_control` requires all of these
before a plan can become ready:

- the repository allow-list is non-empty and explicitly opts the repository into
  runner control
- the requested action is enabled by policy
- mutate mode is explicitly requested
- the lane baseline is ready
- existing lanes carry the managed label, defaulting to `launchplane-managed`
- busy drain, restart, or removal requests include explicit drain confirmation

The planner does not create, drain, restart, or remove a runner. It only returns
structured blockers and next steps so a later host adapter can be reviewed
against a stable policy boundary. Repository values are canonicalized to
`owner/name` before comparing policy, request, and inventory records, and a
matching lane name must resolve to exactly one inventory record before the plan
can target it.

To build the first read-only control plan from saved inventory and baseline
evidence, run:

```bash
uv run launchplane work-graph runner-control-plan \
  --action restart \
  --repository owner/name \
  --lane-name launchplane-runner-1 \
  --allow-restart \
  --allowed-repository owner/name \
  --inventory-file runner-inventory.json \
  --baseline-readiness-file runner-baseline.json
```

The command does not contact GitHub and does not mutate hosts. It only evaluates
the typed runner-control policy and request against the supplied inventory and
baseline readiness JSON. `--mutate` records the operator's explicit mutation
intent in the request so the planner can tell whether a future host adapter would
be allowed to proceed, but this CLI command itself remains a dry-run surface.

Runner maintainer planning is also read-only. The typed contract in
`control_plane.contracts.runner_lane_maintainer` captures the desired durable
runner state for a repository, host, lane, registration root, service user,
systemd unit name, labels, and runner-version policy. It compares that desired
state against saved GitHub runner inventory and saved baseline readiness, then
returns the future maintainer decision without contacting GitHub or the host.

To build the first desired-state plan for a product lane, run:

```bash
uv run launchplane work-graph runner-maintainer-plan \
  --repository owner/name \
  --host-name chris-testing \
  --lane-name product-runner-1 \
  --runner-directory \
    /home/launchplane-runner-hygiene/actions-runners/product-runner-1 \
  --service-user launchplane-runner-hygiene \
  --systemd-unit-name launchplane-runner@product-runner-1.service \
  --label self-hosted \
  --label launchplane \
  --label launchplane-managed \
  --allowed-repository owner/name \
  --approved-host chris-testing \
  --allowed-registration-root /home/launchplane-runner-hygiene/actions-runners \
  --allowed-service-user launchplane-runner-hygiene \
  --inventory-file runner-inventory.json \
  --baseline-readiness-file runner-baseline.json
```

The plan decision can be `recommend_create`, `recommend_verify_adoption`,
`recommend_remove_recreate`, or `blocked`. A recommendation names the durable
action a future maintainer should take; it does not authorize this command to
perform that action. Until the supervised host maintainer exists,
otherwise-valid create, adoption-verification, and remove/recreate plans remain
`status: blocked` with the typed capability blocker
`supervised_maintainer_required`. These packets set `policy_ready: true` and
`capability_ready: false` and expose no mutation capability.

Baseline readiness has different timing for absent-lane planning and existing
lane remediation. An absent lane may remain policy-ready for
`recommend_create` despite a non-ready packet only when that packet has zero
observed lanes, zero compliant lanes, and no violations. A non-ready packet
with existing fleet observations or violations blocks even an absent-lane
recommendation. By default, any matching existing lane must have a ready
aggregate baseline packet before the planner can recommend adoption
verification or remove/recreate; otherwise it returns `decision: blocked` with
`baseline_not_ready`. The operator may explicitly disable this coarse
pre-action gate with
`--allow-missing-baseline-readiness`, but the recommendation remains blocked by
`supervised_maintainer_required` and does not become completion evidence. The
current readiness packet is fleet-aggregate evidence and does not prove that
the desired lane itself was observed. A future supervised executor must bind
post-action baseline evidence to the exact lane before it can claim completion
or product-job admission; this read-only planner does not implement that
completion gate.

Other policy blockers such as an unapproved host, unsafe runner directory,
missing managed label, unsupported observed lane status, duplicate lane name,
or repository mismatch also produce `decision: blocked` with
`policy_ready: false` and must be resolved before any host mutation can be
considered. The maintainer planner's desired runner directories and allowed
registration roots reject explicit `..` path components rather than resolving
them into a different path. Registration and retirement remain separate
lifecycle contracts and are not covered by this planner validation.

## Lifecycle Executors

The narrow host adapters for creating and retiring a repo-scoped runner lane
share the manual ops-lane workflow
`.github/workflows/runner-lane-registration.yml`. The filename remains stable so
the existing exact GitHub OIDC workflow identity and service-backed audit grant
do not broaden during the lifecycle expansion. Operators select `register` or
`retire`; both operations share one non-canceling per-repository/per-lane
concurrency group and the runner-host hygiene lock.

The registration CLI entrypoint is:

```bash
uv run launchplane work-graph runner-lane-registration-executor \
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
the operator-supplied GitHub Actions runner package, runs `config.sh`, starts the
root-owned `launchplane-runner@<lane>.service` unit through the reviewed narrow
privilege boundary, and verifies that GitHub inventory reports exactly one
online lane with the expected labels before writing a `completed` audit. If any
step fails, the executor writes a `failed` audit. The raw registration token must
never be written to the audit record, command output, or JSON result.
GitHub's runner `config.sh` still requires the token as a command-line option,
so the ops lane must treat same-user process inspection on the host as privileged
and keep unrelated workloads off the constrained service user during mutation.

The retirement CLI entrypoint is:

```bash
uv run launchplane work-graph runner-lane-retirement-executor \
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
canonical runner directory and owner, stops and disables only the exact
root-authorized systemd unit, verifies target processes stopped, then re-reads
repository runs and GitHub inventory before deleting the unchanged runner ID.
It verifies GitHub no longer lists the lane and removes the inactive runner
directory only when `lsof` finds no open files. Any pre-delete failure restores
the supervised service when the GitHub registration remains present. If the
terminal service audit cannot be delivered after mutation, the executor returns
`audit_delivery_pending` and embeds the complete terminal audit in the uploaded
workflow result so the job fails visibly without discarding the outcome record.

Existing-lane adoption, remove/recreate, generic service restarts, and automatic
scaling remain future maintainer capabilities. The earlier proof path briefly
started `run.sh` from inside the GitHub Actions job, but that can produce
transient online evidence and leave the runner offline after job cleanup. That
shortcut remains disabled; the runner must be supervised outside the Actions
job process tree.

The manual workflow defaults `registration_root` to `auto`, which resolves to
`LAUNCHPLANE_RUNNER_REGISTRATION_ALLOWED_ROOT` when configured, otherwise to
`$HOME/actions-runners` for the constrained service user. An explicit root must
match that resolved approved root exactly.

The manual workflow requires the repository secret
`LAUNCHPLANE_RUNNER_REGISTRATION_GITHUB_TOKEN` for cross-repository runner
inventory and registration-token requests. The default `GITHUB_TOKEN` from the
Launchplane workflow is not sufficient authority for product repository runner
administration.

This executor is the proving-ground adapter for #1231. Mutating runs require an
`ACTIONS_RUNNER_TARBALL_URL` value that points to an `actions/runner` release
tarball on GitHub, plus a preinstalled `launchplane-runner@.service` template
and a narrow sudo rule or equivalent root helper for `systemctl enable --now`
and `systemctl is-active` on that template. Durable service-backed audit
persistence is available through
`POST /v1/evidence/runner-lane-registration/audits` under
`runner_lane_registration_audit.write`; descriptor-backed operator routing for
registration planning remains a later slice. Retirement reuses the same route
with `operation: retire`, preserving the exact authorized workflow identity and
persisted audit table while distinguishing lifecycle intent in the typed record.
The workflow still uploads the JSON artifact so operators can inspect the exact
plan/result packet.

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
sudo user, canonical root, systemd unit user, and unit `ExecStart`, then stops
and disables only that exact unit. A sudoers entry may grant only this helper;
the root-owned target file remains the runtime authority for each approved
retirement:

```sudoers
launchplane-runner-hygiene ALL=(root) NOPASSWD: NOSETENV: /usr/local/sbin/launchplane-runner-service-retire *
```

Validate the helper, target file, and candidate sudoers policy before the
mutating lifecycle dispatch. Remove a completed one-time target binding after
post-retirement verification so stale host authority does not accumulate.

## Supervised Runner Maintainer Plan

The durable runner maintainer grows from the create-only executor into full
desired-state reconciliation. It must keep reconciling runner state rather than
starting a transient runner process:

1. Read GitHub runner inventory and local service state for the requested lane.
2. If registration is required, request a short-lived GitHub registration token
   and run `config.sh` under an approved registration root.
3. Install or update a persistent supervisor outside the GitHub Actions job
   process tree. Prefer a root-owned systemd unit such as
   `launchplane-runner@<lane>.service` with `User=<service_user>`, an approved
   `WorkingDirectory`, `ExecStart=<runner-dir>/bin/runsvc.sh`, and
   `Restart=always`.
4. Use a small validated root helper or narrow sudo rule for the privileged
   systemd verbs. Do not grant arbitrary `systemctl`, file-write, or shell
   authority.
5. Mark the audit completed only after the service is enabled and active, the
   process runs as the expected service user, GitHub inventory shows the lane
   online with expected labels, and baseline readiness passes.

The offline `cm-website-chris-testing` proof runner was removed from GitHub
inventory after demonstrating why transient process supervision is not
acceptable. Future cm-website proof runs should first produce a maintainer
dry-run that decides whether to adopt, remove, or recreate any existing runner
record, then apply only through the supervised maintainer path.
