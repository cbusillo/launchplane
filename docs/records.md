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
  deployments/
    <record-id>.json
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
- Transitional records may use compatibility artifact identifiers when a full
  immutable artifact pipeline has not been wired into the handoff yet.
- When a single stored artifact manifest already matches the promoted commit,
  prefer that real artifact id over the synthetic compatibility id.

## Deployment Record

- One file per direct ship attempt owned by `odoo-control-plane`.
- Record the requested source git ref, target, deploy status, recorded
  executor, post-deploy update evidence, and destination health evidence.
- Ship execution may still delegate the underlying runtime work to `odoo-ai`,
  but the durable deploy record belongs here.
- The final deployment status now also reflects control-plane-owned health
  verification rather than relying on delegated runtime steps to make that final
  readiness call.
- Deployment records now also persist the resolved Dokploy target so the
  control plane owns the exact runtime target identity used for the deploy.
- The recorded executor now reflects control-plane-owned Dokploy execution,
  while the Odoo-specific post-deploy update step is orchestrated separately
  through the canonical `odoo-ai platform update` path.
- Deploy execution should also drive the Dokploy image selection from stored
  artifact manifests when possible by syncing an exact
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` override before the deploy starts.
- Native ship requests should not persist branch-sync evidence because branch
  movement is no longer part of artifact-backed execution.
- When no stored artifact manifest is available for a direct ship, deploy
  execution should fail closed instead of falling back to the ordinary repo/tag
  image contract.
- Deployment records should make that remaining seam explicit by recording
  whether the Odoo-specific post-deploy update was skipped, pending, passed, or
  failed.
- When a direct `ship` request arrives without an explicit artifact id but the
  control plane already has exactly one stored artifact manifest for the
  requested commit, deployment history should prefer that real artifact id over
  leaving artifact identity blank.

## Inventory

- Inventory records are keyed by environment.
- Inventory may be replaced in place because it represents current state rather
  than append-only event history.
- Inventory records now capture the current deployed source git ref, artifact
  identity when known, deploy evidence, post-deploy update evidence,
  destination health, and the deployment/promotion records that established the
  current state.
- Successful waited `ship` executions refresh inventory directly from the final
  deployment record.
- Successful waited `promote` executions refresh the same inventory record and
  add promotion linkage so the current state can still be tied back to the
  controlling promotion record.
