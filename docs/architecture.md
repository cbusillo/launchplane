---
title: Architecture
---

## Purpose

- Keep long-term release ownership out of code and local-DX repos.
- Make artifact identity and promotion records first-class control-plane data.
- Own promotion and deploy orchestration behind explicit contracts.

This repo is the current Odoo implementation of the Harbor operator surface.
The contracts documented here are the implemented Odoo control-plane
boundaries that exist today. Reusable Harbor product direction lives in saved
plans until the same concepts are expressed through generic code and operator
surfaces.

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

## Harbor Shape Today

- Stable remote environment lanes are `testing` and `prod` only.
- Harbor currently lives inside `odoo-control-plane`; there is no separate
  extracted Harbor repo or package contract yet.
- PR previews are Harbor-managed preview identities backed by separate preview
  generations and ephemeral preview runtime state, not extra long-lived Dokploy
  lanes.
- The tracked Dokploy route catalog is therefore limited to stable tenant lanes
  rather than acting as a registry for every preview or ad hoc environment.
- Durable control-plane records use generic deployment nouns when the concept
  is reusable across products, but Odoo-specific runtime behavior remains
  explicit in the Odoo workflow code and deploy evidence.

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
- Deployment records persist post-deploy update evidence as first-class
  control-plane state instead of hiding that work behind another repo's CLI.
- Current environment inventory is also persisted here and refreshed by
  successful waited `ship`/`promote` flows, so this repo owns both append-only
  deploy history and the replace-in-place current-state view.
- Ship execution prefers immutable artifact image references at runtime by
  syncing `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` to Dokploy whenever a stored
  artifact manifest is available.
- `odoo-devkit` is the expected build/publish handoff for those manifests: it
  stages the tenant and shared-addon sources into a real downstream image
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

## Runtime Shape

- Persist records to a local state directory.
- Keep storage pluggable, but start with file-backed JSON.
- Avoid service/API complexity until the workflow boundary is proven.
