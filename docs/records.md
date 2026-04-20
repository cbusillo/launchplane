---
title: Records
---

## Storage Policy

- Persist records as JSON files in a local state directory.
- Keep git history separate from operational history.
- Favor append-style writes for promotion records.

This file layout describes today's local Harbor implementation, not the final
cross-product communication boundary. The stable long-term contract should be
Harbor's authenticated service ingress plus the durable record semantics those
API payloads map onto.

These records are the durable Odoo-first Harbor truth for this repo today.
Stable lane records (`testing`, `prod`) and preview records are separate on
purpose: previews are not another long-lived environment lane.

The current cross-product posture is evidence-first. A second product such as
VeriReel should first land in these existing Harbor record shapes through
deployment, promotion, inventory, and preview evidence ingestion before this
control plane takes over product-specific runtime actions.

Under the target Harbor shape, product workflows and drivers should speak in
typed evidence payloads. Harbor may store those facts in file-backed JSON while
it still lives in this repo, but callers should treat the durable record model
as canonical and the storage engine as replaceable.

## Layout

```text
state/
  artifacts/
    <artifact-id>.json
  backup_gates/
    <record-id>.json
  deployments/
    <record-id>.json
  harbor_preview_generations/
    <generation-id>.json
  harbor_preview_enablements/
    <enablement-id>.json
  harbor_previews/
    <preview-id>.json
  promotions/
    <record-id>.json
  inventory/
    <context>-<instance>.json
  release_tuples/
    <context>-<channel>.json
```

## Artifact Manifest

- One file per immutable artifact identifier.
- Record the public app commit, private enterprise digest, and final image
  identity.
- Preserve build-affecting addon, OpenUpgrade, and flag inputs alongside the
  image identity so the control plane owns the full manifest instead of a thin
  image pointer.
- Use generic artifact vocabulary at the record level, but keep Odoo-specific
  source inputs explicit in the stored evidence.

## Promotion Record

- One file per promotion attempt.
- Record source, destination, artifact id, gate evidence, deploy evidence, and
  destination health.
- Promotion records can also carry `deployment_record_id` so Harbor can
  refresh current inventory from externally produced promotion evidence
  without guessing which deployment record established the promoted state.
- Promote inputs should reference the immutable artifact id directly.
- Promotion records also persist the authorizing `backup_record_id` so
  current inventory can be traced back to the exact stored backup-gate record
  that authorized the live promotion.
- Promotion execution should normalize backup-gate evidence from a stored
  backup-gate record instead of trusting ad-hoc inline request payloads.
- Promotion execution also resolves the deployable ship request natively in
  `odoo-control-plane` from this repo's Dokploy source-of-truth, instead of
  shelling out for a pre-rendered JSON request.
- Promotion execution requires the source lane to have a current release tuple
  record for the requested artifact before it can deploy to the destination.
- For a second product such as VeriReel, promotion evidence from the existing
  production-promotion workflow is the smallest proof point that this record
  shape works beyond Odoo.

## Backup Gate Record

- One file per backup gate run that can authorize a promotion.
- Record the destination environment, evidence source, pass/fail status, and
  concrete backup evidence such as snapshot or archive identifiers.
- Promotion execution should fail closed unless the referenced backup-gate
  record exists, targets the same destination environment, and has `status`
  `pass`.

## Deployment Record

- One file per direct ship attempt owned by `odoo-control-plane`.
- Record the requested source git ref, target, deploy status, recorded
  executor, post-deploy update evidence, and destination health evidence.
- Ship execution no longer delegates runtime deploy/update work back to
  another repo; the durable deploy record belongs entirely here.
- The final deployment status also reflects control-plane-owned health
  verification rather than relying on delegated runtime steps to make that final
  readiness call.
- Deployment records also persist the resolved Dokploy target so the
  control plane owns the exact runtime target identity used for the deploy.
- The recorded executor reflects control-plane-owned Dokploy execution,
  including the compose post-deploy update schedule workflow when it applies.
- Deploy execution drives the Dokploy image selection from stored artifact
  manifests when possible by syncing an exact
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` override before the deploy starts.
- Native ship/deploy records do not persist branch-mutation evidence because
  branch movement is not part of artifact-backed execution.
- When no stored artifact manifest is available for a direct ship, deploy
  execution fails closed.
- Deployment records make the native follow-up step explicit by
  recording whether the Odoo-specific compose post-deploy update was skipped,
  pending, passed, or failed.
- Direct `ship` and `promote` execution fail closed if the referenced artifact
  id does not already have a stored manifest in control-plane state.
- Artifact manifests may also carry `addon_selectors` metadata so operators can
  inspect the original selector intent, but `addon_sources` remains the exact
  SHA-backed release truth used for tuple minting and deploy execution.
- For a second product such as VeriReel, the first Harbor onboarding slice
  should ingest deployment evidence from that product's existing release
  workflows into this record shape before Harbor owns the deploy execution.

## Release Tuple Record

- Release tuple records are keyed by long-lived environment channel.
- Successful waited `ship` executions for `testing` and `prod` mint the current
  channel tuple from the stored artifact manifest after deploy evidence passes.
- Tuple minting requires artifact manifest source refs to be exact git SHAs;
  branch names such as `main` or `origin/testing` are rejected instead of being
  written as release truth.
- Promotion execution copies the source channel tuple to the destination
  channel after the destination deploy passes, retaining the promotion and
  deployment record ids that established the promoted state.
- Externally produced promotion evidence can mint the same destination tuple
  through `release-tuples write-from-promotion` when the stored promotion
  record carries explicit `deployment_record_id` linkage and Harbor already has
  the current source tuple for the promoted-from lane.
- Harbor previews are not long-lived release-tuple channels; they derive their
  baseline from stored tuple evidence plus preview generation records.
- Runtime tuple records live under `state/` and do not rewrite the tracked
  default TOML catalog implicitly.

## Harbor Preview Record

- One file per stable Harbor preview identity.
- Record the anchor PR identity, deterministic preview label, canonical preview
  URL, lifecycle timestamps, current preview state, and the active/serving/
  latest generation links.
- Preview records model the durable Harbor identity for PR review, while the
  underlying preview runtime remains ephemeral and replaceable.
- Destroyed previews should remain readable durable evidence instead of being
  removed from state.
- Preview records should preserve one stable identity per anchor PR even when
  Harbor replaces the serving generation over time.
- The initial explicit mutation surface is `harbor-previews write-preview`,
  which builds the stored record from typed request input plus the dedicated
  Harbor preview base-url runtime contract.
- Preview mutations may also carry an explicit `canonical_url` when the live
  preview route is produced outside Harbor, so a second product can land
  preview evidence in the same record shape without first adopting Harbor-
  managed routing.
- Higher-level transition commands may also rewrite preview records through the
  tested Harbor transition helpers so operators do not have to hand-edit link
  fields for common lifecycle states.
- For a second product such as VeriReel, preview-control-plane and cleanup
  workflow evidence is the first candidate source for proving this preview
  model without forcing Harbor to provision or destroy those previews itself
  on day one.
- `harbor-previews write-destroyed` is the matching cleanup-evidence ingest
  surface for that model: it accepts typed teardown evidence and applies the
  stored destroyed transition without implying Harbor executed the cleanup.
  Under the target Harbor service shape, that same payload should enter through
  authenticated API ingress rather than a repo-local CLI command.

## Harbor Preview Generation Record

- One file per Harbor preview generation.
- Record the resolved manifest fingerprint, exact repo-to-SHA source map,
  baseline release tuple, artifact identity, health evidence, and failure
  details when a replacement does not become ready.
- Generation history should remain ordered and inspectable even when the latest
  generation failed and an older generation is still serving.
- Harbor read models should derive status/list/history payloads from these
  durable generation facts rather than storing separate page blobs.
- The initial explicit mutation surface is `harbor-previews write-generation`,
  which requires an existing preview record and can assign the next sequence
  automatically when the input does not pin one.
- Higher-level transition commands such as generation request/ready/failed
  reuse the same stored generation records while updating preview linkage
  semantics through the Harbor transition helpers.
- `harbor-previews write-from-generation` is the first explicit evidence-ingest
  surface for that path: it accepts typed preview plus generation evidence,
  writes the generation record, and refreshes the preview linkage according to
  the ingested generation state.
- Together with `harbor-previews write-destroyed`, Harbor can now ingest the
  full external preview lifecycle: create or refresh route evidence, persist
  generation outcome, and record confirmed cleanup.
- Those CLI surfaces should be treated as temporary adapters for the target
  Harbor API payloads, not as the final integration boundary external products
  are expected to couple to forever.

## Harbor Preview Enablement Record

- One file per tenant PR enablement snapshot.
- Record the anchor PR identity, enablement state, normalized preview-request
  metadata, candidate/request evidence, and timestamps.
- PR ingest and `harbor-previews write-enablement` write the same typed record
  shape so webhook and non-webhook flows preserve comparable evidence.

## Inventory

- Inventory records are keyed by environment.
- Inventory may be replaced in place because it represents current state rather
  than append-only event history.
- Inventory records capture the current deployed source git ref, artifact
  identity when known, deploy evidence, post-deploy update evidence,
  destination health, and the deployment/promotion records that established the
  current state.
- The CLI status/read-model commands are expected to compose inventory with the
  linked promotion, deployment, and backup-gate records rather than forcing
  operators to open those files directly.
- Successful waited `ship` executions refresh inventory directly from the final
  deployment record.
- Successful waited `promote` executions refresh the same inventory record and
  add promotion linkage so the current state can still be tied back to the
  controlling promotion and deployment records.
- For a second product such as VeriReel, inventory should first be derived from
  ingested deployment/promotion evidence before Harbor becomes the runtime
  executor for that product. The first explicit mutation surfaces for that are
  `inventory write-from-deployment` and `inventory write-from-promotion`.
