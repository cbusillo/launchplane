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
- Launchplane runtime identity echo from `LAUNCHPLANE_RUNTIME_IDENTITY_JSON`
  before the lane is marked strict for runtime identity verification
- documented required runtime environment keys
- product-specific smoke check command when generic health is not enough

For most web products, `generic-web` can use this contract directly from the
DB-backed product profile.

Simple service products, such as bots or workers deployed as Dokploy
applications, can also use `generic-web` when their lifecycle is image deploy,
optional health verification, and Launchplane-owned provider mutation. See
[dokploy-service-deployments.md](dokploy-service-deployments.md) for the
service-specific contract, including persistent volumes and internal ports such
as Discord Blue's Every Code bridge port `8787`.

## Launchplane Records

Onboarding still requires the [existing admin operation](operations.md#conventional-generic-web-onboarding) to create the product
profile and target records. That is an existing compatibility operation, not a
product-repository integration contract; no deployed service replacement is
claimed here. Keep its inputs in admin-owned state, outside the product
repository and git-tracked history when they contain site-specific runtime
details.

After a product repository is archived or its onboarding GitHub App installation
is gone, preserve the existing `Generic Web Preview Authorization`
`operation=retire` cleanup until the grant retirement and readback complete. It
resolves immutable authority from the active product preview rules and does not
require a repository lookup. Do not remove that cleanup merely because new
products have no caller grant.

New-product build and handoff requirements are canonical in
[artifact provenance](artifact-provenance.md) and
[event-driven deploys](event-driven-deploys.md).

## GitHub Actions Shape

Start with these workflows:

- CI: lint, test, build, and product-owned checks.
- Security: causal pull-request dependency checks plus absolute default-branch
  and artifact health appropriate for the repo. See
  [dependency-health-contract.md](dependency-health-contract.md).
- Build: `.github/workflows/build.yml`, run on `push` to the default branch and
  on `pull_request`, builds and pushes an immutable image and uploads the
  `artifact-manifest-<run_attempt>` Actions artifact described in
  [artifact-provenance.md](artifact-provenance.md).

A product repository never calls Launchplane (see
[DIRECTION.md](../DIRECTION.md)): it holds no Launchplane workflow reference,
OIDC request, grant, secret, or build setting. Launchplane hears the build and
pull request events itself, verifies the build, and deploys previews and the
`testing` lane; see [event-driven-deploys.md](event-driven-deploys.md).

Product-owned smoke tests manage their own dynamic fixtures. They do not use a
Launchplane maintenance route or a generated Launchplane smoke client.

## Choose A Driver

Use `generic-web` when the product is a stateless or mostly stateless web app,
or a simple service deployed as a Dokploy application, whose lifecycle is image
deploy, health check, preview refresh when enabled, preview cleanup when
enabled, and PR feedback.

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

- For repositories using the train, verify the intended PR authors against the
  active [merge-train policy](merge-train-policy.md). Inspect a controller dry-run
  against real open PRs when the target and required access are configured.
  The GitHub adapter evaluates the PR author, not the ready-label actor;
  passing CI and a ready label do not admit an unrecognized author. A missing
  ready label alone is expected before enqueueing. Report an unconfigured target
  or unavailable author evidence separately from product deploy readiness.
- Verify the agent's GitHub App installation coverage and permissions for its
  operations using the shared
  [GitHub capability profile](https://github.com/cbusillo/codex-skills/blob/main/skills/github/references/github-permissions.md),
  including dispatch and rerun where used. Resolve the train's configured
  credential source first: it may use an App, an environment variable, or a
  runtime context. Where Apps are used, resolve their actual IDs before changing
  permissions; agent and train may share an installation. Verify the train's
  [credential contract](merge-train-policy.md#credential-source-and-policy-readback);
  its installation minimums and exact issued token scope are separate. Check
  other consumers' installation limits before expanding shared grants. App
  permissions cover the installation's repositories; train author allowlists are separate
  repository/base records. Apply existing authorization that covers this task
  and scope. Present any additional permission or repository-coverage grants
  together for Director approval
  under [DIRECTION.md](../DIRECTION.md#stop-boundaries), then use each system's
  supported change path. Author-policy changes use the
  [managed policy import](merge-train-policy.md#admin-changes); check all
  targets for in-flight work because their records bind the full policy digest.
- CI and pull-request dependency regression checks pass, and the current
  default-branch/artifact absolute health evidence is acceptable.
- The image or artifact is immutable and traceable to a source SHA.
- Launchplane can read the product profile and target records.
- A non-prod deploy or preview path has been exercised through Launchplane.
- Product workflows do not mutate providers directly.
- Product workflows do not render Launchplane evidence or PR feedback markdown.
- Any remaining Launchplane adapter scripts are small, temporary, and listed as
  migration candidates.
