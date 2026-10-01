# Event-Driven Deploys

Status: issue #2605. The receiver, the reconciler, its acting on testing and
previews, and the staff-testing hold on the testing lane are built. Its pull
request feedback is built (#2659). Depends on
[artifact provenance](artifact-provenance.md).

A product repository never calls Launchplane. Launchplane hears GitHub's
events for the product's repository, verifies the build, and deploys it.

## Events

Launchplane receives the webhook of the GitHub App its merge train already
uses (the App is installed on every product repository). One receiver,
`POST /v1/github/app-webhook`, takes `workflow_run` and `pull_request`
deliveries. Everything else is acknowledged and ignored.

Events carry no instructions. An event only says which target to look at
again:

- a completed `workflow_run` of the product's `.github/workflows/build.yml`
  from a `push`: the product's **testing** target;
- a completed build from a `pull_request`, or a PR opened, reopened,
  synchronized, labeled, unlabeled or closed: that PR's **preview** target.

Launchplane re-reads every fact it acts on through the GitHub API with the
product's read-only build-provenance token (`verify_build_artifact`). A
forged or replayed delivery can at most cause a reconcile that finds nothing
to do.

## Receiving

1. Verify `X-Hub-Signature-256` with the App's webhook secret. The secret is
   a Launchplane managed secret, not service env, and it is used for nothing
   else.
2. Map `repository.id` through Launchplane's repository inventory, which is
   the authority for immutable repository ids: the current tracked inventory
   record with that `repository_id` names a `repository`, and exactly one
   active product profile must have that `repository` (compared
   case-insensitively). A repository that is missing from the inventory,
   retired there, or names no active profile or more than one is ignored
   (`repository_not_mapped`). No per-product grant or profile write is
   needed; the repository only has to be tracked in the inventory. Ids stored
   on a profile are an optional cross-check: when present they must equal the
   inventory's, or the delivery is ignored (`repository_identity_mismatch`).
   The reconcile, its build-provenance token and Launchplane's reconcile
   grant read the same inventory identity and fail closed without it. See
   [records.md](records.md#product-repository-identity).
3. In one transaction, record the delivery by `X-GitHub-Delivery` and
   request a reconcile of the target. Only then return `202`. A repeated
   delivery id does nothing. A crash before the commit returns an error, and
   GitHub redelivers.

There is at most one pending reconcile per target. A new request for a
target that already has one pending is folded into it, so bursts of events,
redeliveries and the sweep cost one reconcile.

## Reconciling

The existing Odoo worker claims pending reconciles. Each one reads current
state and makes the target match it, under the target's existing mutation
reservation. The webhook request never waits on a deploy.

- **testing:** the desired artifact is the newest commit on the default
  branch's first-parent history that has a verified release build. A late
  build of an older commit is never desired while a newer one exists, so it
  cannot replace a newer deploy.
  - If testing already runs the desired artifact, stop.
  - Otherwise, `record_verified_build_artifact` and queue the stable target
    replacement for the testing lane. Its idempotency key is the lane plus
    the artifact id, so a crashed or repeated reconcile can't queue it twice.
  - An active attempt (including one awaiting provider reconciliation) is
    reported, never bypassed. When the last attempt ended (failed, cancelled,
    or passed but rolled back) and testing still differs, the next attempt's
    key names the attempt before it. After three failed attempts of one
    artifact the reconcile fails until a newer build.
  - That operation already runs Odoo post-deploy, so there is no separate
    post-deploy step.
  - If the lane is busy, the reconcile stays pending and runs again after it.
  - While the testing lane is held for staff testing, the reconcile records
    its plan as held (`action: wait`, reason `staff_testing`) and deploys
    nothing; see [Staff-testing hold](#staff-testing-hold).
- **preview:** read the PR now.
  - If it's open, carries the product's preview label, and its current head
    has a verified preview build, the desired state is a preview running
    that build. Otherwise the desired state is no preview.
  - Compare the desired state with the preview record's verified build (run
    id and attempt), not just whether a preview exists, then apply or
    destroy.
  - The manifest is not recorded in the artifact store. The preview's slug
    and URL come from the product profile as today. The result is posted on
    the PR; see [Pull request feedback](#pull-request-feedback).
  - Read the PR state again after taking the preview's reservation and just
    before the provider apply; if it closed, lost its label, or moved its
    head, the reservation is released with no provider effect and the
    reconcile runs again.
  - The apply or destroy issues the same service plan as the preview inputs
    route and runs it through `run_odoo_preview_apply_operation`, under
    reservation scope `launchplane-reconcile:<product>`. Its key is the PR,
    the verified build's run id and attempt (or `destroy`), and the preview's
    current lifecycle state, so a repeated reconcile replays it.

Each target keeps one reconcile request with its state, attempt count, last
plan and last error. `GET /v1/product-profiles/{product}/reconcile-requests`
returns them to a caller with `product_profile.read` on the product, so an
event-driven preview or testing deploy can be checked without service logs.
Exact commit SHAs and `sha256:` digests come back as they are, and so do the
ids Launchplane records itself: the GitHub delivery id and the plan's top-level
`*_id` fields, when they are a UUID or a `name-<hex>` id. Every other
string in the plan and error goes through the shared redactor, which removes
secret assignments, tokens, authorization headers, URLs, paths and long
random strings. A failed testing deploy's reason is on the plan as
`last_failed_error_code` and `last_failed_error_summary`, so the operator
need not read the deploy operation, whose status read needs the grant that
starts a deploy. The summary is structured: the code's fixed description, step
statuses and attempt, with no provider text (see
[records](records.md#reconciler)).

## Pull request feedback

The reconciler says on the pull request what it did, so a product repository
needs no workflow to report previews or testing deploys.

- **Preview:** one comment on the PR, marked `<!-- launchplane-reconcile-preview -->`
  and edited in place: waiting for a verified build of the head commit, ready
  (with the preview URL), retired, or failed with a short, redacted reason
  (`cleanup_failed` when a destroy failed). A plan that changes nothing, is
  deferred, or is held for another driver says nothing.
- **Testing:** one comment on the PR GitHub merged as the desired commit
  (`merge_commit_sha` equals it), marked `<!-- launchplane-reconcile-testing -->`:
  the deploy is queued, the testing lane runs it, it is waiting because the
  lane is held for staff testing (with the hold's reason), or it failed three
  times. A commit that no PR was merged as (a direct push) is announced
  nowhere.
- **Identity:** the comment is posted with the product repository's
  merge-train GitHub App, the same App the build-provenance token comes from.
  Each post mints a token for that one repository with only Pull requests
  write and Contents read, a subset of what the train's own token has, so the
  App needs no new permission. No App, key, or matching repository id means
  nothing is posted; there is no fallback to another token.
- **Recording:** posting never changes, fails, or rolls back the reconcile.
  The plan keeps the outcome as `pr_feedback`: the status, PR number, the
  body's `sha256`, `delivery_status` (`delivered`, `skipped` or `failed`),
  the comment action and id, and any error. A preview's feedback is also
  written as a preview PR feedback record, so it shows where the feedback
  route's records do, and a failed delivery alerts through the product's
  preview feedback notification policy.
- **No churn:** a reconcile whose comment would be the same as the one it last
  delivered posts nothing, so the half-hourly sweep doesn't rewrite comments.
  A "queued" testing comment becomes "runs this change" at the next reconcile
  after the deploy finishes, at the latest the next sweep.

- **Owner review:** when the PR carries the product Owner's review label and
  the preview is ready, the comment mentions the Owner with the link to record
  Accept or Request changes, as the preview feedback route does; with no Owner
  set it says the operator needs to set one. The link's origin is Launchplane's
  own bootstrap `LAUNCHPLANE_PUBLIC_URL`, the setting the human session
  manager's public origin comes from, which the workers share with the service.
  Without it (or with an invalid one) the comment has no mention, the reconcile
  is unaffected, and `pr_feedback.owner_review` says why (`no_public_origin` or
  `invalid_public_origin`; otherwise `mentioned` or `owner_not_set`).
- **Missing preview settings:** a preview refused because its runtime
  environment is incomplete names the missing keys (names only, never values)
  on the plan as `missing_keys`, in the request's error, and in the PR comment.

## Staff-testing hold

Once site staff test on a testing lane, a merge must not deploy mid-session.
The site operator holds the lane, and lifts the hold when staff are done.

- The hold is `policies.staff_testing_hold` on the testing lane's tracked
  target record: a reason, who recorded it and when. It is set and lifted only
  through `POST /v1/product-config/testing-hold/apply` (dry-run, then a
  reviewed apply; see [operations](operations.md)), never in code or config.
  Only a `testing` lane takes one.
- While it's on, the testing reconcile still works out the desired build, then
  records the plan as held with reason `staff_testing` and the hold's reason.
  It records no artifact and queues nothing. A testing lane already running the
  desired build is reported as `already_deployed`, as before.
- A deploy the reconciler queued just before the hold is cancelled by the
  worker before any provider effect. Its cancellation names the hold and the
  reconciler; it doesn't count toward the three failed attempts.
- A deploy an operator queued runs regardless: deploying during staff testing
  is the operator's call.
- Lifting the hold requests a reconcile of the product's testing target, so
  the newest verified build deploys right away rather than at the next sweep.
- Previews are unaffected.

## Bounded work

A PR author controls that PR's build, so its uploads are untrusted input.

- The manifest archive download stops at 2 MiB.
- The unpacked manifest is read with a 2 MiB cap, so a compression bomb
  fails without being expanded.
- One worker handles one reconcile at a time per product.
- A target whose reconcile fails is retried with backoff and stays visible
  as failing.

## Who the work runs as

Launchplane needs no caller grant for the work it starts from source-control
events (DIRECTION.md). Every other queued deploy still carries a caller
identity with a matching policy rule, re-checked before it runs.

Operations the reconciler starts carry the `launchplane_reconcile` grant
instead: caller identity type `launchplane_reconcile`, subject
`launchplane-reconciler`, and no managed rule or policy fields. Only
`control_plane/launchplane_reconcile_authorization.py` builds it, called from
the reconciler. It is stored only on the stable target replacement of a
product's own testing lane, the one operation it queues for later.

A preview apply or destroy runs in-process, so it carries no grant: the
reconciler checks directly that the destination is the product's own preview,
in its preview context, before it runs.

No request can supply it: route payloads forbid unknown fields, no request or
response schema carries a durable authorization, and each route builds its
authorization from the verified caller. Before the testing replacement runs,
the worker re-reads the product profile and accepts the grant only for that
product's testing lane in the recorded context; any other operation kind,
instance, context, or product fails closed. The grant does not
replace operator approval at a stop boundary, a site owner's release approval,
or a backup gate.

## Catch-up sweep

Every 30 minutes the worker requests a reconcile of every product's testing
target, and of every preview target with an open labeled PR or an existing
preview record. Reconciling is idempotent, so the sweep runs the same code as
the events, and a missed or out-of-order event is corrected within one sweep.

## Owner steps

Once the receiver is deployed: set the App's webhook URL to the receiver,
generate its secret and store it through Launchplane's managed-secret path,
and subscribe the App to "Workflow runs" and "Pull requests". No product
repository changes.

## Deleted afterwards (#2606)

The site's `odoo-preview.yml`, `odoo-testing-deploy.yml`,
`ship-testing-on-merge.yml`, `odoo-post-deploy.yml` and
`odoo-artifact-publish.yml`, and their reusable workflows, routes and grants.
Also the profile's stored `repository_id` and `repository_owner_id` copies and
`POST /v1/product-profiles/repository-identity/apply`, now that the repository
inventory is read directly.
