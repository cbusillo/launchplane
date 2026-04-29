---
title: Launchplane Service Boundary
---

## Purpose

This document defines the first explicit Launchplane service boundary: the initial
HTTP ingress, the GitHub Actions OIDC trust model, the claim-to-permission
mapping, and the first stable API payloads Launchplane should accept.

It exists to keep new cross-product work aligned with Launchplane's target form:

- long-running service
- authenticated machine ingress
- Launchplane-owned drivers
- thin repo extensions

The current repo-local CLI and file-backed state directory are implementation
scaffolding. This document defines the boundary those adapters should converge
on.

## Current Implementation Status

The service boundary is implemented and deployed for the current Odoo and
VeriReel product paths:

- CLI: `uv run launchplane service serve`
- health route: `GET /v1/health`
- authenticated evidence routes:
  - `POST /v1/evidence/backup-gates`
  - `POST /v1/evidence/deployments`
  - `POST /v1/evidence/promotions`
  - `POST /v1/evidence/previews/generations`
  - `POST /v1/evidence/previews/destroyed`
- product driver routes:
  - `POST /v1/drivers/odoo/artifact-publish-inputs`
  - `POST /v1/drivers/odoo/artifact-publish`
  - `POST /v1/drivers/odoo/post-deploy`
  - `POST /v1/drivers/odoo/prod-backup-gate`
  - `POST /v1/drivers/odoo/prod-promotion`
  - `POST /v1/drivers/odoo/prod-rollback`
  - `POST /v1/drivers/verireel/testing-deploy`
  - `POST /v1/drivers/verireel/stable-environment`
  - `POST /v1/drivers/verireel/app-maintenance`
  - `POST /v1/drivers/verireel/prod-deploy`
  - `POST /v1/drivers/verireel/prod-backup-gate`
  - `POST /v1/drivers/verireel/prod-promotion`
  - `POST /v1/drivers/verireel/prod-rollback`
  - `POST /v1/drivers/verireel/preview-refresh`
  - `POST /v1/drivers/verireel/preview-inventory`
  - `POST /v1/drivers/verireel/preview-destroy`

Launchplane verifies GitHub OIDC, authorizes workflow identity claims, accepts
deployment/promotion/preview lifecycle evidence over HTTP, and executes the
current Odoo/VeriReel artifact, deploy, backup, promotion, rollback, maintenance,
and preview mutations as authenticated Launchplane routes.

The service also serves the built operator UI shell at `/`, with `/ui` retained
as a compatibility alias. Built assets live under `/ui/assets/...`, while
`/ui/*` falls back to the app shell so the frontend can own client-side routes.
Versioned API ingress remains under `/v1`.

## Host Assumption

- Launchplane runs behind an operator-owned HTTPS host.
- Launchplane exposes versioned API ingress under `/v1`.
- Launchplane returns JSON for both success and failure cases.

## Boundary Layers

```text
GitHub Actions workflow
  -> OIDC token from GitHub
  -> Launchplane HTTP ingress
  -> Launchplane authn/authz
  -> Launchplane core record/write logic
  -> Launchplane read models and driver hooks
```

## Authentication

Machine callers should authenticate with GitHub Actions OIDC.

Human browser callers authenticate with GitHub OAuth. Launchplane owns the
browser session after OAuth callback and sets an `HttpOnly`, `SameSite=Lax`
session cookie. Sessions are backed by the Launchplane database when
`LAUNCHPLANE_DATABASE_URL` is configured. GitHub access tokens stay server-side
and are not exposed to the React operator UI.

Launchplane should verify:

- `iss` is GitHub's OIDC issuer
- `aud` matches Launchplane's expected audience
- signature validates against GitHub's published keys
- token is not expired and is valid for current time

Recommended audience:

- the Launchplane service host name

That keeps the audience tied to the Launchplane service identity instead of to a
temporary repo or local CLI name.

## Authorization Model

Launchplane should authorize machine callers from workflow identity claims,
not from human repo-admin status and not from copied long-lived service
tokens.

The first policy model should be allow-list based and fail closed.

### Claims Launchplane should rely on first

- `repository`
- `repository_owner`
- `workflow_ref`
- `job_workflow_ref` when reusable workflows are involved
- `ref`
- `ref_type`
- `event_name`
- `environment` when present
- `sub`
- `sha`

### Claims Launchplane should not treat as the primary authorization boundary

- `actor`
- `actor_id`
- human repo role such as admin/maintainer

Those are still useful for audit display, but the authorization decision should
primarily trust the workflow identity GitHub issued.

### First policy shape

Launchplane should map verified claims to a small policy rule set:

```text
rule
  - subject type: github-actions
  - repository match
  - workflow_ref or job_workflow_ref match
  - event_name match
  - environment/ref constraints
  - allowed product
  - allowed contexts
  - allowed actions
```

Example policy intent:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/preview-control-plane.yml@*
event_name: pull_request
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - verireel_preview_refresh.execute
  - preview_generation.write
```

Another example:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/preview-cleanup.yml@*
event_name: pull_request
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - preview_lifecycle.plan
  - preview_lifecycle.cleanup
  - verireel_preview_destroy.execute
  - preview_destroyed.write
```

Janitor backstop example:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/preview-janitor.yml@refs/heads/main
event_name: schedule or workflow_dispatch
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - preview_lifecycle.plan
  - preview_lifecycle.cleanup
  - verireel_preview_destroy.execute
  - preview_destroyed.write
```

Stable-lane examples:

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/publish-image.yml@refs/heads/main
event_name: push or workflow_dispatch
allowed product: verireel
allowed contexts: verireel
allowed actions:
  - verireel_testing_deploy.execute
  - deployment.write
```

```text
repository: example-org/verireel
workflow_ref: example-org/verireel/.github/workflows/promote-image.yml@refs/heads/main
event_name: workflow_dispatch
allowed product: verireel
allowed contexts: verireel
allowed actions:
  - backup_gate.write
  - verireel_prod_deploy.execute
  - verireel_prod_promotion.execute
  - verireel_prod_rollback.execute
  - deployment.write
  - promotion.write
```

Odoo stable-lane example:

```text
repository: example-org/product-repo
workflow_ref: example-org/product-repo/.github/workflows/deploy-product.yml@refs/heads/main
event_name: workflow_dispatch
allowed product: odoo
allowed contexts: opw
allowed actions:
  - odoo_post_deploy.execute
  - odoo_prod_backup_gate.execute
  - odoo_prod_promotion.execute
  - odoo_prod_rollback.execute
```

The initial policy engine can be config-backed and static. It does not need a
full RBAC system yet.

Human policy rules use the same reviewed policy file under `github_humans`.
The first supported roles are `read_only` and `admin`. Browser sessions can
authorize read endpoints, but POST mutation routes remain GitHub Actions OIDC
only until browser-initiated mutation workflows get a dedicated CSRF and audit
design.

For first access, `LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS` may name comma-separated
verified GitHub email addresses that receive the `admin` role even before a
matching `github_humans` rule exists. The GitHub OAuth client requests
`user:email` so that this bootstrap path works for private profile emails.

## First API Surface

The first Launchplane service surface should focus on evidence ingress and record
writes, not on every possible operator action.

### Evidence ingress endpoints

- `POST /v1/evidence/deployments`
- `POST /v1/evidence/backup-gates`
- `POST /v1/evidence/promotions`
- `POST /v1/evidence/previews/generations`
- `POST /v1/evidence/previews/destroyed`

### Preview lifecycle endpoints

- `POST /v1/previews/lifecycle-plan`
- `POST /v1/previews/lifecycle-cleanup`
- `POST /v1/previews/desired-state`
- `POST /v1/previews/pr-feedback`

The first preview lifecycle endpoint remains the source of the durable decision:
Launchplane can discover desired preview anchors from GitHub PR label state,
record that desired-state scan, compare the anchors with the latest recorded
provider inventory scan, write a durable lifecycle plan, and return
keep/orphaned/missing sets. Cleanup execution uses a second endpoint that
requires an existing lifecycle `plan_id`; it defaults to `apply=false`
report-only behavior and records the cleanup request/result next to the plan.
Destructive provider cleanup is only attempted when `apply=true` is explicitly
supplied by an authorized GitHub Actions workflow.

PR feedback delivery is part of the same preview lifecycle boundary. Product
repos submit thin preview outcome facts to `POST /v1/previews/pr-feedback`;
Launchplane renders the review comment, upserts the anchored GitHub PR comment
when its runtime token is available, and stores an append-only feedback record
with the comment body, delivery action, comment URL, and any skip/failure reason.

### Operator read endpoints

- `GET /v1/previews/{preview_id}`
- `GET /v1/previews/{preview_id}/history`
- `GET /v1/inventory/{context}/{instance}`
- `GET /v1/promotions/{record_id}`
- `GET /v1/deployments/{record_id}`
- `GET /v1/contexts/{context}/secrets`
- `GET /v1/contexts/{context}/instances/{instance}/secrets`
- `GET /v1/secrets/{secret_id}`
- `GET /v1/contexts/{context}/operations/recent`

These operator reads use the same Launchplane authn/authz boundary as evidence
ingress. The intent is to give operators a minimal typed read surface for the
current Launchplane record nouns without forcing them to infer state from
workflow logs or host-local files. Secret status reads return metadata only:
Launchplane does not expose plaintext secret retrieval through the service
boundary.

### Driver execution endpoints

These use the same authn/authz boundary as evidence ingress:

- `POST /v1/drivers/odoo/post-deploy`
- `POST /v1/drivers/odoo/artifact-publish`
- `POST /v1/drivers/odoo/prod-backup-gate`
- `POST /v1/drivers/odoo/prod-promotion`
- `POST /v1/drivers/odoo/prod-rollback`
- `POST /v1/drivers/verireel/...`

The first explicit driver routes now in service are:

- `POST /v1/drivers/odoo/post-deploy`
- `POST /v1/drivers/odoo/artifact-publish-inputs`
- `POST /v1/drivers/odoo/artifact-publish`
- `POST /v1/drivers/odoo/prod-backup-gate`
- `POST /v1/drivers/odoo/prod-promotion`
- `POST /v1/drivers/odoo/prod-rollback`
- `POST /v1/drivers/verireel/testing-deploy`
- `POST /v1/drivers/verireel/stable-environment`
- `POST /v1/drivers/verireel/app-maintenance`
- `POST /v1/drivers/verireel/prod-deploy`
- `POST /v1/drivers/verireel/prod-backup-gate`
- `POST /v1/drivers/verireel/prod-promotion`
- `POST /v1/drivers/verireel/prod-rollback`
- `POST /v1/drivers/verireel/preview-refresh`
- `POST /v1/drivers/verireel/preview-inventory`
- `POST /v1/drivers/verireel/preview-destroy`

The product-neutral preview lifecycle route should become the common boundary
for preview desired/current-state comparison. Product-specific driver routes can
continue to perform provider runtime work, but repos should not each reimplement
orphan detection once they can submit desired preview anchors to Launchplane.

### Driver discovery endpoints

These are read-only endpoints for the provider-neutral driver descriptor and
read-model contract documented in [driver-descriptors.md](driver-descriptors.md):

- `GET /v1/drivers`
- `GET /v1/drivers/{driver_id}`
- `GET /v1/contexts/{context}/driver-view`
- `GET /v1/contexts/{context}/instances/{instance}/driver-view`

They use action `driver.read`. Discovery authorizes against context
`launchplane`; context and instance views authorize against the requested
context. These routes expose Launchplane capabilities and repository-backed read
state, not runtime-provider primitives.

The preview driver cut stays intentionally narrow but keeps topology in
Launchplane: Launchplane owns preview URL derivation from the
`LAUNCHPLANE_PREVIEW_BASE_URL` runtime-environment value, preview app naming,
runtime refresh, inventory, and teardown. VeriReel still owns image
build/publish, browser verification, and the follow-up preview evidence write.
Browser verification uses the preview URL returned by the driver plus
allow-listed app maintenance actions keyed by preview slug when it needs remote
owner-admin setup/cleanup.

The first Odoo driver cuts are intentionally narrow as well: Launchplane owns the
artifact publish handoff, remote post-deploy data-workflow trigger, and prod
rollback for stable Odoo compose targets. Artifact publish resolves DB-backed
runtime records and managed secrets in Launchplane, invokes `odoo-devkit` as the
build engine with a one-shot runtime payload, validates the returned artifact
manifest, and writes it to Launchplane records. Post-deploy reads DB-backed Odoo
instance override records, renders the typed override payload, invokes the
Dokploy data-workflow runner, and writes `last_apply` evidence back to
Launchplane. Prod rollback reads DB-backed release tuples, artifact manifests,
target records, and current inventory, deploys the selected artifact-backed
image, verifies health, and writes durable rollback/deployment/inventory/release
tuple evidence. Local Odoo runtime commands remain in `odoo-devkit`; these
drivers are for remote control-plane execution only.

Privileged product rollback actions should use a narrow delegated-worker runtime
contract when they require network reach or host authority that does not belong
inside the main Launchplane API container.

Do not generalize the full driver surface before a few product-specific routes
have proven the shape.

## Request And Response Rules

- Requests and responses are JSON.
- Evidence endpoints should be idempotent from Launchplane's perspective when the
  same product/workflow submits the same stable identity twice.
- Launchplane should support an explicit idempotency key header for workflow
  retries.
- Launchplane should return durable record identifiers, not local file paths.
- Launchplane should include a request or trace id in every response.

Recommended first headers:

- `Authorization: Bearer <github_oidc_token>`
- `Content-Type: application/json`
- `Idempotency-Key: <stable-retry-key>`

The current Launchplane service implementation now honors `Idempotency-Key`
for all write routes. Launchplane replays the first successful accepted
response when the same authenticated workflow scope retries the same route
with the same key and the same request fingerprint. Launchplane rejects reuse
of the same key for a different payload on the same route.

Current VeriReel key shapes:

- preview generation: `preview-generation:<product>:<context>:<anchor_repo>:<pr_number>:<sha>`
- preview destroy: `preview-destroyed:<product>:<context>:<anchor_repo>:<pr_number>:<destroy_reason>`
- VeriReel preview refresh driver:
  `verireel-preview-refresh:<product>:<context>:<anchor_repo>:<pr_number>:<sha>`
- VeriReel preview destroy driver:
  `verireel-preview-destroy:<product>:<context>:<anchor_repo>:<pr_number>:<destroy_reason>`

For VeriReel, `destroy_reason` should stay stable per destroy lane so idempotent
retries do not collide. The regular cleanup workflow uses
`external_preview_cleanup_completed`; the janitor backstop uses
`external_preview_janitor_cleanup_completed`.

- testing deployment evidence: `testing-deployment:<product>:<context>:<instance>:<record_id>`
- prod deployment evidence: `prod-deployment:<product>:<context>:<instance>:<record_id>`
- prod promotion evidence: `prod-promotion:<product>:<context>:<from_instance>:<to_instance>:<record_id>`
- VeriReel testing deploy driver:
  `verireel-testing-deploy:<product>:<context>:<instance>:<artifact_id>:<source_git_ref>`
- VeriReel prod deploy driver:
  `verireel-prod-deploy:<product>:<context>:<instance>:<artifact_id>:<source_git_ref>`
- VeriReel prod backup gate driver:
  `verireel-prod-backup-gate:<product>:<context>:<instance>:<backup_record_id>`
- VeriReel prod promotion driver:
  `verireel-prod-promotion:<product>:<context>:<from_instance>:<to_instance>:<artifact_id>:<source_git_ref>:<backup_record_id>:<promotion_record_id>:<expected_build_revision>:<expected_build_tag>`

Recommended first success shape:

```json
{
  "status": "accepted",
  "trace_id": "launchplane_req_01jabc...",
  "records": {
    "preview_id": "preview-verireel-pr-123",
    "generation_id": "preview-verireel-pr-123-generation-0003"
  }
}
```

Recommended first error shape:

```json
{
  "status": "rejected",
  "trace_id": "launchplane_req_01jabc...",
  "error": {
    "code": "authorization_denied",
    "message": "Workflow cannot write preview evidence for verireel-testing."
  }
}
```

## First Stable Payloads

Launchplane should keep the typed evidence payloads already proven in the current
CLI adapters and expose them over HTTP.

### Preview generation evidence

`POST /v1/evidence/previews/generations`

```json
{
  "product": "verireel",
  "preview": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
    "canonical_url": "https://pr-123.preview.example.com",
    "state": "active",
    "updated_at": "2026-04-16T08:10:00Z",
    "eligible_at": "2026-04-16T08:10:00Z"
  },
  "generation": {
    "schema_version": 1,
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
}
```

### Preview destroyed evidence

`POST /v1/evidence/previews/destroyed`

```json
{
  "product": "verireel",
  "destroy": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "destroyed_at": "2026-04-16T09:04:00Z",
    "destroy_reason": "external_preview_cleanup_completed"
  }
}
```

The deployment and promotion endpoints should follow the same pattern: stable
typed record payloads inside a Launchplane-owned API envelope.

For VeriReel's first stable-lane Launchplane slice, use context `verireel` for the
long-lived `testing` and `prod` instances. Preview evidence remains separate
under `verireel-testing` because previews are not another durable promotion
lane.

## CLI Relationship

Current commands such as:

- `control-plane launchplane-previews write-from-generation`
- `control-plane launchplane-previews write-destroyed`

should be treated as temporary compatibility clients of these Launchplane payloads.
They should not remain the permanent integration boundary for external product
workflows.

## Driver Relationship

The first Launchplane API should separate two concerns:

- evidence ingress into Launchplane core records
- runtime execution through Launchplane-owned drivers

That keeps the initial service slice small while still allowing a later shift
from repo-owned operational scripts into Launchplane-owned driver execution.

The first explicit drivers should be:

- Odoo driver
- VeriReel driver

Repo-specific variation should stay thin and declarative where possible.

## Out Of Scope For This First Slice

- full human/operator auth design
- multi-tenant billing or quota models
- generalized plugin marketplace design
- replacing file-backed storage immediately
- moving every current CLI command behind HTTP at once

## Recommended Next Implementation Steps

1. Convert the existing CLI preview evidence commands into local clients of the
   same service-layer handler or payload contract.
2. Add local clients for deployment and promotion evidence where Launchplane-facing
   workflows still write through repo-local CLI adapters.
3. Define the first explicit Odoo and VeriReel driver interfaces after the
   service ingress exists.
