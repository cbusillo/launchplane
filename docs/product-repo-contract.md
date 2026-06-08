---
title: Product Repo Contract
---

## Purpose

Product repos should stay product-shaped. They own application code, local
developer ergonomics, product tests, and artifact publishing. Launchplane owns
the durable lifecycle around those artifacts: product profiles, runtime targets,
deployments, previews, feedback, promotion evidence, backup gates, rollbacks,
cleanup, and provider mutations.

This document is the approval gate for new website repos and the cleanup target
for older repos that grew Launchplane-like scripts before the service boundary
existed.

## Target Shape

```text
product repo
  - app source
  - Dockerfile and runtime contract
  - local dev/test commands
  - product-specific smoke or E2E checks
  - image build and publish workflow
  - thin Launchplane trigger workflow

Launchplane
  - product profile and lane configuration
  - driver descriptors and driver routes
  - provider credentials and managed secrets
  - preview/deploy/promotion/rollback orchestration
  - health, readiness, inventory, cleanup, and feedback records or driver
    responses
  - protected artifact inventory for registry cleanup
  - PR feedback rendering and delivery
```

The product repo should not carry Launchplane lifecycle truth in TOML, JSON,
checked-in fixtures, or copied ops scripts. Product and lane configuration lives
in Launchplane DB-backed records.

## What Product Repos Own

- Application source code and product-owned business behavior.
- Product dependencies, lockfiles, and package/build tooling.
- Dockerfile or image build contract.
- Documented runtime ports, health paths, and persistent state mounts for
  service-shaped products that run as Dokploy applications.
- Local development helpers, including local-only databases when the product
  needs them.
- CI checks that validate the source artifact before Launchplane sees it: lint,
  typecheck, unit tests, app build, container build, and product-specific smoke
  checks.
- Publishing an immutable image or artifact reference that Launchplane can
  deploy.
- A minimal GitHub Actions trigger that authenticates to Launchplane with OIDC
  and submits the product key, source ref or SHA, PR number when relevant, and
  immutable artifact reference.

Product-specific checks may stay in the repo when they exercise product behavior
Launchplane cannot know generically, such as a checkout flow, owner route, QR
scan flow, or domain-specific API behavior. Generic runtime health and revision
checks should move to Launchplane drivers once the driver has the necessary
profile data.

## What Launchplane Owns

- Product profile records, lane profiles, preview policy, runtime port, health
  path, preview slug policy, and public URL/domain policy.
- Dokploy or other provider target records and target-id records.
- Runtime-environment records and managed secret records.
- Driver request validation, idempotency policy, action safety, and audit
  evidence.
- Dokploy application deploys for simple service products that follow the
  [Dokploy service deployment contract](dokploy-service-deployments.md).
- Provider mutations: create/update/delete preview apps, deploy stable lanes,
  promote, rollback, capture backup gates, and cleanup stale runtime state.
- Readiness checks before provider mutation.
- Health checks, public page readiness, and deployed build identity checks when
  they are based on profile-owned health paths and expected revisions or image
  references.
- PR feedback records, markdown rendering, comment delivery, and stale feedback
  cleanup.
- Promotion, rollback, deployment, preview, inventory, and cleanup records.
- Protected artifact inventory used by registry cleanup to identify live
  testing, production, release-tuple, and active-preview image references.

## Minimal Trigger Inputs

A product workflow should submit only the facts Launchplane cannot derive from
DB-backed profiles or GitHub OIDC claims:

- product key
- source ref or commit SHA
- immutable artifact or image reference
- PR number for preview actions
- explicit production confirmation for destructive or high-risk actions
- optional run URL for audit display

Launchplane should derive context, lane, preview slug, preview URL, target,
health path, feedback marker, provider credentials, managed secrets, and record
ids unless a driver-specific route documents an explicit exception.

## Approval Gate

A product repo is approved when all of these are true:

- Workflows build, test, and publish product artifacts, then trigger
  Launchplane. They do not directly mutate runtime providers.
- Scripts do not own Launchplane record or evidence shaping that Launchplane can
  derive from profiles, driver requests, provider results, or GitHub OIDC
  claims.
- Driver-trigger workflows rely on Launchplane routes to write the records for
  provider actions they execute. If product-specific smoke checks still run in
  the repo, the repo sends only primitive result facts back to Launchplane.
- Preview, deploy, promotion, rollback, and cleanup triggers pass minimal inputs
  only.
- Product-specific checks remain in the repo only when they validate product
  behavior rather than generic deploy plumbing.
- Removed scripts are unused or replaced by equivalent Launchplane routes with
  tests.
- CI and security gates pass after cleanup.
- At least one non-prod Launchplane path is exercised after the cleanup.

## Odoo Ownership Regression Check

Launchplane owns the Odoo ownership-boundary regression check. Run it from a
workspace that contains `launchplane` and the Odoo sibling repos:

```bash
uv run launchplane odoo-ownership check --workspace-root ..
```

The check is intentionally narrow. It allows product-owned source, tests,
artifact publishing, GHCR login, devkit local build/runtime behavior, and thin
Launchplane connectors through either:

- `cbusillo/launchplane/.github/actions/launchplane-request@main`
- `cbusillo/launchplane/.github/workflows/reusable-odoo-*.yml@main`

It blocks the patterns that previously caused ownership drift:

- repo-local GitHub OIDC token clients instead of the shared request action or
  reusable workflow
- repo-local Launchplane HTTP clients that duplicate the shared connector
- tenant workflows or scripts mutating Dokploy, SSH, compose, or other runtime
  providers directly
- devkit or retired repos exposing shared/prod mutation flows from arbitrary
  checkouts
- repo-local derivation of Launchplane-owned preview URLs, target IDs, release
  tuple IDs, deployment IDs, promotion IDs, or backup-gate IDs outside approved
  thin workflow response handling

When a product repo genuinely needs new source-adjacent facts, add a typed
Launchplane driver input or shared connector path before expanding the allowlist.
Do not copy a request client, provider planner, or durable-record builder into a
tenant, image, shared-addon, or local-DX repo.

Retired `odoo-ai` archival authority is intentionally handled by the separate
quarantine plan rather than this active-repo regression gate.
Known `odoo-devkit` Dokploy-managed local/remote runtime helpers are still
tracked by the local-DX separation plan; this check guards against new drift in
tenant, image, shared-addon, and workflow/script surfaces while that cleanup
continues.

## Cleanup Workflow

For an existing repo, classify each workflow and script before deleting code:

- `keep`: product build, test, lint, local dev, local DB, or real product smoke
  behavior.
- `move`: Launchplane lifecycle behavior that should become or already is a
  driver route.
- `delete`: stale compatibility code with no active caller or with a proven
  Launchplane replacement.
- `adapter`: temporary OIDC trigger glue. Prefer the reusable
  `cbusillo/launchplane/.github/actions/launchplane-request` GitHub Action for
  raw Launchplane HTTP calls, then keep only the product-specific payload
  assembly that cannot yet move into a driver route. When Launchplane owns the
  full handoff, product repos should call a Launchplane reusable workflow and
  keep only dispatch inputs, confirmation text, and product-owned build or test
  facts locally.

Odoo artifact publication now follows the reusable workflow shape: tenant repos
own the manual dispatch confirmation and the source workspace, while
`reusable-odoo-artifact-publish.yml` owns the Launchplane publish-input request,
artifact-record request, idempotency keys, and response mapping. The tenant
workflow should not duplicate `/v1/drivers/odoo/artifact-publish-inputs` or
`/v1/drivers/odoo/artifact-publish` wiring once it uses that workflow. The
reusable workflow defaults the Launchplane product key to
`odoo-tenant-${context}` with underscores normalized to dashes, so publish
metadata resolves through the tenant product profile that owns the image
repository and stable lanes. Reusable Odoo workflows read the Launchplane service
URL from `LAUNCHPLANE_PUBLIC_URL` by default and derive the GitHub OIDC audience
from that URL host unless the caller passes an explicit `launchplane_audience`
input. The reusable jobs run on GitHub-hosted runners because they call the
deployed Launchplane service over HTTPS; product repos do not need direct access
to Launchplane self-hosted runners, and privileged provider mutations still run
inside the Launchplane service boundary.

Odoo testing deploys follow the same ownership shape. Tenant repos own the
manual dispatch confirmation and pass an explicit stored `artifact_id` plus
`source_git_ref` into `reusable-odoo-testing-deploy.yml`; the reusable workflow
calls `/v1/drivers/odoo/target-replacement-apply` with product
`odoo-tenant-${context}` by default. The Launchplane service owns the provider
mutation, runtime identity injection, Odoo post-deploy extension, stable
readiness checks, deployment and inventory records, and the testing release
tuple.

Start with low-risk deletions and documentation, then replace active workflow
behavior in small slices. Do not remove active backup, promotion, rollback,
runtime health, or cleanup safety gates until Launchplane owns the equivalent
behavior and tests.

Registry artifact cleanup is a Launchplane-owned liveness question. Product
repos may still perform provider-specific registry deletion, but they must first
load Launchplane's protected artifact inventory, validate the response, and
abort without deleting anything when the inventory is unavailable, unauthorized,
or has unresolved live-artifact warnings for the registry being cleaned. Product
cleanup jobs should treat Launchplane-protected image references and artifact
ids as a deny set; they must not infer that testing, production, or active
preview artifacts are deletable from local tag shape alone.

Cleanup consumers must check both `artifact_ids` and `image_references` from the
protected inventory. Some active-preview protections come from ready PR feedback
records that carry immutable and refresh image references but no artifact id, so
an artifact-id-only cleanup filter can still delete a live preview tag. Whole-
product cleanup callers should request `GET /v1/artifacts/protected?product=...`
with an `artifact_protection.read` grant that allows wildcard context for that
product; context-specific cleanup may pass `context=` and use a matching scoped
grant.

## Reusable Launchplane Request Action

Product workflows that only need to send JSON to an existing Launchplane route
should not carry their own GitHub OIDC transport client. Use the Launchplane
repo action instead:

```yaml
- name: Request Launchplane preview refresh
  uses: cbusillo/launchplane/.github/actions/launchplane-request@main
  with:
    launchplane-url: ${{ vars.LAUNCHPLANE_URL }}
    audience: ${{ vars.LAUNCHPLANE_AUDIENCE }}
    route-path: /v1/drivers/generic-web/preview-refresh
    payload-file: ${{ runner.temp }}/launchplane-preview-refresh.json
    idempotency-key: >-
      generic-web-preview-refresh:${{ github.event.number }}:${{ github.sha }}
    timeout-ms: "900000"
    output-paths: >-
      refresh_status=result.refresh_status,
      application_id=result.application_id,
      preview_url=result.preview_url,
      error_message=result.error_message
```

The preview refresh payload should identify the product, preview slug, immutable
image reference, and optional PR metadata. New product workflows should omit
`preview_url`; Launchplane derives the live URL from the product preview context
and `LAUNCHPLANE_PREVIEW_BASE_URL`. `preview_url` is reserved as a compatibility
override for older callers.

The action requests a GitHub OIDC token, sends the JSON request with a stable
`Idempotency-Key`, exposes the raw response body, and can map response JSON paths
to GitHub outputs. When a later product step needs a JSON file instead of a
scalar output, set `response-output-file` and, optionally,
`response-output-path` to write the full response or a nested response value.
Use `payload-fields` for small workflow-input overlays instead of a repo-local
JSON builder when the base request is already static:

```yaml
payload: >-
  {"schema_version":1,"product":"odoo","rollback":{"schema_version":1}}
payload-fields: |-
  rollback.context=cm
  rollback.instance=prod
  rollback.reason=${{ github.event.inputs.reason }}
```

Each `payload-fields` line is `json.path=value`. Values are strings unless they
parse as JSON literals or objects, so `false`, `300`, and `{}` keep their JSON
types. Use `payload-json-files` when a workflow already has a JSON artifact file
and only needs to splice it into a static Launchplane request:

```yaml
payload: >-
  {"schema_version":1,"product":"odoo","publish":{"schema_version":1}}
payload-fields: |-
  publish.context=cm
  publish.instance=${{ github.event.inputs.instance }}
payload-json-files: |-
  publish.manifest=${{ steps.publish.outputs.manifest_file }}
```

Each `payload-json-files` line is `json.path=file-path`; the action parses the
file as JSON and writes that value into the request before sending it.

For asynchronous Launchplane routes that report a temporary status, configure
polling instead of reimplementing OIDC and retry logic in the product repo:

```yaml
- name: Request Launchplane backup gate
  uses: cbusillo/launchplane/.github/actions/launchplane-request@main
  with:
    launchplane-url: ${{ vars.LAUNCHPLANE_URL }}
    audience: ${{ vars.LAUNCHPLANE_AUDIENCE }}
    route-path: /v1/drivers/verireel/prod-backup-gate
    payload: ${{ steps.backup_payload.outputs.payload }}
    idempotency-key: ${{ steps.backup_payload.outputs.idempotency_key }}
    poll-result-path: result.backup_status
    poll-result-statuses: pending
    poll-interval-ms: "30000"
    poll-timeout-ms: "2400000"
    fail-result-paths: result.backup_status
    output-paths: >-
      backup_status=result.backup_status,
      snapshot_name=result.snapshot_name,
      backup_gate_record_id=records.backup_gate_record_id
```

Polling repeats the same idempotent request while the configured JSON path
matches a polling status. After polling finishes, the normal fail-result and
output mapping rules still apply.

## New Repo Checklist

When creating a new website repo for Launchplane:

- Build the app as a normal product repo first.
- Add a health endpoint that returns enough non-secret version data for
  Launchplane to verify the deployed artifact. New products should expose the
  Launchplane runtime identity env payload from `LAUNCHPLANE_RUNTIME_IDENTITY_JSON`
  or the equivalent discrete env keys, then mark lanes as requiring runtime
  identity after the echo is verified.
- Publish immutable container images or artifacts from GitHub Actions.
- Apply an operator-owned Launchplane product onboarding manifest to seed the
  product profile, lane profiles, target records, runtime environment, disabled
  managed secret binding placeholders, and then update DB-backed authz policy in
  Launchplane.
- Use `generic-web` directly when the product is a stateless or mostly
  stateless web app with standard preview/deploy behavior.
- Use the Dokploy service deployment contract when the product is a simple bot
  or worker service whose deployment can be represented as a single immutable
  image, one Dokploy application per lane, Launchplane-managed runtime
  settings/secrets, and an optional health endpoint.
- Add a product driver only when the product has named extra obligations such as
  database bootstrap, data migration, backup gates, restore/rollback behavior,
  product smoke checks, or platform-specific post-deploy actions.
- Keep Launchplane lifecycle config out of the product repo unless this document
  or a driver-specific doc explicitly names a temporary compatibility exception.
