---
title: Operations
---

## Command Groups

Use `uv run control-plane --help` for the complete CLI surface. The current
top-level groups are:

Today this CLI is the local Harbor operator surface for the Odoo system owned
by this repo. It owns stable-lane deploy and promotion workflows for
`testing` and `prod`, plus Harbor preview records and read models for PR
review flows. It should not be treated as the final cross-product ingress
boundary for Harbor.

- `artifacts`: write, ingest, and inspect artifact manifests.
- `backup-gates`: write and inspect backup-gate records.
- `deployments`: write and inspect deployment records.
- `environments`: resolve runtime environment contracts.
- `harbor-previews`: inspect, mutate, render, ingest, and replay Harbor preview
  state.
- `inventory`: inspect current environment inventory.
- `promote`: record, resolve, and execute artifact-backed promotions.
- `promotions`: write and inspect promotion records.
- `release-tuples`: inspect state-backed tuple records and explicitly export a
  TOML catalog from minted state.
- `service`: run the first local Harbor HTTP ingress slice.
- `ship`: plan, resolve, and execute artifact-backed deploy requests.

`deployments write`, `promotions write`, `inventory write-from-deployment`,
`inventory write-from-promotion`, and `release-tuples write-from-promotion`
are the current small evidence-ingest surfaces that let Harbor accept
externally-produced deployment and promotion facts without claiming it
executed that product's runtime action itself.

Those commands are current implementation scaffolding. The target Harbor
boundary is a long-running service with authenticated HTTP ingress, where the
CLI becomes a client of Harbor's stable API contract instead of defining that
contract itself.

## Target Harbor Ingress

The target communication model is:

- Harbor runs as a long-running service behind a stable host such as
  `harbor.shinycomputers.com`.
- Product workflows communicate with Harbor over authenticated HTTP.
- GitHub Actions OIDC is the default machine-to-machine trust boundary.
- Harbor authorizes requests from GitHub-issued identity claims such as repo,
  workflow, ref, environment, and event context.
- Typed evidence payloads are the stable contract; CLI commands are temporary
  adapters while the service boundary is being built.

Harbor should eventually expose API ingress for at least:

- deployment evidence
- promotion evidence
- inventory refresh triggers or derived writes
- preview generation evidence
- preview destroyed evidence
- driver-triggered runtime actions where Harbor owns execution

The first explicit version of that boundary, including the OIDC claim mapping
and endpoint list, lives in [`service-boundary.md`](service-boundary.md).

The first implemented service command is:

```bash
uv run control-plane service serve \
  --state-dir ./state \
  --policy-file ./config/harbor-authz.toml
```

Start from `config/harbor-authz.toml.example` when creating the first local
policy file.

Current implementation scope:

- `GET /v1/health`
- `POST /v1/evidence/deployments`
- `POST /v1/evidence/previews/generations`
- `POST /v1/evidence/previews/destroyed`

The service currently uses a static authz policy file and GitHub OIDC bearer
tokens. Additional evidence routes should land against the same authn/authz
boundary rather than creating separate ad hoc ingress patterns.

## Core Rules

- Promotions and deploys reference explicit artifact identifiers.
- Missing control-plane config is a hard error, not a silent fallback.
- Operator-local runtime records belong under `state/` or another explicit
  state directory outside git.
- Artifact manifests handed off from build/export steps are persisted here
  before later workflows depend on them.
- The normal split-repo build/export handoff comes from `odoo-devkit`
  `platform runtime publish`, which writes a control-plane-compatible artifact
  manifest JSON file after it stages tenant/shared source inputs, pushes the
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
- Externally produced promotion evidence can also refresh current inventory
  when the stored promotion record carries explicit `deployment_record_id`
  linkage to the deployment record that established the promoted state.
- The same promotion evidence can also mint the destination stable-lane tuple
  when Harbor already has the source tuple state for the promoted-from lane.
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
- external evidence ingest: `write-from-generation`, `write-destroyed`
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

`harbor-previews write-from-generation` and `harbor-previews write-destroyed`
are the current local preview-evidence ingest adapters for products whose
preview runtime already exists outside Harbor. They mirror the payload shape
Harbor should later accept through its service ingress. When the preview
request carries an explicit `canonical_url`, Harbor can store the live preview
route, generation status, and cleanup outcome directly from external workflow
evidence without requiring a Harbor-managed preview base-url contract.

### VeriReel Preview Evidence Handoff

VeriReel already computes the route, PR slug, image tags, and workflow run URL
inside `.github/workflows/preview-control-plane.yml` and
`.github/workflows/preview-cleanup.yml`. Harbor's handoff contract should stay
at that evidence layer instead of asking VeriReel to adopt Harbor-owned preview
provisioning first. The target integration is OIDC-authenticated HTTP into
Harbor. The local CLI examples below exist only to pin the payload shape while
the Harbor service ingress is still under construction.

For a successful or failed preview refresh, emit two JSON payloads and hand
them to Harbor's preview-generation evidence ingress. The current local adapter
is `harbor-previews write-from-generation`:

```json
{
  "context": "verireel-testing",
  "anchor_repo": "verireel",
  "anchor_pr_number": 123,
  "anchor_pr_url": "https://github.com/every/verireel/pull/123",
  "canonical_url": "https://pr-123.ver-preview.shinycomputers.com",
  "state": "active",
  "updated_at": "2026-04-16T08:10:00Z",
  "eligible_at": "2026-04-16T08:10:00Z"
}
```

```json
{
  "context": "verireel-testing",
  "anchor_repo": "verireel",
  "anchor_pr_number": 123,
  "anchor_pr_url": "https://github.com/every/verireel/pull/123",
  "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
  "state": "ready",
  "requested_reason": "external_preview_refresh",
  "requested_at": "2026-04-16T08:02:00Z",
  "ready_at": "2026-04-16T08:10:00Z",
  "finished_at": "2026-04-16T08:10:00Z",
  "resolved_manifest_fingerprint": "verireel-preview-manifest-pr-123-6b3c9d7",
  "artifact_id": "ghcr.io/every/verireel-app:pr-123-6b3c9d7",
  "deploy_status": "pass",
  "verify_status": "pass",
  "overall_health_status": "pass"
}
```

That evidence maps directly from the VeriReel workflow outputs:

- `preview_url` -> `preview.canonical_url`
- `pr_number` -> `anchor_pr_number`
- `pr_sha` -> `anchor_head_sha`
- `run_url` should be retained in the calling workflow logs or wrapper script
  alongside the payload write for traceability
- immutable preview image tag or digest -> `artifact_id`

For cleanup, emit the destroy payload and hand it to
Harbor's preview-destroyed evidence ingress once the preview teardown has
actually completed. The current local adapter is
`harbor-previews write-destroyed`:

```json
{
  "context": "verireel-testing",
  "anchor_repo": "verireel",
  "anchor_pr_number": 123,
  "destroyed_at": "2026-04-16T09:04:00Z",
  "destroy_reason": "external_preview_cleanup_completed"
}
```

That cleanup payload should be written only after the preview URL, Dokploy app,
and backing database teardown has succeeded. If cleanup fails, Harbor should
keep the preview record live and instead receive a failed generation or workflow
signal later, rather than a premature destroyed transition.

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
- Harbor should converge on a separate long-running service boundary even while
  the first implementation still lives inside this repo.
- The stable Harbor contract should be service ingress plus Harbor-owned
  drivers, not repo-local shell wrappers around file writes.
