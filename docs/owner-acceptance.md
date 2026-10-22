---
title: Client Review and Retired Acceptance History
---

## Current Client review

Setting a product's Client resolves the login through the repository-scoped
Delivery App credential. A `github_credentials_unavailable` response identifies
the failed stage: `request`, `identity`, `repository_inventory`, or `token_mint`.
The mint stage includes installation lookup, permission checks and validation of
the returned token's scope and expiry. Provider messages and credential values
are never included. A failed lookup leaves the profile unchanged; it does not
fall back to another credential. Service logs include the same failure stage.

Products recorded with `release_on_acceptance: director_standing` do not wait
for a Client-preview check at merge admission or receive preview-review invitations,
even when the PR has test notes or an old review label. The Director's standing
direction supplies acceptance; note validation and engineering checks still
apply. Production release remains on the gated path
described in [release-review.md](release-review.md), including its checklist-note validation.

The Client named on the product profile reviews a preview at `/ui/owner-review`.
Launchplane requests review when a pull request has real `Client test notes`
(`Owner test notes` remains a supported heading). No label is needed. The
product's review label remains an explicit override requiring real test notes.
`Nothing for the Client to test: <reason>` with a nonempty reason marks an
engineering-only change; it cannot be used with the review label. When the
Director reroutes an already labeled request, use [pending request withdrawal](#withdrawing-a-pending-request). Missing notes,
empty template comments, or a malformed engineering-only declaration (including
other "nothing to test" wording such as `Nothing for Justin to test.`, which
the check names instead of waiting for the Client) visibly refuse preview readiness and merge admission for repositories feeding a Client
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
For an otherwise pure Dependabot update, add the configured Client review label
to request review even when hand-written Client test notes are present. The
exception classifies PR authorship and changed file paths; it does not certify
manifest contents or replace the train's commit-provenance, dependency-safety or CI
gates.
The train carries the derived engineering-only explanation into batch and release
notes; it requests no Client preview review and does not accept a release.
Engineering changes with real Client checks at release use the explicit marker in
[the shared ready-handoff notes contract](https://github.com/cbusillo/codex-skills/blob/5d5379271bac82101c94de441541e3cb4d9ee095/skills/github/references/cli-reference.md#ready-handoffs)
as a plain first notes line, followed by real release test steps. Launchplane
imports that classification from its pinned shared package. Each notes section
and batch member keeps its own review requirement. Unmarked steps and changes
the Client must approve before merge, including wording, screens, workflows or
data, still need acceptance before merge; do not mark them as engineering.
The review label and a current Client rejection still hold the change.
Release test steps remain in the release checklist. This classification permits
merge to testing and records no acceptance; every release still needs the
Client's acceptance.

Editing the PR body re-plans its preview and review intent without needing a
push or label change.
The preview reconciler refreshes the Client-review check independently of
preview planning and delivery, including a deferred operation, an unknown
provider outcome, or a failure to recover the original preview proof. The
publisher rereads the PR and saved decisions, even when there is no new preview
comment to post. Valid engineering-only notes
publish a neutral check, replacing any earlier missing-notes failure, saying no
Client preview review is required, without
recording a Client decision or sending a review invitation. A later review label,
missing notes, or real test notes restores the corresponding review gate on the
same head when the check refreshes. Until then, an engineering-only check cannot
satisfy required Client acceptance. An existing Client rejection stays visible.
Both acceptance and rejection projections use the newest applicable decision
for the product's current immutable Client ID, product, repository, pull request
and current head. A changed login with the same ID keeps the decision applicable.
A former Client's decision stays saved with its feedback, but neither accepts,
vetoes nor masks the current Client's decision. Carried decisions must also match
the current base branch. The same Client and target selection applies when
recording a decision's first base branch or choosing an acceptance to carry.
The train independently reads saved current-head decisions for the product's
current Client at admission and immediately before merging. A Request changes
decision blocks even after notes are edited to engineering-only or while its
GitHub projection is stale. A later Accept on that head clears the veto;
decisions for another head, product or former Client do not veto this change.
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
Every decision submission includes the commit, URL, and serving generation
shown by the review page. A changed preview refuses the stale submission without
saving or publishing it. The Client uses **Refresh review**, inspects the current
preview, and decides again; the page never resubmits a decision automatically.
Resubmitting the same decision for the same serving preview reuses its record.
A retry after that preview changes is refused, preserving the original decision.
A new decision or reviewed generation creates a new record. A ready-preview refresh also retries undelivered
decisions. The Client-review status remains pending until the current decision's
feedback has a delivery receipt. Historical decisions keep their reviewed commit.
Decisions saved before this feature have `feedback_requested=false`: refreshes
do not backfill their prose or downgrade their existing acceptance status.
The Client can explicitly send that saved feedback or resubmit their decision.
The form explains that new decisions and feedback are shared on the pull request.

## Carried acceptance

Per-change Client acceptance remains required. Release acceptance is a separate
final check; it does not replace the decision about each change.

For a later descendant head, acceptance carries only when every intervening
commit has one `Client-impact: none` git trailer in its final trailer paragraph.
`Client-impact: visible`, missing or ambiguous trailers need a new decision,
even when the final patch happens to match. Agents declare the impact and the
second-model review checks it against the actual changes. A test-only follow-up
can therefore carry without asking the Client to accept the same feature twice.
Launchplane requires a complete, ordered single-parent history from the accepted
head to the new head (at most 250 commits); incomplete history never carries.
A merge trailer alone cannot prove it was conflict-free, so unproven merges,
including conflict-resolving merges, need a new decision. The existing clean
merge-train refresh proof below continues to apply.
If recorded train refreshes and invisible commits are mixed between status
publishes, neither proof covers the whole history and a new decision is needed.

This carry records reason `client_impact_none` and every commit SHA with its
`client_impact` trailer value in `carried_from.commits`, in order. The Client sees
a short note that later commits were marked as having no Client-visible changes
and their acceptance still applies. The recorded Client, original acceptance
and delivered feedback remain the source, rather than inventing a new decision.
The same-current-Client, delivered-feedback and unchanged-base checks below apply.

The Client accepts a feature, seen on its preview. A base update or rebase may
change its commit without changing the feature. Launchplane carries the
acceptance across a recorded train refresh or rewritten history (such as a
rebase) instead of asking again. This existing unchanged-patch proof carries only
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

The review page's Recorded decision card preserves saved history, including a
former Client's decision or an older preview version. Its carry explanation
describes the recorded version and why the acceptance was carried at that time;
it does not establish that the acceptance applies now. The current Client can
still record a new decision on a ready preview. The shared review projection
checks current Client, head and base applicability separately.

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

## Withdrawing a pending request

When the Director records a decision to reroute a change as engineering, use
`POST /v1/product-review/withdrawal` through the bounded write-action helper's
`product-review-withdrawal-dry-run`, `product-review-withdrawal-apply` and
`product-review-withdrawal-readback` commands. Do not remove the review label by
hand. Supply the product, repository, PR number, exact current head, a reason,
and the recorded Director-decision comment URL. The authenticated Director
agent or admin attests that the citation records that decision; a citation is
not a Client decision or an authorization grant. Apply requires the reviewed
plan digest and an original idempotency key. The helper binds saved dry-run
evidence and follows apply with service readback.

The service serializes with Client decision saves and refuses withdrawal if
any Client decision for this product already names the current head. It
removes only the product's configured review label, confirms the removal,
and adds a withdrawal entry to the PR history. Its durable operation retains
the actor, head, reason and Director-decision citation. An uncertain response
keeps the same request, digest and key; read back before recovery. Recovery can
finish undelivered history after the label is absent, but never removes a label
again. A pre-dispatch failure can resume the same reviewed operation. If a
request is present after an uncertain dispatch, or the head or review authority
changed, recovery settles the original key with `applied=false` and
`replan_required=true`, preserving any unknown prior effects. It neither removes
the current label nor claims withdrawal. Review a fresh dry run and use a new
key to withdraw that request; the old operation no longer holds the PR busy.
Unsettled calls report `product_review_withdrawal_unsettled`, distinct from a
definite refusal. A completed-key replay returns its original receipt; readback reports
whether it still matches current state. Replay and readback validate the saved
original intent, so later unrelated profile edits do not invalidate receipts.

Withdrawal records no acceptance or rejection, changes no saved Client decision,
and changes no release acceptance. It does not rewrite test notes or publish a
successful Client-review check. Set truthful engineering notes separately; real
test notes or a later review label request review normally, including on the
same head. The normal preview reconciler refreshes the check after those edits.

An unlabeled request needs no label withdrawal: edit its notes truthfully and
the next event reconcile or open-PR sweep refreshes the check. A held preview
operation remains held; check reclassification neither retries it nor records
Client acceptance. Withdrawal still requires a labeled pending request.

Recovery and readback can observe a closed PR; a new withdrawal still requires
an open PR. A published history receipt remains historical proof after closure.
A closed request without a receipt settles the original intent as needing a new
plan, preserving uncertainty about prior effects, without deleting any label.
A competing operation returns `product_review_withdrawal_target_busy`: the new
call dispatched nothing and its key cannot recover the original caller's work.
`product_review_withdrawal_in_progress` names a still-running original call;
`product_review_withdrawal_conflict` names a key/request conflict.

Provider evidence failures are unavailable evidence, not eligibility refusals.
Dry-run and fresh apply check authenticated history before removing a label.
A permanent history bound or ambiguous receipt returns
`product_review_withdrawal_history_unverifiable` before dispatch and releases a
fresh reservation. Resolve history verification before retrying; changing keys
cannot fix it. Recovery after an uncertain effect settles the original operation
with `history_unverifiable=true`, preserving prior-effect uncertainty. That key
is terminal: after resolving history verification, review a fresh dry-run and use
a new key. Completed
readback retains its applied receipt and reports `readback_matches=false` when
history cannot be verified. A missing original operation returns
`product_review_withdrawal_not_started`; resubmit the exact original apply with
the same caller and key. Invalid keys and stale reviewed plans return distinct
not-dispatched statuses; refresh and review a dry-run for a stale plan. The durable operation
retains the full caller identity; the public history contains the reviewed head,
reason and Director citation without internal identity labels.
