---
title: Driver Descriptors
---

## Purpose

Launchplane driver descriptors are the backend-owned contract for capability
discovery, operator read models, and future GUI action rendering. They describe
what a product driver can do without making the UI understand the runtime
provider that currently executes the work.

This is a read-first contract. It does not execute actions and it does not add a
frontend plugin system.

## Provider Boundary

- Launchplane exposes product capabilities and durable evidence: artifact,
  context, instance/lane, deployment, promotion, rollback, backup gate, preview,
  runtime setting, managed secret, and audit state.
- Runtime-provider details belong behind backend adapters and evidence records.
- Descriptors may point to existing Launchplane driver routes, but the operator
  vocabulary should stay provider-neutral: deploy, promote, backup, rollback,
  refresh preview, destroy preview, apply settings.
- Provider-specific fields stay in provider records or JSONB evidence until they
  become normal query, authorization, display, or action-driving state.

## Contracts

The descriptor contracts live in
`control_plane/contracts/driver_descriptor.py`.

- `DriverDescriptor`: static driver metadata, optional base driver id, context
  patterns, capabilities, actions, and setting groups.
- `DriverCapabilityDescriptor`: grouped product capability such as stable
  promotion, artifact publish, preview lifecycle, or post-deploy settings.
- `DriverActionDescriptor`: read-only action metadata, route path, method,
  authorization action, operator visibility, scope, safety level, and records the
  action can write.
- `DriverSettingGroupDescriptor`: setting/status groups the UI can render later
  without knowing product-specific storage internals.
- `DriverContextView`: context or context/instance read model composed from
  existing repository summaries.

Action safety levels are intentionally coarse:

- `read`: resolves or reads state without mutating Launchplane/product state.
- `safe_write`: writes evidence or captures a gate without changing the served
  application version.
- `mutation`: changes runtime or product state in a normal forward direction.
- `destructive`: rolls back or destroys runtime state and must be visually and
  procedurally distinct in future UI flows.

## Registry

The v1 registry is in code at `control_plane/drivers/registry.py`. It contains
the reusable generic-web base descriptor plus Odoo and VeriReel descriptors, and
composes driver views from existing storage repository methods:

- `LaunchplaneLaneSummary` for stable lane state.
- `LaunchplanePreviewSummary` for preview lifecycle state.

The registry is deliberately not a database table yet. Driver descriptor shape
should stabilize before Launchplane adds writable driver metadata. Product and
lane configuration still belongs in DB-backed Launchplane records, not in
repo-local Launchplane TOML manifests.

For guidance on adding a new driver type or product-specific driver, see
[driver-development.md](driver-development.md). For the expected shape of a
product repo that calls a driver, see
[product-repo-contract.md](product-repo-contract.md).

## Read Endpoints

All endpoints are authenticated and use action `driver.read`.

- `GET /v1/drivers`
- `GET /v1/drivers/{driver_id}`
- `GET /v1/contexts/{context}/driver-view`
- `GET /v1/contexts/{context}/instances/{instance}/driver-view`

Discovery endpoints authorize against Launchplane context `launchplane`.
Context/instance views authorize against the requested context.

The view endpoints return provider-neutral descriptors plus repository-backed
read state. They do not execute actions, reveal secret values, or ask the UI to
inspect JSONB payloads directly.

## Initial Drivers

Generic web exposes base capabilities and common stable-lane actions:

- image deployment evidence
- HTTP health checking
- testing-to-prod promotion evidence
- preview lifecycle and inventory read models
- PR feedback ownership

The `stable_deploy` action routes to `POST /v1/drivers/generic-web/deploy`. The
route resolves product lane context from DB-backed product profile records and
runtime target bindings from DB-backed Dokploy target records.

The `prod_promotion` action routes to
`POST /v1/drivers/generic-web/prod-promotion`. It promotes a generic-web
testing image to prod using DB-backed product profile lanes, records source and
destination health evidence, writes promotion/deployment linkage, and refreshes
prod inventory after successful verified deploys. Product-specific drivers such
as VeriReel or Odoo can wrap this common action when they need additional gates
such as backups, migrations, rollout checks, or tenant-specific validation.

The `preview_desired_state` action routes to
`POST /v1/drivers/generic-web/preview-desired-state`. Product workflows provide
the product key; Launchplane resolves the preview context, owning repository,
anchor repo, and slug template from the DB-backed product profile before writing
desired preview state records.

The `preview_refresh`, `preview_inventory`, `preview_readiness`, and
`preview_destroy` actions route to `POST /v1/drivers/generic-web/preview-refresh`,
`POST /v1/drivers/generic-web/preview-inventory`,
`POST /v1/drivers/generic-web/preview-readiness`, and
`POST /v1/drivers/generic-web/preview-destroy`. Refresh runs readiness first,
then creates or updates a stateless Dokploy application from the DB-backed
template lane, applies explicit settings transport, deploys the submitted image,
and checks the product health path. Inventory and destroy scan and delete
Dokploy applications by the product profile's preview application-name prefix.

Product drivers can declare `base_driver_id="generic-web"` when they reuse the
generic web lifecycle and add named product-specific gates or runtime actions.
The relationship is explicit metadata; product-specific capabilities are still
declared directly on the product driver.

Odoo exposes:

- artifact publish handoff
- post-deploy settings
- prod backup gate
- testing-to-prod promotion
- prod rollback

VeriReel exposes:

- stable deploy/environment/maintenance
- prod backup gate
- testing-to-prod promotion
- prod rollback
- preview refresh/inventory/destroy

These descriptors intentionally reference Launchplane routes, not runtime
provider concepts, as the future GUI-facing action surface.

Descriptor actions are also the source of truth for driver route authorization
metadata. `authz_action` must match the live service handler authorization
string for that route. Some service callback routes, such as verification
writeback routes, are declared with `operator_visible=false`; they remain in the
driver route authorization map but are not surfaced as operator actions.

Preview read models are capability-driven. A driver that exposes
`previewable`, `preview_inventory_managed`, legacy `preview_lifecycle`, or the
`preview_inventory` panel receives preview summaries without being named VeriReel
in the registry.
