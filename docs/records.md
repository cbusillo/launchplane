---
title: Records
---

## Storage Policy

- Persist records as JSON files in a local state directory.
- Keep git history separate from operational history.
- Favor append-style writes for promotion records.

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
  harbor_previews/
    <preview-id>.json
  promotions/
    <record-id>.json
  inventory/
    <context>-<instance>.json
```

## Artifact Manifest

- One file per immutable artifact identifier.
- Record the public app commit, private enterprise digest, and final image
  identity.
- Preserve build-affecting addon, OpenUpgrade, and flag inputs alongside the
  image identity so the control plane owns the full manifest instead of a thin
  image pointer.

## Promotion Record

- One file per promotion attempt.
- Record source, destination, artifact id, gate evidence, deploy evidence, and
  destination health.
- Promote inputs should reference the immutable artifact id directly.
- Promotion records also persist the authorizing `backup_record_id` so
  current inventory can be traced back to the exact stored backup-gate record
  that authorized the live promotion.
- Promotion execution should normalize backup-gate evidence from a stored
  backup-gate record instead of trusting ad-hoc inline request payloads.
- Promotion execution also resolves the deployable ship request natively in
  `odoo-control-plane` from this repo's Dokploy source-of-truth, instead of
  shelling out for a pre-rendered JSON request.

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
- Deploy execution should also drive the Dokploy image selection from stored
  artifact manifests when possible by syncing an exact
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` override before the deploy starts.
- Native ship/deploy records should not persist branch-sync evidence because
  branch movement is no longer part of artifact-backed execution.
- When no stored artifact manifest is available for a direct ship, deploy
  execution should fail closed instead of falling back to the ordinary repo/tag
  image contract.
- Deployment records should make that native follow-up step explicit by
  recording whether the Odoo-specific compose post-deploy update was skipped,
  pending, passed, or failed.
- Direct `ship` and `promote` execution should fail closed if the referenced
  artifact id does not already have a stored manifest in control-plane state.

## Harbor Preview Record

- One file per stable Harbor preview identity.
- Record the anchor PR identity, deterministic preview label, canonical preview
  URL, lifecycle timestamps, current preview state, and the active/serving/
  latest generation links.
- Destroyed previews should remain readable durable evidence instead of being
  removed from state.
- Preview records should preserve one stable identity per anchor PR even when
  Harbor replaces the serving generation over time.
- The initial explicit mutation surface is `harbor-previews write-preview`,
  which builds the stored record from typed request input plus the dedicated
  Harbor preview base-url runtime contract.

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
  controlling promotion record.
