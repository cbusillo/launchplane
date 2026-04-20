---
title: Harbor Service Boundary
---

## Purpose

This document defines the first explicit Harbor service boundary: the initial
HTTP ingress, the GitHub Actions OIDC trust model, the claim-to-permission
mapping, and the first stable API payloads Harbor should accept.

It exists to keep new cross-product work aligned with Harbor's target form:

- long-running service
- authenticated machine ingress
- Harbor-owned drivers
- thin repo extensions

The current repo-local CLI and file-backed state directory are implementation
scaffolding. This document defines the boundary those adapters should converge
on.

## Current Implementation Status

The first service slice is now implemented locally in this repo:

- CLI: `uv run control-plane service serve`
- health route: `GET /v1/health`
- first authenticated evidence route:
  `POST /v1/evidence/previews/generations`

That slice is intentionally narrow. It proves the Harbor HTTP/OIDC/authz
boundary in code before broader evidence routes and driver-triggered actions
move across it.

## First Host Assumption

- Harbor runs behind `https://harbor.shinycomputers.com`.
- Harbor exposes versioned API ingress under `/v1`.
- Harbor returns JSON for both success and failure cases.

## Boundary Layers

```text
GitHub Actions workflow
  -> OIDC token from GitHub
  -> Harbor HTTP ingress
  -> Harbor authn/authz
  -> Harbor core record/write logic
  -> Harbor read models and driver hooks
```

## Authentication

Machine callers should authenticate with GitHub Actions OIDC.

Harbor should verify:

- `iss` is GitHub's OIDC issuer
- `aud` matches Harbor's expected audience
- signature validates against GitHub's published keys
- token is not expired and is valid for current time

Recommended first audience:

- `harbor.shinycomputers.com`

That keeps the audience tied to the Harbor service identity instead of to a
temporary repo or local CLI name.

## Authorization Model

Harbor should authorize machine callers from workflow identity claims, not from
human repo-admin status and not from copied long-lived service tokens.

The first policy model should be allow-list based and fail closed.

### Claims Harbor should rely on first

- `repository`
- `repository_owner`
- `workflow_ref`
- `job_workflow_ref` when reusable workflows are involved
- `ref`
- `ref_type`
- `event_name`
- `environment` when present
- `sub`
- `sha`

### Claims Harbor should not treat as the primary authorization boundary

- `actor`
- `actor_id`
- human repo role such as admin/maintainer

Those are still useful for audit display, but the authorization decision should
primarily trust the workflow identity GitHub issued.

### First policy shape

Harbor should map verified claims to a small policy rule set:

```text
rule
  - subject type: github-actions
  - repository match
  - workflow_ref or job_workflow_ref match
  - event_name match
  - environment/ref constraints
  - allowed product
  - allowed contexts
  - allowed actions
```

Example policy intent:

```text
repository: every/verireel
workflow_ref: every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main
event_name: pull_request
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - preview_generation.write
```

Another example:

```text
repository: every/verireel
workflow_ref: every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main
event_name: pull_request
allowed product: verireel
allowed contexts: verireel-testing
allowed actions:
  - preview_destroyed.write
```

The initial policy engine can be config-backed and static. It does not need a
full RBAC system yet.

## First API Surface

The first Harbor service surface should focus on evidence ingress and record
writes, not on every possible operator action.

### Evidence ingress endpoints

- `POST /v1/evidence/deployments`
- `POST /v1/evidence/promotions`
- `POST /v1/evidence/previews/generations`
- `POST /v1/evidence/previews/destroyed`

### Operator read endpoints

- `GET /v1/previews/{preview_id}`
- `GET /v1/previews/{preview_id}/history`
- `GET /v1/inventory/{context}/{instance}`
- `GET /v1/promotions/{record_id}`
- `GET /v1/deployments/{record_id}`

### Driver execution endpoints

These can exist later, but should use the same authn/authz boundary:

- `POST /v1/drivers/odoo/...`
- `POST /v1/drivers/verireel/...`

Do not block the first evidence-ingest service slice on driver execution.

## Request And Response Rules

- Requests and responses are JSON.
- Evidence endpoints should be idempotent from Harbor's perspective when the
  same product/workflow submits the same stable identity twice.
- Harbor should support an explicit idempotency key header for workflow retries.
- Harbor should return durable record identifiers, not local file paths.
- Harbor should include a request or trace id in every response.

Recommended first headers:

- `Authorization: Bearer <github_oidc_token>`
- `Content-Type: application/json`
- `Idempotency-Key: <stable-retry-key>`

Recommended first success shape:

```json
{
  "status": "accepted",
  "trace_id": "harbor_req_01jabc...",
  "records": {
    "preview_id": "preview-verireel-pr-123",
    "generation_id": "preview-verireel-pr-123-generation-0003"
  }
}
```

Recommended first error shape:

```json
{
  "status": "rejected",
  "trace_id": "harbor_req_01jabc...",
  "error": {
    "code": "authorization_denied",
    "message": "Workflow cannot write preview evidence for verireel-testing."
  }
}
```

## First Stable Payloads

Harbor should keep the typed evidence payloads already proven in the current
CLI adapters and expose them over HTTP.

### Preview generation evidence

`POST /v1/evidence/previews/generations`

```json
{
  "product": "verireel",
  "preview": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "anchor_pr_url": "https://github.com/every/verireel/pull/123",
    "canonical_url": "https://pr-123.ver-preview.shinycomputers.com",
    "state": "active",
    "updated_at": "2026-04-16T08:10:00Z",
    "eligible_at": "2026-04-16T08:10:00Z"
  },
  "generation": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "anchor_pr_url": "https://github.com/every/verireel/pull/123",
    "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
    "state": "ready",
    "requested_reason": "external_preview_refresh",
    "requested_at": "2026-04-16T08:02:00Z",
    "ready_at": "2026-04-16T08:10:00Z",
    "finished_at": "2026-04-16T08:10:00Z",
    "resolved_manifest_fingerprint": "verireel-preview-manifest-pr-123-6b3c9d7",
    "artifact_id": "ghcr.io/every/verireel-app:pr-123-6b3c9d7",
    "deploy_status": "pass",
    "verify_status": "pass",
    "overall_health_status": "pass"
  }
}
```

### Preview destroyed evidence

`POST /v1/evidence/previews/destroyed`

```json
{
  "product": "verireel",
  "destroy": {
    "schema_version": 1,
    "context": "verireel-testing",
    "anchor_repo": "verireel",
    "anchor_pr_number": 123,
    "destroyed_at": "2026-04-16T09:04:00Z",
    "destroy_reason": "external_preview_cleanup_completed"
  }
}
```

The deployment and promotion endpoints should follow the same pattern: stable
typed record payloads inside a Harbor-owned API envelope.

## CLI Relationship

Current commands such as:

- `control-plane harbor-previews write-from-generation`
- `control-plane harbor-previews write-destroyed`

should be treated as temporary compatibility clients of these Harbor payloads.
They should not remain the permanent integration boundary for external product
workflows.

## Driver Relationship

The first Harbor API should separate two concerns:

- evidence ingress into Harbor core records
- runtime execution through Harbor-owned drivers

That keeps the initial service slice small while still allowing a later shift
from repo-owned operational scripts into Harbor-owned driver execution.

The first explicit drivers should be:

- Odoo driver
- VeriReel driver

Repo-specific variation should stay thin and declarative where possible.

## Out Of Scope For This First Slice

- full human/operator auth design
- multi-tenant billing or quota models
- generalized plugin marketplace design
- replacing file-backed storage immediately
- moving every current CLI command behind HTTP at once

## Recommended Next Implementation Steps

1. Add a Harbor API design slice in code with these first evidence endpoints.
2. Implement GitHub OIDC verification and a static claim-to-permission policy.
3. Expose one real preview evidence ingress path end to end.
4. Convert the existing CLI preview evidence commands into local clients of the
   same service-layer handler or payload contract.
5. Define the first explicit Odoo and VeriReel driver interfaces after the
   service ingress exists.
