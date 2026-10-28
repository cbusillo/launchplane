---
title: Product Repo Contract
---

## Purpose

Product repos should stay product-shaped. They own application code, local
developer ergonomics, product tests, and artifact publishing. Launchplane owns
the durable lifecycle around those artifacts: product profiles, runtime targets,
deployments, previews, feedback, promotion evidence, backup gates, rollbacks,
cleanup, and provider mutations.

This document is the approval gate for new website repos and the cleanup target
for older repos that grew Launchplane-like scripts before the service boundary
existed.

Status: a product repository never calls Launchplane
([DIRECTION.md](../DIRECTION.md)). Its whole handoff is a
`.github/workflows/build.yml` that publishes an immutable image and a build
manifest ([artifact-provenance.md](artifact-provenance.md)); Launchplane hears
the events and deploys previews and `testing` itself
([event-driven-deploys.md](event-driven-deploys.md)). The old trigger-input, OIDC request-action, reusable-workflow, and caller-grant
recipes are intentionally not repeated here. Existing compatibility remains
bounded by [the transitional preview contract](preview-workflow-contract.md);
new products follow the canonical event-driven and provenance contracts above.

## Target Shape

The durable north star is:

> Product repos build, test, smoke, and publish immutable artifacts with
> verifiable provenance, and never call Launchplane. Launchplane reacts to
> source-control events, derives lifecycle meaning, and owns runtime
> authority: it authorizes, decides, mutates, records, explains, and protects.
> Admins act through Launchplane, not around it.

Product repos may document non-authoritative runtime contract facts such as
ports, health paths, smoke commands, package quality gates, and repo ergonomics
metadata. That documentation is not authority for live topology, runtime
configuration, provider targets, secrets, managed environments, promotion
policy, rollback policy, preview URL policy, or cleanup safety. Moving a live
value from code into workflow YAML, `.github/github.json`, TOML, JSON, or a
fixture changes the hiding place, not the ownership boundary.

```text
product repo
  - app source
  - Dockerfile and runtime contract
  - local dev/test commands
  - product-specific smoke or E2E checks
  - image build and publish workflow (.github/workflows/build.yml) with
    its build manifest

Launchplane
  - product profile and lane configuration
  - driver descriptors and driver routes
  - provider credentials and managed secrets
  - preview/deploy/promotion/rollback orchestration
  - health, readiness, inventory, cleanup, and feedback records or driver
    responses
  - protected artifact inventory for registry cleanup
  - PR feedback rendering and delivery
```

The product repo should not carry Launchplane lifecycle truth in TOML, JSON,
checked-in fixtures, or copied ops scripts. Product and lane configuration lives
in Launchplane DB-backed records.
Moving lifecycle truth from code into repo metadata, workflow defaults, TOML,
JSON, or YAML is still a boundary violation unless the file is docs, tests, or
Launchplane self-bootstrap.

## `Client test notes`

Every product pull request includes a **`Client test notes`** heading with test
instructions or an explicit engineering-only declaration under the
[Client notes contract](owner-acceptance.md). That contract owns the required
reason, label override and supported historical spellings. The action
accepts the Client heading only from the Launchplane commit that introduced it;
a repository whose pin is older keeps the `Owner test notes` heading until it
bumps the pin. CI checks presence without
deciding whether a change needs human review. Add the shared action as a step in
an existing required CI job, before checking out product code:

```yaml
"on":
  pull_request:
    types: [opened, reopened, synchronize, edited]

jobs:
  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: Require Client test notes
        if: github.event_name == 'pull_request'
        uses: cbusillo/launchplane/.github/actions/owner-test-notes@<launchplane-sha>
```

Keep the rest of that job's build and test steps. The immutable action reference
must name a full commit SHA. No extra token or write permission is required.
Launchplane compiles the release from actual commits and PR notes, independently
of GitHub release-issue or milestone membership; see [Client release review](release-review.md).

## Repo Metadata Boundary

Product repos may keep `.github/github.json` as repo ergonomics metadata. It can
name non-authoritative facts such as the default branch, project type, docs
index, quality-gate commands, important workflow names, cleanup preferences,
GitHub signal capability hints, labels used by repo automation, and public repo
relationships used for navigation or admin orientation.

Repo metadata must not become the source of truth for Launchplane lifecycle or
runtime state. Do not store real product profiles, product domains, Client
identity, Launchplane driver selection, preview slug policy, preview route
topology, lane URLs, lane health URLs, deploy routes, provider targets, provider
target ids, runtime environments, managed secret bindings, authz grants,
admin identities, promotion policy, rollback policy, cleanup protection, or
production readiness authority in `.github/github.json`.

Product-repository metadata is not a place for Launchplane caller workflow,
route, OIDC, grant, or build-setting configuration.

Disallowed metadata examples:

- concrete product domains, lane URLs, health URLs, or preview URL templates
- provider target ids, Dokploy compose/application ids, or edge endpoint ids
- runtime-environment records, managed secret bindings, or secret key maps
- authz grants, admin subjects, token labels, or workflow policy catalogs
- Launchplane route batches, idempotency catalogs, or copied provider payloads

Product repositories should run Launchplane's changed-file authority gate before
merge once the local baseline is clean:

```bash
uv run launchplane service audit-config-authority \
  --control-plane-root . \
  --mode changed-files-gate \
  --base-sha "$BASE_SHA" --head-sha "$HEAD_SHA" \
  --fail-on-findings \
  --gate-profile product-repo
```

The gate prints the same redacted audit report as the full scanner, adds a JSON
`gate` summary when enforcement is enabled, then exits non-zero for new findings
that still need classification or
[incomplete Python coverage](config-boundary.md#inspection). The same section
owns supported-interpreter selection and coverage-failure handling. In changed-file mode, findings that already
existed at the explicit base commit stay visible in the report with
`preexisting_changed_file_finding`, but they do not block unrelated edits to the
same file. Supply the pull request base and head SHAs and fetch both commits.
Missing commits and git read failures fail closed; local edits are ignored. Docs, tests,
schema fixtures, bootstrap wiring, and explicitly allowed thin connector
mechanics are reported with allow reasons instead of blocking the default gate.
Repository review metadata such as CODEOWNERS and `.github/github.json`
`reviewPolicy` fields is classified as repository ergonomics rather than
runtime authority. In a root or `.github/` `renovate.json`, so are exactly
these entries: the root `$schema` URL, root `extends` preset references, root
and `packageRules` `description` text, and the `packageRules` selector lists
`matchPackageNames`, `matchDepNames`, `matchDatasources`, `matchManagers`,
`matchFileNames` and `matchUpdateTypes`. Every other entry, including
`matchStrings`, replacement templates, `env`, registry settings, host rules
and `postUpgradeTasks`, is still checked as runtime authority. Shell-local filesystem paths such as
`/var/run/docker.sock` and `/dev/null` are not interpreted as GitHub
owner/repository identities, while package scripts using explicit local paths
such as `./node_modules/.bin/...` are not treated as repository authority.
Real owner/repository literals and GitHub API paths such as
`/repos/owner/repository` remain audited.
The `product-repo` profile keeps ordinary
product-owned test fixtures allowed, but also rejects test fixtures that carry
Launchplane lifecycle authority such as authz policy, provider target,
runtime-environment, managed secret, route batch, topology, or target-id
material.

## What Product Repos Own

- Application source code and product-owned business behavior.
- Product dependencies, lockfiles, and package/build tooling.
- Dockerfile or image build contract.
- Documented runtime ports, health paths, and persistent state mounts for
  service-shaped products that run as Dokploy applications.
- Local development helpers, including local-only databases when the product
  needs them.
- CI checks that validate the source artifact before Launchplane sees it: lint,
  typecheck, unit tests, app build, container build, and product-specific smoke
  checks.
- Source-adjacent dependency scan evidence produced with explicit scanner,
  advisory-revision, scope, and configuration provenance. Product repos own the
  scan invocation; Launchplane owns normalized comparison semantics. See
  [dependency-health-contract.md](dependency-health-contract.md).
- Trusted baseline and candidate Trivy reports may be passed to the Launchplane
  dependency-health action pinned at an immutable commit. The product workflow
  still owns commit selection, scanner database reuse, scan configuration, and
  artifact construction. Reports must be vulnerability-only and preserve full
  package and suppression evidence as required by the dependency-health
  contract.
- The product-repo config-authority gate treats inputs structurally nested under
  an immutable dependency-health action and a digest-pinned Trivy tool image as
  scanner mechanics. Dependabot pull-request text must use the exact trusted
  bot/dependency-type guard or a step output. Mutable action/image references,
  unrelated action inputs, and runtime identities disguised as scanner
  configuration remain rejected.
- A publish guard derived from `github.event.repository.default_branch` is a
  workflow mechanic; a checked-in literal branch remains runtime authority.
- Direct `${{ inputs.* }}` forwarding inside action metadata is connector
  plumbing; composed expressions and literal runtime identities remain audited.
- Publishing an immutable image or artifact reference that Launchplane can
  deploy.
- The build manifest Actions artifact (`artifact-manifest-<run_attempt>`) that
  names the commit and the image digest; see
  [artifact-provenance.md](artifact-provenance.md).

Product-specific checks may stay in the repo when they exercise product behavior
Launchplane cannot know generically, such as a checkout flow, Client route, QR
scan flow, or domain-specific API behavior. Their results stay product-owned build or CI evidence rather than defining
product topology, target inventory, domains, or runtime authority. Generic
runtime health and revision checks should move to Launchplane drivers once the
driver has the necessary profile data.

Product smoke checks own their fixture setup and cleanup. They do not call a
Launchplane maintenance endpoint. The old generated smoke-maintenance client is
retired. VeriReel runs its browser checks against each candidate image, using
disposable product-owned fixtures and a credential-free browser before Build
publishes its artifact manifest. Launchplane retains shared runtime qualification
and never runs the product check inside its credentialed service.

## What Launchplane Owns

- Product profile records, lane profiles, preview policy, runtime port, health
  path, preview slug policy, and public URL/domain policy.
- Dokploy or other provider target records and target-id records.
- Runtime-environment records and managed secret records.
- Driver request validation, idempotency policy, action safety, and audit
  evidence.
- Dokploy application deploys for simple service products that follow the
  [Dokploy service deployment contract](dokploy-service-deployments.md).
- Provider mutations: create/update/delete preview apps, deploy stable lanes,
  promote, rollback, capture backup gates, and cleanup stale runtime state.
- Readiness checks before provider mutation.
- Health checks, public page readiness, and deployed build identity checks when
  they are based on profile-owned health paths and expected revisions or image
  references.
- PR feedback records, markdown rendering, comment delivery, and stale feedback
  cleanup.
- Promotion, rollback, deployment, preview, inventory, and cleanup records.
- Protected artifact inventory used by registry cleanup to identify live
  testing, production, release-tuple, and active-preview image references, and
  each stable lane's default rollback target.

## Approval Gate

A product repo is approved when all of these are true:

- Workflows build, test, and publish product artifacts and the build manifest.
  They do not call Launchplane or directly mutate runtime providers.
- Product-specific checks remain in the repo only when they validate product
  behavior rather than generic deploy plumbing.
- Launchplane lifecycle, provider, authorization, and evidence shaping stay out
  of product scripts and repository metadata.
- CI and security gates pass after cleanup.
- Launchplane can verify the artifact provenance and reconcile a non-production
  event using its own records and credentials.

## New Repo Checklist

When creating a new website repo for Launchplane:

- Build the app as a normal product repo first.
- Add a health endpoint that returns enough non-secret version data for
  Launchplane to verify the deployed artifact. New products should expose the
  Launchplane runtime identity env payload from `LAUNCHPLANE_RUNTIME_IDENTITY_JSON`
  or the equivalent discrete env keys, then mark lanes as requiring runtime
  identity after the echo is verified.
- Publish immutable container images or artifacts from
  `.github/workflows/build.yml`, with the build manifest described in
  [artifact-provenance.md](artifact-provenance.md).
- Use the existing admin onboarding operation to create the required product
  profile and target records. This remains compatibility work until a deployed
  service operation replaces every required mode; do not put its inputs or
  authorization policy in the product repository.
- Use `generic-web` directly when the product is a stateless or mostly
  stateless web app with standard preview/deploy behavior.
- Use the Dokploy service deployment contract when the product is a simple bot
  or worker service whose deployment can be represented as a single immutable
  image, one Dokploy application per lane, Launchplane-managed runtime
  settings/secrets, and an optional health endpoint.
- Add a product driver only when the product has named extra obligations such as
  database bootstrap, data migration, backup gates, restore/rollback behavior,
  product smoke checks, or platform-specific post-deploy actions.
- Keep Launchplane lifecycle config out of the product repo unless this document
  or a driver-specific doc explicitly names a scoped bootstrap or rehearsal
  exception.

A testing reconcile reports `action: none, reason: build_workflow_missing` when
GitHub returns 404 for Build runs and a complete, accessible Actions workflow
inventory confirms the Build workflow is absent. Unavailable, incomplete, or
contradictory workflow reads remain failures; they do not authorize a deploy.
