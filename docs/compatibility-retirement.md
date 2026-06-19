---
title: Compatibility Retirement
---

## Purpose

Launchplane keeps local CLI helpers and file-backed stores only for local
development, tests, local rehearsal, read-only diagnostics, and emergency
operator inspection. They are not production authority, and production-capable
service or CLI mutation paths must fail closed unless they have DB-backed
authority or explicit operator-supplied input.

Use this page as the review checklist before keeping or deleting compatibility
surfaces.

## Retirement Rules

Delete or demote a compatibility surface when all of these are true:

- a typed Launchplane service route exists for the same action or record family
- the product workflow uses GitHub OIDC to call that route
- the route writes DB-backed Launchplane records or managed secret bindings
- a targeted test covers the service route and a client/request test covers the
  product workflow wrapper
- live evidence proves the route for at least one real context or product lane

For the legacy WSGI HTTP fallback, apply the same rule route-family by
route-family. A native FastAPI replacement must own the path before the mounted
fallback, carry Pydantic/OpenAPI contract coverage, preserve the relevant legacy
behavior in native route tests, and have caller evidence before the old WSGI
handler is deleted or demoted. If a PR keeps the old handler reachable, it must
name the removal condition or owning follow-up issue.

Keep a compatibility surface only when it is one of these:

- local-development scaffolding used by tests or operator rehearsal
- a read-only diagnostic helper that does not mutate production truth
- an emergency operator client for the same typed service contract
- docs or tests that use non-production examples and are not reachable by
  production code

## Current Checkpoints

- Odoo artifact publish, post-deploy, prod backup gate, prod promotion, and prod
  rollback are service routes. Tenant repos should keep only thin request
  workflows and artifact build context.
- VeriReel testing deploy, stable environment reads, app maintenance, prod backup
  gate, prod deploy/promotion/rollback, preview refresh, preview inventory, and
  preview destroy are service routes. VeriReel should keep source/build,
  verification, and thin request wrappers.
- Tracked release-tuple catalogs are examples or seed/debug material only.
  Production release truth is the Launchplane release-tuple record shape in the
  shared store.
- File-backed JSON state is local-dev/test scaffolding. Production truth is
  Launchplane service-owned persistence.
- The Launchplane health read uses the native FastAPI `GET /v1/health` route.
  Its legacy WSGI branch is deleted; direct fallback calls fail closed while
  the mounted fallback remains for retained non-native routes.
- Launchplane service runtime and Odoo worker status reads use native FastAPI
  routes for bearer-token and human-session callers. Odoo worker reconcile uses
  native FastAPI `POST /v1/service/odoo-workers/reconcile` on the bearer/OIDC
  write identity path with the existing operation-record storage protocol,
  `launchplane_service.reconcile_odoo_workers` authorization, `max_attempts`
  validation, and `200` reconcile result payload. Their legacy WSGI branches are
  deleted; direct fallback calls fail closed while the mounted fallback remains
  for retained non-native routes.
- Odoo stable-bootstrap and target-replacement operation status reads use native
  FastAPI routes for bearer-token and human-session callers. Their legacy WSGI
  read branches are deleted; the POST enqueue routes still return the same poll
  URLs while those write paths remain on the retained fallback.
- Tracked target logs use the native FastAPI
  `GET /v1/contexts/{context}/instances/{instance}/logs` route. The legacy WSGI
  branch is deleted; direct fallback calls fail closed while the mounted fallback
  remains for retained non-native routes.
- Edge endpoint record reads use native FastAPI `GET /v1/edge-endpoints/records`
  and `GET /v1/edge-endpoints/records/{endpoint_key}` routes. Their legacy WSGI
  read branch is deleted. Edge endpoint apply uses native FastAPI
  `POST /v1/edge-endpoints/apply`; its legacy WSGI write branch is deleted, and
  direct fallback calls fail closed.
- Private health endpoint record reads use native FastAPI
  `GET /v1/private-health-endpoints/records` and
  `GET /v1/private-health-endpoints/records/{endpoint_key}` routes. Their
  legacy WSGI read branch is deleted. Private health endpoint apply uses native
  FastAPI `POST /v1/private-health-endpoints/apply`; its legacy WSGI write
  branch is deleted, and direct fallback calls fail closed.
- Ingress canary route record reads use native FastAPI
  `GET /v1/ingress/canary-routes/records` and
  `GET /v1/ingress/canary-routes/records/{canary_key}` routes. Their legacy
  WSGI read branch is deleted. Ingress canary route record apply uses native
  FastAPI `POST /v1/ingress/canary-routes/records/apply`, and ingress canary
  route apply uses native FastAPI `POST /v1/ingress/canary-routes/apply` with
  the existing idempotency replay/conflict contract. Their legacy WSGI write
  branches are deleted, and direct fallback calls fail closed.
- Ingress route audit record reads use native FastAPI
  `GET /v1/ingress/route-audits/records` and
  `GET /v1/ingress/route-audits/records/{record_id}` routes. Their legacy WSGI
  read branch is deleted. Ingress route apply uses native FastAPI
  `POST /v1/drivers/ingress/route-apply` with mode-sensitive
  `ingress_route.plan`/`ingress_route.apply` authorization and apply-mode
  idempotency replay/conflict behavior. Its legacy WSGI write branch is deleted,
  and direct fallback calls fail closed.
- Dokploy target inspect and setup use native FastAPI routes:
  `GET /v1/dokploy-targets/inspect` and `POST /v1/dokploy-targets/setup`.
  Their legacy WSGI branches are deleted, and direct fallback calls fail
  closed. Setup keeps the apply-only idempotency replay/conflict contract while
  dry-runs remain repeatable.
- Merge-train admission, controller-status, and policy-target reads use native
  FastAPI routes. Their legacy WSGI read branches are deleted; merge-train
  worker, controller mutation, feedback, and phase runner routes remain on the
  retained fallback until their native write replacements land.
- Work graph snapshot, work-graph rank, GitHub issue-inbox reads, and GitHub
  issue-inbox reconcile use native FastAPI routes. Their legacy WSGI
  read/rank/reconcile branches and WSGI-only helpers are deleted.
- Protected artifact inventory and product environment config-status reads use
  native FastAPI routes for bearer-token and human-session callers. Their legacy
  WSGI branches are deleted; cleanup callers use the service route instead of a
  second production implementation.
- Deployment, promotion, preview, inventory, recent-operations,
  managed-secret status, product-profile, product/site read-model,
  repo-product mapping, agent-context, and product context cutover audit reads
  use native FastAPI routes for bearer-token and human-session callers. The
  product-profile collection also preserves the dedicated Every Code worker
  token. Their legacy WSGI branches are deleted; direct fallback calls fail
  closed while the mounted fallback remains for retained non-native routes.
- Product profile writes use native FastAPI `POST /v1/product-profiles` for
  bearer-token callers and preserve product-profile write-contract validation,
  record storage, and optional `Idempotency-Key` replay/conflict behavior. The
  legacy WSGI write branch is deleted; direct WSGI fallback calls fail closed.
- Product context cutover and legacy context cleanup apply writes use native
  FastAPI `POST /v1/product-profiles/context-cutover/apply` and
  `POST /v1/product-profiles/legacy-context-cleanup/apply` for bearer-token
  callers. They preserve `product_profile.write` authorization, DB-backed
  storage gating, redacted result payloads, and optional `Idempotency-Key`
  replay/conflict behavior. Their legacy WSGI write branches are deleted;
  direct WSGI fallback calls fail closed.
- Every Code work-request, summary, PR-feedback, preview-gate,
  notification-attempt, and preview-readiness reads use native FastAPI routes.
  The worker-facing read routes preserve the dedicated Every Code worker token;
  preview PR-feedback notification-attempt reads stay bearer/human authorized.
  The legacy WSGI read map, read payload helper, and worker-token GET bypass are
  deleted. Every Code work-request create uses native FastAPI, preserves
  `every_code_work_request.write` authorization, record-store write capability
  checks, and optional `Idempotency-Key` replay/conflict behavior. Every Code
  PR-feedback write and preview-gate write routes use native FastAPI, preserve
  the dedicated Every Code worker token, require only their direct record-store
  write capabilities, and intentionally do not add idempotency state. Their
  legacy WSGI write branches are deleted; direct WSGI fallback calls fail
  closed. Every Code claim, status, rerun, webhook, and PR-feedback status
  routes remain on the mounted WSGI fallback until their native write
  replacements land.
- Deployment, backup-gate, promotion, preview generation, preview destroyed,
  runner-host hygiene audit, and runner-lane registration audit evidence
  ingestion use native FastAPI routes for bearer-token callers and preserve the
  existing `Idempotency-Key` replay/conflict contract. Their legacy WSGI
  branches are deleted; the mounted fallback remains only for retained
  non-native routes, and direct WSGI calls to these evidence-ingress paths fail
  closed.
- Public ingress monitor run-once uses native FastAPI
  `POST /v1/products/public-ingress-monitor/run-once` for bearer-token callers
  and preserves the existing optional `Idempotency-Key` replay/conflict
  contract. The stale legacy WSGI write branch, old read-route matcher, and
  local checkout `public-ingress-monitor run-once` CLI mutation command are
  deleted; the route has no `GET` API, manual reruns go through the GitHub
  workflow, and direct WSGI fallback calls fail closed.
- Public ingress, Every Code, and preview PR feedback notification policy apply
  use native FastAPI routes for bearer-token callers and preserve DB-backed
  storage enforcement, local operator reason requirements, explicit preview
  scope validation, and optional `Idempotency-Key` replay/conflict behavior.
  Their legacy WSGI write branches are deleted, and direct WSGI fallback calls
  to these policy-apply paths fail closed.
- Runtime key-safety policy apply uses native FastAPI
  `POST /v1/runtime-key-safety/policies/apply` for bearer-token callers and
  preserves `runtime_key_safety.write` authorization, DB-backed storage
  enforcement, metadata-only policy writes, and optional `Idempotency-Key`
  replay/conflict behavior. Its legacy WSGI write branch and WSGI-only helper
  code are deleted, and direct WSGI fallback calls fail closed.
- Live target runtime apply uses `POST /v1/live-target-runtime/apply` and the
  `live-target-runtime.yml` workflow for shared and production live changes.
  The local checkout `environments apply-live-target` mutation command is
  deleted, and the local checkout `environments sync-live-target` drift-preview
  compatibility command is deleted. Operators use service/API identity so
  Launchplane resolves current DB-backed target authority and records sanitized
  key/count evidence.
- Provider-target manifest input and product-onboarding service response aliases
  are retired. Product context audit/cutover responses are also retired from
  Dokploy-named target buckets. Manifests must use `provider_targets`;
  obsolete `dokploy_targets` input raises a validation error.
  Product-onboarding service responses expose only neutral `provider_target*`
  summary keys. Context audit/cutover and cleanup summaries expose
  `provider_targets` and `provider_target_ids`. Product onboarding requests use
  `provider_targets`; obsolete `dokploy_targets` input is rejected.
- Generic-web and VeriReel driver result payloads no longer expose the
  response-only `target_type` alias. Responses use `target_category`,
  `provider_id`, and `provider_target_type`; internal Dokploy execution config
  keeps target-type fields only where application-vs-compose behavior is still
  required.
- Odoo-shaped preview desired-state, inventory, readiness, verification,
  refresh, and destroy aliases are retired. Odoo preview workflows use
  `preview-apply-inputs` and `preview-apply` for isolated provider mutation,
  and common preview read/planning/evidence callers use the inherited
  generic-web routes.
- VeriReel app-maintenance action-only payload compatibility is retired. Product
  workflows must pass the scoped `intent` matching the requested maintenance
  action and stable or preview lane.
- The Odoo-shaped stable verification alias is retired. Odoo stable smoke
  follow-ups use `POST /v1/drivers/generic-web/stable-verification`.
- The Odoo-shaped rollback-plan alias is retired. Odoo rollback planning uses
  `POST /v1/drivers/generic-web/prod-rollback-plan`.
- `control_plane` remains the Python package name for now. Do not add public
  `odoo-control-plane` names, env vars, or docs; prefer Launchplane wording for
  product/operator surfaces.
- The first driver-migration working plan is retired. New driver work should be
  tracked as capability expansion in the active Launchplane GUI/driver plan or
  in focused PRs/issues, not by reopening the old migration checklist.

## File-Backed State Inventory

`state/`, `FilesystemRecordStore`, `--state-dir`, and `LAUNCHPLANE_STATE_DIR`
remain local-only surfaces. They are allowed only for local development, tests,
import/backfill, explicitly flagged local rehearsal, and emergency inspection.
Shared and production live mutations must use the deployed Launchplane service
API or DB-backed operator records. Service startup requires `--database-url` or
`LAUNCHPLANE_DATABASE_URL`; filesystem state is never a service persistence
fallback.

### Keep

- `control_plane.storage.filesystem.FilesystemRecordStore` stays as the local
  JSON implementation used by tests, local rehearsals, and temporary import
  sources.
- Unit and integration tests may keep temporary `state_dir` fixtures. These
  validate record contract parity and service behavior without requiring a live
  Postgres database.
- `launchplane service serve --state-dir ...` may pass an operator-local runtime
  directory for non-authoritative process artifacts, but service persistence is
  always DB-backed. Omitting `--database-url` or `LAUNCHPLANE_DATABASE_URL`
  fails closed, including loopback local development.
- Every Code local worker commands may keep `--state-dir` for daemon pid/log
  files and local worktree/session state. Work-request authority should use
  `--service-url` mode for the deployed Launchplane service.
- `storage import-core-records --state-dir ... --database-url ...` stays as the
  explicit backfill bridge from local JSON records into Postgres-backed records.
- `promote execute` and `ship execute` require `--database-url` or
  `LAUNCHPLANE_DATABASE_URL`; explicit offline filesystem execution must opt in
  with `--local-rehearsal`.
- Generic record mutation commands such as `artifacts write`,
  `backup-gates write`, `promotions write`, `deployments write`,
  `inventory write-from-*`, and `release-tuples write-from-promotion` require
  `--database-url` or `LAUNCHPLANE_DATABASE_URL`; explicit offline filesystem
  writes must opt in with `--local-rehearsal`.
- `launchplane-previews` mutation, ingest, replay, and lifecycle transition
  commands require `--database-url` or `LAUNCHPLANE_DATABASE_URL`; explicit
  offline filesystem writes must opt in with `--local-rehearsal`. Read and
  render commands may keep `--state-dir` as local inspection surfaces.
- `service inspect-data-freshness` requires `--database-url` or
  `LAUNCHPLANE_DATABASE_URL`; explicit local JSON inspection must opt in with
  `--local-inspection`.

### Migrate Or Demote

- Generic record CLI read/export groups such as `artifacts`, `backup-gates`,
  `deployments`, `promotions`, `inventory`, and `release-tuples` may read local
  `--state-dir` data for inspection. Mutation commands require DB authority or
  an explicit `--local-rehearsal` flag. Do not document local record writes as
  live shared-service mutation paths.
- Service routes and product driver workflows should accept generic record-store
  protocols instead of concrete file-backed stores. The first compatibility
  retirement pass migrated Launchplane preview, Odoo promotion, VeriReel deploy,
  VeriReel backup-gate, VeriReel promotion, VeriReel rollback, and service route
  store boundaries to protocol-shaped interfaces.
- New product workflow docs should point to the service route, not to a local
  checkout plus `--state-dir` command.
- Any public doc that shows a product/tenant operator mutating live state through
  local JSON files should be rewritten to use service ingress, DB-backed
  operator commands, or a clearly labeled local-only rehearsal.
- Static operator pages may still render shell recipes for local rehearsal and
  incident inspection, but those snippets must label `--state-dir` paths as
  local-only and must not present file-backed writes as shared-service mutation
  authority.

### Delete Later

- Delete file-backed write commands once an equivalent service route exists,
  product workflows use that route, and import/backfill no longer needs the
  command.
- Delete product-specific file-backed workflow modules after the matching
  Launchplane driver route and DB-backed tests cover the same behavior.
- Delete stale examples or seed files that imply repo-tracked operational state.
  Durable runtime records belong in Postgres-backed Launchplane storage, not in
  git history.

### Retirement Checklist

Before removing a file-backed command or workflow path, verify all of the
following:

- a typed service route or DB-backed operator command exists for the same record
  family or mutation
- the shared/product workflow calls the service route with GitHub OIDC or uses a
  Launchplane-owned DB-backed operator path
- tests cover both the replacement route and the retired compatibility behavior
- local/dev users still have a documented rehearsal path, or the old local path
  is explicitly obsolete
- live evidence shows the replacement route has succeeded for at least one real
  product/context lane

## Review Cadence

Review this page after adding any new product driver route or tenant workflow.
If a product workflow still shells into a Launchplane CLI to mutate production
truth after a service route exists, that workflow is not done.
