# Event-Driven Deploys

Status: issue #2605. The receiver, the reconciler, its acting on testing and
previews, and the staff-testing hold on the testing lane are built. Its pull
request feedback is built (#2659). Generic-web testing lanes deploy from the
reconciler too (#2742), and so do generic-web previews (#2740). Depends on
[artifact provenance](artifact-provenance.md).

A product repository never calls Launchplane. Launchplane hears GitHub's
events for the product's repository, verifies the build, and deploys it.

## Events

Launchplane receives the webhook of the GitHub App its merge train already
uses (the App is installed on every product repository). One receiver,
`POST /v1/github/app-webhook`, takes `workflow_run` and `pull_request`
deliveries for deploy reconciliation. Opted-in source checks also consume
`push` and `merge_group` deliveries as described below. Other events are
acknowledged and ignored.

Events carry no instructions. An event only says which target to look at
again:

- a completed `workflow_run` of the product's `.github/workflows/build.yml`
  from a `push`: the product's **testing** target;
- a completed build from a `pull_request`, or a PR opened, reopened,
  synchronized, labeled, unlabeled, closed or retargeted to another base: that
  PR's **preview** target. Label events only matter for the Client mention
  below, and a retarget only re-checks an acceptance carried on the old base;
  neither creates or removes a preview.

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
  - Selection refuses an incomplete read: the run list must include the running
    commit and its recorded build run, and first-parent history must reach the
    running commit. The saved plan includes run and history counts and rejected
    builds. Missing evidence leaves the lane unchanged with a recorded reason.
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
  - **Generic-web:** the reconcile deploys the verified image itself, in
    process, through the generic-web deploy route's durable provider
    operation, under reservation scope `launchplane-reconcile:<product>`.
    Nothing goes to the artifact store; the deploy records the image as the
    lane's runtime identity. Its key is the desired digest, the stored lane
    authority, and the deployment record testing ran when the deploy
    was decided, so a repeated reconcile
    replays a recorded result instead of deploying again. Every deploy and
    rollback records a new deployment, so a lane changed since (even back to the
    same older image) gets the desired image again. A replayed success whose
    image Launchplane's testing record does not show (a deploy recovery that
    closed out from runtime evidence records none) fails the reconcile instead of
    being announced; the next verified build or an admin deploy records it. A deploy whose
    provider outcome is unknown stays reserved for generic-web deploy recovery
    and fails the reconcile until it settles; one that failed with a recorded
    result is not retried until a newer build or the lane changes. Changes to
    the tracked target's source/configuration and provider binding, applicable global/context/lane
    runtime settings, or effective runtime-secret bindings/versions allow a
    real retry of the same digest. Target and settings writes identify their
    configuration by content, excluding audit timestamps. The service's existing
    keyed fingerprint protects plaintext target/settings values; managed-secret
    values are neither read nor hashed for the key. Identical stored authority
    replays the failure. Rotating the service's active encryption key also changes
    this keyed identity and permits a fresh attempt of a known failure. Upgrading
    from the earlier key format likewise permits one fresh attempt of a previously
    recorded failure without a configuration change. The existing
    provider-target reservation still fences an unknown or concurrent operation.
    Before using a new authority key, the reconciler also checks held attempts
    for its testing lane. Matching provider target and
    deploy request reuse the held attempt's key for normal observation/recovery.
    Otherwise a running attempt defers it, and an unknown or expired attempt
    requires generic-web deploy recovery first, even after a provider-binding
    repair moves the lane to another application.
    Provider-only edits outside Launchplane's
    records do not authorize a retry. A refusal
    before any provider change (a missing target, say) is tried again at the
    next event or sweep. The plan records `deploy_operation_status`,
    `deploy_status`, `post_deploy_status` and `deployment_record_id`, never the
    driver's message; a refusal's reason goes to the worker log only. A product on any other driver is held with
    `no_reconcile_deploy_for_driver`.
  - If the lane is busy, the reconcile stays pending and runs again after it.
  - While the testing lane is held for staff testing, the reconcile records
    its plan as held (`action: wait`, reason `staff_testing`) and deploys
    nothing; see [Staff-testing hold](#staff-testing-hold).
- **preview:** read the PR now.
  - A preview stays up until its pull request closes or merges. If the PR is
    open (a draft included) and its current head has a verified preview build,
    the desired state is a preview running that build. A closed or merged PR
    has no preview. Labels and draft state play no part (#2735).
  - Compare the desired state with the preview record's verified build (run
    id and attempt), not just whether a preview exists, then apply or
    destroy.
  - The manifest is not recorded in the artifact store. The preview's slug
    and URL come from the product profile as today. The result is posted on
    the PR; see [Pull request feedback](#pull-request-feedback).
  - Read the PR state again after taking the preview's reservation and just
    before the provider apply; if it closed or moved its head, the reservation is released with no provider effect and the
    reconcile runs again.
  - A refused or failed destroy is attempted at most three times for the same
    preview lifecycle record, product-profile revision, context, and destroy
    reason. Busy operations, observation-only passes for unknown provider outcomes,
    retryable transport exceptions, transient storage errors, and
    moved PRs do not consume attempts. A generic-web terminal failure result still
    consumes an attempt even if its provider-side cause was an outage; the
    automatic limit does not reinterpret unstructured provider messages. Once
    exhausted, successful reconciliation passes record a held destroy with reason
    `preview_destroy_retry_limit` and complete the request
    without attempting another provider mutation. Odoo destroy attempts that return
    an unknown outcome consume the budget when a durable provider-effect checkpoint
    is reached, including
    retries proved safe by provider observation and execution that raises instead
    of returning a result. The runner
    continues to observe the fenced operation after exhaustion and can adopt its
    completion, but does not reacquire it for another automatic mutation. Temporary
    preflight or destination refusals while that operation needs observation do
    not consume attempts or prevent observation after the refusal is repaired;
    those passes still report the refusal rather than completing as held.
    Observation first uses the original issued destroy plan retained in the reconcile
    record, so removing the domain during destroy does not prevent observing the
    original compose. Fresh planning and destination checks still precede any
    mutation retry.
    Typed permanent read refusals remain bounded failures. A stale or incompatible
    retained plan falls back to fresh planning instead of preventing profile repairs.
    Older requests without a retained plan still need ready inputs; no target is
    guessed from a name or an error message.
    The plan retains `destroy_failed_attempts`,
    `last_failed_error_code`, `last_failed_error_summary`, and
    `destroy_retry_stop_reason`; the preview is not marked destroyed or removed.
    A changed lifecycle record, product-profile revision, or destroy reason permits
    a new bounded run;
    reopening the PR resumes normal preview reconciliation.
    For a legacy preview without runtime target evidence, the refusal remains:
    an admin must establish whether its provider target still exists before
    retirement. After fixing the underlying evidence/configuration, an admin
    can request fresh destroy inputs through `POST /v1/drivers/odoo/preview-apply-inputs`
    and apply a ready plan through `POST /v1/drivers/odoo/preview-apply`, or use
    `POST /v1/drivers/generic-web/preview-destroy` for a generic-web preview.
    These supported service paths retain their existing authorization and evidence
    checks and do not use the automatic retry budget. This retry limit supplies
    no evidence-free destroy or record deletion route, and does not authorize
    live retirement.
  - **Odoo:** the apply or destroy issues the same service plan as the preview
    inputs route and runs it through `run_odoo_preview_apply_operation`, under
    reservation scope `launchplane-reconcile:<product>`. Its key is the PR,
    the verified build's run id and attempt (or `destroy`), and the preview's
    current lifecycle state, so a repeated reconcile replays it.
  - **Generic-web:** the build is verified with `verify_generic_web_build`
    (`purpose` `preview`), and the refresh or destroy runs in-process as the
    generic-web preview refresh and destroy routes do, driver extensions
    (VeriReel) included. The lease on the PR's reconcile request makes it the
    only writer; the worker renews that lease every third of its length while
    the reconcile runs, so a refresh longer than one lease keeps it, and a
    worker that stops loses it; a refresh that crashed is planned and run again, as a re-run
    of the product's old preview workflow was. The refresh waits until the
    preview's health endpoint reports the expected build, and the reconcile
    then records that as the generation's verification, so the preview serves
    it. A generation without that record serves nothing, so a refresh the
    worker stopped in the middle of runs again. A build run whose refresh failed
    is not retried until the PR has a new build run (a push, or a re-run of its
    Build workflow). Driver and provider text goes to the worker log only; the
    plan and the PR comment carry statuses.

Each target keeps one reconcile request with its state, attempt count, last
plan and last error. `GET /v1/product-profiles/{product}/reconcile-requests`
returns them to a caller with `product_profile.read` on the product, so an
event-driven preview or testing deploy can be checked without service logs.
Exact commit SHAs and `sha256:` digests come back as they are, and so do the
ids Launchplane records itself: the GitHub delivery id and the plan's top-level
`*_id` fields, when they are a UUID or a `name-<hex>` id; those ids also stay
readable where the last error names them. Every other
string in the plan and error goes through the shared redactor, which removes
secret assignments, tokens, authorization headers, URLs, paths and long
random strings. A failed testing deploy's reason is on the plan as
`last_failed_error_code` and `last_failed_error_summary`, so the admin
need not read the deploy operation, whose status read needs the grant that
starts a deploy. The summary is structured: the code's fixed description, step
statuses, validated key names and attempt, with no provider text (see
[records](records.md#reconciler)). Text fields are cut at 400 characters,
except the summary, which keeps up to 1,500 so a long key list comes back
whole.

## Pull request feedback

The reconciler says on the pull request what it did, so a product repository
needs no workflow to report previews or testing deploys.

- **Preview:** one comment on the PR, marked `<!-- launchplane-reconcile-preview -->`
  and edited in place: waiting for a verified build of the head commit, ready
  (with the preview URL), retired, or failed with a short, redacted reason
  (`cleanup_failed` when a destroy failed). Closing a PR before its preview
  build arrives removes its pending comment and records terminal cleared feedback;
  a closed PR without feedback history posts nothing. Other plans that change
  nothing, are deferred, or are held for another driver say nothing.
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

- **Client review:** when the PR carries the product Client's review label and
  the preview is ready, the comment mentions the Client with the link to record
  Accept or Request changes, as the preview feedback route does; with no Client
  set it says an admin needs to set one. The link's origin is Launchplane's
  own bootstrap `LAUNCHPLANE_PUBLIC_URL`, the setting the human session
  manager's public origin comes from, which the workers share with the service.
  Without it (or with an invalid one) the comment has no mention, the reconcile
  is unaffected, and `pr_feedback.owner_review` says why (`no_public_origin` or
  `invalid_public_origin`; otherwise `mentioned` or `owner_not_set`).
  Before the comment is written, the reconciler writes the
  `launchplane/owner-review` status with the preview context's feedback
  credential (the merge-train App cannot write statuses), carrying an acceptance
  across a base-only merge train refresh when it qualifies (see
  [carried acceptance](owner-acceptance.md#carried-acceptance)). When the head
  is accepted, the comment says so without mentioning the Client, and
  `pr_feedback.owner_review` is `accepted`.
- **Missing preview settings:** a preview refused because its runtime
  environment is incomplete names the missing keys (names only, never values)
  on the plan as `missing_keys`, in the request's error, and in the PR comment.

## Forward build changes

Testing and preview selection, stable deploys (including Client promotions,
native VeriReel deploys and generic-web recovery retries), the ship executor,
and preview applies check the requested build against the lane's current
recorded build before provider effects. A different commit must be a proven
descendant, or diverged history with verified provenance proving a newer
artifact. This permits an explicit deploy after a history rewrite and a
preview following a rebased PR head.
An ancestor is always refused. Changing an image at the same commit requires
newer build provenance; legacy preview records can use a verified build that
started after the serving generation was requested. Historical preview build
verification must match the exact recorded image; desired preview builds
still have to match the current PR head. Missing ordering evidence is a refusal, not permission
to replace the lane. Existing explicit rollback operations, including pinned
failed-release recovery and the release drill, retain their rollback authority.
Requests cannot supply a rollback exception to a deploy or preview apply.

The supported forward path is a verified descendant build, or a same-commit
or diverged-history build whose provenance proves it is newer. Generic-web
requests must bind their source to the verified uploaded image. A provider
tag must be the full source-SHA tag or a tag declared in that build's manifest;
the immutable image digest remains the artifact identity. An intentional backward change
uses the existing rollback operation and its own authority. Refusals remain in
the reconcile plan or failed deployment/operation record. Stable-lane profile
repair edits routing metadata and does not deploy an image.

Generic-web deploy records retain verified build provenance with the exact
image/source pair, so later promotion or a release drill does not depend on
the uploaded manifest still being available on GitHub. Rollbacks carry that
proof forward. For older builds without provenance, the earliest successful
deployment of the same image/source is the observation bound; a rollback's
new timestamp does not make that old artifact younger. A legacy lane with no
usable bound still requires a newly built verified artifact. Native VeriReel
preview refreshes, including the driver extension, enforce the preview guard
inside refresh serialization. Existing previews need their configured product
profile to resolve source authority; product onboarding/profile records supply
that supported configuration path.

Testing searches up to 20 pages of successful build runs and first-parent
history to find its running build. An omitted running run or commit still
holds selection with the observed counts. If the running build is beyond
those bounds, or its history was rewritten, the supported service
deploy/target-replacement operation can request an exact verified newer build;
it still checks source and artifact order before
effects. A source read outage before effects retains refusal evidence and
allows another attempt, without consuming the testing failure budget or
stopping an accepted Client release. Odoo operations wait before retrying,
with exponential delays from 30 seconds to 30 minutes, so a source outage
does not starve work on other lanes, including their rollbacks. Operations on
the same lane remain serialized; an authorized pending-operation cancellation
is the existing way to free that lane. Missing configured read authority
is a terminal refusal. Direct preview source-read failures can retry with the
same idempotency key; each refusal still gets a failed deployment record.
Preview refusals retain the serving generation and remain visible in the
operation response or reconcile plan. Generic-web preview checks include a
failed active generation because it may already have changed a provider app.

This guards code order, not database reversibility. An Odoo rollback preserves
the existing database and runs post-deploy module work; it does not undo schema
or data migrations. Backup, restore and release gates retain their own rules.

## Staff-testing hold

Once site staff test on a testing lane, a merge must not deploy mid-session.
An admin holds the lane, and lifts the hold when staff are done.

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
- A deploy an admin queued runs regardless: deploying during staff testing
  is that admin's call.
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

A preview apply or destroy, and a generic-web testing deploy, run in-process,
so they carry no grant: the reconciler checks directly that the destination is
the product's own preview, in its preview context, or a generic-web product's
own `testing` lane (never `prod`), before it runs.

No request can supply it: route payloads forbid unknown fields, no request or
response schema carries a durable authorization, and each route builds its
authorization from the verified caller. Before the testing replacement runs,
the worker re-reads the product profile and accepts the grant only for that
product's testing lane in the recorded context; any other operation kind,
instance, context, or product fails closed. The grant does not
replace Director approval at a stop boundary, a Client's release approval,
or a backup gate.

## Catch-up sweep

Every 30 minutes the worker requests a reconcile of every product's testing
target, of every preview target with an existing, not yet destroyed preview
record, and of every open pull request, drafts included (one list of open pull
requests per product, with the build-provenance token). A missed event is
corrected within one sweep. Reconciling is idempotent, so the sweep runs the same code as
the events, and a missed or out-of-order event is corrected within one sweep.

## Director steps

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

For SellYourOutboard and VeriReel (#2740): each repository's
`launchplane-preview.yml` and `launchplane-preview-notice.yml`, replaced by its
own `.github/workflows/build.yml`, then its `preview` label. Product-run preview
verification went with `launchplane-preview.yml`; Launchplane's own health check
of the expected build is what marks the preview ready, as for Odoo.

## Product configuration authority from source events

Source implementation: #3021. Runtime activation and required-check policy are
separate Director decisions; consumer workflows remain until coverage is verified.
An enrolled base branch's DB-backed merge-train policy can opt in with
`config_authority_events_enabled`. Its default is false, and omitting it
preserves historical policy digests. No checked-in product catalog or service
environment variable activates it.

For an opted-in, tracked inventory repository, the signed App receiver records
an independent scan request for PR opened/reopened/synchronize/edited, push, or
merge-group checks-requested events for that enrolled base branch, even without
a deploy profile or build.
It acknowledges after the transaction, without waiting for GitHub file reads.
Redelivery preserves the original request and deploy-target deduplication.
Pushes are scanned only for enrolled base branches with an explicit nonzero
before/head pair. Creation, deletion, tag and unrelated feature/train-candidate
pushes request no scan; PR and merge-group events provide their comparisons.

The existing operation worker leases each delivery and rereads repository
identity. PRs use the API's current explicit base/head pair; push and merge-group
use the signed event's explicit pair, confirmed as commits through the API.
Two complete immutable Git trees supply the changed paths, rather than GitHub
compare's merge-base diff. Verified blob bytes feed the same scanner and
`product-repo` gate as the CLI. Inherited findings, file classifications,
symlink handling, and reported coverage gaps retain that scanner's behavior.
Dirty checkouts, workflow instructions, and product executables are never inputs.
A source read, identity mismatch, corrupt blob, truncated tree, or exceeded scan
budget refuses verification. Scans have a four-minute read budget under a
ten-minute lease; an expired lease is recovered and its older attempt cannot
publish a check or stored result. Publication holds the database delivery fence
through the provider write and completion; different deliveries for one
repository are serialized. A separate scan thread is admitted after deploy and
reconcile polling; a slow source read does not hold up newly queued operations.
With no branch opted in, idle polling skips the scan table. The source migration
adds a PostgreSQL index on scan state and receipt time for enabled queue reads.

Completed results are projected through the existing checks-only App as
`launchplane/config-authority/<event>/<encoded-base-branch>`, with event
`pull-request`, `push`, or `merge-group` and a URL-encoded branch name.
Separate event and branch names prevent a narrower comparison from overwriting a PR's
result on the same head. A failed gate produces failure. An unavailable scan
projects in-progress while a retry is pending, then failure on exhaustion;
these checks are not excluded from normal check readiness as advisory governance
projections are. A missing projection credential/permission or failed projection
is stored as unavailable, with the scan outcome preserved separately. No token
fallback or access grant is created. Source reads use the repository's existing
train App with a contents/pull-requests read token (a subset of the train's
existing permissions); projection uses the existing
checks-only identity. Both installations and managed-key bindings need to be
verified before activation. Requiring these check names is an Director decision.

An authorized inventory reader can retrieve a delivery's request, queue/lease
state, commit pair, redacted gate findings, coverage gaps, hashes and projection
receipt through `GET /v1/repository-inventory?repository_id=<id>&delivery_id=<id>`.
The delivery must belong to that repository. Literal configuration values and
file contents are absent. A rejected gate stays failed. Unavailable source or
projection evidence makes up to three attempts with backoff (30 then 60 seconds); native
signed redelivery can retry an exhausted unavailable scan without repeating its
deploy targets. Summaries stay within the check API's size limit, with full
evidence in the reader and its delivery ID in the summary. Interrupted workers recover through their expired lease.

Before removing the RepairShopr, VeriReel or SellYourOutboard workflow, verify
runtime activation, event subscriptions, source reads and check projection for
that product, and account for any required-check change. Source fixture parity
and read-only scans of their commits demonstrate scanner coverage; they do not
prove deployed activation or authorize any live change.

The queue itself does not create a GitHub check: a delivery awaiting a worker
has no source result yet. Existing checks alone therefore cannot prove that this
scan finished before merge. Before consumer removal, the Director's required-check
handling must account for a missing/queued source check and for irrelevant old
branch contexts after a PR retarget. Source implementation does not change that
live policy. Runtime proof must include a delayed scan, not only a completed pass.

Source reads share the train App installation's rate budget. The four-minute
budget limits source reads, and the blob cache avoids repeats within an attempt.
Repeated large change sets or retries can still consume that shared allowance. Rate admission,
large-change coverage and interrupted-process resource behavior need operational
qualification before broad activation; this source proof does not establish them.
