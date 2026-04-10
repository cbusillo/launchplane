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
- Phase 5 now leaves only the Odoo-specific post-deploy update as a remaining
  cross-repo runtime step, and that step goes through the canonical
  `odoo-ai platform update` path rather than a hidden compatibility worker.
- Compatibility wrappers in `odoo-ai` must fail closed when this repo cannot
  accept control.
- Compatibility wrappers are transitional and should be removed after parity.

## Runtime Shape

- Persist records to a local state directory.
- Keep storage pluggable, but start with file-backed JSON.
- Avoid service/API complexity until the workflow boundary is proven.
