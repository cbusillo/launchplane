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
  - health, readiness, inventory, cleanup, and feedback records
  - PR feedback rendering and delivery
```

The product repo should not carry Launchplane lifecycle truth in TOML, JSON,
checked-in fixtures, or copied ops scripts. Product and lane configuration lives
in Launchplane DB-backed records.

## What Product Repos Own

- Application source code and product-owned business behavior.
- Product dependencies, lockfiles, and package/build tooling.
- Dockerfile or image build contract.
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
- Provider mutations: create/update/delete preview apps, deploy stable lanes,
  promote, rollback, capture backup gates, and cleanup stale runtime state.
- Readiness checks before provider mutation.
- Health checks when they are based on profile-owned health paths and expected
  revisions or image references.
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
- `adapter`: temporary OIDC trigger glue that should shrink or move into a
  reusable Launchplane GitHub Action/CLI.

Start with low-risk deletions and documentation, then replace active workflow
behavior in small slices. Do not remove active backup, promotion, rollback, or
cleanup safety gates until Launchplane owns the equivalent behavior and tests.

## New Repo Checklist

When creating a new website repo for Launchplane:

- Build the app as a normal product repo first.
- Add a health endpoint that returns enough non-secret version data for
  Launchplane to verify the deployed artifact.
- Publish immutable container images or artifacts from GitHub Actions.
- Seed the product profile, lane profiles, target records, runtime environment,
  managed secrets, and authz policy in Launchplane.
- Use `generic-web` directly when the product is a stateless or mostly
  stateless web app with standard preview/deploy behavior.
- Add a product driver only when the product has named extra obligations such as
  database bootstrap, data migration, backup gates, restore/rollback behavior,
  product smoke checks, or platform-specific post-deploy actions.
- Keep Launchplane lifecycle config out of the product repo unless this document
  or a driver-specific doc explicitly names a temporary compatibility exception.
