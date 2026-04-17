---
title: Operations
---

## Command Groups

Use `uv run control-plane --help` for the complete CLI surface. The current
top-level groups are:

Today this CLI is the Harbor operator surface for the Odoo system owned by
this repo. It owns stable-lane deploy and promotion workflows for `testing`
and `prod`, plus Harbor preview records and read models for PR review flows.

- `artifacts`: write, ingest, and inspect artifact manifests.
- `backup-gates`: write and inspect backup-gate records.
- `deployments`: inspect deployment records.
- `environments`: resolve runtime environment contracts.
- `harbor-previews`: inspect, mutate, render, ingest, and replay Harbor preview
  state.
- `inventory`: inspect current environment inventory.
- `promote`: record, resolve, and execute artifact-backed promotions.
- `promotions`: write and inspect promotion records.
- `release-tuples`: inspect state-backed tuple records and explicitly export a
  TOML catalog from minted state.
- `ship`: plan, resolve, and execute artifact-backed deploy requests.

## Core Rules

- Promotions and deploys reference explicit artifact identifiers.
- Missing control-plane config is a hard error, not a silent fallback.
- Operator-local runtime records belong under `state/` or another explicit
  state directory outside git.
- Artifact manifests handed off from build/export steps are persisted here
  before later workflows depend on them.
- The normal split-repo build/export handoff comes from `odoo-devkit`
  `platform runtime publish`, which writes a control-plane-compatible artifact
  manifest JSON file after it stages tenant/shared addon sources, pushes the
  image, and resolves the pushed digest.
- Promotion execution validates a stored passing backup-gate record for the
  destination environment before ship execution begins.
- Deploy execution prefers immutable artifact image references by syncing
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` to Dokploy when a stored artifact
  manifest is available.
- Direct `ship` and `promote` execution fail closed when the referenced
  artifact manifest is missing.
- Direct artifact-backed execution also fails closed when the Dokploy target
  still points at a legacy monorepo source or carries mutable addon repository
  refs instead of exact git SHAs.
- Successful waited `ship` executions for `testing` and `prod` mint current
  release tuple records from stored artifact manifests.
- `promote execute` requires the source lane's current release tuple to match
  the requested artifact, then promotes that exact tuple to the destination
  lane after the deploy passes.
- Current environment inventory is refreshed from successful waited `ship` and
  `promote` executions.
- The tracked Dokploy route catalog is only for stable remote lanes. If a pull
  request needs runtime state, Harbor models that through preview records and
  preview generations instead of adding another long-lived route.
- Operator read models compose inventory, deployment, promotion, and
  backup-gate records instead of requiring operators to inspect raw JSON first.

## Dokploy Contracts

- Tracked Dokploy route definitions live in `config/dokploy.toml` by default.
- Tracked route definitions are expected to be stable remote lanes only:
  `testing` and `prod`.
- Live Dokploy `target_id` values come from untracked
  `config/dokploy-targets.toml` by default.
- Set `ODOO_CONTROL_PLANE_DOKPLOY_SOURCE_FILE` to use an alternate route
  catalog.
- Set `ODOO_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE` to use an alternate local
  target-id catalog.
- Dokploy source loading fails closed when target ids are missing, duplicate
  routes are present, or the local target-id catalog contains routes not found
  in the tracked source catalog.

## Runtime Environment Contracts

- `environments resolve` reads the control-plane-owned runtime environment
  contract for a context and instance.
- `environments show-live-target` reads the live Dokploy target payload for a
  tracked route and reports whether the target is ready for artifact-backed
  split-repo execution.
- `environments sync-live-target --apply` pushes the tracked Dokploy source and
  tracked env overlay for a route into the live target before re-reading the
  artifact-readiness summary.
- `ship execute` and `promote execute` can take an explicit `--env-file` overlay
  for the compose post-deploy update path.
- The post-deploy overlay supports only `ODOO_DB_NAME`, `ODOO_FILESTORE_PATH`,
  and `ODOO_DATA_WORKFLOW_LOCK_FILE`.
- When multiple healthcheck URLs are resolved for a lane, Harbor treats them as
  alternate verification surfaces and accepts the first `2xx` response instead
  of requiring every URL to succeed.

Artifact handoff example:

```bash
uv --directory ../odoo-devkit run platform runtime publish \
  --manifest ./workspace.toml \
  --instance testing \
  --image-repository ghcr.io/example/odoo-opw \
  --image-tag opw-20260416-deadbeef \
  --output-file /tmp/opw-artifact.json
uv run control-plane artifacts write \
  --state-dir ./state \
  --input-file /tmp/opw-artifact.json
```

## Harbor Preview Operations

Harbor commands operate on durable preview, generation, and enablement records.
The current command group supports:

- inventory and detail reads: `list`, `show`, `history`, `show-tenant`
- static rendering: `render-status-page`, `render-index-page`,
  `render-policy-page`, `render-site`
- direct record writes: `write-preview`, `write-generation`,
  `write-enablement`
- lifecycle transitions: `request-generation`, `mark-generation-ready`,
  `mark-generation-failed`, `destroy-preview`
- PR/webhook ingest: `ingest-pr-event`, `ingest-github-webhook`
- captured delivery replay: `replay-github-webhook`,
  `build-github-webhook-replay-envelope`

`show-tenant`, `render-index-page`, and `render-site` accept
`--release-tuples-file` when a cockpit or local render needs an explicit tuple
catalog without relying on process-wide environment setup.

The tracked default catalog at `config/release-tuples.toml` now records the
current split-repo artifact-backed baseline for CM and OPW stable lanes. Pull
requests flow through Harbor preview records instead of a tracked long-lived
`dev` tuple lane. Runtime `ship` and `promote` flows continue to write current
tuple records under the selected state directory rather than silently rewriting
this tracked file.

Use `release-tuples export-catalog --state-dir <state>` to render those minted
state records as catalog TOML when an operator is ready to review and
materialize a new tracked baseline.

GitHub PR feedback uses one Harbor-owned marker comment per PR. The comment is
a review surface over durable Harbor records: preview URL/state, manifest and
baseline tuple, source inputs, artifact identity when present, health status,
next action, and apply outcome.

Harbor treats tenant PRs as preview anchors in the current workspace:
`tenant-opw -> opw` and `tenant-cm -> cm`. `shared-addons` is companion-only,
and infra/tooling repos are not preview anchors.

Preview enablement records retain the anchor PR head SHA plus any resolved
companion PR head SHA snapshots from ingest. Tenant renders use those stored
snapshots for preview request recipes and keep unresolved companion requests
blocked instead of guessing source inputs.

## Harbor Boundary

- GitHub remains the engineering workflow surface: issues, branches, pull
  requests, labels, checks, PR comments, releases, and CI execution.
- `odoo-control-plane` owns the durable operational truth behind those
  workflows: artifacts, release tuples, previews, deployments, promotions,
  backup gates, and inventory.
- Harbor is the operator surface inside this repo today, not a separate
  general-purpose repo boundary.
- Broader reusable Harbor product direction stays in saved plans until a
  generic contract exists in code and operator surface.
