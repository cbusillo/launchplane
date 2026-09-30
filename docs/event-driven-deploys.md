# Event-Driven Deploys

Status: issue #2605. The receiver, the reconciler, its acting on testing and
previews, and the staff-testing hold on the testing lane are built. Depends on
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
2. Map `repository.id` to exactly one product profile by its recorded
   `repository_id`. An unknown repository is ignored. A profile gets its
   `repository_id` and `repository_owner_id` through
   `POST /v1/product-profiles/repository-identity/apply` at switch-over:
   the route copies both ids from the current tracked repository inventory
   record for the profile's `repository`, so the repository must be in
   Launchplane's repository inventory first. See
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
    and URL come from the product profile as today. Post the result on the
    PR.
  - Read the PR state again after taking the preview's reservation and just
    before the provider apply; if it closed, lost its label, or moved its
    head, the reservation is released with no provider effect and the
    reconcile runs again.
  - The apply or destroy issues the same service plan as the preview inputs
    route and runs it through `run_odoo_preview_apply_operation`, under
    reservation scope `launchplane-reconcile:<product>`. Its key is the PR,
    the verified build's run id and attempt (or `destroy`), and the preview's
    current lifecycle state, so a repeated reconcile replays it.

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
