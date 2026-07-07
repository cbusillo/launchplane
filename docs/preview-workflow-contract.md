---
title: Reusable Preview Workflow Contract
---

## Purpose

Product preview workflows should be thin event adapters. Product repos build and
publish the product artifact; Launchplane owns preview event semantics,
idempotency, provider mutation, durable records, unsupported notices, feedback
comments, inventory, and cleanup.

This contract is the migration target for preview-enabled product repos that
currently carry bespoke preview-control-plane workflows.

## Ownership Boundary

Product repos own:

- GitHub event wiring for `pull_request`, `pull_request_target`, and manual
  `workflow_dispatch` entrypoints.
- Product build, test, package, and immutable artifact publishing.
- Product-specific smoke facts that Launchplane cannot derive from a product
  profile.
- The minimal Launchplane request payload containing product key, PR number,
  source SHA, immutable artifact reference, run URL, and primitive smoke result.

Launchplane owns:

- Preview-request interpretation and idempotency conventions.
- Preview refresh, destroy, inventory, readiness, and lifecycle cleanup routes.
- Scheduled lifecycle sweeps for every product profile where
  `preview.enabled=true`.
- Preview records, generation records, desired-state records, cleanup records,
  and PR feedback records.
- Unsupported notices for fork and Dependabot preview requests.
- PR comment rendering, delivery, replacement, and cleanup.
- Provider credentials, runtime settings, managed secrets, preview URL policy,
  and preview slug policy.

Product workflows must not render Launchplane preview comments directly or keep
their own durable lifecycle truth once a matching Launchplane route exists.

## Event Contract

Use `PreviewWorkflowEvent` and `decide_preview_workflow_operation` from
`control_plane.contracts.preview_workflow_contract` as the shared event contract.
The decision is deliberately small:

- `refresh`: build and publish the product artifact, then call the product
  driver's preview-refresh route.
- `destroy`: call the product driver's preview-destroy route.
- `unsupported_notice`: call `POST /v1/previews/pr-feedback` with
  `status="unsupported"` from a trusted base-branch workflow.
- `ignore`: make no Launchplane mutation.

The same contract applies to SYO, VeriReel, Odoo CM, and future products. Driver
routes may differ by product family, but event interpretation stays the same.

Product repo adapters can ask Launchplane to render that decision from a GitHub
event payload before doing product-owned work:

```shell
uv run launchplane work-graph preview-workflow-decision \
  --event-file "$GITHUB_EVENT_PATH" \
  --event-name "$GITHUB_EVENT_NAME" \
  --product sell-your-outboard \
  --context sellyouroutboard-testing \
  --run-id "$GITHUB_RUN_ID" \
  --run-attempt "$GITHUB_RUN_ATTEMPT"
```

The command is read-only. It does not contact GitHub, call Launchplane service
routes, check out pull-request code, or mutate a provider. It emits the normalized
event, decision, route path, feedback status, and run-scoped idempotency key as
JSON so product workflows can branch on the shared contract instead of
duplicating event semantics.

Same-repository preview build jobs that need an importable helper for GitHub
event labels and image tags should use
`cbusillo/launchplane/.github/actions/setup-preview-prepare-client@main`. The
generated client is read-only and product-agnostic: callers pass the current
repository, head repository, PR author, PR number, source SHA, image name,
labels, and run URL; the client returns refresh/unsupported/noop mode, same-repo
support flags, `pr-<number>` image tags, and full image references. It does not
call Launchplane, choose provider targets, render comments, derive preview URLs,
or store lifecycle truth.

Once the product workflow has decided to refresh, destroy, or send an
unsupported notice, it should hand off to Launchplane's reusable workflow instead
of constructing route payloads locally:

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

The reusable workflow derives the product key from the caller repository by
default, derives a run-scoped idempotency key, calls the correct Launchplane
route, and exposes the returned preview slug, preview URL, refresh status,
destroy status, or feedback status as job outputs. Callers should omit preview
context so Launchplane can derive it from the product profile before
authorization and recording. It does not accept `preview_slug`, `preview_url`,
provider target ids, feedback markdown, or idempotency keys as caller inputs.

Preview comment updates that are not part of the lifecycle workflow use
`cbusillo/launchplane/.github/workflows/reusable-preview-pr-feedback.yml@main`.
Callers provide only primitive display facts such as PR number, status, preview
URL, image references, revision, run URL, and failure summary. The reusable
feedback workflow owns the `/v1/previews/pr-feedback` route, marker, payload
shape, delivery behavior, and run-scoped idempotency key. Callers may omit
preview context; Launchplane derives it from the product profile before
authorization and recording.

## Required Workflow Shape

Same-repository PRs use `pull_request` because the workflow may check out and
build the PR head:

- `opened`, `reopened`, `synchronize`, `edited`, or adding the preview label
  with the label present: `refresh`.
- `closed`: `destroy`.
- Removing the preview label: `destroy`.
- Any PR without the preview label: `ignore`.

Fork and Dependabot PRs use `pull_request_target` only for an unsupported notice.
That job must run from the base branch and call
`cbusillo/launchplane/.github/workflows/reusable-preview-request-notice.yml@main`.
The reusable workflow owns the trusted event decision, unsupported/cleared
status selection, failure summary, and `/v1/previews/pr-feedback` handoff.
Product repos must not check out code, choose a checkout ref, render feedback
markdown, build request payloads, or call `POST /v1/previews/pr-feedback`
directly from their own fork/Dependabot notice workflows.

Manual `workflow_dispatch` may request `refresh` or `destroy` when a product repo
needs an operator retry path. Manual refresh still follows the same build,
publish, and Launchplane-refresh handoff as a PR refresh.

## Idempotency

Every Launchplane mutation from a product preview workflow needs a stable
idempotency key. Use `preview_workflow_idempotency_key` or the same shape:

```text
preview-workflow:<product>:<context>:<operation>:pr-<number>:<run-id>:<run-attempt>
```

Run-scoped keys make repeated HTTP attempts safe while preserving a distinct
record for a later retry or GitHub rerun. Ignored decisions do not have
idempotency keys. Launchplane-owned reusable workflows may use an equivalent
route-specific key when the service derives context from product records.

Preview refresh readiness should allow the serving runtime identity to converge
after a provider deploy trigger. A health response that still reports the prior
deployment/artifact/source identity for the same product, context, preview slug,
and environment kind remains a polling signal until the route timeout; a
different product, context, preview slug, or environment kind remains a hard
mismatch.

## Route Handoff

Preview refresh routes receive only product-local facts:

- product key, and context only for compatibility routes that still require it
- PR number and source SHA
- immutable image or artifact reference
- run URL
- primitive smoke/readiness facts when the check is product-specific

Generic-web preview refresh callers should pass `anchor_pr_number` and omit
context, `preview_slug`, and `preview_url`. Launchplane derives the context from
the product profile, derives the slug from the product profile slug policy,
derives the live URL from the preview context's runtime environment records, and
rejects a supplied slug when it conflicts with the derived value. `context`,
`preview_slug`, and `preview_url` remain compatibility fields for older
adapters, not product-repo authority.

Launchplane also owns the reusable request-shape builders for Odoo tenant
preview workflows. Tenant repos may keep thin adapter jobs for checkout, image
publication, runner selection, and product smoke facts, but the route paths,
payload skeletons, JSON-file bindings, fail-result paths, and run-scoped
idempotency keys for `artifact-publish-inputs`, `preview-apply-inputs`, and
`preview-apply` are Launchplane contract fixtures. New or migrated tenant
preview workflows should install
`cbusillo/launchplane/.github/actions/setup-odoo-preview-request-client@main`
and import the generated ESM client to render `launchplane-request` inputs
instead of copying inline route paths, JSON request bodies, file bindings, fail
paths, or idempotency key templates. The generated request object exposes
`payloadInput`, `payloadJsonFilesInput`, `failResultPathsInput`,
`responseOutputPath`, and `idempotencyKey` fields shaped for direct handoff to
the existing `launchplane-request` action.

Odoo CM is the exception where Launchplane now owns both the isolated provider
apply planning inputs and the stage-preview smoke contract after refresh. Product
workflows should call `POST /v1/drivers/odoo/preview-apply-inputs` with PR,
image, and source facts, then pass a ready redacted dry-run plan to
`POST /v1/drivers/odoo/preview-apply` instead of assembling runtime bindings,
Dokploy environment ids, template compose ids, or Odoo database and volume names
inside the tenant repo. The route discovers existing Odoo preview composes from
provider inventory for refresh reuse and destroy planning, and it blocks destroy
when the matching preview compose and hostname cannot be proven. After refresh,
Launchplane owns `/launchplane/health`, `/web/health`, `/cm-website/health`, `/cell-mechanic`,
artifact/revision evidence, and module install/update evidence. Product
workflows should treat the Odoo refresh route's `refresh_status="pass"` as the
ready-to-comment signal instead of independently deciding readiness from raw
health checks.
If a later browser or product-specific smoke workflow needs to publish common
preview evidence, it should call
`cbusillo/launchplane/.github/workflows/reusable-generic-web-preview-verification.yml@main`
with the PR number, `verification_status`, `verified_at`, optional checked URLs
plus timeout, and optional failure summary. The reusable workflow owns the
`POST /v1/drivers/generic-web/preview-verification` payload, route handoff, and
run-scoped idempotency key. Launchplane resolves the preview context and
generic-web base-driver compatibility from the product profile, updates the
latest preview generation, and returns a typed `generic_web_preview_verification`
result while preserving durable status/failure evidence in the same preview
records used by refresh. Odoo preview verification uses this generic-web route;
the former Odoo-shaped preview verification alias is retired.

Preview destroy routes receive the PR number, source/run metadata, and an
explicit destroy reason such as `pull_request_closed`, `preview_label_removed`,
or `manual_destroy_requested`. Generic-web preview destroy follows the same
context and slug policy as refresh: callers should pass PR identity, and
Launchplane derives the preview context and preview slug from the product
profile before provider deletion.

Preview feedback routes receive the status and primitive display facts.
Launchplane derives the marker, rendered markdown, delivery behavior, and record
id. If the feedback comment cannot be delivered, Launchplane records the skipped
or failed feedback result and, when a preview PR feedback notification policy is
configured, emits operator notification attempts from the control plane. Product
workflows should not render fallback PR comments themselves; missing runtime
GitHub credentials and GitHub API failures are Launchplane-owned operator
signals.

Product repos should call the reusable preview feedback workflow instead of
assembling `/v1/previews/pr-feedback` payloads, markers, or idempotency keys in
repo-local scripts.

Use `cbusillo/launchplane/.github/actions/launchplane-request@main` for the OIDC
transport only when a Launchplane-owned reusable workflow does not exist yet.

## Migration Checklist

- Replace product-rendered preview comments with `/v1/previews/pr-feedback`.
- Move unsupported fork and Dependabot notices to a base-branch
  `pull_request_target` job that does not check out PR code.
- Replace bespoke refresh/destroy event branching with the shared decision
  contract.
- Keep product build/publish/smoke logic local until a Launchplane driver route
  owns the equivalent facts.
- Use run-scoped idempotency keys for every Launchplane mutation.
- Keep product configuration, preview URL policy, provider credentials, and
  cleanup truth in Launchplane records rather than product-repo files.
- Verify one refresh, one destroy, and one unsupported-notice path per migrated
  product repo.

## Source Workflows

This contract was shaped from the current preview paths in SYO, VeriReel, and
Odoo CM. Odoo CM is the reference thin workflow after its preview feedback moved
to Launchplane. SYO and VeriReel should migrate toward the same event contract
before deleting their bespoke preview-control-plane logic.
