---
title: Architecture
---

## Purpose

- Keep long-term release ownership out of code and local-DX repos.
- Make artifact identity and promotion records first-class control-plane data.
- Own promotion, deploy, and preview orchestration behind explicit contracts.
- Own the authoritative delivery evidence and admission decisions required to
  move exact changes into managed environments.

This repo is the Launchplane implementation and operator surface. Odoo was the
first product proving ground, and VeriReel is now the second product proof, but
the durable boundary is Launchplane: an audited, forge-neutral product-delivery
control plane with DB-backed records, authenticated forge ingress, product
drivers, provider calls, admission evidence, and operator read models.

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
- authoritative Owner acceptance, engineering-review evidence, merge admission,
  dependency health, and the exact-change/dependency state needed to decide
  whether a change may enter a managed environment

Product, tenant, and local-DX repos own:

- product and addon source code
- product tests and build definitions
- local developer workflows
- explicit artifact/source inputs
- thin OIDC-authenticated Launchplane request wrappers
- product verification that must run next to source or browser context

An external forge owns source hosting and engineering collaboration: Git
storage and transport, branches, pull requests, diffs, issues, labels, checks,
comments, releases, CI execution, runners, and package hosting. GitHub is the
current forge adapter, not Launchplane's permanent product boundary. Future
adapters may target an open-source or hosted forge without moving Launchplane's
delivery authority back into the forge.

Product/system human ownership is represented by additive Launchplane records.
Owner membership, Owner requirements, and preferred routing are separate
revision streams. Owner requirements and exact-change acceptance are authoritative
for Launchplane merge readiness; preferred routing cannot grant authority and
production authorization remains separate. See
`docs/product-owner-policy.md`.

Launchplane does not become a Git host, general issue or project-planning
system, engineering work queue, CI runner, package registry, generic provider
administration console, or second source of runtime truth. Keep reusable nouns
in Launchplane core, product-specific runtime behavior in Launchplane drivers,
forge/provider variation behind replaceable adapters, and repo-specific
variation in thin request/config surfaces.

## Target Launchplane Shape

- Launchplane should become a long-running control-plane service, expected to live
  behind an operator-owned stable address.
- Launchplane should expose authenticated service ingress for runtime evidence,
  operator actions, and eventually driver-triggered orchestration.
- Forge-issued workload identity should be the default machine-to-machine
  authentication boundary for product workflows talking to Launchplane.
  GitHub Actions OIDC is the first adapter for that contract.
- Launchplane should authorize workflow callers from verified forge identity
  claims such as repository, workflow, ref, environment, and event context,
  rather than from copied long-lived static tokens.
- Launchplane core should own durable records, operator read models, auditability,
  and shared orchestration contracts.
- Launchplane should own forge-neutral Owner, review, dependency, admission, and
  landing evidence while projecting checks, comments, and merge operations
  through replaceable forge adapters.
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
- Launchplane runs as a shared service with Postgres-backed operational truth.
- The CLI and file-backed state directory remain transitional local-development,
  test, rehearsal, and emergency scaffolding. They are not product surfaces or
  production authority and must be removed after their required capabilities
  move into service contracts, read models, narrow recovery paths, or generic
  test infrastructure.
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
  GHCR, delegated worker hosts, public health endpoints, or backup storage.
- `forge adapter`: replaceable integration for source identity, change events,
  checks, comments, and guarded merge operations. The forge owns collaboration;
  Launchplane owns delivery authority and evidence.
- `repo extension`: the minimal source-adjacent wrapper, manifest, or workflow
  input that lets a product repo ask Launchplane to act without owning durable
  runtime truth.

## Launchplane Core And Drivers

Launchplane should converge on three layers:

```text
Launchplane core
  - API and operator UI
  - forge-neutral authentication and authorization
  - durable records and audit log
  - read models and operator views
  - shared orchestration engine
  - delivery governance and admission evidence

Launchplane drivers
  - odoo driver
  - verireel driver
  - future product drivers

Forge and provider adapters
  - GitHub today
  - future open-source or hosted forge adapters
  - Dokploy, GHCR, health, and backup providers

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
- Forge workflows should authenticate with Launchplane using verified workload
  identity. GitHub Actions currently supplies that identity through OIDC.
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
- Launchplane owns provider-neutral environment route-binding records keyed by
  product/context/instance. These records describe desired domains, runtime
  target summary, ingress termination, and TLS ownership while keeping
  provider-specific host ids, certificate ids, target ids, edge addresses, and
  provider payloads as evidence rather than neutral authority.
- Ship execution prefers immutable artifact image references at runtime by
  syncing `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` to Dokploy whenever a stored
  artifact manifest is available.
- `odoo-devkit` is the expected build/publish handoff for those manifests: it
  stages the tenant and shared source inputs into a real downstream image
  build context, pushes the image, resolves the pushed digest, and emits JSON
  for local `artifacts write` rehearsal here.
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
- Do not add product capability to file-backed JSON, local CLI writers,
  compatibility readers, or transitional GitHub credential/workflow paths.
  Move required testing, rehearsal, diagnostics, and recovery into retained
  boundaries, then delete the obsolete code, docs, commands, and tests.
- New cross-product integrations should target the Launchplane service boundary
  and a forge workload-identity adapter, not repo-local CLI mutation.
- Use SQLAlchemy ORM models plus Alembic migrations for shared-service schema
  changes. Compatibility `ensure_schema()` paths are for local, test, and
  bootstrap tolerance, not the production migration strategy.
- Keep schema changes backward-compatible enough that deploy rollback can safely
  return to the previous Launchplane image when possible.

## Driver Ownership Status

Product repos keep source, build, verification, and thin OIDC request wrappers,
while Launchplane owns the durable service routes, DB-backed records,
managed-secret/runtime authority, driver execution, and operator read models for
the current Odoo and VeriReel deployment, promotion, rollback, backup, and
preview paths.

Future driver work should be incremental capability expansion behind the same
service/read-model contract, not a second migration track.

Compatibility paths are deletion-bound, not a permanent architecture layer.
Production-capable mutation paths must use typed Launchplane service routes with
DB-backed authority or explicit operator input. Any required local-development,
test, rehearsal, diagnostic, or recovery capability must migrate into generic
test infrastructure, supported read models, operator APIs, or narrow
root-of-trust recovery paths before the obsolete compatibility implementation is
deleted.
