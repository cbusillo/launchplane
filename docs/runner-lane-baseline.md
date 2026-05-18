---
title: Runner Lane Baseline
---

Launchplane owns the safety contract for self-hosted GitHub Actions runner lanes
used by product publish, preview, and promotion workflows. Product repositories
may request work, but runner host readiness and Docker credential hygiene belong
to Launchplane rather than repo-by-repo workflow workarounds.

## Baseline Policy

Every Launchplane-controlled runner lane must satisfy the baseline before it is
treated as ready for shared product automation:

- The lane is discoverable through GitHub's self-hosted runner inventory.
- The lane carries the required identity labels, including `self-hosted` and
  `launchplane` unless a narrower policy overrides them.
- Docker registry credentials are isolated per job. The readiness contract fails
  closed unless there is positive evidence that jobs do not share a mutable
  host-level Docker config.
- Optional host checks may constrain the service user and home-directory roots
  used by managed runner lanes.

The baseline does not grant permission to mutate product or provider state. It
only says whether a runner lane is safe enough to receive work that other
Launchplane policies already authorized.

## Readiness Contract

The typed contract in `control_plane.contracts.runner_lane_baseline` separates
policy from observation:

- `RunnerLaneBaselinePolicy` records required labels, whether Docker credential
  isolation is mandatory, and optional service-user or home-root constraints.
- `RunnerLaneBaselineObservation` records observed lane facts from a host or
  runner bootstrap check.
- `evaluate_runner_lane_baseline(...)` returns `RunnerLaneBaselineReadiness`
  with a fail-closed summary and structured violations.

No observation means not ready. Unknown Docker credential isolation means not
ready. Missing labels or configured host guardrails also make the lane not ready.
Home-directory checks canonicalize observed and allowed paths before comparing
roots, so traversal segments such as `..` cannot satisfy a configured root by
string prefix alone.

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
  --allowed-home-root /opt/actions-runners
```

The observation command is read-only. By default it reads `RUNNER_NAME`,
`RUNNER_TRACKING_ID`, `RUNNER_LABELS`, `USER`, `HOME`, `DOCKER_CONFIG`, and
`LAUNCHPLANE_ISOLATED_DOCKER_CONFIG` from the current job environment, then
evaluates the readiness contract in the same JSON payload. Docker credential
isolation is considered present only when the job exposes matching
`DOCKER_CONFIG` and `LAUNCHPLANE_ISOLATED_DOCKER_CONFIG` values, or when an
operator passes explicit `--docker-config-isolated` evidence. Missing evidence
for labels or Docker isolation still fails closed.

For live products, use a non-production runner or a read-only observation path
until the operator explicitly approves host changes. VeriReel production must
not be used as a runner-baseline test surface without explicit operator
permission.

## Control Planning

Runner control starts with a dry-run plan, not a host mutation. The typed
contract in `control_plane.contracts.runner_lane_control` requires all of these
before a plan can become ready:

- the repository is explicitly opted into runner control
- the requested action is enabled by policy
- mutate mode is explicitly requested
- the lane baseline is ready
- existing lanes carry the managed label, defaulting to `launchplane-managed`
- busy drain, restart, or removal requests include explicit drain confirmation

The planner does not create, drain, restart, or remove a runner. It only returns
structured blockers and next steps so a later host adapter can be reviewed
against a stable policy boundary.
