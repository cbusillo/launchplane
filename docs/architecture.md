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
- Compatibility wrappers in `odoo-ai` must fail closed when this repo cannot
  accept control.
- Compatibility wrappers are transitional and should be removed after parity.

## Runtime Shape

- Persist records to a local state directory.
- Keep storage pluggable, but start with file-backed JSON.
- Avoid service/API complexity until the workflow boundary is proven.
