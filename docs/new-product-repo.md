---
title: New Product Repo
---

## Purpose

Use this checklist when creating a website or service repo that will be operated
by Launchplane. The goal is a normal product repo with a thin Launchplane
handoff, not a repo that grows its own control plane.

## Build The Product First

Create the repo around the product's normal development needs:

- application source, tests, and local dev commands
- package manager lockfile and dependency policy
- Dockerfile or artifact build contract
- local-only fixtures, seed data, and development database helpers when needed
- product-specific smoke checks that prove real product behavior

Keep Launchplane lifecycle records, lane topology, provider targets, managed
secrets, and deployment truth out of the repo.

## Runtime Contract

Every Launchplane-operated web product should expose a small runtime contract:

- immutable container image or artifact reference
- known runtime port
- health endpoint path
- non-secret build revision or image tag in the health response
- Launchplane runtime identity echo from `LAUNCHPLANE_RUNTIME_IDENTITY_JSON`
  before the lane is marked strict for runtime identity verification
- documented required runtime environment keys
- product-specific smoke check command when generic health is not enough

For most web products, `generic-web` can use this contract directly from the
DB-backed product profile.

Simple service products, such as bots or workers deployed as Dokploy
applications, can also use `generic-web` when their lifecycle is image deploy,
optional health verification, and Launchplane-owned provider mutation. See
[dokploy-service-deployments.md](dokploy-service-deployments.md) for the
service-specific contract, including persistent volumes and internal ports such
as Discord Blue's Every Code bridge port `8787`.

## Launchplane Records

For a conventional generic-web product, dispatch `Product Onboarding` with
typed product, repository, image, port, health, preview base URL, and optional
Dokploy naming inputs. The preview base URL is the root URL whose subdomains
host previews, such as `https://product-preview.example.com`; wildcard DNS and
ingress for `*.product-preview.example.com` must already route to the selected
Dokploy server. The workflow resolves immutable GitHub repository identity,
plans one new non-production Dokploy application, plans the product record
bundle, and plans six exact preview authz rules. `mode=apply` then pauses at the
protected `launchplane-authz-admin` review before applying the three reviewed
contracts.

The normal path accepts no base64 manifest, copied provider id, authz JSON, or
database credential. Stable idempotency keys make a same-plan retry replay the
created target rather than create a duplicate. If records apply but authz apply
fails, the product remains safely unauthorized; rerun the same reviewed flow
instead of deleting records or creating another target.

The reviewed flow writes the generic-web product profile, immutable repository
identity, one `testing` lane, provider target records, preview policy, the
context-scoped `LAUNCHPLANE_PREVIEW_BASE_URL` runtime-environment record, and
the complete `operator.generic-web-preview` desired set. The authz planner
retains all existing managed rules and sends the resulting desired set through
the existing digest-bound reconcile route. Product onboarding never writes
authz policy directly.

Existing targets, production targets, compose targets, Odoo products, and
non-generic drivers remain explicit advanced operations. Use `Dokploy Target
Setup` for those cases. Use `Product Onboarding Manifest (Advanced)` only for
an operator-owned manifest that cannot use the conventional path. Direct local
CLI mutation remains break-glass bootstrap or repair behavior and requires
`--allow-direct-db-mutation`; it is not an onboarding alternative.

If live proof finds that an existing preview wildcard ingress route needs a
Launchplane-managed repair, `Generic Web Preview Authorization` can temporarily
include the scoped `Ingress Route Dry Run` and `Ingress Route Apply` workflow
rules. Expand with `include_ingress_operator=true` and the exact reviewed
reusable-workflow SHA, perform the route dry-run/apply, then contract with the
same SHA and `include_ingress_operator=false`. The planner copies the pinned
ingress workflow identity from the active policy and fails closed when no single
template exists; operators do not hand-author authz JSON or edit a per-product
policy secret.

The conventional product-repo caller files are
`.github/workflows/launchplane-preview.yml` for pull-request refresh,
verification, and feedback, and
`.github/workflows/launchplane-preview-notice.yml` for trusted cleanup and
fork/Dependabot notice handling. The conventional caller contract and onboarding
authz planner use these exact paths; custom filenames require an advanced
contract rather than an implicit fallback.

Do not store these as product-repo Launchplane manifests. The repo may document
the expected app runtime contract, but Launchplane records are the live source
of lifecycle truth. Store operator manifests under Launchplane state or another
operator-owned state location, not in product repos and not in git-tracked
history when they contain site-specific runtime details.

## GitHub Actions Shape

Start with these workflows:

- CI: lint, test, build, and product-owned checks.
- Security: causal pull-request dependency checks plus absolute default-branch
  and artifact health appropriate for the repo. See
  [dependency-health-contract.md](dependency-health-contract.md).
- Publish image: build and publish an immutable artifact, then trigger
  Launchplane stable deploy for `testing`.
- Preview trigger: for PRs that request preview, build and publish an immutable
  preview image, then trigger Launchplane preview refresh.
- Preview cleanup trigger: on PR close or preview label removal, trigger
  Launchplane preview destroy.

The Launchplane trigger steps should use GitHub Actions OIDC and pass minimal
facts only: product key, source ref or SHA, PR number when relevant, immutable
artifact reference, and optional run URL.

For a conventional generic-web product, use the thin preview facade documented
in [product-repo-contract.md](product-repo-contract.md). One same-repository
`pull_request` caller delegates image publication, preview refresh,
product-owned verification, evidence, and feedback to
`reusable-generic-web-preview.yml`. A second `pull_request_target` caller uses
`reusable-preview-request-notice.yml` for trusted same-repository cleanup and
fork/Dependabot notices. Pin both reusable workflows to full reviewed
Launchplane commit SHAs. Do not grant OIDC to product build or verification
jobs; the reusable workflows scope OIDC to Launchplane requests that do not
check out untrusted code.

When a product workflow needs to turn local publish/provision/verification or
cleanup job results into preview feedback status, call
`cbusillo/launchplane/.github/workflows/reusable-preview-feedback-status.yml@<launchplane-sha>`.
Keep product-owned smoke facts local, pass primitive job results and failure
summaries to the reusable workflow, and let Launchplane derive the final
`status` and `failure_summary` before it calls `reusable-preview-pr-feedback`.
For generic-web cleanup, pass the lifecycle workflow's `destroy_outcome` output
as `cleanup_outcome`; `no_preview_recorded` clears stale managed feedback while
real and unknown failures remain fail-closed. Do not copy Launchplane feedback
route, payload, idempotency, marker, or delivery logic into the product repo.

For direct JSON calls to Launchplane service routes, use the reusable
`cbusillo/launchplane/.github/actions/launchplane-request` action rather than
copying an OIDC/fetch helper into the product repo. Product repos can still keep
small scripts that assemble product-specific payload JSON until Launchplane owns
that request-shaping layer too.

If a product-owned smoke test creates dynamic users and needs Launchplane to
grant, promote, or clean them up during the same browser run, install the
Launchplane-owned smoke maintenance client with
`cbusillo/launchplane/.github/actions/setup-smoke-maintenance-client@<launchplane-sha>` and
import the generated client from the smoke script. The workflow job must grant
`id-token: write` for the client to authenticate to Launchplane. Do not copy
Launchplane OIDC, route, payload, driver intent, idempotency, or retry helpers
into the product repo for that path.

## Choose A Driver

Use `generic-web` when the product is a stateless or mostly stateless web app,
or a simple service deployed as a Dokploy application, whose lifecycle is image
deploy, health check, preview refresh when enabled, preview cleanup when
enabled, and PR feedback.

Create a product driver when the product has named extra obligations:

- database migration, clone, bootstrap, seed, or anonymization
- backup gate, restore, rollback, or destructive repair behavior
- product-specific promotion smoke checks
- post-deploy maintenance commands
- platform-specific artifact or runtime semantics

See [driver-development.md](driver-development.md) for the driver workflow and
[product-repo-contract.md](product-repo-contract.md) for the approval gate.

## Before Approval

Before treating the repo as Launchplane-ready:

- CI and pull-request dependency regression checks pass, and the current
  default-branch/artifact absolute health evidence is acceptable.
- The image or artifact is immutable and traceable to a source SHA.
- Launchplane can read the product profile and target records.
- A non-prod deploy or preview path has been exercised through Launchplane.
- Product workflows do not mutate providers directly.
- Product workflows do not render Launchplane evidence or PR feedback markdown.
- Any remaining Launchplane adapter scripts are small, temporary, and listed as
  migration candidates.
