---
title: Owner Acceptance
---

## Purpose

Owner acceptance is a shadow-only exact-change ledger for product/system Owner
review of pull requests. Every affected product receives an independent binding
and decision. It does not authorize production, merge trains, tenant admission,
promotion, GitHub required checks, or manager-preview flows.

Launchplane is the only authority. GitHub comments, reviews, checked-in files,
workflow inputs, local operator bearer tokens, agents, and workers cannot create
Owner acceptance or impersonate an Owner.

## Exact Binding

Every acceptance event binds the current server-derived evidence for one exact
change:

- numeric GitHub repository ID, owner ID, owner/name, PR number, head SHA, and
  tree SHA from Launchplane's GitHub evidence provider;
- active change-impact policy record ID, revision, and digest;
- affected product, system, Owner action, and environment from change-impact;
- active product Owner policy and requirement record IDs, revisions, and
  digests;
- when an enabled product preview has one active serving record, its context,
  preview and generation IDs, artifact ID and immutable image digest, manifest
  fingerprint, canonical URL, and an explicit verified-runtime identity
  projection.

Preferred Owner routing affects notification order only; it does not grant or
deny Owner authority and is intentionally excluded from the acceptance
binding.

The API does not accept caller-supplied head, tree, policy, Owner, preview,
artifact, manifest, or runtime provenance. Launchplane resolves all of it from
service-owned GitHub, product-profile, preview, generation, and runtime records.
The runtime projection deliberately excludes deployment time, so redeploying
the same verified identity does not stale acceptance, while any identity field
used to prove the serving artifact remains bound.

## Evaluation

`GET /v1/owner-acceptance/evaluation` accepts only `repository` and
`pull_request_number` query parameters. Launchplane resolves change impact,
Owner requirement, Owner membership, and prior events from server-owned
providers and storage. The pure read remains outside the browser-mutation
surface and cannot consume a request body.

Engineering-only changes return `not_required` and write no event. Product
changes with incomplete change-impact evidence, unavailable Owner policy, or
missing Owner requirement fail closed to `unavailable`. Once any event exists
for an older exact binding, a changed head, tree, policy, requirement,
membership, serving generation, artifact, manifest, preview URL, or verified
runtime identity evaluates as `stale` for the new binding. An enabled preview
with ambiguous, incomplete, non-serving, or failed verification evidence is
`unavailable`. If a preview-bound acceptance already exists, preview teardown
evaluates as `preview_evidence_stale` and cannot be replaced by a weaker
exact-change-only acceptance.

For every successfully impact-resolved product change, the response includes a
deterministic `products` entry for each affected product in change-impact order;
single-product changes therefore contain one entry. Early engineering-only,
stale-impact, and unavailable-impact results contain no product entries. Each
entry carries its own status, reason, binding, and current event. The top-level
status is the worst current product status using this precedence: `unavailable`, `stale`,
`revoked`, `changes_requested`, `pending`, `accepted`, `not_required`. Ties use
the existing product order. The singular top-level binding and event mirror
that governing product for compatibility. Aggregate acceptance is therefore
`accepted` only when every affected product is currently accepted.

Products removed from later current change-impact evidence stop governing the
read result; evaluation does not write synthetic ledger events. Products added
by a later exact head or policy/evidence revision receive their own current
binding, while changed evidence continues to stale prior bindings normally.

## Event Authoring

`POST /v1/owner-acceptance/events` is for browser-authenticated GitHub humans
and requires an `Idempotency-Key` header plus the
`expected_binding_sha256` returned by the latest evaluation.
The route requires the browser mutation channel, then the service checks that
the human's immutable GitHub user ID is a current Owner for the affected
product/system, repository, action, and environment.

The binding digest is a compare-only precondition, not caller-owned evidence.
Launchplane re-resolves all exact-change and authority evidence at write time
and returns `409 owner_acceptance_binding_changed` without writing an event if
the current binding differs from the one the Owner reviewed. This prevents a
force-push, policy revision, requirement revision, Owner-scope change, or
serving-preview/runtime change between evaluation and event authoring from
silently changing what the human accepts.

For a multi-product change, `expected_binding_sha256` also selects exactly one
of the current server-derived product bindings. The request still cannot name a
product. A digest that matches no current affected-product binding returns the
same conflict without writing. One request writes at most one product event;
the response returns the recomputed aggregate decision so remaining product
acceptances stay visible.

The `Idempotency-Key` is scoped by the exact binding because the immutable event
ID includes both values. Reusing one key for different product-binding digests
creates distinct product events; replaying it for the same binding remains
idempotent.

Human actions are:

- `accepted`
- `changes_requested`
- `revoked`

System-only actions are:

- `superseded`
- `invalidated`

System actions cannot carry human authorization, and human routes cannot write
system actions.

## Persistence

Filesystem rehearsal records live under `launchplane_owner_acceptance_events/`.
PostgreSQL stores the append-only ledger in
`launchplane_owner_acceptance_events` with an event-id primary key, subject,
binding, and acceptance indexes, and JSONB payload storage. Replaying an
identical event is deterministic; replaying the same event ID with a changed
payload is a conflict. Optional preview evidence lives inside the existing JSONB
payload; non-preview bindings omit the field and preserve the original binding,
acceptance, event, and replay digests byte-for-byte.

Multi-product aggregation uses the existing subject indexes and one PR-scoped
event read followed by in-memory product grouping. It adds no table, column,
index, backfill, or migration revision.

Migration `f3a5c7e9b1d4` creates the empty table and indexes only. It performs
no backfill from GitHub comments, manager-preview approvals, technical waivers,
or tenant-admission evidence.

## Out Of Scope

- production authorization and promotion consumers
- GitHub status projection
- frontend Owner workbench
- tenant-admission cutover
- manager/delegate cleanup
- break-glass acceptance
