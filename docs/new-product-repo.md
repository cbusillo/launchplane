---
title: New Product Repo
---

## Purpose

Use this checklist when creating a website or service repo that will be operated
by Launchplane. The goal is a normal product repo with a thin Launchplane
handoff, not a repo that grows its own control plane.

## Build The Product First

Create the repo around the product's normal development needs:

- application source, tests, and local dev commands
- package manager lockfile and dependency policy
- Dockerfile or artifact build contract
- local-only fixtures, seed data, and development database helpers when needed
- product-specific smoke checks that prove real product behavior

Keep Launchplane lifecycle records, lane topology, provider targets, managed
secrets, and deployment truth out of the repo.

## Runtime Contract

Every Launchplane-operated web product should expose a small runtime contract:

- immutable container image or artifact reference
- known runtime port
- health endpoint path
- non-secret build revision or image tag in the health response
- documented required runtime environment keys
- product-specific smoke check command when generic health is not enough

For most web products, `generic-web` can use this contract directly from the
DB-backed product profile.

## Launchplane Records

Before wiring workflows, seed or verify these records in Launchplane with an
operator-owned onboarding manifest:

```sh
uv run launchplane product-onboarding apply \
  --database-url "$LAUNCHPLANE_DATABASE_URL" \
  --manifest-file state/product-onboarding/<product>.json
```

The manifest is applied idempotently and writes Launchplane-owned records for:

- product profile with product key, owning repo, driver id, image repository,
  runtime port, health path, and preview policy
- lane profiles for stable instances such as `testing` and `prod`
- Dokploy or provider target records and target-id records
- runtime-environment records for non-secret settings
- disabled managed secret binding placeholders for required secret keys

Then import or update DB-backed authz policy records for the product's GitHub
Actions workflows. Authz policy merging remains a separate operator step so a
new product onboarding manifest cannot accidentally replace unrelated product
access rules.

Do not store these as product-repo Launchplane manifests. The repo may document
the expected app runtime contract, but Launchplane records are the live source
of lifecycle truth. Store operator manifests under Launchplane state or another
operator-owned state location, not in product repos and not in git-tracked
history when they contain site-specific runtime details.

## GitHub Actions Shape

Start with these workflows:

- CI: lint, test, build, and product-owned checks.
- Security: dependency and static/security checks appropriate for the repo.
- Publish image: build and publish an immutable artifact, then trigger
  Launchplane stable deploy for `testing`.
- Preview trigger: for PRs that request preview, build and publish an immutable
  preview image, then trigger Launchplane preview refresh.
- Preview cleanup trigger: on PR close or preview label removal, trigger
  Launchplane preview destroy.

The Launchplane trigger steps should use GitHub Actions OIDC and pass minimal
facts only: product key, source ref or SHA, PR number when relevant, immutable
artifact reference, and optional run URL.

## Choose A Driver

Use `generic-web` when the product is a stateless or mostly stateless web app
whose lifecycle is image deploy, health check, preview refresh, preview cleanup,
and PR feedback.

Create a product driver when the product has named extra obligations:

- database migration, clone, bootstrap, seed, or anonymization
- backup gate, restore, rollback, or destructive repair behavior
- product-specific promotion smoke checks
- post-deploy maintenance commands
- platform-specific artifact or runtime semantics

See [driver-development.md](driver-development.md) for the driver workflow and
[product-repo-contract.md](product-repo-contract.md) for the approval gate.

## Before Approval

Before treating the repo as Launchplane-ready:

- CI and security pass.
- The image or artifact is immutable and traceable to a source SHA.
- Launchplane can read the product profile and target records.
- A non-prod deploy or preview path has been exercised through Launchplane.
- Product workflows do not mutate providers directly.
- Product workflows do not render Launchplane evidence or PR feedback markdown.
- Any remaining Launchplane adapter scripts are small, temporary, and listed as
  migration candidates.
