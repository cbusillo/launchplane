# Event-Driven Deploys

Status: design for issue #2605, not yet built. Depends on
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
   `repository_id`. An unknown repository is ignored.
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
  - That operation already runs Odoo post-deploy, so there is no separate
    post-deploy step.
  - If the lane is busy, the reconcile stays pending and runs again after it.
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
  - Read the PR state again just before the provider change; if it moved,
    the reconcile runs again.

The preview apply orchestration now lives inside the HTTP app factory. It
moves to a module the worker can call, with no change in behavior.

## Bounded work

A PR author controls that PR's build, so its uploads are untrusted input.

- The manifest archive download stops at 2 MiB.
- The unpacked manifest is read with a 2 MiB cap, so a compression bomb
  fails without being expanded.
- One worker handles one reconcile at a time per product.
- A target whose reconcile fails is retried with backoff and stays visible
  as failing.

## Who the work runs as

Every queued deploy today carries a caller identity with a matching grant
rule, re-checked before it runs. Reconciles have no outside caller:
Launchplane acts from its own records and GitHub's.

Proposal: an internal-only authorization variant, `launchplane_reconcile`,
that the worker attaches to the operations it queues itself. It covers only:

- the testing lane's stable target replacement;
- a product's own PR previews.

A request can never select this variant. Every route still requires its
normal caller identity and grant, and the worker's re-check accepts the
variant only for these operation kinds and destinations. Anything a person
or another agent starts still needs its grant. This is the direction change
the owner approved on 2026-09-30, "Launchplane needs no grant to act on its
own records", and it is decided before this is built.

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
