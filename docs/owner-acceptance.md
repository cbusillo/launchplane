---
title: Client Review and Retired Acceptance History
---

## Current Client review

The Client named on the product profile reviews a preview at `/ui/owner-review`.
The signed-in GitHub user's immutable id must equal `owner.github_id` before
Launchplane accepts a product-review decision.

A preview invitation's first visible line names **Change review (preview)** and
asks whether this one change is exactly right. It links that change's preview
and explains that Accept approves the change while nothing goes live yet.
The separate release invitation is described in [release-review.md](release-review.md).

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

## Carried acceptance

The Client accepts a change, seen on its preview. When the merge train only
merges the base branch into the pull request, the change is the same, so
Launchplane carries the acceptance to the new head instead of asking again. It
carries only when all of these hold:

- Every commit from the accepted head to the new head is exactly the merge commit
  the train's own refresh made. After asking GitHub to refresh, the train reads
  the pull request's new head back, keeps it only if it is a two-parent merge
  whose first parent is the head it refreshed from, and records that commit, the
  previous head, the base commit it merged, and the base branch
  (`launchplane_merge_train_branch_refreshes`). Another merge of the same parents,
  however it was made, matches no record. Commit dates and committer names are
  not used.
- The merged base commit is on the pull request's current base branch, and that
  branch is the base the acceptance was given on. A decision keeps the pull
  request's base branch from the first time Launchplane shows it on the pull
  request; a decision without one does not carry.
- The pull request's change against its base is the same at both heads: every
  changed file has the same name, status, and added and removed lines, in order,
  in GitHub's comparison from the newest merged base commit. Hunk positions and
  context lines are not compared, so a base edit that moves or surrounds the
  change in a file the pull request also changes still carries; any difference in
  a line the pull request adds or removes does not. A file without a patch
  (binary or too large) or a list of 300 files or more cannot be compared and
  does not carry.
- The newest decision is that acceptance, made by the product's current Client,
  with its feedback delivered.

Anything else, including a new commit by anyone, a conflict resolution, or a
different base, leaves the new head waiting for the Client. A carried acceptance
also stops applying if the pull request is later retargeted to another base, even
without a new commit. The carry is saved
as a new decision record with `carried_from` naming the decision and head it came
from, the reason, and the train's refresh records, so it reads as carried, not
re-decided. The `launchplane/owner-review` check on the new head says
"Accepted by @client (carried from `<short head>` after a base-only refresh)",
the review page shows which version it was carried from, and the ready preview
comment says the change is accepted rather than asking the Client again. The
carry is checked when the status is next written: on the ready preview comment
from the feedback route or the reconciler, and after a decision.

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
