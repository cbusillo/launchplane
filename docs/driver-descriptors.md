---
title: Driver Descriptors
---

## Purpose

Launchplane driver descriptors are the backend-owned contract for capability
discovery, operator read models, and future GUI action rendering. They describe
what a product driver can do without making the UI understand the runtime
provider that currently executes the work.

This is a read-first contract and does not add a frontend plugin system.
Descriptor presence alone does not execute actions; service dispatch requires a
matching backend handler registration, and tests must fail closed when the
descriptor route and handler registration drift.

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

Artifact publish input derivation has a generic request/result contract in
`control_plane/contracts/artifact_publish_inputs.py`. Product drivers should
accept that shared shape, then add only product-specific derivation such as
runtime environment key selection, image tag policy, or preview slug resolution.
This keeps tenant repositories on a thin handoff instead of teaching them
Launchplane product profile or runtime environment wiring.

Promotion input derivation follows the same boundary in
`control_plane/contracts/generic_promotion_inputs.py`. The generic contract owns
source-lane release tuple lookup, artifact manifest lookup, ready/blocked
results, immutable image evidence, and deterministic backup-gate record id
formatting. Product drivers keep stricter lane policy and any product-specific
promotion gates.

Action safety levels are intentionally coarse:

- `read`: resolves or reads state without mutating Launchplane/product state.
- `safe_write`: writes evidence or captures a gate without changing the served
  application version.
- `mutation`: changes runtime or product state in a normal forward direction.
- `destructive`: rolls back or destroys runtime state and must be visually and
  procedurally distinct in future UI flows.

## Registry

The v1 registry is in code at `control_plane/drivers/registry.py`. It contains
the reusable generic-web base descriptor plus ingress, Odoo, and VeriReel
descriptors, and composes driver views from existing storage repository methods:

- `LaunchplaneLaneSummary` for stable lane state.
- `LaunchplanePreviewSummary` for preview lifecycle state.

The registry is deliberately not a database table yet. Driver descriptor shape
should stabilize before Launchplane adds writable driver metadata. Product and
lane configuration still belongs in DB-backed Launchplane records, not in
repo-local Launchplane TOML manifests.

The `ingress` descriptor is a global control driver. It exposes
`POST /v1/drivers/ingress/route-apply` for planning and applying public edge
routes through Launchplane-owned provider adapters. It intentionally has no
context patterns, so it appears in driver discovery but not as a lane or preview
driver for every product context. Dry-run requests require `ingress_route.plan`;
apply requests require `ingress_route.apply`. Provider-specific adapter names
and credentials stay behind the service boundary.

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

Generic web also declares the shared preview runtime-environment setting group,
including the context-level `LAUNCHPLANE_PREVIEW_BASE_URL` value and product
profile preview transport keys. Product drivers that inherit `generic-web` reuse
that setting metadata instead of redeclaring common preview routing and runtime
transport fields.

Generic web is also the default public-ingress monitoring family. Any stable
lane on `generic-web` or a driver based on it is eligible for Launchplane's
scheduled synthetic check when the lane has a public `base_url` or `health_url`.
Based drivers inherit the same observation record and notification path; they do
not need tenant-local monitor workflows.

The `stable_deploy` action routes to `POST /v1/drivers/generic-web/deploy`. The
route resolves product lane context from DB-backed product profile records and
runtime target bindings from explicit provider-target rows, while Dokploy target
records continue to hold provider-specific execution configuration. Generic-web
deploy validates that the provider-target row and Dokploy execution record agree
before mutating the provider, so stale or divergent runtime identity fails
closed. Generic-web
deploy records post-deploy evidence as `skipped` unless a based driver explicitly
provides a product post-deploy extension. That extension point is the boundary
for product-only work after a provider deploy succeeds; it must return terminal
post-deploy evidence and must keep deploy status distinct from post-deploy
status. Odoo profiles receive this extension when they execute generic-web
deploy, which runs the Odoo post-deploy driver after the provider deploy
succeeds.

The `prod_promotion` action routes to
`POST /v1/drivers/generic-web/prod-promotion`. It promotes a generic-web
testing image to prod using DB-backed product profile lanes, records source and
destination health evidence, writes promotion/deployment linkage, and refreshes
prod inventory after successful verified deploys. Product-specific drivers such
as VeriReel or Odoo can wrap this common action when they need additional gates
such as backups, migrations, rollout checks, or tenant-specific validation.

The `prod_rollback_plan` action routes to
`POST /v1/drivers/generic-web/prod-rollback-plan`. It is a safe-write planner:
Launchplane reads the product profile, destination lane, selected deployment
record, and optional backup gate evidence, then writes a
`GenericWebRollbackPlanRecord`. It does not mutate the provider. Odoo rollback
planning uses this generic-web route; the former Odoo-shaped rollback-plan alias
is retired. This route is registered through descriptor-backed dispatch, so
descriptor/handler drift fails closed before the service starts.

The `prod_rollback` action routes to
`POST /v1/drivers/generic-web/prod-rollback`. It re-runs the same rollback-plan
validation, persists the plan record, and applies ready plans through the normal
generic-web deploy path using the previous immutable artifact identity. Generic
rollback also forwards the generic deploy post-deploy extension hook, so a
based driver can keep product-only post-deploy checks while reusing the common
rollback deployment path once its other invariants are represented. Product
drivers keep their own `prod_rollback` action only when they need additional
product-specific gates, such as Odoo backup, release tuple, manifest, migration,
or post-deploy checks. Odoo keeps `POST /v1/drivers/odoo/prod-rollback` as its
product-specific apply route, but that route delegates provider mutation to
Odoo stable target replacement so runtime identity, post-deploy maintenance,
canonical/logo verification, deployment records, inventory, and release tuples
stay on the canonical stable executor while the rollback wrapper owns promotion
rollback provenance.

The `stable_verification` action routes to
`POST /v1/drivers/generic-web/stable-verification`. Product workflows submit the
deployment record, optional promotion record, checked URLs, and pass/fail status;
Launchplane updates deployment, promotion, and inventory evidence without
mutating provider state. Odoo stable smoke follow-ups use this generic-web route;
the former Odoo-shaped stable verification alias is retired. This route is
registered through descriptor-backed dispatch, so descriptor/handler drift fails
closed before the service starts.

The `preview_desired_state` action routes to
`POST /v1/drivers/generic-web/preview-desired-state`. Product workflows provide
the product key; Launchplane resolves the preview context, owning repository,
anchor repo, and slug template from the DB-backed product profile before writing
desired preview state records.

The `preview_refresh`, `preview_inventory`, `preview_readiness`,
`preview_destroy`, and `preview_verification` actions route to
`POST /v1/drivers/generic-web/preview-refresh`,
`POST /v1/drivers/generic-web/preview-inventory`,
`POST /v1/drivers/generic-web/preview-readiness`,
`POST /v1/drivers/generic-web/preview-destroy`, and
`POST /v1/drivers/generic-web/preview-verification`. Refresh runs readiness
first, then creates or updates a stateless Dokploy application from the
DB-backed template lane, derives the live preview URL from the context-level
`LAUNCHPLANE_PREVIEW_BASE_URL` runtime-environment record plus the preview slug,
applies explicit settings transport, deploys the submitted image, and checks the
product health path. Inventory and destroy scan and delete Dokploy applications
by the product profile's preview application-name prefix. Verification records
common post-refresh smoke evidence against the latest Launchplane preview
generation and is available to any product profile that uses the generic-web
base driver. Generic-web preview verification is registered through
descriptor-backed dispatch, so descriptor/handler drift fails closed before the
service starts.

Preview resource cleanup uses a shared Launchplane destroy helper for Dokploy
applications and compose previews. The generic helper owns domain lookup,
matching domain deletion, resource deletion, missing-resource handling, and
cleanup error aggregation. Product drivers supply only the resource identity and
policy knobs such as compose volume deletion or whether domain cleanup must fail
closed before deleting the resource.

Product drivers can declare `base_driver_id="generic-web"` when they reuse the
generic web lifecycle and add named product-specific gates or runtime actions.
The relationship is explicit metadata; product-specific capabilities are still
declared directly on the product driver.

Odoo declares `base_driver_id="generic-web"` and inherits generic-web preview
actions as its advertised lifecycle surface. The Odoo-specific preview mutation
surface is the isolated compose planner/apply pair, not Odoo-shaped
`preview-refresh` or `preview-destroy` compatibility aliases. Odoo preview
desired-state, inventory, readiness, and verification aliases are retired;
callers should use the inherited generic-web routes for common
read/planning/evidence actions.
New tenant workflows should call `POST /v1/drivers/odoo/preview-apply-inputs`
and then `POST /v1/drivers/odoo/preview-apply` for refresh and destroy. The
lower-level `POST /v1/drivers/odoo/preview-apply` route applies a ready isolated-preview
Dokploy plan to provider state after service authorization and idempotency
checks. The route resolves runtime env values from Launchplane-owned
runtime-environment records and managed secret overlays, derives preview-specific
database and volume names inside the service, returns the adapter's redacted step
evidence, and keeps secret runtime values inside the service boundary. The
companion `POST /v1/drivers/odoo/preview-apply-inputs` route is the thin-workflow
entry point for planning that provider apply. Callers provide product, PR,
image, and source facts only; Launchplane derives the preview slug, public URL,
runtime binding evidence, template compose id, Dokploy environment id, Odoo
runtime plan, and redacted provider dry-run plan from product profiles,
runtime-environment records, managed secrets, and tracked Dokploy target records.
The route is read-only, returns no plaintext runtime or secret values, and
supports both refresh and destroy planning. Tenant workflows should call this
route before `preview-apply` instead of assembling Odoo runtime or Dokploy plan
payloads in the tenant repo.
Odoo stable promotion also exposes
`POST /v1/drivers/odoo/prod-promotion-inputs` as a read-only action before the
backup gate and promotion mutation routes. The route resolves the promotable
testing artifact and source ref from Launchplane release tuple and artifact
records, then returns the deterministic backup-gate record ID for the caller's
request ID. Tenant workflows should use that response instead of prompting an
operator to enter artifact or source facts by hand.
The preferred tenant-facing mutation route is
`POST /v1/drivers/odoo/prod-promotion-run`, which keeps the full inputs,
backup-gate, and promotion sequence inside Launchplane while returning each
phase status and the written record IDs. The lower-level routes remain available
for diagnostics and explicit operator workflows.
The
standard refresh/destroy routes use the generic-web preview request schema, live
URL derivation, and record writer so Odoo PR previews land in the same
Launchplane preview and preview-generation records as generic-web previews.
Preview smoke follow-ups use
`POST /v1/drivers/generic-web/preview-verification`, which accepts optional
checked URL evidence and returns a typed `generic_web_preview_verification`
result while writing durable status to the shared preview records.
Odoo's staged compose preview MVP is now retired. Generic-web preview readiness
blocks compose template lanes, including historical Odoo bootstrap-mode compose
profiles; Odoo PR previews must enter through `plan_odoo_preview_runtime` and
`execute_odoo_preview_dokploy_apply`. Historical inventory and destroy evidence
may still contain `providerType="compose-domain"`, but new Odoo preview provider
mutation is isolated-runtime only.
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
missing, stamps per-preview `ODOO_PROJECT_NAME` and `ODOO_STACK_NAME` values so
the raw compose does not inherit the template runtime identity, renders
Launchplane-owned raw compose source without publishing shared host ports,
renders explicit HTTP and HTTPS Traefik routers in the raw compose while also
reconciling the preview domain record for Dokploy UI/provider state, and defaults
preview domain certificate management to `none` so public TLS is owned by the
external edge wildcard certificate rather than Dokploy ACME.
Fresh-create dry-runs must carry the template compose id; apply creates new
preview composes on that template compose's Dokploy server, deploys the compose,
and returns redacted step evidence. Destroy looks up domains for the matching
preview compose, deletes the matching preview hostname, then deletes the compose
with `deleteVolumes`; if the domain lookup is already empty and Dokploy reports
the compose missing, or if a matching preview domain was deleted immediately
before the missing-compose response, destroy treats the runtime as already clean.
The adapter is not a generic local fallback: shared/provider execution still
needs an approved non-production target and a caller surface that sources
Launchplane-managed Dokploy credentials without printing secret values.
Odoo isolated preview apply also runs Launchplane-owned smoke before reporting a
ready apply result. The apply path requires image artifact evidence, source
revision evidence, rendered Odoo module evidence, and successful preview health
checks. Passing smoke records the preview generation as `ready` with deploy,
verify, and overall health status `pass`; failed smoke records a concise
verification failure so PR feedback does not advertise ready before the checks
pass.
The verification route is a Launchplane-owned evidence ingress for follow-up
browser or product smoke checks: it marks the latest preview generation ready or
failed through the same preview-generation records without mutating provider
state.
Stable smoke follow-ups should use
`POST /v1/drivers/generic-web/stable-verification`; the Odoo-shaped stable
verification route is retired.

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

Preview lifecycle cleanup also follows the product profile's driver boundary.
VeriReel-shaped products use the VeriReel cleanup executor, while products whose
driver descriptor inherits `base_driver_id="generic-web"`, including Odoo, use
the generic-web cleanup executor with the product profile's preview slug
template. This keeps cleanup truth in Launchplane records and prevents product
repos from carrying separate provider cleanup wiring.

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
- preview refresh/inventory/destroy/verification

These descriptors intentionally reference Launchplane routes, not runtime
provider concepts, as the future GUI-facing action surface.

Descriptor actions are also the source of truth for advertised driver route
authorization metadata. `authz_action` must match the live service handler
authorization string for the route's primary mutation or read behavior. If one
route has multiple service modes with distinct authorization checks, declare the
non-primary checks in `alternate_authz_actions` so policy tooling can discover
them without parsing free-form descriptions. Some service callback routes, such
as verification writeback routes, are declared with `operator_visible=false`;
they remain in the driver route authorization map but are not surfaced as
operator actions. Compatibility routes that should remain callable but not
advertised as current driver actions belong in `route_aliases` with
`operator_visible=false`.
The HTTP service admits product-driver POST routes from descriptor action and
route-alias paths and reads product-driver handler authorization actions from
descriptor route metadata, so new drivers do not need a second hardcoded router
allowlist or authz-action entry.
Routes can also opt into descriptor-backed service dispatch when a backend
handler is explicitly registered for the same descriptor route. Generic-web
stable verification, rollback planning, preview verification, and the VeriReel
testing deploy plus testing and preview verification writebacks use
descriptor-backed dispatch. This keeps descriptor metadata as the route/authz
source of truth while preventing an advertised descriptor action from becoming
executable without implementation.
Descriptor route metadata and service compatibility policy also drive
product-driver compatibility checks. A
product whose descriptor names a `base_driver_id` can use the base driver's
shared lifecycle routes when its profile owns the requested stable lane or
preview context, which keeps reusable deploy, promotion, workflow-dispatch, and
preview plumbing in Launchplane instead of copied into every site repo.
Base-driver compatibility stays route-scoped: products only inherit descriptor
routes that are part of the base driver contract, while child-driver routes keep
their concrete product-specific admission and authorization checks.

Preview read models are capability-driven. A driver that exposes
`previewable`, `preview_inventory_managed`, legacy `preview_lifecycle`, or the
`preview_inventory` panel receives preview summaries without being named VeriReel
in the registry.
