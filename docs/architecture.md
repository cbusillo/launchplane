---
title: Architecture
---

## Purpose

- Keep long-term release ownership out of code and local-DX repos.
- Make artifact identity and promotion records first-class control-plane data.
- Own promotion, deploy, and preview orchestration behind explicit contracts.

This repo is the current Odoo implementation of the Harbor operator surface.
The contracts documented here now need to serve two jobs at once: describe the
implemented Odoo control-plane behavior that exists today, and describe the
target Harbor boundary that future cross-product work should aim at. Harbor is
still implemented inside `odoo-control-plane` today, but the target shape is a
long-running Harbor service rather than a permanently repo-local CLI.

## Repo Boundary

`odoo-control-plane` owns:

- artifact manifests
- release tuple catalogs
- backup-gate records
- promotion records
- deployment records
- environment inventory
- promotion execution
- deploy orchestration
- backup and restore control-plane workflows
- control-plane-owned operator secrets for deploy/runtime orchestration
- Harbor preview and generation records

Code and local-DX repos own:

- addon code
- local developer workflows
- Odoo-specific validation
- explicit artifact and operator handoff surfaces only

GitHub owns the engineering workflow around this system: issues, branches,
pull requests, labels, checks, PR comments, releases, and CI execution.

This repository is not the generic Harbor product boundary today. It is the
Odoo-specific control plane that currently contains Harbor preview and
promotion behavior.

## Target Harbor Shape

- Harbor should become a long-running control-plane service, expected to live
  behind a stable address such as `harbor.shinycomputers.com`.
- Harbor should expose authenticated service ingress for runtime evidence,
  operator actions, and eventually driver-triggered orchestration.
- GitHub Actions OIDC should be the default machine-to-machine authentication
  boundary for product workflows talking to Harbor.
- Harbor should authorize workflow callers from GitHub-issued identity claims
  such as repository, workflow, ref, environment, and event context, rather
  than from copied long-lived static tokens.
- Harbor core should own durable records, operator read models, auditability,
  and shared orchestration contracts.
- Product-specific runtime logic should live behind Harbor-owned drivers,
  starting with Odoo and VeriReel, instead of being duplicated as near-identical
  scripts across many client repos.
- Repo-specific variation should enter Harbor as thin repo extensions,
  declarative config, or small driver inputs, not as a full second copy of the
  same operational workflow in every product repo.

## Harbor Shape Today

- Stable remote environment lanes are `testing` and `prod` only.
- Harbor currently lives inside `odoo-control-plane`; there is no separate
  extracted Harbor repo or package contract yet.
- The CLI and file-backed state directory remain the current implementation
  surface, but they should be treated as temporary local scaffolding around the
  target Harbor service boundary rather than as the final cross-product ingress
  contract.
- PR previews are Harbor-managed preview identities backed by separate preview
  generations and ephemeral preview runtime state, not extra long-lived Dokploy
  lanes.
- The tracked Dokploy route catalog is therefore limited to stable tenant lanes
  rather than acting as a registry for every preview or ad hoc environment.
- Durable control-plane records use generic deployment nouns when the concept
  is reusable across products, but Odoo-specific runtime behavior remains
  explicit in the Odoo workflow code and deploy evidence.

## Harbor Core And Drivers

Harbor should converge on three layers:

```text
Harbor core
  - API and operator UI
  - GitHub OIDC authentication and authorization
  - durable records and audit log
  - read models and operator views
  - shared orchestration engine

Harbor drivers
  - odoo driver
  - verireel driver
  - future product drivers

Repo extensions
  - product/repo inputs
  - optional repo-specific config
  - small hooks only when genuinely needed
```

The intent is to keep common operational behavior centralized in Harbor while
still leaving room for product-specific execution differences. A driver lives
in Harbor. A repo extension only supplies the minimum extra information a
specific repo needs.

## Ingress And Trust

- The canonical Harbor ingress should be authenticated HTTP, not repo-to-repo
  shelling into a CLI as the long-term contract.
- GitHub Actions workflows should authenticate with Harbor using OIDC-issued
  identity from GitHub.
- Harbor should map those claims to allowed products, contexts, actions, and
  environments. Example: a VeriReel preview workflow may be allowed to write
  preview evidence for `verireel-testing`, while a promotion workflow may be
  allowed to write promotion evidence for production lanes.
- Human/operator access in Harbor may still use a separate auth layer, but
  machine evidence ingress should trust workflow identity first.
- The stable cross-product contract is the typed Harbor API payload, not the
  particular client used to submit it.

## Current Contract

- `promote` accepts the native artifact-backed promotion contract and uses this
  repo's own ship-request resolution while this repo owns the promotion record
  and the live ship execution boundary.
- Direct `ship` ownership also enters through this repo, and Dokploy target
  resolution, credentials, and trigger/wait execution run here.
- The tracked Dokploy route catalog lives in this repo under
  `config/dokploy.toml` by default, with an explicit
  `ODOO_CONTROL_PLANE_DOKPLOY_SOURCE_FILE` override for alternate operator
  paths.
- The tracked Dokploy route catalog is limited to stable remote lanes
  (`testing`, `prod`). Pull-request previews flow through Harbor preview
  records instead of tracked Dokploy lane entries.
- Harbor baseline release tuples belong here as explicit control-plane data.
  Tuple entries carry exact repo SHAs for preview-manifest resolution, not
  floating branch names.
- Successful waited `ship` executions for long-lived lanes mint current release
  tuple records from stored artifact manifests when the manifest carries exact
  split-repo SHAs.
- Promotion execution requires the source lane's current release tuple to match
  the requested artifact, then writes the destination tuple from that same
  source tuple after the deploy passes.
- The tracked `config/release-tuples.toml` now records the active split-repo
  artifact-backed baseline for CM and OPW stable lanes. Future tracked
  baseline changes should come from reviewed state-backed tuple evidence rather
  than legacy monorepo branch heads.
- Live Dokploy `target_id` values load from operator-local
  `config/dokploy-targets.toml` by default, with an explicit
  `ODOO_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE` override for alternate local
  paths.
- The Odoo-specific compose post-deploy update runs natively here via
  a control-plane-owned Dokploy schedule workflow, so deploy execution no
  longer shells back into another repo at runtime.
- That post-deploy path is also the first real candidate for an eventual Odoo
  driver seam in Harbor: it is a product-specific runtime call pattern already
  owned end to end by this control plane, without forcing broader runtime
  abstraction ahead of evidence.
- Deployment records persist post-deploy update evidence as first-class
  control-plane state instead of hiding that work behind another repo's CLI.
- Current environment inventory is also persisted here and refreshed by
  successful waited `ship`/`promote` flows, so this repo owns both append-only
  deploy history and the replace-in-place current-state view.
- That same inventory view can now also be refreshed from stored external
  promotion evidence when Harbor has both a promotion record and explicit
  linked deployment record, which keeps second-product onboarding evidence-
  first instead of forcing Harbor to own runtime execution on day one.
- Harbor preview records now support the same posture for preview runtime: the
  live preview route can be supplied as explicit evidence, and preview plus
  generation state can be refreshed from external workflow results without
  requiring Harbor to provision the preview itself first.
- Harbor now has the matching cleanup-evidence path too, so an external product
  can report confirmed preview teardown into the same durable preview identity
  without Harbor claiming it executed that teardown itself.
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

- Persist records to a local state directory today.
- Keep storage pluggable, but start with file-backed JSON while Harbor still
  lives inside this repo.
- Treat the current CLI and local JSON layout as implementation scaffolding,
  not as the final communication contract for external products.
- New cross-product integrations should target the future Harbor service
  boundary in design, even if a temporary local adapter is still required
  during migration.
