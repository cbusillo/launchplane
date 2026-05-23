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
  through an approved Launchplane-owned host adapter. This phase is not active
  until a disposable host or explicit operator target exists.

The current Launchplane surface implements report-only evidence and local
apply-boundary planning. It still does not execute host mutations.

## Report Contract

The typed contract in `control_plane.contracts.runner_host_hygiene` separates
policy from observation:

- `RunnerHostHygienePolicy` records minimum free disk, optional Docker
  reclaimable and runner work-directory budgets, required warm builders, and
  whether orphan BuildKit artifacts are tolerated.
- `RunnerHostHygieneObservation` records facts collected by an approved
  read-only probe, including host name, free disk, Docker reclaimable bytes,
  runner work-directory bytes, warm builders, and orphan BuildKit counts.
- `evaluate_runner_host_hygiene(...)` returns a structured report with
  `healthy` or `attention` status, findings, and non-mutating next steps.

The report is intentionally conservative. Missing required warm builders, low
free disk, Docker reclaimable bytes over budget, runner work-directory bytes over
budget, and orphan BuildKit artifacts all produce `attention` unless policy
explicitly permits that condition.

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
the JSON payload. The current CLI still only prints the planned record; a future
approved executor or service route must write planned, completed, or failed
records through Launchplane-owned storage.

## Adapter Boundary Planning

Before a real host mutation adapter is implemented, operators can review the
privileged execution boundary against a ready apply plan:

```bash
uv run launchplane work-graph runner-host-hygiene-adapter-boundary-plan \
  --adapter-type github_actions_runner \
  --host-name chris-testing \
  --execution-lane chris-testing-ops-gate \
  --service-user gha \
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
  --allowed-adapter-type github_actions_runner \
  --allowed-execution-lane chris-testing-ops-gate \
  --allowed-service-user gha \
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
executor.

The privileged scope must match the planned action exactly:

- `prune_docker_cache` requires `docker_cache`.
- `prune_runner_workdir` requires `runner_workdir`.
- `restart_runner_service` requires `runner_service`.

Extra privileged scopes block the boundary plan so a Docker-cache prune cannot
quietly grow service-restart or work-directory powers.

## Future Apply Requirements

Before Launchplane grows a host mutation adapter, the apply design must name:

- the disposable or explicitly approved host target
- the runner lane and repository scope the host adapter is allowed to affect
- the evidence snapshot captured before and after cleanup
- the warm builder retention budget
- the rollback or stop condition when cleanup cannot be completed safely
- the audit record written back to Launchplane-owned storage

Until those are present, host hygiene work remains report-only or dry-run
planning only.
