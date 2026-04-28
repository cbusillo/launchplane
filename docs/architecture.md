---
title: Architecture
---

## Purpose

- Keep long-term release ownership out of code and local-DX repos.
- Make artifact identity and promotion records first-class control-plane data.
- Own promotion, deploy, and preview orchestration behind explicit contracts.

This repo is the Launchplane implementation and operator surface. Odoo was the
first product proving ground, and VeriReel is now the second product proof, but
the durable boundary is Launchplane: a long-running service with DB-backed
records, GitHub OIDC ingress, product drivers, provider calls, and operator read
models.

## Repo Boundary

`launchplane` owns:

- service API and GitHub OIDC authn/authz
- artifact manifests
- release tuple records
- backup-gate records
- promotion records
- deployment records
- environment inventory
- promotion and deploy execution
- backup, restore, and rollback workflows
- Launchplane-managed secrets for deploy/runtime orchestration
- Launchplane preview and generation records
- product drivers for Odoo and VeriReel
- provider integrations for Dokploy, GHCR, GitHub, health, and backups

Product, tenant, and local-DX repos own:

- product and addon source code
- product tests and build definitions
- local developer workflows
- explicit artifact/source inputs
- thin OIDC-authenticated Launchplane request wrappers
- product verification that must run next to source or browser context

GitHub owns the engineering workflow around this system: issues, branches,
pull requests, labels, checks, PR comments, releases, and CI execution.

This repository is the product boundary today. Keep reusable nouns in
Launchplane core, product-specific runtime behavior in Launchplane drivers, and
repo-specific variation in thin request/config surfaces.

## Target Launchplane Shape

- Launchplane should become a long-running control-plane service, expected to live
  behind a stable address such as `launchplane.shinycomputers.com`.
- Launchplane should expose authenticated service ingress for runtime evidence,
  operator actions, and eventually driver-triggered orchestration.
- GitHub Actions OIDC should be the default machine-to-machine authentication
  boundary for product workflows talking to Launchplane.
- Launchplane should authorize workflow callers from GitHub-issued identity claims
  such as repository, workflow, ref, environment, and event context, rather
  than from copied long-lived static tokens.
- Launchplane core should own durable records, operator read models, auditability,
  and shared orchestration contracts.
- Product-specific runtime logic should live behind Launchplane-owned drivers,
  starting with Odoo and VeriReel, instead of being duplicated as near-identical
  scripts across many client repos.
- Repo-specific variation should enter Launchplane as thin repo extensions,
  declarative config, or small driver inputs, not as a full second copy of the
  same operational workflow in every product repo.
- When a product-specific operation needs materially different network reach or
  host-local authority, Launchplane should prefer a narrow delegated worker
  contract over teaching the main API host to absorb every privileged runtime
  concern directly.

## Launchplane Shape Today

- Stable remote environment lanes are `testing` and `prod` only.
- Launchplane runs as the shared service behind
  `launchplane.shinycomputers.com` with Postgres-backed operational truth.
- The CLI and file-backed state directory are local-development, test, and
  emergency operator scaffolding around the service boundary. They are not the
  production integration contract for product workflows.
- PR previews are Launchplane-managed preview identities backed by separate preview
  generations and ephemeral preview runtime state, not extra long-lived Dokploy
  lanes.
- The tracked Dokploy route catalog is therefore limited to stable tenant lanes
  rather than acting as a registry for every preview or ad hoc environment.
- Durable control-plane records use generic deployment nouns when the concept
  is reusable across products, but Odoo-specific runtime behavior remains
  explicit in the Odoo driver code and deploy evidence.

## Vocabulary

- `Launchplane core`: service API, authn/authz, durable records, audit,
  idempotency, inventory, read models, and shared orchestration contracts.
- `product driver`: Launchplane-owned executable product behavior, such as Odoo
  post-deploy/update, Odoo backup/promotion/rollback, VeriReel deploy,
  maintenance, promotion, rollback, and preview lifecycle operations.
- `provider`: an external execution or data system such as Dokploy, GitHub,
  GHCR, Proxmox, public health endpoints, or backup storage.
- `repo extension`: the minimal source-adjacent wrapper, manifest, or workflow
  input that lets a product repo ask Launchplane to act without owning durable
  runtime truth.

## Launchplane Core And Drivers

Launchplane should converge on three layers:

```text
Launchplane core
  - API and operator UI
  - GitHub OIDC authentication and authorization
  - durable records and audit log
  - read models and operator views
  - shared orchestration engine

Launchplane drivers
  - odoo driver
  - verireel driver
  - future product drivers

Repo extensions
  - product/repo inputs
  - optional repo-specific config
  - small hooks only when genuinely needed
```

The intent is to keep common operational behavior centralized in Launchplane while
still leaving room for product-specific execution differences. A driver lives
in Launchplane. A repo extension only supplies the minimum extra information a
specific repo needs.

The first concrete HTTP/OIDC/API shape for that boundary is defined in
[`service-boundary.md`](service-boundary.md).

## Ingress And Trust

- The canonical Launchplane ingress should be authenticated HTTP, not repo-to-repo
  shelling into a CLI as the long-term contract.
- GitHub Actions workflows should authenticate with Launchplane using OIDC-issued
  identity from GitHub.
- Launchplane should map those claims to allowed products, contexts, actions, and
  environments. Example: a VeriReel preview workflow may be allowed to write
  preview evidence for `verireel-testing`, while a promotion workflow may be
  allowed to write promotion evidence for production lanes.
- Human/operator access in Launchplane may still use a separate auth layer, but
  machine evidence ingress should trust workflow identity first.
- The stable cross-product contract is the typed Launchplane API payload, not the
  particular client used to submit it.

## Current Contract

- `promote` accepts the native artifact-backed promotion contract and uses this
  repo's own ship-request resolution while this repo owns the promotion record
  and the live ship execution boundary.
- Direct `ship` ownership also enters through this repo, and Dokploy target
  resolution, credentials, and trigger/wait execution run here.
- The tracked Dokploy route catalog resolves from Launchplane DB-backed target
  records plus DB-backed target-id records.
- The tracked Dokploy route catalog is limited to stable remote lanes
  (`testing`, `prod`). Pull-request previews flow through Launchplane preview
  records instead of tracked Dokploy lane entries.
- Launchplane baseline release tuples belong here as explicit control-plane data.
  Tuple entries carry exact repo SHAs for preview-manifest resolution, not
  floating branch names.
- Successful waited `ship` executions for long-lived lanes mint current release
  tuple records from stored artifact manifests when the manifest carries exact
  split-repo SHAs.
- Promotion execution requires the source lane's current release tuple to match
  the requested artifact, then writes the destination tuple from that same
  source tuple after the deploy passes.
- The active split-repo artifact-backed baseline for CM and OPW stable lanes
  now resolves from DB-backed release-tuple records. Any exported release-
  tuple catalog is seed material rather than live runtime authority.
- Live Dokploy `target_id` values load from Launchplane DB-backed target-id
  records.
- The Odoo-specific compose post-deploy update runs natively here via
  a control-plane-owned Dokploy schedule workflow, so deploy execution no
  longer shells back into another repo at runtime.
- That post-deploy path is also the first real candidate for an eventual Odoo
  driver seam in Launchplane: it is a product-specific runtime call pattern already
  owned end to end by this control plane, without forcing broader runtime
  abstraction ahead of evidence.
- Deployment records persist post-deploy update evidence as first-class
  control-plane state instead of hiding that work behind another repo's CLI.
- Current environment inventory is also persisted here and refreshed by
  successful waited `ship`/`promote` flows, so this repo owns both append-only
  deploy history and the replace-in-place current-state view.
- That same inventory view can now also be refreshed from stored external
  promotion evidence when Launchplane has both a promotion record and explicit
  linked deployment record, which keeps second-product onboarding evidence-
  first instead of forcing Launchplane to own runtime execution on day one.
- Launchplane preview records now support the same posture for preview runtime:
  the live preview route can be supplied as explicit evidence, and preview plus
  generation state can be refreshed from external workflow results without
  requiring Launchplane to provision the preview itself first.
- Launchplane now has the matching cleanup-evidence path too, so an external product
  can report confirmed preview teardown into the same durable preview identity
  without Launchplane claiming it executed that teardown itself.
- Ship execution prefers immutable artifact image references at runtime by
  syncing `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` to Dokploy whenever a stored
  artifact manifest is available.
- `odoo-devkit` is the expected build/publish handoff for those manifests: it
  stages the tenant and shared source inputs into a real downstream image
  build context, pushes the image, resolves the pushed digest, and emits JSON
  for `artifacts write` / `artifacts ingest` here.
- Artifact-backed execution also rejects Dokploy targets that still depend on
  the legacy `odoo-ai` monorepo source or mutable addon repository refs.
- Native ship requests are artifact-backed and do not carry branch-mutation
  metadata through the handoff or execution path.
- When the control plane cannot resolve a stored artifact manifest, ship
  execution fails closed.
- Upstream handoffs fail closed when this repo cannot accept control.
- Immutable promotion ownership includes validating a stored backup-gate
  record for the destination environment before ship execution begins.
- Operator-facing status/history reads should also terminate here by composing
  inventory, deployment, promotion, and backup-gate records into a control-
  plane-owned read model.
- Planning-time ship request rendering, Dokploy target source-of-truth
  ownership, promotion-request rendering, deploy execution, and compose
  post-deploy update all terminate here.

## Implementation Posture

- Use Postgres-backed storage and managed secrets for shared-service production
  truth.
- Keep file-backed JSON and local CLI writers for local development, focused
  tests, and emergency diagnostics only.
- New cross-product integrations should target the Launchplane service boundary
  and GitHub OIDC, not repo-local CLI mutation.
- Launchplane does not yet have a formal schema migration system.
- Until that migration story exists, schema changes for DB-backed Launchplane state
  should remain additive and backward-compatible so deploy rollback can safely
  return to the previous Launchplane image.

See [compatibility-retirement.md](compatibility-retirement.md) for the checkpoint
rules that decide whether a local CLI/file-backed compatibility path can remain.
