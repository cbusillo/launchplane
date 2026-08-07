---
title: Owner Acceptance
---

## Purpose

Owner acceptance is a shadow-only exact-change ledger for product/system Owner
review of pull requests. It does not authorize production, merge trains,
tenant admission, promotion, GitHub required checks, or manager-preview flows.

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
  digests.

The first HTTP slice intentionally does not accept caller-supplied head, tree,
policy, or Owner provenance. Verified preview/runtime binding is deliberately
deferred to the next Owner-acceptance slice rather than accepting caller-owned
runtime assertions.

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
or membership evaluates as `stale` for the new binding. Multi-product changes
also fail closed until evaluation can require and aggregate one acceptance per
affected product.

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
force-push, policy revision, requirement revision, or Owner-scope change
between evaluation and event authoring from silently changing what the human
accepts.

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
payload is a conflict.

Migration `f3a5c7e9b1d4` creates the empty table and indexes only. It performs
no backfill from GitHub comments, manager-preview approvals, technical waivers,
or tenant-admission evidence.

## Out Of Scope

- production authorization and promotion consumers
- GitHub status projection
- frontend Owner workbench
- tenant-admission cutover
- multi-product aggregation
- verified preview/runtime evidence
- manager/delegate cleanup
- break-glass acceptance
