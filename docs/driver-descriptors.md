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

Generic web can also operate simple service products deployed as Dokploy
applications. In that shape, the product may be a bot or worker instead of a
public website, but the driver still owns immutable image deployment, optional
health evidence, and deployment records. The product-specific contract for this
shape is [dokploy-service-deployments.md](dokploy-service-deployments.md).

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
template lane, derives the live preview URL from the context-level
`LAUNCHPLANE_PREVIEW_BASE_URL` runtime-environment record plus the preview slug,
applies explicit settings transport, deploys the submitted image, and checks the
product health path. Inventory and destroy scan and delete Dokploy applications
by the product profile's preview application-name prefix.

Product drivers can declare `base_driver_id="generic-web"` when they reuse the
generic web lifecycle and add named product-specific gates or runtime actions.
The relationship is explicit metadata; product-specific capabilities are still
declared directly on the product driver.

Odoo declares `base_driver_id="generic-web"` and exposes Odoo-shaped preview
routes for the same lifecycle: `POST /v1/drivers/odoo/preview-desired-state`,
`POST /v1/drivers/odoo/preview-refresh`,
`POST /v1/drivers/odoo/preview-inventory`,
`POST /v1/drivers/odoo/preview-readiness`,
`POST /v1/drivers/odoo/preview-destroy`, and
`POST /v1/drivers/odoo/preview-verification`. These routes use the generic-web
preview request schema, live URL derivation, and record writer so Odoo PR
previews land in the same Launchplane preview and preview-generation records as
generic-web previews.
The preview-verification route accepts optional checked URL evidence and returns
a typed `odoo_preview_verification` result while only mutating Launchplane
preview-generation records.
Odoo's staged compose preview MVP is now historical compatibility evidence, not
the target runtime architecture. That path updated and deployed the configured
compose target when the product profile used `preview.data_transport_mode =
"bootstrap"` and its template lane was a Dokploy `compose` target; inventory and
destroy read compose domains with `/api/domain.byComposeId` and deleted only
domains whose hostnames matched the preview slug template. This exception remains
route-scoped to Odoo preview refresh, inventory, destroy, and readiness while the
isolated runtime migration lands; it does not grant Odoo products access to
stable generic-web deploy or promotion routes, and it does not make compose
templates valid for generic-web products.
The isolated Odoo preview replacement starts with the
`plan_odoo_preview_runtime` contract. It must return `ready` before provider
apply code can create, update, deploy, or destroy a PR runtime. The planner fails
closed unless the strategy is an isolated per-PR Dokploy compose runtime, the
public preview URL is already resolved, required preview-safe runtime bindings are
present, default sensitive Odoo credentials are absent, provider capabilities
include rollback/delete operations, and destroy evidence points to a preview
runtime matching the requested preview slug or PR number. Stable, testing, prod,
or slug-mismatched targets are blocked before any provider mutation.
`build_odoo_preview_dokploy_dry_run` is the provider-facing bridge from that
ready runtime plan to Dokploy operation intent. It is dry-run only: blocked
runtime plans produce no operations, env updates are marked as secret-bearing
payloads, and create rollback lists only the newly created preview domain and
compose runtime. The dry-run uses Dokploy's documented `/api/compose.create` and
`/api/compose.delete` endpoints by default, but still blocks if a product/profile
clears those paths. Preview compose delete intent includes `deleteVolumes` so the
eventual apply path removes per-PR database and filestore volumes instead of
leaving orphaned runtime state.
`execute_odoo_preview_dokploy_apply` is the first live-provider adapter behind
that contract. It accepts only a ready dry-run plan plus explicit runtime env
values, blocks before reading Dokploy credentials when required Odoo env keys are
missing, renders Launchplane-owned raw compose source, reconciles the preview
domain, deploys the compose, and returns redacted step evidence. Destroy looks up
domains for the matching preview compose, deletes the matching preview hostname,
then deletes the compose with `deleteVolumes`. The adapter is not a generic local
fallback: shared/provider execution still needs an approved non-production target
and a caller surface that sources Launchplane-managed Dokploy credentials without
printing secret values.
For the CM staged preview contract, Odoo preview refresh also runs Launchplane-
owned smoke before reporting `refresh_status="pass"`: image artifact evidence,
source revision evidence, module install/update evidence from rendered Odoo env,
`/web/health`, `/cm-website/health`, and `/cell-mechanic`. Passing smoke records
the preview generation as `ready` with deploy, verify, and overall health status
`pass`; failed smoke records a concise verification failure so PR feedback does
not advertise ready before the checks pass.
The verification route is a Launchplane-owned evidence ingress for follow-up
browser or product smoke checks: it marks the latest preview generation ready or
failed through the same preview-generation records without mutating provider
state.
Stable smoke follow-ups use the same shape at
`POST /v1/drivers/odoo/stable-verification`: product workflows submit the
deployment record, optional promotion record, checked URLs, and pass/fail status;
Launchplane updates deployment, promotion, and inventory evidence without
mutating provider state.

Odoo also exposes `POST /v1/drivers/odoo/stable-bootstrap` as a destructive
instance-scoped action. It is enabled per product-profile lane through
`odoo_stable_bootstrap` and `odoo_data_policy` rather than driver code literals.
The policies define the destructive confirmation phrase, allowed data-source
mode, lane data authority, expected target name/domains, and required
verification checks before Launchplane reuses the existing devkit `--bootstrap`
data workflow through a Launchplane-owned Dokploy schedule. Routine Odoo probes
are derived by the driver from the lane base URL and Odoo conventions. It is
separate from target replacement: replacement reconciles provider/runtime target
state, while bootstrap rebuilds Odoo application data for lanes that are
explicitly safe to recreate.
The POST route returns a durable operation record instead of holding the request
open for the whole bootstrap. Callers poll
`/v1/drivers/odoo/stable-bootstrap/operations/{operation_id}` and treat terminal
`pass`/`fail` as the source of truth for workflow exit status and artifacts.
Odoo target replacement apply uses the same durable operation shape for the
guarded `recreate-in-place` path: `POST /v1/drivers/odoo/target-replacement-apply`
creates or replays an operation, and callers poll
`/v1/drivers/odoo/target-replacement/operations/{operation_id}` until terminal.
Replay is scoped to the authenticated caller identity, and storage reserves the
product/context/instance lane before the worker starts so concurrent apply
requests cannot launch duplicate replacements.

Driver action routes are owned by the base driver contract. The service accepts
any product key on Odoo and VeriReel action envelopes, then authorizes the call
against that product and verifies the product profile's `driver_id` or driver
descriptor `base_driver_id` matches the requested base driver before dispatching
the workflow. This keeps product repos on stable Launchplane routes while
allowing new Odoo- or VeriReel-shaped products to be added by product profile and
authz records instead of code forks.

VeriReel preview lifecycle cleanup also uses the product and context on the
preview lifecycle plan as the cleanup boundary. A VeriReel-shaped product can
therefore clean up previews recorded under its own product key and preview
context instead of being pinned to the canonical `verireel`/`verireel-testing`
pair.

VeriReel stable-lane actions follow the same product-profile boundary. For
non-canonical products, service dispatch verifies that the requested context and
instance match a lane on the product profile before invoking stable deploy,
environment, rollout verification, backup gate, promotion, or rollback
workflows.

Odoo exposes:

- artifact publish handoff
- post-deploy settings
- PR preview desired state, refresh, readiness, inventory, and destroy through
  the generic-web preview lifecycle
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
The HTTP service admits product-driver POST routes from descriptor action route
paths and reads product-driver handler authorization actions from descriptor
route metadata, so new drivers do not need a second hardcoded router allowlist
or authz-action entry.
The same route metadata also drives product-driver compatibility checks. A
product whose descriptor names a `base_driver_id` can use the base driver's
shared preview routes when its profile owns the requested preview context, which
keeps reusable preview plumbing in Launchplane instead of copied into every site
repo. Base-driver compatibility is route-scoped: stable deploy and promotion
routes require the profile's concrete `driver_id` to match the requested driver,
so Odoo or other generic-web-based products do not inherit generic-web stable
mutation authority.

Preview read models are capability-driven. A driver that exposes
`previewable`, `preview_inventory_managed`, legacy `preview_lifecycle`, or the
`preview_inventory` panel receives preview summaries without being named VeriReel
in the registry.
