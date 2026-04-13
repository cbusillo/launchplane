---
title: Architecture
---

## Purpose

- Keep long-term release ownership out of code and local-DX repos.
- Make artifact identity and promotion records first-class control-plane data.
- Own promotion and deploy orchestration behind explicit contracts.

## Repo Boundary

`odoo-control-plane` owns:

- artifact manifests
- backup-gate records
- promotion records
- deployment records
- environment inventory
- promotion execution
- deploy orchestration
- backup and restore control-plane workflows
- control-plane-owned operator secrets for deploy/runtime orchestration

Code and local-DX repos own:

- addon code
- local developer workflows
- Odoo-specific validation
- explicit artifact and operator handoff surfaces only

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
- Native ship requests are artifact-backed from the start and no longer
  carry branch-sync metadata through the handoff or execution path.
- When the control plane cannot resolve a stored artifact manifest for ship
  execution, it fails closed instead of falling back to branch-sync or
  repo/tag image selection.
- Any upstream handoff seam must fail closed when this repo cannot accept
  control.
- Any temporary compatibility bridge should stay explicit and removable; native
  tenant/devkit convenience commands are fine when the ownership boundary stays
  clear.
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
