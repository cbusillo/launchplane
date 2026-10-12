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
requires the existing immutable-ID-bound human administrator, checks one exact
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

This path creates no agent access. Instance-scoped retirement grants remain
instance-scoped: `include_previews=true` is refused for machine callers.
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

## First canary use after deployment

1. Sign in as the administrator, open **Product Ops**, and select the
   `launchplane-generic-web-canary` product.
2. In **Retire product**, enter the retirement reason and the related issue
   `cbusillo/launchplane#2152`, then click **Review retirement plan**.
3. Check that the plan contains only this product's testing application and
   previews for PRs 1 and 6, with exact routes, affected records and exclusive
   ownership. A blocked plan is a reason to investigate, not to clear records.
4. Check **I reviewed this deletion plan and want to retire this product**,
   then click **Confirm retirement**. On an uncertain result, keep the tab or
   reload it and click **Resume original retirement**.
5. Require the card's **Product retired** read-back. The assigned qualification
   agent then verifies current preview/provider absence and that the scheduled
   lifecycle/reconcile sweep no longer selects this product. Keep GHCR packages.

Source tests and a merge do not establish deployed capability or live retirement.
The Supervisor routes the PR, qualifies the deployed card, and records those
click steps on the canary issue when the capability is available.

## Existing lane-scoped requests

The protected [dispatch wrapper](../.github/workflows/product-retirement.yml)
and [reusable worker](../.github/workflows/reusable-product-retirement.yml)
own the current machine inputs and pinned worker revision. Tracked requests
review one profile-owned lane and its exact target digest. With
`include_previews=false`, active, pending, failed, paused or teardown-pending
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

The existing transitional authorization selector is `product-retirement` in
**Manage Launchplane Authorization**, backed by managed set
`operator.product-retirement` and
`LAUNCHPLANE_AUTHZ_PRODUCT_RETIREMENT_MANAGED_SET_JSON`. Existing workflow
bindings name both the caller `workflow_ref` and exact immutable
`job_workflow_ref`; derive these from the dispatch wrapper and its `retire`
job's `uses:` value, never a copied revision. This describes the existing path,
not approval to create or expand it. New grants and managed-set changes follow
[authorization authority](authorization-authority.md#who-can-do-what).
