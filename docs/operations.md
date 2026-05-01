---
title: Operations
---

## Command Groups

Use `uv run launchplane --help` for the complete CLI surface. The current
top-level groups are:

Today this CLI is the local Launchplane operator surface around the service API.
It owns stable-lane deploy and promotion records for `testing` and `prod`, plus
Launchplane preview records and read models for PR review flows. It should not
be treated as the final cross-product ingress boundary for Launchplane.

- `artifacts`: write, ingest, and inspect artifact manifests.
- `backup-gates`: write and inspect backup-gate records.
- `deployments`: write and inspect deployment records.
- `environments`: write, list, and resolve DB-backed runtime environment
  contracts.
- `launchplane-previews`: inspect, mutate, render, ingest, and replay
  Launchplane preview state.
- `inventory`: inspect current environment inventory.
- `promote`: record, resolve, and execute artifact-backed promotions.
- `promotions`: write and inspect promotion records.
- `product-config`: dry-run and apply trusted product runtime/secret config
  bundles from a live Launchplane context.
- `release-tuples`: inspect state-backed tuple records and explicitly export a
  TOML catalog from minted state.
- `service`: run the first local Launchplane HTTP ingress slice.
- `ship`: plan, resolve, and execute artifact-backed deploy requests.

`deployments write`, `promotions write`, `inventory write-from-deployment`,
`inventory write-from-promotion`, and `release-tuples write-from-promotion`
are the current small evidence-ingest surfaces that let Launchplane accept
externally-produced deployment and promotion facts without claiming it
executed that product's runtime action itself.

Those commands are current implementation scaffolding. The target Launchplane
boundary is a long-running service with authenticated HTTP ingress, where the
CLI becomes a client of Launchplane's stable API contract instead of defining that
contract itself.

## Target Launchplane Ingress

The target communication model is:

- Launchplane runs as a long-running service behind an operator-owned stable
  host.
- Product workflows communicate with Launchplane over authenticated HTTP.
- GitHub Actions OIDC is the default machine-to-machine trust boundary.
- Launchplane authorizes requests from GitHub-issued identity claims such as repo,
  workflow, ref, environment, and event context.
- Typed evidence payloads are the stable contract; CLI commands are temporary
  adapters while the service boundary is being built.

Launchplane should eventually expose API ingress for at least:

- deployment evidence
- promotion evidence
- inventory refresh triggers or derived writes
- preview generation evidence
- preview destroyed evidence
- driver-triggered runtime actions where Launchplane owns execution

The first explicit version of that boundary, including the OIDC claim mapping
and endpoint list, lives in [`service-boundary.md`](service-boundary.md).

The first implemented service command is:

```bash
uv run launchplane service serve \
  --state-dir ./state \
  --policy-file ./bootstrap-policy.toml
```

The service needs an explicit minimal bootstrap policy input, but the repo no
longer tracks the live policy. Product and workflow grants should be represented
as DB-backed authz policy records.

Current implementation scope:

- `GET /v1/health`
- `POST /v1/evidence/backup-gates`
- `POST /v1/evidence/deployments`
- `POST /v1/evidence/promotions`
- `POST /v1/evidence/previews/generations`
- `POST /v1/evidence/previews/destroyed`
- `POST /v1/previews/lifecycle-plan`
- `POST /v1/drivers/verireel/preview-refresh`
- `POST /v1/drivers/verireel/preview-destroy`
- `POST /v1/drivers/verireel/testing-deploy`
- `POST /v1/drivers/verireel/testing-verification`
- `POST /v1/drivers/verireel/prod-deploy`
- `POST /v1/drivers/verireel/prod-backup-gate`
- `POST /v1/drivers/verireel/prod-promotion`
- `POST /v1/drivers/verireel/prod-rollback`
- `POST /v1/drivers/odoo/post-deploy`
- `POST /v1/drivers/odoo/prod-backup-gate`
- `POST /v1/drivers/odoo/prod-promotion`
- `POST /v1/drivers/odoo/prod-rollback`

Privileged product rollback routes should stay behind narrow delegated-worker
contracts rather than being absorbed into the main API host. Product-private
runtime memos belong in product repos; this repo keeps only the shared driver
and record contracts.

The service uses GitHub OIDC bearer tokens and DB-backed authz policy records.
Additional evidence routes should land against the same authn/authz boundary
rather than creating separate ad hoc ingress patterns.

Render an explicit emergency bootstrap policy or import a policy into DB-backed
records with:

```bash
uv run launchplane service render-authz-policy --policy-file ./bootstrap-policy.toml
uv run launchplane service render-authz-policy \
  --policy-file ./bootstrap-policy.toml \
  --format b64
uv run launchplane authz-policies import-toml --policy-file ./bootstrap-policy.toml
```

When operators need to preview or apply an explicit emergency bootstrap policy to
the live Launchplane Dokploy target without editing any rendered host-side env
file, use:

```bash
uv run launchplane service sync-bootstrap-policy \
  --target-type "$LAUNCHPLANE_DOKPLOY_TARGET_TYPE" \
  --target-id "$LAUNCHPLANE_DOKPLOY_TARGET_ID" \
  --policy-file ./bootstrap-policy.toml

uv run launchplane service sync-bootstrap-policy \
  --target-type "$LAUNCHPLANE_DOKPLOY_TARGET_TYPE" \
  --target-id "$LAUNCHPLANE_DOKPLOY_TARGET_ID" \
  --policy-file ./bootstrap-policy.toml \
  --apply
```

Preview workflows should normally authorize by workflow path with a wildcard
ref suffix such as `.../preview-control-plane.yml@*`, because pull-request runs
execute from branch-specific workflow refs rather than a fixed `main` ref.

The Launchplane container entrypoint now fails closed unless one of
`LAUNCHPLANE_POLICY_TOML`, `LAUNCHPLANE_POLICY_B64`, or
`LAUNCHPLANE_POLICY_FILE` is supplied. It also refuses to start from the
checked-in `.example` policy path.

## Launchplane Service Deploy Posture

The first real Launchplane service deployment should be GitHub-driven and
Dokploy-hosted.

- Keep test and deploy automation separate.
- `CI` is the gate for Launchplane code changes and must pass before a deploy
  workflow replaces the live Launchplane app.
- The first real Launchplane bring-up should target a single Dokploy-hosted Launchplane
  instance rather than introducing a separate Launchplane testing instance during
  bootstrap.
- Launchplane deploy automation should publish an immutable image artifact, update
  Dokploy by digest, and record the previously running digest before
  replacement.
- The current repo workflow for that posture is
  `.github/workflows/deploy-launchplane.yml`.
- Deploy verification should probe Launchplane's live health endpoint, currently
  `GET /v1/health`, after the Dokploy update.
- When rollout health fails, deploy automation should restore the previous
  digest automatically instead of requiring a manual Dokploy click path.
- Keep a manual rollback path too, so operators can redeploy a known-good
  digest even after a technically successful rollout.

This posture is the current safety net while Launchplane still lacks a dedicated
testing environment of its own.

Required GitHub configuration for that workflow:

- repository variables:
  - `LAUNCHPLANE_DOKPLOY_TARGET_TYPE`
  - `LAUNCHPLANE_DOKPLOY_TARGET_ID`
  - `LAUNCHPLANE_DEPLOY_HEALTH_URLS`
  - optional `LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS`
  - optional `LAUNCHPLANE_DEPLOY_HEALTH_TIMEOUT_SECONDS`
  - optional `LAUNCHPLANE_IMAGE_REPOSITORY`

The workflow should use GitHub OIDC to call Launchplane's own service API and
update the image digest plus known OAuth env only. DB-backed authz policy records
own live product/workflow grants; keep Dokploy host/token authority in
Launchplane-managed secrets instead of duplicating those credentials in GitHub
repository secrets.

`LAUNCHPLANE_DEPLOY_HEALTH_URLS` must resolve from the runner that executes the
deploy workflow. Use a Launchplane `GET /v1/health` endpoint reachable from that
runner rather than an internal-only provider hostname.

The Dokploy-hosted Launchplane target should consume `DOCKER_IMAGE_REFERENCE` from
its env so deploy automation can switch the service by immutable digest and
roll back to the prior digest when verification fails.

Before a real Launchplane deploy, run the sanitized preflight check against the
live Dokploy target:

```bash
uv run launchplane service inspect-dokploy-target \
  --target-type compose \
  --target-id "$LAUNCHPLANE_DOKPLOY_TARGET_ID"
```

That command reports only non-secret metadata and fails closed when the live
Launchplane target is missing critical runtime pieces such as
`LAUNCHPLANE_DATABASE_URL`, `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`,
Launchplane-managed Dokploy secret bindings, or a Dokploy SSH key for a private
`git@github.com:...` compose source.

The intended live service contract is now bootstrap-only target env plus
DB-backed Launchplane records:

- keep bootstrap/process inputs such as `LAUNCHPLANE_DATABASE_URL`,
  `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`, and policy selectors on the service
  target
- move Dokploy credentials into Launchplane-managed secret records
- move per-context runtime values, ship-mode overrides, preview base URLs, and
  product-specific worker config into Launchplane runtime-environment records
- use target-id records in the shared store when possible instead of relying on
  env-carried target-id catalogs
- inspect tracked stable-lane Dokploy target records with
  `uv run launchplane dokploy-targets list` / `show`
- mutate tracked target Shopify guard policy with
  `uv run launchplane dokploy-targets put-shopify-protected-store-key ...` and
  `unset-shopify-protected-store-key ...` instead of editing repo-local target
  catalogs or ad-hoc DB rows

Two deployment prerequisites remain Dokploy-side operational contracts rather
than Launchplane CLI validations:

- Dokploy must already have a working saved registry credential that can pull
  Launchplane's GHCR image.
- The Postgres service referenced by `LAUNCHPLANE_DATABASE_URL` must already be
  deployed and reachable on the Dokploy network before Launchplane is redeployed.

Current derived-state behavior:

- accepted deployment evidence also refreshes current environment inventory for
  that `context/instance`
- accepted promotion evidence refreshes destination inventory when the
  promotion record includes valid `deployment_record_id` linkage
- Launchplane can now execute the first explicit VeriReel driver actions
  directly: `POST /v1/drivers/verireel/testing-deploy` and
  `POST /v1/drivers/verireel/prod-deploy` trigger the shared testing and prod
  deploys, `POST /v1/drivers/verireel/prod-backup-gate` captures the prod
  backup gate and writes the backup-gate record, and the promotion / rollback
  drivers own the remaining stable-lane execution path. The prod-promotion
  driver writes the promotion record from the backup gate, deploy result,
  migration result, destination health check, and primitive testing-lane health
  status sent by the product workflow. The testing-verification route accepts
  primitive migration, browser verification, and owner-route statuses and
  updates the existing testing deployment record plus current inventory.
  VeriReel maintenance operations that need Dokploy authority, such as testing
  migrations, preview owner-admin verification helpers, reset-testing, and
  preview inventory, also flow through Launchplane driver routes instead of
  product-repo workflow secrets. Stable testing/prod base URLs and target
  identity are resolved from Launchplane's DB-backed target/runtime records
  through the stable-environment route. Those routes return durable record
  identifiers, topology metadata, or timing/status for the caller to thread into
  later verification or promotion evidence.
- Launchplane can also execute the Odoo stable-lane driver path directly:
  `POST /v1/drivers/odoo/prod-backup-gate` captures DB and filestore backup
  evidence, `POST /v1/drivers/odoo/prod-promotion` validates the stored
  artifact, source release tuple, and required backup gate before promoting
  `testing` to `prod`, and `POST /v1/drivers/odoo/prod-rollback` deploys an
  explicit previous artifact. These routes resolve target identity, runtime
  values, override inputs, and managed secrets from DB-backed Launchplane
  records; tenant workflows should only send thin OIDC-authenticated requests
  and record returned IDs.
- Generic web products can use the common
  `POST /v1/drivers/generic-web/prod-promotion` route for testing-to-prod image
  promotion when product-specific gates are not needed. The route resolves
  product profile lanes, deploys the submitted image to the prod lane, records
  source and destination health evidence, writes promotion/deployment linkage,
  and refreshes prod inventory after a verified deploy. Product-specific
  drivers can wrap this base path with stricter backup, migration, rollout, or
  tenant checks instead of reimplementing the shared promotion record flow.

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
- VeriReel stable deploys update the Dokploy Application docker provider to the
  exact immutable artifact id before triggering deploy; product workflows do not
  publish mutable prod tags as the promotion authority.
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
  when Launchplane already has the source tuple state for the promoted-from lane.
- The tracked Dokploy route catalog is only for stable remote lanes. If a pull
  request needs runtime state, Launchplane models that through preview records and
  preview generations instead of adding another long-lived route.
- Operator read models compose inventory, deployment, promotion, and
  backup-gate records instead of requiring operators to inspect raw JSON first.
- Until Launchplane has a formal schema migration system, DB-backed schema changes
  must stay additive and backward-compatible so image rollback remains a valid
  recovery path.
- Local CLI/file-backed compatibility paths must pass the review checkpoints in
  [compatibility-retirement.md](compatibility-retirement.md). Product workflows
  should use service routes once matching OIDC-authenticated routes exist.

## Dokploy Contracts

- Tracked Dokploy route definitions live in Launchplane DB-backed target
  records.
- Tracked route definitions are expected to be stable remote lanes only:
  `testing` and `prod`.
- Live Dokploy `target_id` values should come from Launchplane DB-backed
  target-id records in steady state.
- Dokploy source loading fails closed when target ids are missing, duplicate
  routes are present, or the tracked target records omit a required target id.
- `environments logs --context <context> --instance <instance> --lines <n>`
  resolves the DB-backed tracked Dokploy target and target id before fetching
  bounded application logs. The first cut supports Dokploy `application`
  targets, includes route/target/app/server metadata, accepts optional
  `--since` and `--search`, and redacts likely secret values from returned log
  lines.
- `GET /v1/contexts/{context}/instances/{instance}/logs?lines=200` exposes the
  same tracked-target log reader through the authenticated service API using
  action `target_logs.read`.

## Runtime Environment Contracts

- `environments put` writes explicit non-secret `KEY=VALUE` runtime settings
  directly into DB-backed runtime-environment records for `global`, `context`,
  or `instance` scope. It rejects secret-shaped keys and returns key metadata
  only, not plaintext values.
- `product-config apply --dry-run|--apply --input-file <json>` is the supported
  trusted-context bundle path for product runtime config changes. It writes
  non-secret values to runtime-environment records and secret-shaped values to
  managed secret records while returning only key names, actions, counts, actor,
  and source metadata. Use it from a live Launchplane context that already has
  current `LAUNCHPLANE_DATABASE_URL`; bundles with secrets also require
  `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`. Runtime and secret scopes default from
  the top-level `context`/`instance`; nested `runtime_env` and secret routes must
  match that top-level target. Dry-run validates secret scope/route
  compatibility before reporting a plan, so apply does not discover invalid
  secret scopes after writing earlier secrets.
- `POST /v1/product-config/apply` exposes the same planner/writer through the
  authenticated service API for operator UI use. Submit `mode: "dry-run"` to
  preview with `product_config.plan`, then `mode: "apply"` with
  `product_config.apply` after review. The service response is redacted and the
  route rejects nested runtime or secret targets that differ from the authorized
  top-level context/instance. It fails closed when secret writes are requested
  without the Launchplane master encryption key in the service runtime.
- The operator UI uses the same service route. It requires a successful dry-run
  result before enabling apply, clears rendered secret input values after each
  submit, and shows only key/action/count metadata from Launchplane responses.
- `environments unset` removes named keys from a DB-backed runtime-environment
  record without reading or printing plaintext values.
- `environments delete-record --dry-run|--apply` deletes a whole mistaken
  runtime-environment record for `global`, `context`, or `instance` scope. The
  dry-run and apply responses include record identity, source label, update
  timestamp, key names, key count, actor, and delete-event metadata only. Apply
  refuses records that can affect a tracked Dokploy target unless
  `--allow-tracked-target` is provided. Apply also fails closed if the target
  record changes after the command reads it; re-run the command after reviewing
  the current record.
- `environments relabel` updates runtime-environment record source metadata
  without reading or printing plaintext values.
- `environments list` shows DB-backed runtime-environment record metadata and
  keys without echoing plaintext values.
- `environments resolve` reads the control-plane-owned runtime environment
  contract for a context and instance.
- `environments apply-live-target --dry-run|--apply` resolves the DB-backed
  runtime environment and managed secret overlay for a tracked Dokploy target,
  compares it against the live target env by key, and can apply those keys
  without requiring an artifact manifest. It preserves unrelated live env keys,
  verifies persistence by key/count metadata only, and never prints plaintext
  env or secret values. Add `--deploy` with `--apply` when the app should be
  explicitly redeployed/reloaded after the config update.
- TOML/env files are not runtime import surfaces; use DB-native
  runtime-environment records and managed secrets instead.
- Product repos and GitHub issues must not contain product secret values. Put
  the JSON bundle on an operator-controlled machine or inside the hosted
  Launchplane execution context, run `--dry-run` first, then run `--apply` only
  after the key/action summary matches the approved change.

Example product config bundle shape:

```json
{
  "schema_version": 1,
  "product": "sellyouroutboard",
  "context": "sellyouroutboard",
  "instance": "prod",
  "runtime_env": {
    "CONTACT_EMAIL_MODE": "smtp",
    "CONTACT_FROM_EMAIL": "owner@example.com"
  },
  "secrets": [
    {
      "name": "smtp-password",
      "binding_key": "SMTP_PASSWORD",
      "value": "operator-supplied-secret"
    }
  ]
}
```

`runtime_env` values are non-secret scalar values. `secrets` default to the
`runtime_environment` integration and the top-level context/instance, which
makes them available as managed runtime environment overlays. Secret scope
routes must be compatible: `global` has no context or instance, `context` has
context only, and `context_instance` has both context and instance.

- `environments show-live-target` reads the live Dokploy target payload for a
  tracked route and reports whether the target is ready for artifact-backed
  split-repo execution.
- `environments apply-live-target` applies Launchplane DB-backed runtime env and
  managed secret overlays to a tracked live target without shipping an artifact.
- `environments sync-live-target --apply` pushes the tracked Dokploy source and
  tracked env overlay for a route into the live target before re-reading the
  artifact-readiness summary.
- `ship execute` and `promote execute` can take an explicit `--env-file` overlay
  for the compose post-deploy update path.
- The post-deploy overlay supports only `ODOO_DB_NAME`, `ODOO_FILESTORE_PATH`,
  and `ODOO_DATA_WORKFLOW_LOCK_FILE`.
- When multiple healthcheck URLs are resolved for a lane, Launchplane treats
  them as alternate verification surfaces and accepts the first `2xx` response
  instead of requiring every URL to succeed.

## Odoo Instance Override Contracts

- `POST /v1/drivers/odoo/post-deploy` is the first Launchplane-owned Odoo
  driver route. It executes the remote compose post-deploy data-workflow runner
  for a stable Odoo target and applies DB-backed instance override records when
  the requested phase matches `apply_on`.
- `POST /v1/drivers/odoo/prod-rollback` rolls a prod-named Odoo lane back to
  the DB-backed `testing` release tuple for the same context. The driver updates
  the Dokploy `DOCKER_IMAGE_REFERENCE`, deploys the compose target, runs the
  Odoo post-deploy workflow, verifies `/web/health`, writes deployment,
  inventory, release tuple, and rollback evidence, and annotates the current prod
  promotion record.
- `POST /v1/drivers/odoo/prod-backup-gate` captures the DB and filestore backup
  evidence required before Odoo prod promotion. It resolves `ODOO_DB_NAME`,
  `ODOO_FILESTORE_PATH`, and `ODOO_BACKUP_ROOT` from DB-backed runtime
  environment records, runs a Dokploy schedule against the compose lane, stops
  the web service while capturing, and writes the backup-gate record only after
  the capture succeeds.
- Odoo rollback is image/release-tuple rollback, not VM snapshot rollback. Do not
  invent artifact ids, source commits, backup gates, or env-file overlays to make
  a rollback proceed; write or import the real Launchplane records first.
- `odoo-overrides put-config-param` writes a typed Odoo `ir.config_parameter`
  override for a context and instance.
- `odoo-overrides put-addon-setting` writes addon-shaped Odoo override intent
  such as Authentik or Shopify settings for a context and instance.
- Secret-shaped override names, including `*_TOKEN`, `*_PASSWORD`, and
  `*_KEY`, must use `--secret-binding-id`; plaintext secret writes are rejected.
- `odoo-overrides list` and `odoo-overrides show` return keys, counts, source
  labels, and timestamps only. They do not echo literal values or managed secret
  binding ids.
- `odoo-overrides mark-apply` updates the latest apply status metadata for a
  record, giving the future Odoo driver a tested result-write path.
- Compose post-deploy updates consume deploy-phase overrides from these records
  and pass them to the Odoo data-workflow runner as one typed payload env var.
- Launchplane passes one typed payload to the Odoo settings apply path; legacy
  `ENV_OVERRIDE_*` values are migration input only, not the deploy-time
  settings contract.
- Secret-backed overrides are still not rendered into schedule scripts as
  plaintext. The payload references the already-present neutral
  `ODOO_OVERRIDE_SECRET__*` script-runner environment key for each managed
  secret binding, and the workflow asserts those keys before Odoo starts.
- This keeps record authority in Launchplane while moving Odoo toward the
  typed payload contract. The remaining legacy `ENV_OVERRIDE_*` inputs are now
  compatibility-only and can be deleted once the DB-backed override records are
  fully migrated.
- `odoo-devkit` remains the local runtime/workspace surface. Launchplane driver
  routes should not be inserted into the local PyCharm or local container loop;
  use them only for remote stable lanes and promotion/deploy evidence.

## Odoo Rollback And Re-Promote Waterfall

- Confirm Launchplane health reports `storage_backend=postgres`.
- Confirm the target context has DB-backed artifact manifests, `testing` and
  `prod` release tuples, Dokploy target records, target-id records, and current
  prod inventory.
- For the first harmless drill, call the Odoo prod rollback driver with no
  explicit artifact id. The driver selects the current `testing` release tuple
  for that context and fails closed if the tuple or artifact manifest is missing.
- For a real rollback after `testing` has advanced, call the same driver with
  an explicit DB-backed artifact id for the previous known-good prod artifact.
  The driver reads the artifact manifest directly from Launchplane records and
  writes rollback evidence with an `artifact:<artifact_id>` source marker.
- A passing rollback writes deployment, inventory, prod release tuple,
  promotion rollback, and rollback-health evidence. Verify the target
  `/web/health` endpoint and `inventory status` before taking another action.
- A real destructive rollback drill requires a second known-good artifact
  manifest. Do not synthesize artifact ids or source SHAs to create one.
- A re-promote drill should use the normal prod promotion path with a fresh
  backup gate for the current prod-named lane. Do not reuse old bootstrap backup
  gates as authorization for a new re-promote.

Artifact handoff example:

```bash
uv run launchplane odoo-artifacts publish \
  --context example \
  --instance testing \
  --manifest ../product-repo/workspace.toml \
  --devkit-root ../devkit \
  --image-repository ghcr.io/example/product \
  --image-tag example-20260416-deadbeef
```

The Odoo artifact publish driver is the control-plane-owned handoff. It
resolves the DB-backed runtime environment and managed secrets in Launchplane,
passes them to `odoo-devkit` as a one-shot runtime payload for the publish
subprocess, validates the returned artifact belongs to the requested context,
and writes the artifact manifest back to Launchplane records. Do not point a
local devkit checkout directly at the live Launchplane database or recreate
runtime env files to publish artifacts.

## Launchplane Preview Operations

Launchplane commands operate on durable preview, generation, and enablement records.
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

`show-tenant`, `render-index-page`, and `render-site` now resolve stable-lane
baseline tuples from Launchplane's DB-backed release-tuple records. Cockpit and
local renders should run with `LAUNCHPLANE_DATABASE_URL` pointed at the same
shared store that owns the current stable-lane tuple state.

Any exported release-tuple catalog is seed/reference material now, not live
runtime authority. Pull requests flow through Launchplane preview records
instead of a tracked long-lived `dev` tuple lane.

`launchplane-previews write-from-generation` and `launchplane-previews write-destroyed`
are local preview-evidence ingest adapters that mirror the service ingress
payload shape. VeriReel preview runtime now flows through Launchplane drivers:
the app repo sends PR/image intent, Launchplane derives the live preview URL
from `LAUNCHPLANE_PREVIEW_BASE_URL`, and evidence stores that returned URL with
generation status and cleanup outcome.

Launchplane now owns the preview lifecycle planning boundary. The scheduled
Launchplane `Preview Lifecycle` workflow discovers desired preview anchors from
GitHub PR label state through `POST /v1/previews/desired-state`, refreshes the
provider inventory, calls `POST /v1/previews/lifecycle-plan`, then records
cleanup through `POST /v1/previews/lifecycle-cleanup`. Cleanup defaults to
report-only and destructive provider cleanup still requires explicit
`apply=true` from an authorized GitHub Actions workflow. PR feedback goes
through `POST /v1/previews/pr-feedback`; Launchplane renders and upserts the
anchored PR comment when runtime GitHub credentials are available, then records
delivery status. Refresh-capable workflows can publish neutral pending feedback
before preview publish/provision/verify outcomes are known, then replace it with
ready or failed feedback after the actual result. Product repos remain thin
adapters for labels, artifact build facts, and product-specific health/config
hints.

### VeriReel Preview Evidence Handoff

VeriReel already computes the route, PR slug, image tags, and workflow run URL
inside `.github/workflows/preview-control-plane.yml` and
`.github/workflows/preview-cleanup.yml`. The scheduled orphan backstop in
`.github/workflows/preview-janitor.yml` should use the same Launchplane destroy
and evidence contract rather than keeping a second repo-local teardown path.
Launchplane's handoff contract is moving from evidence-only toward reusable
preview lifecycle ownership. The target integration is
OIDC-authenticated HTTP into Launchplane. The local CLI examples below exist only
to pin the payload shape while the Launchplane service ingress continues to
absorb the reusable lifecycle behavior.

For a successful or failed preview refresh, emit two JSON payloads and hand
them to Launchplane's preview-generation evidence ingress. The current local adapter
is `launchplane-previews write-from-generation`:

```json
{
  "context": "verireel-testing",
  "anchor_repo": "verireel",
  "anchor_pr_number": 123,
  "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
  "canonical_url": "https://pr-123.preview.example.com",
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
  "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
  "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
  "state": "ready",
  "requested_reason": "external_preview_refresh",
  "requested_at": "2026-04-16T08:02:00Z",
  "ready_at": "2026-04-16T08:10:00Z",
  "finished_at": "2026-04-16T08:10:00Z",
  "resolved_manifest_fingerprint": "verireel-preview-manifest-pr-123-6b3c9d7",
  "artifact_id": "ghcr.io/example-org/verireel-app:pr-123-6b3c9d7",
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
Launchplane's preview-destroyed evidence ingress once the preview teardown has
actually completed. The current local adapter is
`launchplane-previews write-destroyed`:

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
and backing database teardown has succeeded. If cleanup fails, Launchplane should
keep the preview record live and instead receive a failed generation or workflow
signal later, rather than a premature destroyed transition.

The scheduled janitor backstop uses the same payload shape with
`destroy_reason: external_preview_janitor_cleanup_completed` so its retries stay
separate from the pull-request cleanup workflow's idempotency key.

Use `release-tuples export-catalog --state-dir <state>` to render those minted
state records as catalog TOML when an operator is ready to review and
materialize a new tracked baseline.

GitHub PR feedback uses one Launchplane-owned marker comment per PR. The
comment is a review surface over durable Launchplane records: preview URL/state,
manifest and baseline tuple, source inputs, artifact identity when present,
health status, next action, and apply outcome.

Launchplane treats product PRs as preview anchors. Companion, infra, and tooling
repos should remain source inputs only unless a product explicitly maps them to
a preview context.

Preview enablement records retain the anchor PR head SHA plus any resolved
companion PR head SHA snapshots from ingest. Tenant renders use those stored
snapshots for preview request recipes and keep unresolved companion requests
blocked instead of guessing source inputs.

## Launchplane Boundary

- GitHub remains the engineering workflow surface: issues, branches, pull
  requests, labels, checks, PR comments, releases, and CI execution.
- `launchplane` owns the durable operational truth behind those
  workflows: artifacts, release tuples, previews, deployments, promotions,
  backup gates, and inventory.
- Launchplane should converge on a separate long-running service boundary even while
  the first implementation still lives inside this repo.
- The stable Launchplane contract should be service ingress plus Launchplane-owned
  drivers, not repo-local shell wrappers around file writes.
