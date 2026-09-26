---
title: Owner Review and Retired Acceptance History
---

## Current Owner review

The Owner named on the product profile reviews a preview at `/ui/owner-review`.
The signed-in GitHub user's immutable id must equal `owner.github_id` before
Launchplane accepts a product-review decision. A decision merges and deploys
nothing. The release checklist is a separate decision bound to the testing
candidate and its change list. See [release-review.md](release-review.md) and the
Product Review API in [service-boundary.md](service-boundary.md).

Each saved preview decision is projected onto its pull request with the complete
Owner reason, immutable Owner ID, reviewed commit, and a link to that saved
decision in Launchplane. Publication uses the existing preview-feedback
credential and verifies its GitHub `/user` identity. An unreadable identity or
failed comment lookup leaves delivery pending; it never selects another credential.
Comments are reconciled by decision ID and publishing actor under a per-PR
storage lock. Retrying after a lost provider response recovers the existing comment.

The decision remains saved during a delivery failure. The review page reports
pending delivery and keeps entered feedback available for retry. Resubmitting the
same decision for the same serving preview reuses its record; a new decision or
preview creates a new record. A ready-preview refresh also retries undelivered
decisions. The Owner-review status remains pending until the current decision's
feedback has a delivery receipt. Historical decisions keep their reviewed commit.

The maintained agent watcher recognizes the configured publishing automation
identity, checks the PR/revision metadata, and retains full Owner feedback in
every snapshot, including after restart. Agents read and summarize that feedback
before changing the product. The GitHub copy conveys feedback, not authority to
approve a newer commit, merge, or deploy.

The same named Owner may supply an explicitly requested credential through the
separate `/ui/owner-secrets` page. This stores an encrypted submission for operator
application; it does not apply product configuration or grant operational access.
See [Owner credential input](secrets.md#owner-credential-input).

## Retired exact-binding machinery

The old `/v1/owner-acceptance/*` API and
`/ui/engineering/owner-acceptance` screen have been removed, along with their
evaluator, queue, GitHub check projector, event writers, and projection locks.
They are not fallback paths for review, merge readiness, or release approval.
The current product-review publisher may still neutralize an old
`launchplane/owner-acceptance` check on an existing pull request.

Historical `OwnerAcceptanceEventRecord` contracts, filesystem readers, PostgreSQL
readers, tables, and migrations remain for stored-data compatibility. This change
neither deletes persisted events nor rewrites their bindings, event ids, replay
digests, or subject sequences. Importing a filesystem archive containing these
retired events fails before any writes; it does not silently discard history or
re-enable event authoring.

The managed authorization set `operator.owner-acceptance` can be reconciled only
to an empty desired policy, allowing existing grants to be removed. It cannot be
used to create replacement grants. No deployed authorization records are changed
by this code deletion.

Product-owner-policy, change-impact, manager/delegate/waiver, and ordinary-agent
machinery still have separate remaining deletion work under [DIRECTION.md](../DIRECTION.md).
Their historical presence grants no authority to the current review flows.
