---
title: UI Standards
---

## Purpose

Launchplane UI work must read as a tenant environment control plane, not a
generic dashboard and not a preview-only queue. The first screen should make the
operator's current product, lane state, and next safe action obvious.

## Product Objects

Every UI slice declares one primary object before implementation:

- tenant
- environment lane
- preview
- promotion
- policy or secret/integration status

Do not blur these into a generic set of status cards. If a slice needs multiple
objects, make the primary object visually dominant and place supporting objects
as evidence around it.

## Visual Direction

- Tenant-first: prefer a direct tenant environment page over a fleet queue when
  Launchplane is scoped to one tenant.
- Operational density: prioritize scan speed, evidence, timestamps, and status
  clarity over decorative layout.
- Distinct lanes: `prod`, `testing`, and `preview` must have different visual
  semantics, not only different labels.
- Promotion weight: promotion is a first-class product event and should carry
  weight comparable to preview creation or teardown.
- Secret safety: show binding status, validation, rotation, and audit metadata;
  do not normalize plaintext reveal in the default UI.

## Anti-Slop Gate

Before committing a meaningful UI slice, check it against this rubric:

- Can a new operator tell what product/tenant they are looking at within a few
  seconds?
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
- Critique the visible result, not only the HTML or tests.
- Keep implementation deterministic and repo-local; use model critique as review
  input, not as a replacement for the rubric.
- If UI needs new evidence, add minimal typed read-model fields instead of
  inferring production truth from logs or free-form text.
