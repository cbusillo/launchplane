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
  assembly that cannot yet move into a driver route.

Start with low-risk deletions and documentation, then replace active workflow
behavior in small slices. Do not remove active backup, promotion, rollback,
runtime health, or cleanup safety gates until Launchplane owns the equivalent
behavior and tests.

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
to GitHub outputs. Product repos may still need small payload-builder scripts
until Launchplane owns the next layer of product-specific request assembly.

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
  or the equivalent discrete env keys.
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
