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

## Target Shape

The durable north star is:

> Product repos build, test, smoke, and publish immutable artifacts, then pass
> minimal facts. Launchplane derives lifecycle meaning and owns runtime
> authority: it authorizes, decides, mutates, records, explains, and protects.
> Operators act through Launchplane, not around it.

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
  - image build and publish workflow
  - thin Launchplane trigger workflow

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

## Repo Metadata Boundary

Product repos may keep `.github/github.json` as repo ergonomics metadata. It can
name non-authoritative facts such as the default branch, project type, docs
index, quality-gate commands, important workflow names, cleanup preferences,
GitHub signal capability hints, labels used by repo automation, and public repo
relationships used for navigation or operator orientation.

Repo metadata must not become the source of truth for Launchplane lifecycle or
runtime state. Do not store real product profiles, product domains, owner
identity, Launchplane driver selection, preview slug policy, preview route
topology, lane URLs, lane health URLs, deploy routes, provider targets, provider
target ids, runtime environments, managed secret bindings, authz grants,
operator identities, promotion policy, rollback policy, cleanup protection, or
production readiness authority in `.github/github.json`.

Thin connector metadata is allowed only when it identifies a generic connection
surface rather than product topology. Examples include the name of the variable
that supplies the Launchplane service URL, a reusable workflow entrypoint, a
shared action path, a GitHub label that triggers a workflow, or a generic
Launchplane route path used by the shared request action. The product-specific
facts sent to that route should come from Launchplane records, GitHub OIDC
claims, workflow dispatch input, or immutable build outputs, not from a
checked-in catalog.

Existing product repo metadata that lists product identity, public domains,
health URLs, preview/deploy route configuration, or lane topology is an audit and
remediation target unless it is explicitly reclassified as a Launchplane-stamped
read model. A stamped read model must carry provenance that identifies
Launchplane as the writer, the source record or response it mirrors, the stamp
time or source hash, and a contract that operator edits are non-authoritative.
Launchplane must still read authoritative lifecycle state from DB-backed records
or driver responses, not from the stamped repo copy.

Do not replace hard-coded lifecycle authority in code with the same authority in
JSON, TOML, YAML, workflow defaults, or repo metadata. Moving the value changes
the hiding place, not the ownership boundary.

Allowed metadata examples:

- quality gate commands such as `npm test` or `uv run python -m unittest`
- important workflow display names used for operator navigation
- the GitHub variable name that supplies the Launchplane service URL
- a reusable Launchplane workflow reference or shared request action reference
- public-safe related repository links used for docs or operator orientation

Disallowed metadata examples:

- concrete product domains, lane URLs, health URLs, or preview URL templates
- provider target ids, Dokploy compose/application ids, or edge endpoint ids
- runtime-environment records, managed secret bindings, or secret key maps
- authz grants, operator subjects, token labels, or workflow policy catalogs
- Launchplane route batches, idempotency catalogs, or copied provider payloads

Product repositories should run Launchplane's changed-file authority gate before
merge once the local baseline is clean:

```bash
uv run launchplane service audit-config-authority \
  --control-plane-root . \
  --mode changed-files-gate \
  --fail-on-findings \
  --gate-profile product-repo
```

The gate prints the same redacted audit report as the full scanner, adds a JSON
`gate` summary when enforcement is enabled, then exits non-zero for new findings
that still need classification. In changed-file mode, findings that already
existed at the merge base stay visible in the report with
`preexisting_changed_file_finding`, but they do not block unrelated edits to the
same file. Configure product-repo checkouts with enough history for the gate to
resolve `origin/main` or `main`; when no merge base or dirty local comparison is
available, the gate fails closed rather than silently passing. Docs, tests,
schema fixtures, bootstrap wiring, and explicitly allowed thin connector
mechanics are reported with allow reasons instead of blocking the default gate.
The `product-repo` profile keeps ordinary
product-owned test fixtures allowed, but also rejects test fixtures that carry
Launchplane lifecycle authority such as authz policy, provider target,
runtime-environment, managed secret, route batch, topology, or target-id
material.

When a product repository runs the gate from GitHub Actions, use a dedicated
`.github/workflows/launchplane-config-authority.yml` workflow that calls the
Launchplane-owned reusable gate:

```yaml
jobs:
  launchplane-config-authority:
    uses: cbusillo/launchplane/.github/workflows/reusable-product-repo-config-authority.yml@main
```

The reusable workflow checks out the product repository and Launchplane's `main`
audit tool, then runs the product-repo changed-file gate. Product repositories
should not carry a pinned Launchplane tool checkout or run
`uv run launchplane ...` themselves once they can call the reusable gate.
The older pinned-checkout workflow remains a bounded compatibility bridge only:
it may reference `${{ github.repository_owner }}/launchplane` and must pin `ref`
to a 40-character commit SHA. Hard-coded owners, mutable branches, and
non-checkout `repository` values are rejected by the product-repo profile.

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
- Publishing an immutable image or artifact reference that Launchplane can
  deploy.
- A minimal GitHub Actions trigger that authenticates to Launchplane with OIDC
  and submits the product key, source ref or SHA, PR number when relevant, and
  immutable artifact reference.

Product-specific checks may stay in the repo when they exercise product behavior
Launchplane cannot know generically, such as a checkout flow, owner route, QR
scan flow, or domain-specific API behavior. They should send facts to
Launchplane rather than defining product topology, target inventory, domains, or
runtime authority. Generic runtime health and revision checks should move to
Launchplane drivers once the driver has the necessary profile data.

When product smoke checks need Launchplane-backed generated-user setup or
cleanup during the same browser run, the workflow should install a
Launchplane-owned smoke maintenance client with
`cbusillo/launchplane/.github/actions/setup-smoke-maintenance-client@main` and
pass the generated client path to the product script. The workflow job must
grant `id-token: write` so the client can request a GitHub OIDC token for
Launchplane. The initial client covers VeriReel generated-user smoke
maintenance; add new Launchplane-owned clients for other products rather than
copying or generalizing product-specific route logic in product repos. The
product script may pass primitive smoke facts such as action, email, context,
instance, preview slug, and timeout. The client derives the Launchplane driver
intent for supported smoke actions. The product script should not own
Launchplane route paths, request envelopes, driver intent strings,
idempotency-key recipes, GitHub OIDC token exchange, retry behavior, or
driver-result failure rules.

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
  testing, production, release-tuple, and active-preview image references.

## Minimal Trigger Inputs

A product workflow should submit only the facts Launchplane cannot derive from
DB-backed profiles or GitHub OIDC claims:

- product key
- source ref or commit SHA
- immutable artifact or image reference
- PR number for preview actions
- explicit production confirmation for destructive or high-risk actions
- optional run URL for audit display

Launchplane should derive context, lane, preview slug, preview URL, target,
health path, feedback marker, provider credentials, managed secrets, and record
ids unless a driver-specific route documents an explicit exception.

## Approval Gate

A product repo is approved when all of these are true:

- Workflows build, test, and publish product artifacts, then trigger
  Launchplane. They do not directly mutate runtime providers.
- Scripts do not own Launchplane record or evidence shaping that Launchplane can
  derive from profiles, driver requests, provider results, or GitHub OIDC
  claims.
- Driver-trigger workflows rely on Launchplane routes to write the records for
  provider actions they execute. If product-specific smoke checks still run in
  the repo, the repo sends only primitive result facts back to Launchplane.
- Preview, deploy, promotion, rollback, and cleanup triggers pass minimal inputs
  only.
- Product-specific checks remain in the repo only when they validate product
  behavior rather than generic deploy plumbing.
- Removed scripts are unused or replaced by equivalent Launchplane routes with
  tests.
- CI and security gates pass after cleanup.
- At least one non-prod Launchplane path is exercised after the cleanup.

## Odoo Ownership Regression Check

Launchplane owns the Odoo ownership-boundary regression check. Run it from a
workspace that contains `launchplane` and the Odoo sibling repos:

```bash
uv run launchplane odoo-ownership check --workspace-root ..
```

The check is intentionally narrow. It allows product-owned source, tests,
artifact publishing, GHCR login, devkit local build/runtime behavior, and thin
Launchplane connectors through either:

- `cbusillo/launchplane/.github/actions/launchplane-request@main`
- `cbusillo/launchplane/.github/workflows/reusable-odoo-*.yml@main`

It blocks the patterns that previously caused ownership drift:

- repo-local GitHub OIDC token clients instead of the shared request action or
  reusable workflow
- repo-local Launchplane HTTP clients that duplicate the shared connector
- tenant workflows or scripts mutating Dokploy, SSH, compose, or other runtime
  providers directly
- devkit or retired repos exposing shared/prod mutation flows from arbitrary
  checkouts
- repo-local derivation of Launchplane-owned preview URLs, target IDs, release
  tuple IDs, deployment IDs, promotion IDs, or backup-gate IDs outside approved
  thin workflow response handling

When a product repo genuinely needs new source-adjacent facts, add a typed
Launchplane driver input or shared connector path before expanding the allowlist.
Do not copy a request client, provider planner, or durable-record builder into a
tenant, image, shared-addon, or local-DX repo.

Retired `odoo-ai` archival authority is intentionally handled by the separate
quarantine plan rather than this active-repo regression gate.
Known `odoo-devkit` Dokploy-managed local/remote runtime helpers are still
tracked by the local-DX separation plan; this check guards against new drift in
tenant, image, shared-addon, and workflow/script surfaces while that cleanup
continues.

## Cleanup Workflow

For an existing repo, classify each workflow and script before deleting code:

- `keep`: product build, test, lint, local dev, local DB, or real product smoke
  behavior.
- `move`: Launchplane lifecycle behavior that should become or already is a
  driver route.
- `delete`: stale compatibility code with no active caller or with a proven
  Launchplane replacement.
- `adapter`: temporary OIDC trigger glue. Prefer the reusable
  `cbusillo/launchplane/.github/actions/launchplane-request` GitHub Action for
  raw Launchplane HTTP calls, then keep only the product-specific payload
  assembly that cannot yet move into a driver route. When Launchplane owns the
  full handoff, product repos should call a Launchplane reusable workflow and
  keep only dispatch inputs, confirmation text, and product-owned build or test
  facts locally.

Odoo artifact publication now follows the reusable workflow shape: tenant repos
own the manual dispatch confirmation and the source workspace, while
`reusable-odoo-artifact-publish.yml` owns the Launchplane publish-input request,
artifact-record request, idempotency keys, and response mapping. The tenant
workflow should not duplicate `/v1/drivers/odoo/artifact-publish-inputs` or
`/v1/drivers/odoo/artifact-publish` wiring once it uses that workflow. The
tenant workflow must pass the Launchplane product key explicitly; reusable
workflows do not derive product identity from context names. Odoo dependency
repository identities for the devkit and shared addons are resolved by
`/v1/drivers/odoo/artifact-publish-inputs` from Launchplane runtime records and
returned as `devkit_repository` and `shared_addons_repository`, so product repos
do not carry those checked-in defaults either. Reusable Odoo workflows read the
Launchplane service URL from `LAUNCHPLANE_PUBLIC_URL` by default and derive the
GitHub OIDC audience from that URL host unless the caller passes an explicit
`launchplane_audience` input. The reusable jobs run on GitHub-hosted runners
because they call the deployed Launchplane service over HTTPS; product repos do
not need direct access to Launchplane self-hosted runners, and privileged
provider mutations still run inside the Launchplane service boundary.

Odoo testing deploys follow the same ownership shape. Tenant repos own the
manual dispatch confirmation and pass an explicit stored `artifact_id` plus
`source_git_ref` into `reusable-odoo-testing-deploy.yml`; the reusable workflow
calls `/v1/drivers/odoo/target-replacement-apply` with the explicit product key
provided by the caller. The Launchplane service owns the provider mutation,
runtime identity injection, Odoo post-deploy extension, stable readiness checks,
deployment and inventory records, and the testing release tuple.

Start with low-risk deletions and documentation, then replace active workflow
behavior in small slices. Do not remove active backup, promotion, rollback,
runtime health, or cleanup safety gates until Launchplane owns the equivalent
behavior and tests.

Registry artifact cleanup is a Launchplane-owned liveness question. Product
repos may still perform provider-specific registry deletion, but they must first
load Launchplane's protected artifact inventory, validate the response, and
abort without deleting anything when the inventory is unavailable, unauthorized,
or has unresolved live-artifact warnings for the registry being cleaned. Product
cleanup jobs should treat Launchplane-protected image references and artifact
ids as a deny set; they must not infer that testing, production, or active
preview artifacts are deletable from local tag shape alone.

Cleanup consumers must check both `artifact_ids` and `image_references` from the
protected inventory. Some active-preview protections come from ready PR feedback
records that carry immutable and refresh image references but no artifact id, so
an artifact-id-only cleanup filter can still delete a live preview tag. Whole-
product cleanup callers should request `GET /v1/artifacts/protected?product=...`
with an `artifact_protection.read` grant that allows wildcard context for that
product; context-specific cleanup may pass `context=` and use a matching scoped
grant.

## Canonical Image Deploy Connector

Image-backed generic-web deploy is the canonical stable product-repo connector
for simple service and website repos. The product repo builds and publishes an
immutable image, then calls deployed Launchplane over the shared request action.
Launchplane resolves product profile, lane, provider target, runtime
environment, managed secrets, authz, idempotency, and deployment evidence from
service-owned records.

The first real-world proof for this shape is RepairShopr Sync: after PR #1503
deployed Launchplane commit `9dbdf15904a86eea2b742f1e42e341db138e4860`,
`cbusillo/repairshopr_api` Launchplane Deploy run `28415366430` attempt 4
passed through `/v1/drivers/generic-web/deploy`. The product workflow supplied
`ghcr.io/cbusillo/repairshopr_api@sha256:7efdbf139f7f5263c02d38509841253e719a41c44c55aa79aa4a223379808eea`
as `artifact_id`; Launchplane returned deployment record
`deployment-20260630T034901Z-repairshopr-sync-prod`, `deploy_status: pass`,
target `cm-repairshopr-sync`, target category `compose`, provider `dokploy`,
and `post_deploy_status: skipped`. That run is the recovery evidence for this
contract and the baseline for retiring older source-ref or checkout-and-invoke
patterns.

For new or repaired product repos, prefer this connector over product-repo
checkout of Launchplane source or direct invocation of Launchplane internals.
If a repo still needs a compatibility bridge, the bridge must have an issue
reference, a dated owner, and a delete condition.

## Reusable Generic-Web Lifecycle Workflows

Generic-web product repos should prefer Launchplane-owned reusable workflows
before calling raw driver routes. The reusable workflow owns the Launchplane
route path, request JSON shape, response-output mapping, and run-scoped
idempotency key. The product repo supplies only primitive facts: immutable
artifact identity and tested source git ref. The stable deploy workflow derives
the product key from the caller repository name by default and uses the
`testing` stable lane unless the caller supplies a narrower operator override.

Stable deploy uses:

```yaml
jobs:
  launchplane-deploy:
    uses: cbusillo/launchplane/.github/workflows/reusable-generic-web-stable-deploy.yml@main
    with:
      artifact_id: ${{ needs.build.outputs.image_digest }}
      source_git_ref: ${{ github.sha }}
```

Production promotion uses:

```yaml
jobs:
  launchplane-prod-promotion:
    uses: cbusillo/launchplane/.github/workflows/reusable-generic-web-prod-promotion.yml@main
    with:
      product: ${{ vars.LAUNCHPLANE_PRODUCT }}
      artifact_id: ${{ needs.build.outputs.image_digest }}
      source_git_ref: ${{ github.sha }}
```

Preview refresh, destroy, and unsupported-notice handoff use:

```yaml
jobs:
  launchplane-preview:
    uses: cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-lifecycle.yml@main
    with:
      operation: refresh
      anchor_pr_number: ${{ github.event.pull_request.number }}
      anchor_pr_url: ${{ github.event.pull_request.html_url }}
      anchor_head_sha: ${{ github.event.pull_request.head.sha }}
      image_reference: ${{ needs.build.outputs.image_digest }}
```

Destroy calls set `operation: destroy` and pass `destroy_reason`. Fork and
Dependabot `unsupported_notice` handoffs call
`cbusillo/launchplane/.github/workflows/reusable-preview-request-notice.yml@main`
from a trusted `pull_request_target` workflow. Product repos do not choose a
trusted checkout ref, pass preview slugs, preview URLs, provider application
names, feedback context, feedback markdown, route payloads, or idempotency keys;
Launchplane derives those from product profiles, runtime records, GitHub OIDC
claims, the PR event, and the run-scoped workflow context.

These reusable workflows intentionally do not accept provider targets, target
ids, health URLs, preview URLs, feedback markdown, record ids, managed secrets,
runtime environment values, or idempotency keys. Launchplane derives or records
those values from product profiles, lane profiles, provider-target records,
runtime-environment records, managed secret bindings, GitHub OIDC claims, driver
results, and durable evidence. The reusable deploy workflow is the first
non-prod proof path for #1528; product-repo cleanup should wait for a live
non-prod deploy proof before deleting older local request-shaping scripts.

## Reusable Product-Driver Workflows

Some products still need product-driver actions while their product-specific
post-deploy, maintenance, backup, or rollback behavior is being generalized.
Product repos should call Launchplane-owned product-driver reusable workflows
instead of carrying local `request-launchplane-*` scripts. These workflows keep
the driver route path, envelope JSON, output mapping, polling settings, and
idempotency key in Launchplane while allowing the product repo to pass primitive
facts such as product, context, instance, artifact id, source git ref, backup
record id, and product-owned verification statuses. The route ids are
Launchplane-owned driver routes; product repos should not derive them from
product keys or keep local copies of route construction logic.

The product-driver workflow surface for stable deploy, stable environment, and
app-maintenance defaults to the `testing` lane so a testing publish workflow can
pass only the primitive facts it owns, such as immutable artifact identity,
tested source git ref, and operation-level maintenance intent.
The app-maintenance connector defaults to the VeriReel driver for compatibility,
and callers can pass `driver: odoo` for the narrow Odoo post-deploy maintenance
adapter backed by `/v1/drivers/odoo/app-maintenance`.
Product repos should pass an explicit `instance` only for a workflow whose
operator input or job purpose genuinely selects a different lane.
Production readiness wrappers may also accept expected runtime build identity
from operator input or upstream workflow evidence and forward it to
Launchplane-owned runtime verification.

The product-driver reusable surface is:

- `reusable-product-driver-stable-environment.yml@main`
- `reusable-product-driver-runtime-verification.yml@main`
- `reusable-product-driver-stable-deploy.yml@main`
- `reusable-product-driver-app-maintenance.yml@main`
- `reusable-product-driver-post-deploy.yml@main`
- `reusable-product-driver-testing-verification.yml@main`
- `reusable-product-driver-testing-reset.yml@main`
- `reusable-product-driver-prod-backup-gate.yml@main`
- `reusable-product-driver-prod-launch-readiness.yml@main`
- `reusable-product-driver-prod-promotion.yml@main`
- `reusable-product-driver-prod-rollback.yml@main`

These workflows are transitional connectors, not permission to move product
lifecycle authority back into product repos. A product repo may still own image
build/publish, release tagging, and product-specific browser checks, but it
should not own Launchplane route construction, request envelopes, idempotency
recipes, polling behavior, or record-output extraction once a reusable workflow
exists.

`reusable-product-driver-post-deploy.yml@main` preserves explicit driver
post-deploy phases for existing Odoo refresh, manual, promotion, and deploy
callers while moving request shaping into Launchplane. Odoo callers can migrate
from `reusable-odoo-post-deploy.yml@main` without adding a driver input because
the product-driver wrapper defaults to `driver: odoo`; they must still pass the
same explicit `product`, `context`, `instance`, and `phase` inputs. App
maintenance remains the deploy-phase maintenance wrapper for stable lane
operations; product repos should not use it as a generic phase-aware post-deploy
substitute.

When a workflow operation has implied lane or maintenance semantics, prefer an
operation-level reusable workflow over passing checked-in lane, action, or
intent strings from the product repository. For example, a product repo should
call `reusable-product-driver-testing-reset.yml@main` rather than wiring
`instance: testing`, `action: reset-testing`, and `intent:
stable-testing-reset` itself. Product-owned smoke checks may still consume
Launchplane reusable outputs, such as a resolved `primary_base_url`, as
pass-through evidence for product behavior checks.

Generated-user smoke setup that must happen inside a product browser flow should
use `setup-smoke-maintenance-client@main` instead of a workflow-level
app-maintenance call. The setup action writes an importable Node ESM client into
the product job so the browser script can keep its dynamic test email while
Launchplane owns request shaping, driver intent derivation, and OIDC transport.
The job using the generated client must include `permissions: id-token: write`.

Same-repository preview jobs that need label normalization and preview image tag
derivation should use `setup-preview-prepare-client@main` instead of carrying a
product-local helper. The generated Node ESM client returns refresh/noop or
unsupported mode, same-repo support flags, `pr-<number>` image tags, and full
caller-supplied image references from primitive GitHub event facts. It is a
read-only adapter for product-owned artifact publication; Launchplane records
still own preview URL policy, provider targets, lifecycle records, comments, and
cleanup truth.

## Reusable Launchplane Request Action

Product workflows that only need to send JSON to an existing Launchplane route
should not carry their own GitHub OIDC transport client. Use the Launchplane
repo action instead. Raw action calls are a lower-level compatibility surface
when a Launchplane-owned reusable workflow does not exist yet.

Generic web/service repos that deploy as Dokploy applications should build and
push their own immutable image, then submit the image digest to Launchplane's
`generic-web` deploy route. The workflow may derive the GHCR repository from the
current GitHub repository, publish with `docker/login-action` and
`docker/build-push-action`, then pass the product key, stable-lane intent, tested
source SHA, and immutable image digest. While the current `generic-web/deploy`
route still requires context or instance compatibility fields, product workflows
may supply those values from operator-seeded GitHub variables as scoped adapter
inputs. They are not checked-in product topology or durable lifecycle authority,
and #1528 owns reducing that bridge behind Launchplane-owned reusable lifecycle
contracts. The checked-in workflow must not hard-code provider targets, Dokploy
operations, runtime domains, managed secrets, or fixed product topology;
Launchplane resolves those from DB-backed product and target records.

For this compatibility shape, `.github/workflows/launchplane-deploy.yml` is the
supported thin connector workflow name. It should call:

```yaml
- name: Request Launchplane deploy
  uses: cbusillo/launchplane/.github/actions/launchplane-request@main
  with:
    launchplane-url: ${{ vars.LAUNCHPLANE_PUBLIC_URL }}
    route-path: /v1/drivers/generic-web/deploy
    payload-file: ${{ steps.launchplane_payload.outputs.payload_path }}
    idempotency-key: >-
      generic-web-deploy:${{ vars.LAUNCHPLANE_PRODUCT }}:${{ vars.LAUNCHPLANE_CONTEXT }}:${{ vars.LAUNCHPLANE_INSTANCE }}:${{ github.event.workflow_run.head_sha }}:${{ steps.launchplane_payload.outputs.artifact_id }}
```

The payload should include the product key, stable-lane intent required by the
current route, immutable image digest as `artifact_id`, and tested source SHA.
Mutable image tags and checked-in image references are not durable deploy
inputs.

```yaml
- name: Request Launchplane preview refresh
  uses: cbusillo/launchplane/.github/actions/launchplane-request@main
  with:
    launchplane-url: ${{ vars.LAUNCHPLANE_URL }}
    audience: ${{ vars.LAUNCHPLANE_AUDIENCE }}
    route-path: /v1/drivers/generic-web/preview-refresh
    payload-file: ${{ runner.temp }}/launchplane-preview-refresh.json
    idempotency-key: >-
      generic-web-preview-refresh:${{ github.event.number }}:${{ github.sha }}
    timeout-ms: "900000"
    output-paths: >-
      refresh_status=result.refresh_status,
      application_id=result.application_id,
      preview_url=result.preview_url,
      error_message=result.error_message
```

The preview refresh payload should identify the product, PR number, immutable
image reference, source SHA, and optional PR URL. New product workflows should
omit `preview_slug` and `preview_url`; Launchplane derives the preview slug from
the product profile slug policy and derives the live URL from the product
preview context and `LAUNCHPLANE_PREVIEW_BASE_URL`. `preview_slug` and
`preview_url` are compatibility overrides for older callers, and a supplied slug
must match the Launchplane-derived slug when PR identity is also present.

The action requests a GitHub OIDC token, sends the JSON request with a stable
`Idempotency-Key`, exposes the raw response body, and can map response JSON paths
to GitHub outputs. When a later product step needs a JSON file instead of a
scalar output, set `response-output-file` and, optionally,
`response-output-path` to write the full response or a nested response value.
Use `payload-fields` for small workflow-input overlays instead of a repo-local
JSON builder when the base request is already static:

```yaml
payload: >-
  {"schema_version":1,"product":"odoo","rollback":{"schema_version":1}}
payload-fields: |-
  rollback.context=cm
  rollback.instance=prod
  rollback.reason=${{ github.event.inputs.reason }}
```

Each `payload-fields` line is `json.path=value`. Values are strings unless they
parse as JSON literals or objects, so `false`, `300`, and `{}` keep their JSON
types. Use `payload-json-files` when a workflow already has a JSON artifact file
and only needs to splice it into a static Launchplane request:

```yaml
payload: >-
  {"schema_version":1,"product":"odoo","publish":{"schema_version":1}}
payload-fields: |-
  publish.context=cm
  publish.instance=${{ github.event.inputs.instance }}
payload-json-files: |-
  publish.manifest=${{ steps.publish.outputs.manifest_file }}
```

Each `payload-json-files` line is `json.path=file-path`; the action parses the
file as JSON and writes that value into the request before sending it.

For asynchronous Launchplane routes that report a temporary status, configure
polling instead of reimplementing OIDC and retry logic in the product repo:

```yaml
- name: Request Launchplane backup gate
  uses: cbusillo/launchplane/.github/actions/launchplane-request@main
  with:
    launchplane-url: ${{ vars.LAUNCHPLANE_URL }}
    audience: ${{ vars.LAUNCHPLANE_AUDIENCE }}
    route-path: /v1/drivers/verireel/prod-backup-gate
    payload: ${{ steps.backup_payload.outputs.payload }}
    idempotency-key: ${{ steps.backup_payload.outputs.idempotency_key }}
    poll-result-path: result.backup_status
    poll-result-statuses: pending
    poll-interval-ms: "30000"
    poll-timeout-ms: "2400000"
    fail-result-paths: result.backup_status
    output-paths: >-
      backup_status=result.backup_status,
      snapshot_name=result.snapshot_name,
      backup_gate_record_id=records.backup_gate_record_id
```

Polling repeats the same idempotent request while the configured JSON path
matches a polling status. After polling finishes, the normal fail-result and
output mapping rules still apply.

## New Repo Checklist

When creating a new website repo for Launchplane:

- Build the app as a normal product repo first.
- Add a health endpoint that returns enough non-secret version data for
  Launchplane to verify the deployed artifact. New products should expose the
  Launchplane runtime identity env payload from `LAUNCHPLANE_RUNTIME_IDENTITY_JSON`
  or the equivalent discrete env keys, then mark lanes as requiring runtime
  identity after the echo is verified.
- Publish immutable container images or artifacts from GitHub Actions.
- Apply an operator-owned Launchplane product onboarding manifest to seed the
  product profile, lane profiles, target records, runtime environment, disabled
  managed secret binding placeholders, and then update DB-backed authz policy in
  Launchplane.
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
