---
title: GitHub Actions Supply-Chain Security
---

## Purpose

Launchplane workflows treat every remote `uses:` reference and static container
image as executable supply-chain input. Remote actions and cross-repository
composite actions must resolve to a reviewed, immutable 40-character Git commit
SHA. Static container images must retain a reviewable release tag and resolve to
an immutable `sha256` manifest digest.

## Reference Classes

| Reference class | Trust | Typical privilege | Required form |
| --- | --- | --- | --- |
| Same-repository local action or reusable workflow | Same reviewed workflow commit | Depends on the caller workflow | `./.github/actions/...` or `./.github/workflows/...` |
| Same-repository privileged reusable worker | First-party immutable trust anchor | Authz administration, route-authority reconciliation, or exact-instance reviewed product-policy mutation | Repository-qualified path at a full reviewed SHA, limited to the approved dispatch wrappers |
| GitHub-maintained action | GitHub-maintained | Checkout, artifacts, cache, runtime setup, GitHub API, CodeQL | Full SHA plus a release-tag provenance comment |
| Third-party publisher | Third-party publisher | Python bootstrap, registry authentication, image build and publication | Full SHA plus a release-tag provenance comment |
| First-party cross-repository Launchplane action | First-party cross-repository | OIDC-authenticated Launchplane requests and preview-client setup | Full SHA plus `# main` provenance |
| Static workflow container image | Official or third-party publisher | Workflow linting, secret/vulnerability scanning, integration services | Release tag plus `@sha256:<manifest-digest>` |

Relative reusable-workflow references are preferred for same-repository workflow
composition. Reusable workflows retain a SHA-pinned Launchplane composite-action
reference when they must source the action independently of a caller checkout.

## Privilege Priority

High-privilege references receive the strictest review first: the
`launchplane-request` composite action can make OIDC-authenticated control-plane
requests, while Docker login, Buildx, and build-push actions can access registry
credentials and publish images. Checkout and runtime-bootstrap actions execute
before those operations and are pinned to the same immutable standard. Artifact,
cache, GitHub API, and CodeQL actions remain fully pinned because they execute
remote code or move workflow data across trust boundaries.

## Policy

- A remote action source must be listed in
  `tests/test_github_actions_security.py` with its trust and privilege class.
- A remote action must use a full 40-character lowercase commit SHA; tags,
  branches, short SHAs, and expressions are rejected.
- Every remote pin must retain its provenance comment: an exact release tag for
  public actions or `main` for the first-party cross-repository composite
  actions.
- The mutable remote-reference allowlist is explicitly represented by
  `MUTABLE_REFERENCE_ALLOWLIST` and is currently empty. Any temporary exception
  must be scoped to one workflow path and one literal reference, with a
  corresponding test and security review.
- Same-repository reusable workflows must use relative paths rather than a
  repository-qualified reference, except narrowly approved privileged workers
  whose full-SHA `job_workflow_ref` is itself part of the active policy trust
boundary. The approved set is limited to managed authz administration and
route-binding reconciliation, plus exact-instance product health-policy
mutation whose plan/apply actions are separately authorized.
- Changes to that authorization worker's `workflow_call` interface require two
  landings. First land the worker contract without changing caller pins, then
  pin each caller to that landed commit and start passing the new input or
  secret in a follow-up change. GitHub validates the interface at the pinned revision before
  starting any job, so forwarding a value that only exists in the caller's
  current revision causes a workflow startup failure rather than a safe skipped
  job.
- Every static container image source must be classified in the policy test and
  use a release tag plus a 64-character `sha256` manifest digest. Mutable tags
  without a digest are rejected.

The security workflow runs this policy for both same-repository and fork pull
requests before actionlint. The unit-test suite runs it as well, so a mutable
reference cannot pass the required CI path.

## Provenance And Updates

Each public action pin carries the exact release tag in an inline comment, for
example `actions/checkout@<full-sha> # v7.0.0`. The comment makes a pin directly
reviewable and lets Dependabot update the SHA and tag together in a normal pull
request. First-party cross-repository pins use `# main`; Dependabot tracks the
GitHub Actions ecosystem weekly through `.github/dependabot.yml`.

Dependabot does not update container references embedded in workflow scripts.
For those images, resolve the reviewed release manifest with
`docker buildx imagetools inspect <image>:<tag> --format '{{.Manifest.Digest}}'`,
update the tag and digest together, and let the policy test reject omissions or
unclassified sources.

Review each update PR as a supply-chain change: verify the repository, release
or reviewed source commit, action provenance, and the workflow permissions that
will execute it. This repository treats Dependabot updates as normal pull
requests subject to code review, required checks, and branch protection.
