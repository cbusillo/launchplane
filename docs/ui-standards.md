---
title: UI Standards
---

## Purpose

Launchplane UI work must read as a product environment control plane, not a
generic dashboard and not a preview-only queue. The first screen should make the
operator's current product, lane state, and next safe action obvious.

Use [operator-experience.md](operator-experience.md) for the API-first product
and environment contract. Do not polish the transitional context-picker UI as if
it were the target model.

Use this document as the Launchplane UI quality gate. UI slices that pass tests
but fail this rubric are not complete.

## Product Objects

Every UI slice declares one primary object before implementation:

- product workspace
- tenant
- environment lane
- preview
- promotion
- policy or secret/integration status

Do not blur these into a generic set of status cards. If a slice needs multiple
objects, make the primary object visually dominant and place supporting objects
as evidence around it.

The top-level picker chooses a product workspace, not a raw Launchplane context.
Use display names such as `SellYourOutboard`, `VeriReel`, `Odoo CM`, and
`Odoo OPW`. Context strings such as `sellyouroutboard`,
`sellyouroutboard-testing`, `verireel-testing`, `cm`, or `opw` are routing and
record identifiers. They may appear in evidence, route metadata, or scoped write
forms, but they should not be the primary picker label.

Stable lanes (`testing` and `prod`) belong visually under one product workspace.
If a generic-web product still has a legacy testing-shaped context, the UI may
read that route as transition metadata, but the operator model remains one
product with stable lanes and preview inventory under it. Preview routing can
use a separate technical context while previews are isolated from stable lane
state.

## Visual Direction

- Tenant-first: prefer a direct tenant environment page over a fleet queue when
  Launchplane is scoped to one tenant.
- Product-first: for generic-web and reusable drivers, make the product
  workspace the first visible identity and demote driver/context details to
  metadata.
- Operational density: prioritize scan speed, evidence, timestamps, and status
  clarity over decorative layout.
- Distinct lanes: `prod`, `testing`, and `preview` must have different visual
  semantics, not only different labels.
- Promotion weight: promotion is a first-class product event and should carry
  weight comparable to preview creation or teardown.
- Secret safety: show binding status, validation, rotation, and audit metadata;
  do not normalize plaintext reveal in the default UI.
- Driver-shaped: render from Launchplane driver descriptors and read models
  where possible, so Odoo, VeriReel, and future products share the same shell
  instead of drifting into separate hard-coded pages.

## Slice Brief

Before implementation, each meaningful UI slice should declare:

- primary object: tenant, environment lane, preview, promotion, policy, or
  secret/integration status
- first-screen hierarchy: what must be visible without hunting
- primary action and safety level
- explicit anti-goals, such as avoiding fleet-first framing, preview-only
  framing, generic card grids, plaintext secret reveal, or provider-native
  vocabulary as the main UI model
- required evidence states: pass, fail, pending, unknown, blocked, and missing

## Anti-Slop Gate

Before committing a meaningful UI slice, check it against this rubric:

- Can a new operator tell what product/tenant they are looking at within a few
  seconds?
- Does the picker show product names instead of raw context strings?
- Can they tell what is in `prod`, what is in `testing`, and whether previews
  exist?
- Are primary actions visibly safer and more important than secondary actions?
- Do status, evidence, and timestamps remain readable at desktop and narrow
  widths?
- Does the page still make sense if shadows, decorative gradients, and large
  empty cards are removed?
- Is any missing evidence shown as a real blocked/unknown state instead of a
  reassuring placeholder?

## Review Workflow

- Use browser screenshots for every substantial UI change.
- Regenerate and reopen the page before judging screenshots; stale screenshots
  are not acceptance evidence.
- Critique the visible result, not only the HTML or tests.
- Keep implementation deterministic and repo-local. Model critique can help with
  art direction or screenshot review, but the rubric and browser evidence decide
  acceptance.
- If UI needs new evidence, add minimal typed read-model fields instead of
  inferring production truth from logs or free-form text.
- Browser-review desktop and narrow/mobile widths for every committed UI slice.
- Update the active Launchplane plan when a UI issue is really a product-framing
  or read-model contract gap instead of a local visual patch.
