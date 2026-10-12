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
shared, renamed, unbound, duplicate, incomplete or changing resources block the
plan. Missing deployment or tracked-target records are never absence proof.
The plan lists each preview/application, provider classification, routes,
affected records, the testing target and runtime/secret authority, and ownership
proof. Present applications must be idle/retirable. Destroyed previews and
historical generations remain history.

Confirm only that exact plan's ID and digest with
`retire product <product> including its previews`. New previews, generation
changes, profile/target/runtime/secret changes or conflicting provider evidence
invalidate review. The card checks that its reason and related issue still match.

## Execution and recovery

Apply takes an exclusive product serialization lock. Ordinary generic-web
preview mutations and stable deployments share that lock with each other;
read-only legacy plans keep their individual preview lock. In-flight operations drain before retirement;
queued refreshes and deploys reread the current profile under that lock and
refuse retiring or changed authority. The existing durable provider target lease
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
