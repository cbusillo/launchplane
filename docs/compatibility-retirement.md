---
title: Compatibility Retirement
---

## Purpose

Launchplane keeps local CLI helpers and file-backed stores for development,
tests, and emergency operator inspection. They are not production authority once
the matching service route, DB-backed records, and product workflow calls exist.

Use this page as the review checklist before keeping or deleting compatibility
surfaces.

## Retirement Rules

Delete or demote a compatibility surface when all of these are true:

- a typed Launchplane service route exists for the same action or record family
- the product workflow uses GitHub OIDC to call that route
- the route writes DB-backed Launchplane records or managed secret bindings
- a targeted test covers the service route and a client/request test covers the
  product workflow wrapper
- live evidence proves the route for at least one real context or product lane

Keep a compatibility surface only when it is one of these:

- local-development scaffolding used by tests or operator rehearsal
- a read-only diagnostic helper that does not mutate production truth
- an emergency operator client for the same typed service contract
- seed/example material for rebuilding DB-backed records through explicit write
  paths

## Current Checkpoints

- Odoo artifact publish, post-deploy, prod backup gate, prod promotion, and prod
  rollback are service routes. Tenant repos should keep only thin request
  workflows and artifact build context.
- VeriReel testing deploy, stable environment reads, app maintenance, prod backup
  gate, prod deploy/promotion/rollback, preview refresh, preview inventory, and
  preview destroy are service routes. VeriReel should keep source/build,
  verification, and thin request wrappers.
- Tracked release-tuple catalogs are examples or seed/debug material only.
  Production release truth is the Launchplane release-tuple record shape in the
  shared store.
- File-backed JSON state is local-dev/test scaffolding. Production truth is
  Launchplane service-owned persistence.
- `control_plane` remains the Python package name for now. Do not add public
  `odoo-control-plane` names, env vars, or docs; prefer Launchplane wording for
  product/operator surfaces.
- The first driver-migration working plan is retired. New driver work should be
  tracked as capability expansion in the active Launchplane GUI/driver plan or
  in focused PRs/issues, not by reopening the old migration checklist.

## Review Cadence

Review this page after adding any new product driver route or tenant workflow.
If a product workflow still shells into a Launchplane CLI to mutate production
truth after a service route exists, that workflow is not done.
