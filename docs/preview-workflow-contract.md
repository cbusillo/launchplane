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

## Required Workflow Shape

Same-repository PRs use `pull_request` because the workflow may check out and
build the PR head:

- `opened`, `reopened`, `synchronize`, `edited`, or adding the preview label
  with the label present: `refresh`.
- `closed`: `destroy`.
- Removing the preview label: `destroy`.
- Any PR without the preview label: `ignore`.

Fork and Dependabot PRs use `pull_request_target` only for an unsupported notice.
That job must run from the base branch, must not check out untrusted PR code, and
must only call `POST /v1/previews/pr-feedback` with `status="unsupported"`.
Launchplane will render and deliver the comment.

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
idempotency keys.

## Route Handoff

Preview refresh routes receive only product-local facts:

- product key and context when the route still requires them
- PR number and source SHA
- immutable image or artifact reference
- run URL
- primitive smoke/readiness facts when the check is product-specific

Odoo CM is the exception where Launchplane now owns the stage-preview smoke
contract after refresh: `/web/health`, `/cm-website/health`, `/cell-mechanic`,
artifact/revision evidence, and module install/update evidence. Product
workflows should treat the Odoo refresh route's `refresh_status="pass"` as the
ready-to-comment signal instead of independently deciding readiness from raw
health checks.
If a later browser or product-specific smoke workflow needs to publish evidence,
it should call `POST /v1/drivers/odoo/preview-verification` with the PR identity,
`verification_status`, `verified_at`, and optional failure summary. Launchplane
updates the latest preview generation and preserves the result in the same
preview records used by refresh.

Preview destroy routes receive the PR number, source/run metadata, and an
explicit destroy reason such as `pull_request_closed`, `preview_label_removed`,
or `manual_destroy_requested`.

Preview feedback routes receive the status and primitive display facts.
Launchplane derives the marker, rendered markdown, delivery behavior, and record
id.

Use `cbusillo/launchplane/.github/actions/launchplane-request@main` for the OIDC
transport whenever a product workflow only needs to send JSON to Launchplane.

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
