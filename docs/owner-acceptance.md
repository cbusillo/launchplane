---
title: Client Review and Retired Acceptance History
---

## Current Client review

The Client named on the product profile reviews a preview at `/ui/owner-review`.
Launchplane requests review when a pull request has real `Client test notes`
(`Owner test notes` remains a supported heading). No label is needed. The
product's review label remains an explicit override requiring real test notes.
`Nothing for the Client to test: <reason>` with a nonempty reason marks an
engineering-only change; it cannot be used with the review label. Missing notes,
empty template comments, or a malformed engineering-only declaration visibly
refuse preview readiness and merge admission for repositories feeding a Client
release. Shared inputs are resolved from recorded production/testing artifacts,
not a checked-in repository list. Shared inputs require valid notes and remain
covered by the consuming product's release review; they do not gain a separate
preview check on a repository without a product preview. Open PRs using the
older role word still require a reason (a colon or sentence separator is
supported) and cannot override a review label. Compatibility for already-merged
notes is described in [release-review.md](release-review.md).
Pure Dependabot updates are engineering-only without hand-written notes when an
immutable source comparison proves that every changed file is a dependency
manifest or lockfile. The PR must be authored by Dependabot in the same repository
and have no Client review label. Mixed code or workflow changes, unavailable or
incomplete comparisons, and explicit review labels keep the usual notes gate.
The train carries the derived engineering-only explanation into batch and release
notes; it requests no Client preview review and does not accept a release.
Editing the PR body re-plans its preview and review intent without needing a
push or label change.
The preview reconciler refreshes the Client-review check even while waiting for
a verified build or after a failed preview apply. Valid engineering-only notes
publish a neutral check, replacing any earlier missing-notes failure, saying no
Client preview review is required, without
recording a Client decision or sending a review invitation. A later review label,
missing notes, or real test notes restores the corresponding review gate on the
same head when the check refreshes. Until then, an engineering-only check cannot
satisfy required Client acceptance. An existing Client rejection stays visible.
The consuming product's release acceptance remains required.
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

The Client accepts a feature, seen on its preview. A base update or rebase may
change its commit without changing the feature. Launchplane carries the
acceptance to that new head instead of asking again, whether the update was
made by the train, an agent, or another source-control flow. It carries only
when all of these hold:

- The pull request still targets the base branch the acceptance was given on.
  A decision keeps the pull
  request's base branch from the first time Launchplane shows it on the pull
  request; a decision without one does not carry.
- The pull request's change against its base is the same at both heads: every
  changed file has the same name, status, and added and removed lines, in order,
  in source-control comparisons from the same immutable base commit. A recorded
  train refresh supplies its merged base commit after branch membership is
  verified; otherwise Launchplane pins the current base branch's commit before
  comparing either head. Hunk positions are ignored. Outside a recorded train
  refresh, surrounding context, hunk boundaries and section names must also
  match: moving identical added lines into a different function needs a fresh
  Accept. Complete source-control tree evidence must also show the same file
  modes, so changing an executable bit or symlink does not carry. Train-recorded
  refreshes can incorporate base context changes without
  asking again. Any difference in a line the feature adds or removes prevents
  either kind of carry. A file without a patch
  (binary or too large) or a list of 300 files or more cannot be compared and
  does not carry.
- The newest decision is that acceptance, made by the product's current Client,
  with its feedback delivered.
- Outside the train, hunk positions must move only by the base's own line shifts.
  Both merge bases and their complete comparison must be readable. Overlapping
  base edits or relocating a hunk to an identical-looking XML block needs a new
  Accept. Tree modes are read only after the text and position proofs match.

Changed feature patches, including conflict resolutions that change added or
removed lines, require a new Accept. Missing base evidence or incomplete
comparisons leave the new head waiting for the Client. A carried acceptance
also stops applying if the pull request is later retargeted to another base, even
without a new commit. The carry is saved
as a new decision record with `carried_from` naming the decision and head it came
from, the reason, the immutable comparison base, and any train refresh records,
so it reads as carried, not
re-decided. The `launchplane/owner-review` check on the new head says
which earlier acceptance was carried and whether it was a train refresh or an
unchanged-feature base update;
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
