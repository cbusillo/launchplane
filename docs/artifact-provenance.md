# Artifact Provenance

Status: built in `control_plane/build_provenance.py` (#2604). Event handling is
built too (#2605); see [event-driven deploys](event-driven-deploys.md).

A product repository builds its own artifact and never calls Launchplane.
Launchplane decides for itself which artifact came from which commit by reading
GitHub's record of the build run. It trusts no image tag or image label. It
trusts the manifest's digest only because it has verified the run that produced
it.

## What the product repository provides

- A workflow at `.github/workflows/build.yml`
  that runs on `push` to the default branch and on `pull_request`.
- For each run attempt, an Actions artifact named
  `artifact-manifest-<run_attempt>` holding one manifest file:
  - an Odoo product: one devkit artifact manifest (schema v2) with
    `source_commit`, `image.repository`, `image.digest`, and the dependency
    provenance;
  - a generic-web product: the commit it built and the image it pushed, nothing
    else:

    ```json
    {
      "schema_version": 1,
      "kind": "generic-web",
      "source_commit": "<40-hex commit>",
      "image": {"repository": "<image repository>", "digest": "sha256:<64-hex>"}
    }
    ```

That is the whole contract. The repository holds no Launchplane secret, grant,
workflow reference, or setting.

## What Launchplane records per product

The product profile's repository, immutable repository id and image
repository. The build workflow path is the fixed contract path
`.github/workflows/build.yml`, not a per-product setting.

## Verifying an artifact for a commit

Input: product, commit SHA, purpose (`release` for testing and prod, `preview`
for a PR preview, with the PR number).

1. List the repository's workflow runs for `head_sha = commit` through the
   GitHub API, with Launchplane's own App installation token.
2. Keep only runs whose `path` is the recorded build workflow path, whose
   `repository.id` and `head_repository.id` are both the recorded repository id
   (a fork's run never counts), and whose `conclusion` is `success`.
3. By purpose:
   - `release`: the run's `event` must be `push` and `head_branch` the default
     branch. The commit must also be on the default branch's first-parent
     history now, found by walking `parents[0]` from the branch tip within a
     bound. Plain reachability is not enough: a tag can also be named `main`,
     and a tag pushed at a commit from inside a merged PR branch would run
     that commit's unreviewed workflow file. A first-parent commit is a merge
     result or direct push that was the branch tip itself, so its workflow file
     is the reviewed one.
   - `preview`: the run's `event` must be `pull_request`, and the PR's current
     head SHA must be the commit.
4. If more than one run is left, take the newest run and its latest attempt.
5. From that run's artifacts, select the one named
   `artifact-manifest-<attempt>`. There must be exactly one, not expired,
   holding exactly one manifest file.
6. The manifest must have `source_commit == commit`, `image.repository` equal
   to the product's image repository, and a digest in `sha256:` form. For an
   Odoo manifest, the tenant lock's source repository must be the product
   repository, as the current publish route already checks.
7. Odoo only: record the artifact under a key Launchplane assigns from verified identity:
   repository id, run id, run attempt and GitHub artifact id. Store the
   workflow path, event and `purpose` beside them. The manifest's own
   `artifact_id` is data, never a key, so a PR build cannot claim a release's
   record. The record is immutable: a second write with the same key and
   different content fails and overwrites nothing.

A verified generic-web build is not recorded in the artifact store
(`verify_generic_web_build`). Its artifact is the immutable image
`<repository>@<digest>`; the testing deploy records it as the lane's runtime
identity, and promotion and rollback read that.

## Where each purpose may go

- Only `release` artifacts are recorded in the artifact store, and testing
  deploys, prod promotions, rollbacks and restores read only that store. A
  `preview` artifact is handed to its own preview deploy and never recorded
  there, so it can never be promoted. A PR's workflow file comes from the PR
  itself and can build anything.

## Why a `release` run is trustworthy

A `push` run on the default branch executes the workflow file as merged. Only
an admin or Launchplane merges to the default branch, so the build steps
were reviewed like any other code. The image digest in the manifest is what
buildx reported for the push in that run.

## Access this needs

Launchplane mints a separate repository-scoped token from the GitHub App its
merge train already uses for the product repository, requesting only
`actions: read` (runs and artifacts), `contents: read` (commits) and
`pull_requests: read` (a PR's current head). That App's installation already
grants these, and the train's own token keeps its own permission set. No
product repository gets any new access, and no new grant is needed.

## What it replaces

The artifact publish route and publish-inputs route, the reusable publish
workflow, and the workflow-identity grants that let product repos call them.
They are still in the code until #2606 (open) deletes them; nothing new may
use them.

## Not covered here

Event handling (which GitHub events start a verification, and the catch-up
sweep) is in [event-driven deploys](event-driven-deploys.md).
