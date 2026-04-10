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
- environment inventory
- promotion execution
- deploy orchestration
- backup and restore control-plane workflows

`odoo-ai` owns:

- addon code
- local developer workflows
- Odoo-specific validation
- thin compatibility wrappers during migration

## Transition Direction

- The first live workflow owned here is `promote`.
- During the first compatibility slice, `promote` delegates the underlying
  `ship` worker back to `odoo-ai` while this repo owns the promotion record and
  the orchestration boundary.
- Direct `ship` ownership now also enters through this repo, while the
  temporary execution worker still lives in `odoo-ai` as an internal
  compatibility command.
- Compatibility wrappers in `odoo-ai` must fail closed when this repo cannot
  accept control.
- Compatibility wrappers are transitional and should be removed after parity.

## Runtime Shape

- Persist records to a local state directory.
- Keep storage pluggable, but start with file-backed JSON.
- Avoid service/API complexity until the workflow boundary is proven.
