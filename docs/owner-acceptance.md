---
title: Client Review and Retired Acceptance History
---

## Current Client review

The Client named on the product profile reviews a preview at `/ui/owner-review`.
The signed-in GitHub user's immutable id must equal `owner.github_id` before
Launchplane accepts a product-review decision.

A Client does not need an authorization policy role to sign in. When GitHub
sign-in finds no policy role but the user is the named Client of an active
product, Launchplane issues an `owner` session. That session is accepted only by
session status, sign-out, product review, release review, and Client secret
entry; every other route refuses it, and it never satisfies a policy rule. Each
of those routes still checks that the viewer is the named Client of the requested
product. The session ends when the person stops being the named Client of any
active product. A decision merges and deploys
nothing. The release checklist is a separate decision bound to the testing
candidate and its change list. See [release-review.md](release-review.md) and the
Product Review API in [service-boundary.md](service-boundary.md).

Each new preview decision is projected onto its pull request with the complete
Client reason, immutable Client ID, reviewed commit, and a link to that saved
decision in Launchplane. Publication uses the existing preview-feedback
credential and verifies its GitHub `/user` identity. An unreadable identity or
failed comment lookup leaves delivery pending; it never selects another credential.
Comments are reconciled by decision ID and publishing actor under a per-PR
storage lock. Retrying after a lost provider response recovers the existing comment.
Delivery and status updates serialize separately from decision saves, so provider
I/O cannot prevent the Client's decision from being persisted.
If that unique comment was edited, retry repairs it from the saved decision.
One failed historical delivery does not prevent later decisions from being sent.

The decision remains saved during a delivery failure. The review page reports
pending delivery and provides **Retry delivery** for the saved decision, even
after the preview is gone. This never records acceptance of another revision.
Resubmitting the same decision for the same serving preview reuses its record; a new decision or
preview creates a new record. A ready-preview refresh also retries undelivered
decisions. The Client-review status remains pending until the current decision's
feedback has a delivery receipt. Historical decisions keep their reviewed commit.
Decisions saved before this feature have `feedback_requested=false`: refreshes
do not backfill their prose or downgrade their existing acceptance status.
The Client can explicitly send that saved feedback or resubmit their decision.
The form explains that new decisions and feedback are shared on the pull request.

The maintained `codex-skills` agent watcher recognizes the Client feedback marker
from any publisher, then verifies the complete decision and exact comment receipt
against Launchplane through its configured private read route. It retains full
Client feedback in every snapshot, including after restart, and checks the latest
saved decision once that Client channel is known, including a newer decision whose
comment delivery is still pending. Agents read and summarize that feedback
before changing the product. The GitHub copy conveys feedback, not authority to
approve a newer commit, merge, or deploy.

The same named Client may supply an explicitly requested credential through the
separate `/ui/owner-secrets` page. This stores an encrypted submission for admin
application; it does not apply product configuration or grant operational access.
See [Client credential input](secrets.md#client-credential-input).

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

The `product-owner-policy`, change-impact, manager/delegate, and ordinary-agent
machinery still have separate remaining deletion work under [DIRECTION.md](../DIRECTION.md).
Their historical presence grants no authority to the current review flows.
