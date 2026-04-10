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
- Record the requested source git ref, target, deploy status, delegated worker,
  and destination health evidence.
- Persist branch-sync intent there as well so the control plane owns the
  requested git branch movement even while the transitional worker still
  performs the actual push.
- Compatibility ship execution may still delegate the underlying runtime work
  to `odoo-ai`, but the durable deploy record belongs here.

## Inventory

- Inventory records are keyed by environment.
- Inventory may be replaced in place because it represents current state rather
  than append-only event history.
