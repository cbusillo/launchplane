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

The response includes `viewer_capabilities.event_write_authorized`, a
server-issued route-level capability for the evaluated identity. It allows the
workbench to render explicit read-only engineering visibility instead of
showing unusable Owner controls. The capability is advisory: browser-session
requirements and current product Owner authority are revalidated independently
when an event is submitted.

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

## Current Items

`GET /v1/owner-acceptance/current-items` is the automatic Current work source.
Launchplane enumerates open pull requests server-side only for repositories with
active change-impact policy records, then evaluates each candidate through the
same exact evidence path as `GET /v1/owner-acceptance/evaluation`. The response
declares `derivation: active_change_impact_open_pull_requests`, exposes bounded
and truncation counts, includes per-PR unavailable states, and reports
repository discovery failures instead of silently omitting them. The route is
read-only and uses `owner_acceptance.read`; event controls still depend on the
separate route-level event-write capability and current product Owner authority.

The candidate feed is bounded to 10 active repositories and 20 returned pull
requests per request. The default UI request asks for 10. Automatic evaluations
also use a five-page changed-file evidence bound; larger PRs remain visible as
unavailable and can use exact lookup's complete evidence bound. Exact lookup
also remains available for a PR outside discovery bounds or when GitHub
discovery is temporarily unavailable.

## Recorded Queue

`GET /v1/owner-acceptance/queue` is a **ledger-only** endpoint. It derives
candidates exclusively from `OwnerAcceptanceEventRecord` history with no
repository evidence provider calls, no GitHub API calls, and no engineering
review decision store dependency.
The response declares `derivation: ledger_only`; current Owners do not appear in
the queue until an acceptance event exists for their exact subject.

**Folding:** Events are folded by their full subject key:
`(repository_id, pull_request_number, product, system, action, environment)`.
For each unique subject, the latest event is selected deterministically by
`(occurred_at, event_id)`.

**Ledger status:** Each entry carries a `ledger_status` and `next_action`
derived from the latest recorded event action:
- `accepted` → `accepted`
- `changes_requested` → `changes_requested`
- `revoked` → `revoked`
- `superseded` → `stale`
- `invalidated` → `unavailable`

**Pagination counts:** The response exposes `total` (unique subjects in the
scan), `candidate` (after optional filters), `truncated` / `has_more` (whether
filters exceed the 50-entry limit), and `entry_count` (entries in this
response).

**Provenance:** Every entry includes `latest_event` (full persisted event
record), `latest_binding` (the exact binding from that event), `occurred_at`,
and `verification_required: true`.

**Limitation:** The recorded queue reflects what has been explicitly recorded
in the ledger. PRs with no acceptance events do not appear there; they are
surfaced by the automatic Current-items route when they are open in an active
change-impact repository. Use exact evaluation only as the bounded fallback.

The browser supplies only optional filter query parameters (`repository`
substring, `status` exact). It cannot supply candidate targets, head SHAs,
binding digests, or any evidence input. Malformed event actions are not
silently skipped — they fail the route. Queue rows never expose mutation
controls.

The route uses the existing read authorization action.

## Engineering Ops Workbench

`/ui/engineering/owner-acceptance` combines automatic Current items with a
read-only recorded ledger surface. It loads
`GET /v1/owner-acceptance/current-items` on page entry and renders current
server-issued decisions and binding-scoped Owner controls directly on each PR.
It also displays queue entries from `GET /v1/owner-acceptance/queue` with:

- loading, error, denied, and empty states via `EngineeringResourceGate`;
- a boundary note explaining shadow mode, automatic Current discovery, and the
  separate ledger-only recorded derivation;
- server-side filters by status (exact) and repository (substring);
- per-entry recorded binding and event provenance with `verification_required`
  framing — rows are labeled **Recorded**, not Current;
- a passive note that no Owner mutation controls are exposed;
- a collapsed **Exact Lookup fallback** that calls
  `GET /v1/owner-acceptance/evaluation` for a specific repository and PR when a
  candidate is outside server bounds or discovery is unavailable.

Mutation controls appear directly on automatic Current items and only for
server-issued product bindings when
`viewer_capabilities.event_write_authorized=true`. Read-authorized engineering
viewers see the Current decision and binding evidence with an explicit read-only
notice instead. The browser submits repository, PR, action,
reason, and the exact `expected_binding_sha256`; it never builds authority or
evidence. Launchplane re-evaluates the binding and the authenticated GitHub
human's current Owner membership at write time. Request-changes and revoke
require a reason, revoke requires explicit confirmation, replay preserves the
same idempotency key, and `409 owner_acceptance_binding_changed` refreshes the
Current item without auto-resubmitting. Every receipt remains shadow,
non-authoritative, and has no merge or production enforcement effect.

## Advisory GitHub Projection

`POST /v1/owner-acceptance/project` projects the current aggregate decision as
the stable `launchplane/owner-acceptance` GitHub App check run. The caller
supplies only repository and pull-request reference. Launchplane derives every
product decision and exact binding, rechecks the current target before the
provider write, and uses the decision digest as the check-run `external_id`.

The completed check conclusion is always `neutral`; the output lists the
aggregate state plus each affected product and binding. The check remains
shadow-only, is excluded from Launchplane merge/admission technical inputs, and
cannot become Owner authority.

## Out Of Scope

- production authorization and promotion consumers
- tenant-admission cutover
- manager/delegate cleanup
- break-glass acceptance
