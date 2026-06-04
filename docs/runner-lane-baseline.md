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
