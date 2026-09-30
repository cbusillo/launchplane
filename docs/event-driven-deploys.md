# Event-Driven Deploys

Status: design for issue #2605, not yet built. Depends on
[artifact provenance](artifact-provenance.md).

A product repository never calls Launchplane. Launchplane hears GitHub's
events for the product's repository, verifies the build, and deploys it.

## Events

Launchplane receives the webhook of the GitHub App its merge train already
uses (the App is installed on every product repository). One receiver,
`POST /v1/github/app-webhook`, handles:

- `workflow_run` with action `completed`, for the product's
  `.github/workflows/build.yml`:
  - `push` on the default branch: a **release** build, so deploy it to testing;
  - `pull_request`: a **preview** build, so apply that PR's preview if the PR
    carries the product's preview label.
- `pull_request`:
  - `closed`, or the preview label removed: destroy the preview;
  - the preview label added: apply the preview from the PR head's verified
    build, if one exists.

Everything else is acknowledged and ignored.

The webhook body is a hint only. Launchplane re-reads every fact it acts on
through the GitHub API with the product's read-only build-provenance token
(`verify_build_artifact`). A forged or replayed delivery can at most cause a
check that fails or repeats work that is already done.

## Receiving

1. Verify `X-Hub-Signature-256` with the App's webhook secret. The secret is
   a Launchplane managed secret, not service env, and it is used for nothing
   else.
2. Map `repository.id` to exactly one product profile by its recorded
   `repository_id`. An unknown repository is ignored.
3. Record the delivery by `X-GitHub-Delivery` and return `202` at once. A
   repeated delivery id does nothing.
4. Queue one **product event** operation (product, kind, commit, PR number).
   The existing Odoo worker claims it; the webhook request never waits on a
   deploy.

## Working a product event

- **release:** verify the build, `record_verified_build_artifact`, then queue
  the stable target replacement for the product's testing lane with that
  artifact. That operation already runs Odoo post-deploy, so there is no
  separate post-deploy step. If testing already runs this artifact, stop.
- **preview apply:** verify the build for the PR's current head, then run the
  existing preview apply with the verified manifest. The manifest is not
  recorded in the artifact store. The preview's slug and URL come from the
  product profile as today. Post the result on the PR.
- **preview destroy:** the existing destroy.

The preview apply orchestration now lives inside the HTTP app factory. It
moves to a module the worker can call, with no change in behavior.

## Who the work runs as

Every queued deploy today carries a caller identity with a matching grant
rule, re-checked before it runs. Event-driven work has no outside caller:
Launchplane decides to act from its own records and GitHub's.

Proposal: a built-in `launchplane_service` identity for work Launchplane
starts itself. It is scoped to exactly these event-driven operations on
products that name the repository, and it needs no grant rule. Anything a
person or another agent starts still needs its grant. This is the direction
change the owner approved on 2026-09-30, "Launchplane needs no grant to act on
its own records", and it is decided before this is built.

## Catch-up sweep

A worker loop every 30 minutes covers missed deliveries. For each product
with a `repository_id`, it looks at:

- the default branch tip's successful build: if testing does not run it,
  queue a release event;
- open PRs with the preview label whose head has a successful build and no
  current preview: queue a preview apply;
- previews whose PR is closed or unlabeled: queue a destroy.

The sweep reuses the same product event operation, so an event and the
sweep cannot double-deploy.

## Owner steps

Once the receiver is deployed: set the App's webhook URL to the receiver,
generate its secret and store it through Launchplane's managed-secret path,
and subscribe the App to "Workflow runs" and "Pull requests". No product
repository changes.

## Deleted afterwards (#2606)

The site's `odoo-preview.yml`, `odoo-testing-deploy.yml`,
`ship-testing-on-merge.yml`, `odoo-post-deploy.yml` and
`odoo-artifact-publish.yml`, and their reusable workflows, routes and grants.
