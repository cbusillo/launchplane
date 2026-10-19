---
title: Product Retirement
---

The signed-in admin **Retire product** card in Product Ops reviews one non-live
generic-web product's previews and its sole testing application together. It
uses `POST /v1/product-retirement`, the existing retirement records, durable
provider-operation runner, preview inventory and teardown helpers. No new grant,
workflow, separate cleanup approval, or product-name exception is required.

## Review

The card first saves a `mode=plan`, `include_previews=true` request. The service
accepts the existing immutable-ID-bound human administrator or the service-authenticated
Director agent for disposable testing, checks one exact
non-live testing lane, and proves exclusive context and provider-target ownership.
It binds every active preview's exact repository anchor, slug, historical
runtime identities, generations, deployment references and target records.

Complete bounded provider enumeration is repeated. Exact preview identities
must have product source or historical target ownership proof; ambiguous,
shared, unqualified renamed, unbound, duplicate, incomplete or changing resources block the
plan. Missing deployment or tracked-target records are never absence proof.
The plan lists each preview/application, provider classification, routes,
affected records, the testing target and runtime/secret authority, and ownership
proof. Present applications must be idle/retirable. Destroyed previews and
historical generations remain history.
The review shows the observed provider display name and appName, including a
qualified renamed application, and distinguishes recorded-target, product-source
and provider-absence proof.

Confirm only that exact plan's ID and digest with
`retire product <product> including its previews`. New previews, generation
changes, profile/target/runtime/secret changes or conflicting provider evidence
invalidate review. The card checks that its reason and related issue still match.

## Execution and recovery

Apply takes an exclusive product serialization lock. Ordinary generic-web
preview mutations and stable deployments share that lock with each other;
read-only legacy plans keep their individual preview lock. In-flight operations drain before retirement;
queued refreshes and deploys reread the current profile under that lock and
refuse retiring authority; refresh also refuses changed execution configuration.
Cosmetic display-name and timestamp changes do not block it. The existing durable provider target lease
also preserves operation and idempotency fencing.

The complete preview/provider review is revalidated before entering the fence.
A changed pre-effect plan is rejected while the product stays active and its
provider reservation is released, so the admin can review a new plan. The
executor atomically records its operation-bound profile checkpoint and
enters `retiring` with previews disabled. It revalidates the owned preview set,
then uses the existing preview resource teardown helper for each verified
application. Each preview's provider absence is independently enumerated before
its terminal transition, target-authority removal and retirement checkpoint
commit together. A provider-absent legacy preview is reconciled without a delete.
The executor retires the testing application, verifies provider absence again,
rechecks completed preview evidence, and uses the existing finalization checkpoint
to remove stable runtime/target authority, disable scoped secrets with audit
history, and mark the retained product profile `retired`.

After an effect starts, unknown provider or database outcomes, changing ownership, and partial effects
remain reconciliation-required with the product fenced. The browser keeps the
reviewed request and its original key in session storage across reloads. Use
**Resume original retirement**; never substitute a key or reviewed payload.
The runner re-observes the provider and checkpoints before retrying. A committed
preview checkpoint is adopted only for the same operation, caller and plan.
History stays retained. The card independently reads the retired profile after
completion; a failed read-back is reported separately.
Standalone preview reconciliation and preview evidence ingress honor the same
fence so they cannot replace its reviewed records during recovery.

The Director agent may plan preview-inclusive testing retirement without an
action grant. Apply requires the product to be recorded `prelaunch`, exact
testing/preview ownership and no recorded protected-use exception. Before each
provider effect it also requires a fresh, exact application inspection with an
explicitly empty mount inventory; missing inventory and mounted applications
refuse this bounded agent path. Other machine identities and Client agents retain
their existing scopes; an instance grant does not authorize product-wide preview
retirement.
Standalone preview destroy/reconciliation retains its existing authorization.
The existing tracked and provider-absent no-target lane requests remain supported
through their current worker/API contracts. Products with other drivers, a live
lane, multiple stable lanes, active historical previews with unqualified
ownership, or uncertain resources are refused by this card. Restoring ownership
evidence through the existing admin paths permits the original request to
resume; retirement never overrides ambiguity or adopts a changed partial plan.

Container images and GHCR packages are retained. Retirement does not delete a
repository, package, volume, backup or historical generation. Existing package
retention decisions remain in force.

## Director-agent use after deployment

Use the supported service operation under the existing Director-agent identity:
plan first, then apply the same reviewed plan, digest, confirmation and original
idempotency key. A plan does not supply execution authority or safety evidence.
The service rechecks staff holds, recorded protected use, target aliases,
cross-lane secret consumers and the exact application's mounts before provider effects.
Never assume the absence of people or unique data, and never use another identity
or a workflow when a safety check refuses.

### Direction and retirement eligibility

The overall direction's exceptions remain conditions; the previous universal
positive-observation requirement was a stricter tool rule. Retirement stops
the reviewed owned application and removes its lane authority. It does not
start a replacement, restore data, change an external database, revoke a
provider credential or reconfigure another service's port. Runtime sync and
rebuild retain their existing positive safety-evidence requirements.

| Fact | Direction's condition | Retirement check |
| --- | --- | --- |
| Usage | Leave a lane someone is using alone. | A staff-testing hold or recorded `in_use` blocks; a complete occupancy inventory is not required by direction. Resolve uncertain real use with the person who knows before applying. |
| Needed copy | Leave the only copy of something needed alone. | Recorded `only_copy` blocks. Reproducing every disposable output is not required; confirm unknown needed-copy facts with the person who knows. |
| Production resources | Nothing shared with production changes. | Keep ownership and target-alias fences; refuse mounted applications or incomplete mount inspection, recorded sharing and scoped secrets consumed outside the lane. Full physical independence of databases, ports and credentials that deletion never changes is not an additional prerequisite. |
| Production use | Whole-product agent retirement is for a product that never went live. | The authoritative `production_use=prelaunch` classification is required; `unknown` and `live` refuse. A bounded Director-agent attestation retains `production_use_authority=director_agent` and requires testing-only ownership at retirement. Never infer never-live status from a name or missing production history. |

Protective observations remain holds even after their timestamps expire or their
binding changes: age cannot make known users, needed data or sharing disappear.
The retirement consumer checks such retained rows, but the current observation
API has no supported writer for needed-copy facts: `record` stores unknowns,
and `collect` produces only staff holds and target aliases. Those two sources
are independently checked live. Observation refresh and clear can replace old
rows; they are evidence maintenance, not resolution of a person's use or needed
data. Resolve that knowledge with the person before applying. Missing or unknown
observations do not override the product's explicit prelaunch classification;
they are neither positive facts nor a way to qualify runtime sync or rebuild.
The Director's answer on the operational issue supplies missing human knowledge;
the existing reviewed production-use surface records classification. A reply
does not waive resource, ownership, executor, review or recovery checks.

Source tests and a merge do not establish deployed capability or retirement.
The Supervisor routes the source PR and separately briefs actual lane operations.
The canary, CM rebuild and RepairShopr separation are separate operational work.

## Existing lane-scoped requests

The protected [dispatch wrapper](../.github/workflows/product-retirement.yml)
and [reusable worker](../.github/workflows/reusable-product-retirement.yml)
own the current machine inputs and pinned worker revision. Tracked requests
review one profile-owned lane and its exact target digest. That lane must be
the profile's only stable lane. Planning
and Apply refuse multi-lane profiles before lifecycle or provider effects,
including older reviewed plans and finalization recovery. Selecting testing or
production alone cannot establish whole-product retirement. Leave all lane
authority intact when this request is refused; this operation does not support
retiring a multi-lane product. Existing sole-lane tracked retirement and its
same-key recovery remain supported.

With `include_previews=false`, active, pending, failed, paused or teardown-pending
previews still block lane retirement; failure is not absence. Finish verified
standalone teardown while the product is active, then re-plan, or use the admin
card to include the product's previews in one operation. Once retiring, resume
that original operation: standalone reconciliation and preview evidence ingress
cannot change its reviewed previews. Reads remain available.

An explicit `no_target=true` request has an empty `expected_target_sha256` and
requires an active generic-web profile with one lane, an application-name
configuration, no tracked target IDs in current or historical contexts, no
shared context and no preview-generation evidence. Complete provider enumeration
must prove no application, preview prefix, repository, image repository or
known domain belongs to the product. It also compares Dokploy's normalized
`appName` bases, including hyphen-delimited suffixes, independently of display
names. [Dokploy's name transformation](https://github.com/Dokploy/dokploy/blob/48504fde4eb210056f7d9f80406f9692a1a7ea8a/packages/server/src/db/schema/utils.ts)
trims, converts each literal space to a hyphen and lowercases, preserving dots,
underscores and repeated spaces; renaming a display name does not rename its
`appName`. Shared or incomplete evidence blocks absence; there is no override.
This mode performs no provider writes and preserves runtime settings, secrets
and artifact/package history. Its confirmation is supplied by the request
contract. The protected wrapper's inputs and pinned worker must actually support
this mode before using it; source support does not establish wrapper activation.

Tracked finalization seals a checkpoint after provider absence and before
removing local authority. Retry requires that operation's same caller, key,
request, reviewed plan and exact profile/authority digests. It can adopt matching
committed checkpoints after target removal or profile retirement, while
rechecking provider absence and retained runtime/secret authority. Insert races
reuse the matching immutable winner; a conflicting checkpoint or secret rotation
retains reconciliation. Secret-disable audit writes are insert-only, preserve
original timestamps, and compare the reviewed authority under the storage lock.
Historical records cannot substitute for a missing finalization checkpoint.
Before apply, finish other provisioning for the product; the database cannot
prove an independent provider operation that has not recorded its result.

An approved managed-secret re-encryption can commit after finalization's secret
snapshot and before a later secret's guarded disable. Earlier secrets and their
bindings can already be disabled, with immutable disable audit events, and
runtime settings can already be removed. The changed secret keeps its rotated
version and audit event; the profile remains `retiring` and the original target
reservation stays reconciliation-required. An expired lease does not resolve
that authority change. The original-key retry still refuses, another key cannot
take over the target, and planning a replacement retirement refuses the retiring
profile. **Resume original retirement** supports unchanged reviewed authority;
it cannot complete this changed-authority case.

There is currently no service capability to review and accept that changed
secret authority within the held retirement. Preserve its original request,
caller, key, finalization checkpoint, rotated pointer and audit history; do not
clear the fence or restore an older secret pointer to force a retry. A future
recovery proposal would need an explicit review of the changed authority bound
to that original operation, fresh provider-absence and ownership checks, and
durable evidence for adopting only the reviewed secret delta while retaining
already committed disable events. That capability requires separate design and
review; this qualification supplies no operational recovery procedure.

Existing product-retirement authority is DB-backed in
`operator.product-retirement`. Its former GitHub-secret reconcile selector is
removed. Maintain the managed set through the reviewed Access policy proposal
path. Existing workflow bindings name both the caller `workflow_ref` and exact immutable
`job_workflow_ref`; derive these from the dispatch wrapper and its `retire`
job's `uses:` value, never a copied revision. This describes the existing bindings,
not approval to create or expand it. New grants and managed-set changes follow
[authorization authority](authorization-authority.md#who-can-do-what).
