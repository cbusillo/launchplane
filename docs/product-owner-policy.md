---
title: Product Owner Policy
---

## Purpose

Launchplane has an additive, shadow-only product/system Owner policy. It models
future human Owner authority without changing any current authorization,
promotion, tenant-admission, manager-preview, technical-waiver, or trusted-
maintenance decision.

The contract has one human `Owner` class. Membership and evaluated actors are
bound only to an immutable, positive numeric GitHub user ID. GitHub Actions,
terminal agents, local operators, and local administrators are not eligible
Owner identities. Identity and grant records are immutable after validation so
their derived IDs cannot become stale. A current policy may contain multiple
Owners; the quorum is fixed at one.

## Separate Authorities

Three independently revisioned record streams prevent accidental authority:

- `ProductOwnerPolicyRecord` grants Owner membership. It does not make any
  action require an Owner.
- `ProductOwnerRequirementRecord` lists the actions, repositories, and
  environments that would require an Owner. It contains no identities.
- `ProductOwnerRoutingRecord` records preferred Owner routing. It is persisted
  with `authoritative=false` and never participates in membership evaluation.

Launchplane authz grants control who may invoke the read and policy-admin APIs.
They never satisfy an Owner requirement. Administrative roles are not part of
Owner actor identity and cannot influence evaluation. A GitHub human who also
has Launchplane administration satisfies a shadow Owner action only when that
human's immutable GitHub ID appears in the current product/system policy and
matches the action's repository and environment scope.

## Scoped Review Policy

`ProductOwnerPolicyRecord` also carries three scoped policies that shape Owner
product-review admissibility. They are fail-closed by default, so a policy
written before they existed behaves exactly as if it declared the defaults:

- `review_age` declares the finite Owner-review evidence age. Migration defaults
  are 30 days for routine changes and 7 days for sensitive, unknown, and
  production-affecting changes. A policy may only choose shorter values, and the
  elevated value can never exceed the routine value.
- `self_review` denies self-review by default. A routine-only exception requires
  an explicit positive `exception_revision` and a reason, and it never applies to
  sensitive, unknown, or production-affecting changes.
- `preview_trust` declares the minimum provable preview isolation class. The
  default is `synthetic_seeded`; `unknown` and `not_applicable` cannot be chosen
  as a minimum, and `unknown` observed isolation never satisfies any minimum.

These three policies are deliberately excluded from `policy_digest`, which
remains the Owner *membership* fingerprint. Existing records therefore keep their
exact digest, and every dimension is instead bound through separate
`product_owner_scoped_policy_fingerprint` values.

Each fingerprint is explicitly versioned by
`PRODUCT_OWNER_POLICY_FINGERPRINT_VERSION` and scoped to one exact product,
system, repository ID, environment, and action, alongside the source record ID
and revision. Two products, or two actions on one product, can never share a
fingerprint. Owner acceptance binds the membership, self-review, review-age,
requirement, and preview-trust fingerprints; changing any of them changes the
exact binding and stales prior evidence rather than silently reinterpreting it.

## Current-Policy Evaluation

Shadow evaluation resolves only active records for the exact product/system
scope. When an explicit requirement matches, evidence is checked against the
current policy revision and digest. Stale revisions, removed Owners, another
product's Owners, and identities present only in preferred routing do not
satisfy the shadow action.

If a current policy exists but no current Owner grant covers the requested
repository and environment, evaluation returns `unavailable` with
`policy_scope_not_covered`. This is distinct from `actor_not_current_owner`,
which means the policy scope is covered but the evaluated GitHub human is not
one of its current Owners.

Every evaluation response is explicit about its boundary:

- `mode=shadow`
- `authoritative=false`
- `enforcement_effect=none`

When one current Owner would satisfy the quorum, the read model returns every
current Owner in scope as the notification audience. Preferred routing only
marks which Owner is preferred; another current Owner remains able to satisfy
the quorum.

## Persistence

Filesystem rehearsal records live under:

- `launchplane_product_owner_policies/`
- `launchplane_product_owner_requirements/`
- `launchplane_product_owner_routing/`

PostgreSQL uses tables with the same names. Each stream has one active-record
partial unique index, a unique scope/revision index, a current-history index,
and a record-id primary key. The requirement table enforces shadow mode and the
routing table enforces non-authority at the database layer.

Successor revisions must have a non-decreasing `effective_at`. An incoming
active revision must already be effective when applied; future scheduling
cannot supersede the current authority into an unavailable gap. Invalid
revision sequences and optimistic write conflicts are reported separately.

Migration `c1d2e3f4a5b6` creates empty tables only. It performs no inference,
owner mapping, or backfill. Real identities remain operator-supplied runtime
records.

Migration `b2d4f6a8c0e2` writes the fail-closed `review_age`, `self_review`, and
`preview_trust` defaults into existing stored policy payloads so the records are
self-describing. Because those fields are outside `policy_digest`, the backfill
cannot change any persisted digest.

## HTTP API

Reads:

- `GET /v1/product-owner/policy`
- `GET /v1/product-owner/requirement`
- `GET /v1/product-owner/routing`
- `GET /v1/product-owner/shadow-evaluation`

CAS apply/dry-run endpoints:

- `POST /v1/product-owner/policies/apply`
- `POST /v1/product-owner/requirements/apply`
- `POST /v1/product-owner/routing/apply`

The three write actions are classified as `policy_admin`. The shadow read action
remains observational. Generated OpenAPI is the contract source for clients.

## Rollback Boundary

Legacy repository-human admission, manager-preview, technical-waiver, and
trusted-maintenance readers remain fully operational and unchanged. This slice
does not delete manager, delegate, or technical-waiver vocabulary, records, or
code. A later, separately reviewed cutover may consume Owner decisions only
after shadow evidence proves equivalence and the migration plan explicitly
defines rollback.
