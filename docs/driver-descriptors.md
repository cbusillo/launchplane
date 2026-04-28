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

- `DriverDescriptor`: static driver metadata, context patterns, capabilities,
  actions, and setting groups.
- `DriverCapabilityDescriptor`: grouped product capability such as stable
  promotion, artifact publish, preview lifecycle, or post-deploy settings.
- `DriverActionDescriptor`: read-only action metadata, route path, method,
  scope, safety level, and records the action can write.
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
Odoo and VeriReel descriptors and composes driver views from existing storage
repository methods:

- `LaunchplaneLaneSummary` for stable lane state.
- `LaunchplanePreviewSummary` for preview lifecycle state.

The registry is deliberately not a database table yet. Driver descriptor shape
should stabilize before Launchplane adds writable driver metadata.

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
