---
title: Operations
---

## Command Groups

Use `uv run control-plane --help` for the complete CLI surface. The current
top-level groups are:

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
- Promotion execution validates a stored passing backup-gate record for the
  destination environment before ship execution begins.
- Deploy execution prefers immutable artifact image references by syncing
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` to Dokploy when a stored artifact
  manifest is available.
- Direct `ship` and `promote` execution fail closed when the referenced
  artifact manifest is missing.
- Successful waited `ship` executions for `dev`, `testing`, and `prod` mint
  current release tuple records from stored artifact manifests.
- `promote execute` requires the source lane's current release tuple to match
  the requested artifact, then promotes that exact tuple to the destination
  lane after the deploy passes.
- Current environment inventory is refreshed from successful waited `ship` and
  `promote` executions.
- Operator read models compose inventory, deployment, promotion, and
  backup-gate records instead of requiring operators to inspect raw JSON first.

## Dokploy Contracts

- Tracked Dokploy route definitions live in `config/dokploy.toml` by default.
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
- `ship execute` and `promote execute` can take an explicit `--env-file` overlay
  for the compose post-deploy update path.
- The post-deploy overlay supports only `ODOO_DB_NAME`, `ODOO_FILESTORE_PATH`,
  and `ODOO_DATA_WORKFLOW_LOCK_FILE`.

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

The tracked default catalog at `config/release-tuples.toml` records the current
legacy `odoo-ai` deploy-branch heads for `dev`, `testing`, and `prod`. Treat it
as active runtime baseline evidence until split-repo artifact tuple records are
materialized into the baseline catalog. Runtime `ship` and `promote` flows write
current tuple records under the selected state directory rather than silently
rewriting this tracked file.

Use `release-tuples export-catalog --state-dir <state>` to render those minted
state records as catalog TOML when an operator is ready to review and
materialize them.

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

## GitHub Boundary

GitHub is the engineering workflow surface: issues, branches, pull requests,
labels, checks, PR comments, releases, and CI execution. `odoo-control-plane`
owns the durable operational truth behind those workflows: release tuples,
artifacts, previews, deployments, promotions, backup gates, and inventory.
