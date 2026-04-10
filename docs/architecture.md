---
title: Architecture
---

## Purpose

- Keep long-term release ownership out of `odoo-ai`.
- Make artifact identity and promotion records first-class control-plane data.
- Own promotion and deploy orchestration behind explicit contracts.

## Repo Boundary

`odoo-control-plane` owns:

- artifact manifests
- promotion records
- deployment records
- environment inventory
- promotion execution
- deploy orchestration
- backup and restore control-plane workflows
- control-plane-owned operator secrets for deploy/runtime orchestration

`odoo-ai` owns:

- addon code
- local developer workflows
- Odoo-specific validation
- thin compatibility wrappers during migration

## Transition Direction

- The first live workflow owned here is `promote`.
- During the current compatibility slice, `promote` uses `odoo-ai` only for
  read-only ship-request export while this repo owns the promotion record and
  the live ship execution boundary.
- Direct `ship` ownership now also enters through this repo, and Dokploy
  target resolution, credentials, and trigger/wait execution now run here.
- Phase 5 closes with a single explicit cross-repo runtime seam: the
  Odoo-specific post-deploy update. That step remains in `odoo-ai` on purpose
  and runs through the canonical `odoo-ai platform update` path rather than a
  hidden compatibility worker.
- Deployment records now persist post-deploy update evidence as well, so the
  remaining seam is visible in control-plane state instead of being implicit in
  process output.
- Current environment inventory is now also persisted here and refreshed by
  successful waited `ship`/`promote` flows, so this repo owns both append-only
  deploy history and the replace-in-place current-state view.
- Compatibility `ship` execution now also prefers immutable artifact image
  references at runtime by syncing `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` to
  Dokploy whenever a stored artifact manifest is available, while clearing
  stale overrides when it is not.
- When that immutable artifact image is available, compatibility `ship` no
  longer depends on branch-sync to drive deploy execution; the old git-push
  step is bypassed and recorded as skipped-for-artifact-image instead.
- Compatibility wrappers in `odoo-ai` must fail closed when this repo cannot
  accept control.
- Compatibility wrappers are transitional and should be removed after parity.

## Runtime Shape

- Persist records to a local state directory.
- Keep storage pluggable, but start with file-backed JSON.
- Avoid service/API complexity until the workflow boundary is proven.
