---
title: Owner Acceptance
---

## Purpose

Owner acceptance is Launchplane's authoritative exact-change product decision for
pull requests. Every affected product receives an independent binding and decision.
A current accepted decision is required by merge readiness; it does not replace
technical checks, engineering review, merge admission, landing, or production
authorization.

The persisted human action remains `accepted` because the event records the
Owner's durable product judgment. The workbench presents that action as
**Owner product review: accepted** and explicitly separates it from technical
checks, engineering review, merge readiness, merge admission, and production
authorization. A product review event never claims that any of those independent
gates passed.

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

Every current binding also carries a server-resolved `review_context`:

- the reviewed base ref and base SHA the change was compared against;
- the change class for this exact product subject: `routine`, `sensitive`,
  `unknown`, or `production_affecting`;
- the engineering review tier that produced it;
- the finite `review_max_age_seconds` selected by the product Owner policy for
  that change class;
- server-resolved numeric GitHub contributing identities, or an explicit
  `unknown` resolution with a closed reason code;
- product/action-scoped, explicitly versioned Owner membership, self-review,
  review-age, requirement, and preview-trust policy fingerprints;
- the preview data isolation class derived from the product preview profile's
  data transport mode, or `not_applicable` when no preview is bound.

`review_context` is an optional bound field, exactly like preview evidence before
it. A binding recorded before this contract existed still recomputes its original
`binding_sha256` byte-for-byte, so history and replay digests are unchanged. A
current server-derived binding legitimately differs because more evidence is now
bound, which stales prior events until an Owner reviews the richer evidence.

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

The Owner workbench also accepts `repository` and `pull_request` query
parameters at `/ui/engineering/owner-acceptance`. A server-issued GitHub check
details link uses the configured browser public origin and those exact
parameters, so opening the link expands Exact lookup, prefills the repository
and pull-request fields, and evaluates the target automatically. The browser
still sends only the exact repository and PR reference to the evaluation API;
fixture navigation remains preserved for local UI rehearsal.

The response includes `viewer_capabilities.event_write_authorized`, a
server-issued route-level capability for the evaluated identity. It allows the
workbench to render explicit read-only engineering visibility instead of
showing unusable Owner controls. The capability is advisory: browser-session
requirements and current product Owner authority are revalidated independently
when an event is submitted.

When route-level event access is present, `viewer_capabilities.bindings`
provides viewer-specific advisory eligibility keyed by each exact
`binding_sha256`. It identifies only whether the current viewer may submit for
that binding and a closed reason code; it never exposes the Owner roster. A
missing, unsupported, stale, or unavailable eligibility entry fails closed in
the browser. Viewer eligibility remains outside `OwnerAcceptanceDecision`, so
decision and GitHub projection digests stay independent of who reads them.

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

## Admissibility

A recorded event is immutable. Admissibility is a separate, recomputed judgment
about whether that history is _currently_ usable. `OwnerAcceptanceDecision` and
each product decision expose `admissible`, which is true only when the status is
`accepted` and every current check passes.

A currently accepted review becomes inadmissible without any history rewrite
when:

- the binding carries no reviewed context (`review_context_missing`);
- contributing GitHub identities are unresolved or conflicting
  (`contributing_identity_unknown`);
- bound preview isolation is weaker than the current product preview-trust
  policy (`preview_isolation_insufficient`);
- the event was recorded as a self-review that current policy no longer permits,
  or under a superseded self-review exception revision (`self_review_denied`);
- the event is older than the policy-scoped review age (`owner_review_expired`).

Owner authority loss is already handled upstream: without a current Owner grant
the subject has no binding and evaluates as `owner_authority_unavailable`. In
every case the stored event remains readable and unchanged.

## Self-Review

Self-review is denied by default. Launchplane compares the acting human's
immutable numeric GitHub ID against the bound contributing identities:

- unresolved or conflicting contributing evidence denies every actor;
- a contributor may record review only when the current product Owner policy
  enables its explicitly revisioned routine self-review exception, and only for
  a `routine` change;
- `sensitive`, `unknown`, and `production_affecting` changes are always denied.

`changes_requested` and `revoked` are never blocked by this rule. They withdraw
or withhold product judgment rather than asserting it, so denying them would trap
stale evidence instead of failing closed. Such an event still records that the
actor was a bound contributor.

A permitted self-review records `self_review` and the applied
`self_review_exception_revision` on the event authorization, so a later exception
revision makes the historical event inadmissible instead of silently valid.
`POST /v1/owner-acceptance/events` returns
`403 owner_acceptance_self_review_denied` when the rule denies the write, and
viewer eligibility mirrors the same rule with reason code `self_review_denied`.

## Authority Semantics

Stored human actions and their digests never change. API projections expose
machine-readable review semantics through `human_action_semantics` on decisions
and event responses.

The contract rejects admissibility without a current acceptance. Acceptance is
authoritative for the Owner facet while remaining distinct from aggregate merge
readiness, the one-attempt admission record, provider landing, and production
authorization.

For every successfully impact-resolved product change, the response includes a
deterministic `products` entry for each affected product in change-impact order;
single-product changes therefore contain one entry. Early engineering-only,
stale-impact, and unavailable-impact results contain no product entries. Each
entry carries its own status, reason, binding, and current event. The top-level
status is the worst current product status using this precedence:
`unavailable`, `stale`, `revoked`, `changes_requested`, `pending`, `accepted`,
`not_required`. Ties use the existing product order. The singular top-level
binding and event mirror that governing product for compatibility. Aggregate
acceptance is therefore `accepted` only when every affected product is currently
accepted.

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
idempotent. Exact replay returns the already-persisted event and receives no new
subject sequence. A different idempotency key cannot deliberately reaffirm the
same human state on an identical binding; reaffirmation requires changed bound
evidence and therefore a new binding.

Human actions are:

- `accepted`
- `changes_requested`
- `revoked`

System-only actions are:

- `superseded`
- `invalidated`

System actions cannot carry human authorization, and human routes cannot write
system actions.

Human transitions are validated atomically with append:

- an initial `accepted` or `changes_requested` event is allowed;
- `accepted -> changes_requested` is allowed on the identical binding;
- `accepted -> revoked` and `changes_requested -> revoked` require a reason;
- `changes_requested -> accepted` on the identical binding requires structured
  `resolution.summary` plus one or more unique
  `resolution.resolved_evidence_references`;
- identical-state reaffirmation and any later human transition from `revoked`
  on the identical binding are rejected;
- a new exact binding starts a new review and cannot revoke a prior binding.

## Persistence

Filesystem rehearsal records live under `launchplane_owner_acceptance_events/`.
PostgreSQL stores the append-only ledger in
`launchplane_owner_acceptance_events` with an event-id primary key, subject,
binding, acceptance, and unique per-subject sequence indexes, and JSONB payload
storage. `launchplane_owner_acceptance_subject_sequences` serializes sequence
allocation for the full subject
`(repository_id, pull_request_number, product, system, action, environment)` in
the same transaction as transition validation and event insert. Filesystem
rehearsal uses the existing cross-process authority lock and atomic file replace
for equivalent append semantics. Replaying an identical event is deterministic;
replaying the same event ID with a changed payload is a conflict. Sequence is
storage metadata and is excluded from event IDs, binding digests, and replay
digests. Optional preview and resolution evidence lives inside the JSON payload;
records that omit those optional fields preserve the original binding,
acceptance, event, and replay digests byte-for-byte.

Multi-product aggregation uses the existing subject indexes and one PR-scoped
event read followed by in-memory product grouping. It adds no table, column,
index, backfill, or migration revision.

Migration `f3a5c7e9b1d4` creates the empty event table. Migration
`b5d7f9a1c3e6` adds sequence metadata, deterministically backfills existing
subjects using the prior `(occurred_at, event_id)` order to preserve their
pre-migration current state, creates the subject counter table and uniqueness
fence, and leaves semantic payload identities unchanged. It performs no
backfill from GitHub comments, manager-preview approvals, technical waivers, or
tenant-admission evidence.

Migration `b2d4f6a8c0e2` adds queryable `base_ref`, `base_sha`, `change_class`,
`review_max_age_seconds`, `contribution_resolution`, `preview_isolation_class`,
and `self_review` columns projected from the bound reviewed context, and makes
the fail-closed product Owner review-age, self-review, and preview-trust defaults
explicit in stored policy payloads. It adds no authority, infers no identity, and
cannot change `binding_sha256`, event, acceptance, or replay digests.

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
For each unique subject, the latest event is selected by `subject_sequence`
only. `occurred_at` remains audit and display data and cannot change current
state under clock skew.

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
- a boundary note explaining Launchplane authority, automatic Current
  discovery, and the separate ledger-only recorded derivation;
- server-side filters by status (exact) and repository (substring);
- per-entry recorded binding and event provenance with `verification_required`
  framing — rows are labeled **Recorded**, not Current;
- a passive note that no Owner mutation controls are exposed;
- a collapsed **Exact Lookup fallback** that calls
  `GET /v1/owner-acceptance/evaluation` for a specific repository and PR when a
  candidate is outside server bounds or discovery is unavailable.

Mutation controls appear directly on automatic Current items and only for
server-issued product bindings when
`viewer_capabilities.event_write_authorized=true` and the matching exact-binding
eligibility has `can_submit_event=true`. Route-authorized non-Owners still see
the Current decision and binding evidence, but the action form is replaced with
a natural read-only explanation that a current product Owner must record the
review. Read-authorized viewers without route-level event access see the broader
read-only notice instead. The browser submits repository, PR, action,
reason, and the exact `expected_binding_sha256`; it never builds authority or
evidence. Launchplane re-evaluates the binding and the authenticated GitHub
human's current Owner membership at write time. Request-changes and revoke
require a reason, revoke requires explicit confirmation, replay preserves the
same idempotency key, and `409 owner_acceptance_binding_changed` refreshes the
Current item without auto-resubmitting. Every receipt confirms an authoritative
Owner decision and explains that Launchplane will recompute exact-head merge
readiness while production authorization remains separate.
When the current identical-binding state is `changes_requested`, choosing
accepted reveals required resolution-summary and evidence-reference fields; the
browser cannot submit that reversal until both are present.
Before submission, the UI labels the control as **Product review action** and
states that recording it does not indicate technical checks passed, make the PR
merge-ready, or authorize production. The stored API and ledger action remains
`accepted`; the human-facing label clarifies its scope rather than introducing a
second semantic state.

## Advisory GitHub Projection

`POST /v1/owner-acceptance/project` projects the current aggregate decision as
the stable `launchplane/owner-acceptance` GitHub App check run. The caller
supplies only repository and pull-request reference. Launchplane derives every
product decision and exact binding, rechecks the current target before the
provider write, and uses the decision digest as the check-run `external_id`.

Before appending a browser Owner event, Launchplane replaces the exact-head
check with an `action_required` **updating decision** projection. If that
conservative projection or its token lifecycle fails, the event is not
persisted. After append, Launchplane projects the resulting decision against the
exact target bound into the stored event. If final projection fails, the
conservative non-green check remains and the route returns
`503 owner_acceptance_projection_reconciliation_required`; retrying with the
same idempotency key replays the immutable event and retries projection. The
explicit projection endpoint provides the same reconciliation path. Browsers
never receive projection credentials or projection authority.

The completed check conclusion is `success` for accepted or not-required state,
`action_required` for pending, stale, revoked, or changes-requested state, and
`failure` when authoritative evidence is unavailable. The output lists the
aggregate state plus each affected product and binding. The check is excluded
from Launchplane technical-check inputs and cannot replace the Launchplane decision.

## Combined Governance Read Model

`GET /v1/governance/projection` and the Governance evidence workbench preserve
the current Owner evaluation and immutable event history alongside separate L2
readiness, L3 admission, landing outcome, and projection facets. The Level 1
facet is authoritative Owner acceptance and preserves the immutable event history;
later readiness, admission, and landing records remain independent evidence.

## Out Of Scope

- production authorization and promotion consumers
- tenant-admission cutover
- manager/delegate cleanup
- break-glass acceptance
