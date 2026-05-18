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

For live products, use a non-production runner or a read-only observation path
until the operator explicitly approves host changes. VeriReel production must
not be used as a runner-baseline test surface without explicit operator
permission.
