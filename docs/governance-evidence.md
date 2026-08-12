---
title: Governance Evidence Projection
---

## Purpose

Launchplane exposes one bounded read-only governance projection for one pull
request. The projection prevents API consumers and Owners from stitching
unrelated routes into a fused approval claim.

`GET /v1/governance/projection` accepts `repository`,
`pull_request_number`, and an optional `base_branch`. The caller supplies only
scope. Launchplane resolves current repository evidence, Owner history,
ephemeral readiness, immutable admission, landing outcomes, and advisory
observations from service-owned providers and records.

The requested base branch must match the current pull request base ref. The
route accepts either the matching repository policy's service authorization or
the Launchplane merge-train policy-target read permission, and it never requires
mutation authority solely to inspect the projection. Live Level 2 evaluation
uses the GitHub token source declared by that repository policy.

## Independent Facets

The response preserves these independent facts:

- **Level 1 Owner product judgment:** current product-review evaluation plus
  immutable stored events. `accepted` remains product judgment,
  `human_action_semantics=product_review_accepted`, and `authorizes=[]`. Each
  event is explicitly classified as current or historical for the resolved
  head/tree and as current or historical to the folded decision.
- **Level 2 merge readiness:** current ephemeral readiness with every Owner,
  technical-check, engineering-review, policy, candidate, and fence reason.
  It remains `mode=ephemeral`, `authoritative=false`, and `authorizes=[]`.
- **Level 3 merge admission:** the latest immutable admission for one exact
  provider-effect attempt. Its only bounded effect is
  `one_exact_merge_attempt` at record creation; it grants no current effect
  authority. The facet states whether the record targets the current head/tree
  or a historical target and does not claim that landing occurred.
- **Landing outcome:** the latest immutable `landed`, `rejected`, or
  `reconcile_required` observation keyed to that admission. Missing outcome
  evidence is `not_observed`, never landed, and recorded outcomes carry the
  same current/historical target classification as their admission. Landing
  observations are `authoritative=false` and `authorizes=[]`.
- **Advisory observations:** reserved Launchplane GitHub check observations
  copied from current Level 2 evidence or, when no current readiness result is
  available, the admitted Level 2 snapshot. They remain neutral,
  non-authoritative, and authorize nothing.

Historical Level 1 evidence remains visible after current policy, authority,
age, self-review, preview isolation, or binding changes make it inadmissible.
Level 3 and landing records remain immutable after later Owner revocation or
changes requested.

## Current Readiness

The endpoint recomputes Level 2 only when an active landing-plan lineage exists
for the pull request. It uses the same `LiveMergeAdmissionEvaluator` as guarded
landing; HTTP and UI layers do not duplicate merge-readiness evaluation logic.

If no active lineage exists, or the matching landing entry is already merged,
skipped, stale, or blocked, the response reports `not_active` with no result.
If current GitHub, controller, candidate, policy, or other required evidence is
unavailable, it reports `unavailable` with no reusable authority. Historical
Owner, admission, and outcome records remain visible in both cases.

## Workbench

`/ui/engineering/governance-projection` renders five separately named regions:

1. historical Owner product judgment;
2. current ephemeral merge readiness and every sub-facet reason;
3. immutable merge admission;
4. separate landing outcome;
5. neutral advisory observations.

The same vocabulary and hierarchy are preserved on desktop and narrow
viewports. Text and semantic headings identify historical/current,
advisory/authoritative, admitted/landed, and blocked/unknown distinctions; color
is supplemental only. The workbench is read-only and adds no mutation route or
authority.
