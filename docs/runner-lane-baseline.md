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
a stale Buildx CLI plugin is not ready. The `0.13.1+ds1` Debian-packaged Buildx
plugin observed on `chris-testing` fails a `0.23.0` minimum even when the
workflow's BuildKit builder container is newer; the readiness gate is about the
host-side Docker CLI plugin used by `docker buildx build --load`.

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
`capability_ready: false`. Policy blockers such as an unapproved host, unsafe
runner directory, missing managed label, duplicate lane name, repository
mismatch, or failed baseline readiness produce `decision: blocked` with
`policy_ready: false` and must be resolved before any host mutation can be
considered.

## Registration Executor

The first narrow host adapter for creating a repo-scoped runner lane is the
manual ops-lane workflow `.github/workflows/runner-lane-registration.yml` and its
CLI entrypoint:

```bash
uv run launchplane work-graph runner-lane-registration-executor \
  --repository owner/name \
  --host-name chris-testing \
  --execution-lane chris-testing-ops-gate \
  --service-user launchplane-runner-hygiene \
  --lane-name product-runner-1 \
  --registration-root /home/launchplane-runner-hygiene/actions-runners \
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
does not request a GitHub registration token. `--mutate` records apply intent
and writes a failed audit explaining that runner registration requires a
supervised host maintainer. The earlier proof path briefly started `run.sh` from
inside the GitHub Actions job, but that can produce transient online evidence
and leave the runner offline after job cleanup. That shortcut is disabled. Until
the supervised maintainer exists, mutate runs do not request a GitHub
registration token. The token itself must never be written to the audit record,
command output, or JSON result.

The manual workflow defaults `registration_root` to `auto`, which resolves to
`$HOME/actions-runners` for the constrained service user. Operators may pass an
absolute root explicitly, but the service user must already be able to create
lane directories below that root.

The manual workflow requires the repository secret
`LAUNCHPLANE_RUNNER_REGISTRATION_GITHUB_TOKEN` for cross-repository runner
inventory and registration-token requests. The default `GITHUB_TOKEN` from the
Launchplane workflow is not sufficient authority for product repository runner
administration.

This executor is the proving-ground adapter for #1231. Durable service-backed
audit persistence is available through
`POST /v1/evidence/runner-lane-registration/audits` under
`runner_lane_registration_audit.write`; descriptor-backed operator routing for
registration planning remains a later slice. The workflow still uploads the JSON
artifact so operators can inspect the exact plan/result packet for cm-website or
any other product proof.

## Supervised Runner Maintainer Plan

The durable runner maintainer must replace the disabled apply shortcut before a
product repository can rely on a Launchplane-managed runner lane. The maintainer
should reconcile desired runner state rather than simply start `run.sh`:

1. Read GitHub runner inventory and local service state for the requested lane.
2. If registration is required, request a short-lived GitHub registration token
   and run `config.sh` under an approved registration root.
3. Install or update a persistent supervisor outside the GitHub Actions job
   process tree. Prefer a root-owned systemd unit such as
   `launchplane-runner@<lane>.service` with `User=<service_user>`, an approved
   `WorkingDirectory`, `ExecStart=<runner-dir>/run.sh`, and `Restart=always`.
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
