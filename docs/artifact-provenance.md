# Artifact Provenance

Status: design for issue #2604, not yet built.

A product repository builds its own artifact and never calls Launchplane.
Launchplane decides for itself which artifact came from which commit by reading
GitHub's record of the build run. It trusts no image tag, image label, or value
a workflow sends it.

## What the product repository provides

- A workflow at a fixed path (for CM website, `.github/workflows/build.yml`)
  that runs on `push` to the default branch and on `pull_request`.
- For each run, an Actions artifact named `artifact-manifest` holding one
  devkit artifact manifest (schema v2): `artifact_id`, `source_commit`,
  `image.repository`, `image.digest`, and the dependency provenance.

That is the whole contract. The repository holds no Launchplane secret, grant,
workflow reference, or setting.

## What Launchplane records per product

On the product record, not in checked-in config:

- the repository's immutable GitHub id and `owner/name`;
- the build workflow path;
- the image repository (already there).

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
     branch. The commit must also be on the default branch now (compare API:
     `identical` or `behind`), so a force-pushed-away commit fails.
   - `preview`: the run's `event` must be `pull_request`, and the PR's current
     head SHA must be the commit.
4. If more than one run is left (reruns or several attempts), take the latest
   attempt of the newest run. Anything else fails closed.
5. Download that run's `artifact-manifest` artifact. It must exist and not be
   expired, and it must hold exactly one manifest file.
6. The manifest must have `source_commit == commit`, `image.repository` equal
   to the product's image repository, an `artifact_id` with the product
   context's prefix, and a digest in `sha256:` form. The tenant lock's source
   repository must be the product repository, as the current publish route
   already checks.
7. Record the artifact with its provenance: repository id, run id, run attempt,
   workflow path, event, and `purpose`. The record is immutable. If an existing
   record with the same `artifact_id` differs, the write fails and nothing is
   overwritten.

## Where each purpose may go

- Testing deploy and prod promotion accept only `release` artifacts.
- A preview accepts `preview` or `release` artifacts for its own PR head.
- A `preview` artifact can never be promoted, because a PR's workflow file
  comes from the PR itself and can build anything.

## Why a `release` run is trustworthy

A `push` run on the default branch executes the workflow file as merged. Only
the operator or Launchplane merges to the default branch, so the build steps
were reviewed like any other code. The image digest in the manifest is what
buildx reported for the push in that run.

## Access this needs

Launchplane's GitHub App needs `actions: read` on product repositories: to list
runs and download artifacts. It already has `contents` (for the compare call)
through the merge-train profile. No product repository gets any new access.

## What it replaces

The artifact publish route and publish-inputs route, the reusable publish
workflow, and the workflow-identity grants that let product repos call them.
Those are deleted in #2606.

## Not covered here

Event handling (which GitHub events start a verification, and the catch-up
sweep) is #2605.
