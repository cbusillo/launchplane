---
title: Driver Development
---

## Purpose

Launchplane drivers are the backend-owned boundary for product lifecycle
behavior. A driver declares what a product can do, validates requests, executes
or delegates provider work, writes durable records, and exposes read models for
admins and future UI actions.

Drivers exist to move lifecycle knowledge out of product repos, not to rename
product-specific hard-coding inside Launchplane. The common contract is:
product repos build, test, and publish immutable artifacts with a build
manifest ([artifact-provenance.md](artifact-provenance.md)) and never call
Launchplane ([DIRECTION.md](../DIRECTION.md)); Launchplane reacts to
source-control events, verifies the build, derives lifecycle meaning, and owns
runtime authority through driver routes, DB-backed records, managed secrets,
provider adapters, and durable evidence.

Use `generic-web` directly when a product fits the common web-app lifecycle. Add
a new driver type or product driver only when the product has obligations that
should be named, authorized, tested, and operated separately from the generic
web path.

## When To Add A Driver

Add a driver when the product needs one or more of these:

- product-specific backup, restore, or rollback gates
- database bootstrap, migration, seed, clone, anonymization, or cleanup
- post-deploy maintenance commands
- product-specific smoke checks that affect promotion readiness
- platform-specific artifact handling
- runtime behavior that cannot be described with the `generic-web` profile
- a distinct authorization surface for a high-risk action

Do not add a driver just to rename `generic-web` for a product. Prefer a product
profile using `driver_id="generic-web"` until there is a real product-specific
capability to model.

Lifecycle fixtures follow the same boundary. Product repos may keep fixtures for
product behavior, local development, and product-specific smoke tests. Driver
tests and contract builders own fixtures for lifecycle behavior: product
profiles, lane profiles, runtime-environment records, managed-secret bindings,
provider targets, release tuples, artifact manifests,
deployment/promotion/rollback/backup evidence, preview records, and driver
request/result shapes.
Odoo is the reference complex-product case for this split: Odoo repos keep
source/build/local-DX and product-smoke facts, while the Odoo driver owns Odoo
lifecycle derivation and fixture setup that Launchplane can prove centrally.

## Driver Shape

Each driver should have these pieces:

- Descriptor metadata in `control_plane/drivers/registry.py`.
- Shared descriptor-dispatch plumbing from `control_plane/drivers/dispatch.py`.
- Generic-web preview dispatch in
  `control_plane/drivers/generic_web_preview_dispatch.py`.
- Typed request and result models in a workflow module.
- Service routes under `/v1/drivers/{driver_id}/...`.
- Authz actions that match the driver actions and safety level.
- Storage writes through existing record contracts or a new contract when the
  behavior needs durable query state.
- Tests for request validation, authorization, successful execution, failure
  records, and read-model behavior.
- Docs that explain whether the driver extends `generic-web` or stands alone.

Driver routes that mutate runtime state should write the lifecycle records they
can derive from the request and provider result in the same request. Product
repos should not have to shape preview, deployment, promotion, rollback, or
cleanup records after asking Launchplane to perform the matching action.

Product drivers that reuse common web behavior should declare
`base_driver_id="generic-web"` in the descriptor and delegate common work rather
than copying preview/deploy logic. The product-specific behavior still needs
named capabilities and named routes.

Generic-web stable deploy exposes a product post-deploy extension point for
based drivers. The generic driver owns target resolution, artifact deployment,
deployment records, and inventory writes; product drivers can pass an extension
executor only for work that must happen after a provider deploy succeeds, such
as Odoo override/application maintenance. The extension must return terminal
`PostDeployUpdateEvidence` and must not hide provider deploy status: a failed
extension can fail the lifecycle action while the deployment record still shows
the underlying image deploy as `pass` and the post-deploy evidence as `fail`.
Launchplane wires this extension for Odoo profiles when they execute generic-web
deploy or rollback apply, so Odoo can reuse common provider deployment while its
post-deploy maintenance remains explicit driver behavior.

Generic-web lanes can declare application maintenance commands and recurring
schedules in `generic_web_extensions`; their typed contract is
[`GenericWebLaneExtensions`](../control_plane/contracts/generic_web_extensions.py).
These are product profile records, not Launchplane build inputs or checked-in
product catalogs. The deploy provider must implement the application lifecycle
protocol; the current implementation supports Dokploy applications and refuses
other targets before any mutation. Generic deploy, promotion and rollback share
this path. Empty declarations retain existing profile serialization and behavior.

The lifecycle pauses existing declared schedules before deploying, runs the
declared maintenance before product post-deploy verification, then runs each
enabled schedule once as a canary before enabling its recurring invocation.
Disabled schedules remain disabled. Provider readback must match the exact
application and schedule. Failure before an image effect restores original
schedules under the same effect fence; failure after an image effect holds jobs
disabled until a qualified rollback. Both record failed post-deploy evidence. A lost fence
cannot perform restoration, and runtime-image evidence alone cannot close out
an operation whose application lifecycle remains unobserved. Managed cron
expressions use the grammar in [`control_plane/cron.py`](../control_plane/cron.py).
Testing retry authority includes the declaration, so repaired commands do not
replay a previous failed attempt. Command text names an executable in the
product artifact; runtime settings and managed bindings supply its secrets.

Preview profiles using driver data transport can explicitly select the
`verireel-postgres` transport extension through `transport_extension` while their
driver is `generic-web`, declaring its isolated database namespace through
`transport_database_prefix`. The template lane, application prefix, database
namespace and runtime product identity come from the profile. This uses the existing isolated PostgreSQL preparation
and cleanup rather than the plain preview path. The VeriReel testing transition
has qualified this declared shared path; the parallel deploy, promotion and rollback driver is removed. Historical deployment
and backup records and retired instance-scoped policy grants remain readable;
no retired release endpoint is enabled by those grants. Production still changes only through an
accepted release.

A generic-web testing lane selects `verification_extension="verireel-testing"`
in `generic_web_extensions` to reuse VeriReel's creator/admin
qualification. The preset accepts only a recorded testing lane and exact
reserved application target; other lanes and an absent executor are refused
before deployment. The preset is selected only by the declaration; a product
name or historical driver identifier does not enable testing qualification. The shared lifecycle runs the profile's migration commands first. The extension verifies a
disposable creator's QR/public flow, creator denial of admin pages, and the admin
pages after the existing smoke-maintenance intent elevates that fixture. Cleanup
uses the existing test-account deletion intent. The deployment's
`post_deploy_update.evidence` binds creator, admin and cleanup statuses
to the deployment ID, immutable artifact and source revision; all must pass.
The service image includes matching Playwright Chromium. The Compose browser
service uses Playwright's built-in remote server in an unprivileged, read-only
container with no credentials, runtime volume, host socket or published port.
`LAUNCHPLANE_BROWSER_WS_ENDPOINT` is Launchplane self-bootstrap wiring to that
private component. Both the HTTP service and the event-reconcile worker receive
that endpoint and private network; control-plane startup does not wait on browser
health. Verification checks browser availability before migration and Linux
verification refuses an absent endpoint. Local
non-Linux rehearsals use a sandboxed browser with a scrubbed environment.
Fixture passwords are ephemeral and never enter qualification evidence.
See [event-driven deploys](event-driven-deploys.md) for requalification and replay.
Do not move a product apply route onto generic deploy until its remaining
release, backup, promotion, migration, and post-deploy invariants are either
represented in generic contracts or still explicitly wrapped by the product
driver.

Generic-web deploy resolves and executes runtime targets through a deploy
provider adapter. The default adapter is Dokploy, but generic-web orchestration
must depend on the adapter protocol rather than importing provider clients
directly. Deployment records must carry the adapter's provider identity,
provider target reference, and delegated executor so admin evidence stays
accurate when a future deploy provider is introduced.

`control_plane/http_routes/generic_web.py` owns the Generic Web FastAPI edge and
durable provider-mutation adapters. The existing `generic_web_*_http.py` modules
remain the domain boundary for request resolution, execution, record shaping,
and idempotency decisions. Keep new provider behavior behind those protocols and
modules; do not add Generic Web route logic back to `http_app.py`.

### Reviewed shared-web profile transition

The product workspace's **Shared web driver** card uses
`POST /v1/product-profiles/{product}/generic-web-transition`. Its request shape
is [`ProductGenericWebTransitionRequest`](../control_plane/product_generic_web_transition.py).
Declare every recorded lane and the supported preview transport explicitly;
the card starts with the current declarations. Maintenance and schedule commands
run in the product artifact and use existing managed runtime settings.

Review returns the normalized declarations and a plan bound to the whole current
profile. Apply requires that exact review, a signed-in administrator's existing
`product_profile.write` authority, CSRF and an operation key. A concurrent profile
edit refuses the apply. The write and original response commit atomically;
a lost response keeps the card locked to its original key until replay or the
profile mutation receipt settles it.

This changes driver and extension declarations only. Lane placement, Client,
release policy, health settings, runtime settings, secrets, artifacts and deployment
records remain under their existing authority. The periodic testing reconcile
qualifies the declared testing lifecycle; profile readback is not qualification.
Production changes still start from the accepted release. Keep an existing product
stack operational until testing qualifies the replacement; then remove its active
paths while retaining historical readers.

## Capability Design

Use capability names to describe admin-visible behavior, not implementation
mechanics. Prefer names like these:

- `stable_deploy`
- `preview_refresh`
- `preview_destroy`
- `preview_inventory`
- `preview_readiness`
- `preview_pr_feedback`
- `prod_backup_gate`
- `prod_promotion`
- `prod_rollback`
- `app_maintenance`

Provider details such as Dokploy application IDs, endpoint mode, registry
credentials, or deployment job IDs belong behind adapters and evidence records.
Expose them in read models only when admins need them to decide or repair
state.

## Route Design

Driver routes should accept product intent and let Launchplane derive the rest
from records whenever possible.

Good trigger inputs:

- product key
- instance or lane when the action is stable-lane specific
- immutable artifact or image reference
- source ref or commit SHA
- PR number for preview actions
- explicit production confirmation for destructive actions

Avoid requiring product repos to send:

- provider target IDs
- public preview URLs when Launchplane can derive them
- health paths or runtime ports already stored in product profiles
- record IDs that Launchplane can generate idempotently
- rendered feedback markdown
- copied environment values or secret names beyond typed profile policy

If a route temporarily needs one of those fields, document why and add a cleanup
item to move it into product profiles, runtime-environment records, managed
secrets, or driver-owned derivation.

## Implementation Steps

1. Decide whether `generic-web` plus product profile fields is sufficient.
2. Add or extend the driver descriptor in the registry.
3. Add typed request/result models and executor functions in
   `control_plane/workflows/`.
4. Wire native FastAPI route handling using the shared route primitives in
   `control_plane/drivers/dispatch.py`; keep driver-family handlers cohesive
   instead of growing unrelated `service.py` route tables.
   Generic-web preview route changes belong in
   `control_plane/drivers/generic_web_preview_dispatch.py`.
5. Write records through existing storage contracts when possible.
6. Add focused unit tests for validation, authorization, execution, and failure
   evidence.
7. Update docs.
8. Seed or migrate DB-backed product profile, target, runtime environment,
   managed secret, and authz policy records outside the product repo.

Keep slices small. Land read-only descriptors and profile shape before
high-risk provider mutations. Land readiness checks before create/update/delete
actions when a provider mutation depends on external target state.

## Provider Adapter Slices

Provider adapters may land before a full driver route when the first useful
slice is proving an external control-plane boundary. Keep these adapters small,
typed, and tested with mocked provider calls. Do not read secrets from ad hoc
local files inside the adapter; callers must pass credentials from Launchplane
managed secret or admin configuration boundaries.

The NPMplus adapter in `control_plane/npmplus.py` is the first ingress-provider
slice. It models session-cookie authentication, proxy-host payloads, and the
proxy-host create/read/update/disable/enable/delete lifecycle that was proven
against a disposable canary route. Future ingress driver routes should use this
adapter instead of direct NPMplus SQLite or generated nginx config writes.

The service-backed ingress route is `POST /v1/drivers/ingress/route-apply`.
Callers send a product/context envelope plus a typed route request. `mode` is
`dry-run` by default for CLI callers; `apply` must be explicit. The route uses
`ingress_route.plan` for dry-run authorization and `ingress_route.apply` for
provider mutation authorization. The service resolves an ingress provider
adapter for route execution; the default provider is NPMplus. The default
provider adapter constructs its client from environment keys named
`LAUNCHPLANE_NPMPLUS_BASE_URL`, `LAUNCHPLANE_NPMPLUS_IDENTITY`, and
`LAUNCHPLANE_NPMPLUS_SECRET`; do not commit real values or local admin
overrides.
Ingress audit records must persist the adapter's provider identity explicitly;
do not rely on a provider-specific model default for admin evidence.

Dokploy-backed ingress routes should use a DB-backed edge endpoint record instead
of passing raw upstream topology through product workflows. The endpoint record
uses a human-friendly `endpoint_key` and `server_name` for admin review, but
the `upstream_host` stored for NPMplus must be an IP address. The service
resolves `route.edge_endpoint_key` into `forward_scheme`, `forward_host`, and
`forward_port` before calling NPMplus, and fails closed when the endpoint is
missing or disabled. Raw `forward_host` remains an explicit admin override;
it is not product-repo-owned topology authority.

The matching CLI entrypoint is service-mediated:

```bash
uv run launchplane ingress route-apply \
  --service-url https://launchplane.example \
  --product launchplane \
  --context example-prod \
  --domain app.example.com \
  --forward-host 192.0.2.10 \
  --forward-port 8080 \
  --certificate-id 1 \
  --identity-access-provider authentik \
  --reason "Plan ingress route"
```

Use `--apply` only after reviewing a matching dry-run. Provider mutations require
an explicit `--idempotency-key`. The CLI accepts a bearer token from
`LAUNCHPLANE_SERVICE_TOKEN` by default or a signed browser session via
`--session-cookie`; it must not read NPMplus credentials locally.

Ingress identity/access should be modeled separately from the edge data plane.
Routes may carry a provider-neutral `identity_access` binding with
`mode: "none"` or `mode: "forward-auth"`. The binding maps the existing NPMplus
`anubis`, `tinyauth`, `authelia`, and `authentik` auth-request modes into a
provider-neutral provider name. `send_basic_auth: true` is specific to
Authentik and maps to NPMplus `authentik-send-basic-auth`, which forwards basic
auth material through the Authentik integration for routes that intentionally
need that mode. Keep Authentik instance placement, provider ids, app slugs, and
private route evidence in private infra docs. Launchplane should consume only
sanitized desired state and managed-secret references.

Prefer the CLI's `--identity-access-provider` flag for new typed desired state.
Use `--identity-access-send-basic-auth` only with `--identity-access-provider authentik`.
The legacy `--auth-request` flag remains available for provider compatibility,
but mixing it with a conflicting identity/access provider fails closed.

Admins can also use the `Ingress Route Dry Run` GitHub workflow for a
service-mediated canary plan through GitHub OIDC. The workflow accepts the
product, context, domain, upstream target, existing certificate id or `new` for
provider-managed issuance, expected
provider host id, typed identity/access provider, and route toggles as manual
inputs, then calls the same route with `mode: dry-run`. Its authz grant is
plan-only; an apply still requires the separate `ingress_route.apply` permission
and an explicit mutation path. The typed identity/access inputs build the same
`identity_access` route binding as the CLI and fail closed if they conflict with
legacy `npmplus_auth_request` values in route options.

The `Ingress Route Apply` workflow is the generic admin mutation path for
reviewed routes. It accepts `product`, `context`, a compact `route_json` desired
state, an explicit idempotency key, and a confirmation phrase. It defaults to an
update-only guard, but `expected_host_id` is optional for ordinary domain-matched
updates so the workflow is not stricter than the service route. Pass
`expected_host_id` after review when the apply should fail closed unless one
specific provider host owns the domain, and enable
`require_exact_expected_host_domains` only when exact host/domain ownership is
known. A reviewed create apply must set `allow_create: true` in `route_json` and
may use `expected_host_id: null` only when the dry-run showed that no existing
provider host owns the domain. The workflow can carry NPMplus `locations` inside
`route_json.route`. Use it for product-specific path routing only after the
dry-run plan is reviewed and the target product/context has an
`ingress_route.apply` grant.

For existing NPMplus proxy hosts, omit `advanced_config` from the desired route
when the provider host should keep its current advanced nginx config. This lets
Odoo routes inherit the extra NPMplus config used for the current password-gated
edge path. Passing `advanced_config` explicitly replaces the provider value;
passing an explicit empty string clears it.

Future edge-driver work should investigate Cloudflare Tunnel in #1215 as a
Launchplane provider before deepening edge-provider assumptions beyond NPMplus.
The intended spike is a shadow hostname routed through `cloudflared` running in
or near Dokploy, with the Traefik service in the Dokploy compose network as the
origin router and NPMplus kept as the active fallback until Cloudflare health
evidence passes. The Cloudflare slice must model account/zone/tunnel identifiers,
public hostname routes, managed credentials, connector placement, health
evidence, and rollback state as Launchplane records. Keep Authentik as a
separate identity/access layer;
issue #1051 tracks provider-neutral `identity_access` bindings and future
Cloudflare Access/OIDC integration, but public website edge routing should prove
itself without requiring Authentik.

The `Ingress Route Dry Run` workflow can discover the existing provider host for
a domain before an apply. Leave `expected_host_id` blank to ask Launchplane to
match by requested domain without mutation; pass `expected_host_id` after review
when you want the dry-run or apply to fail closed unless that specific provider
host owns the domain. Keep `require_exact_expected_host_domains` disabled during
initial discovery, then enable it for reviewed update-only applies when exact
domain ownership is known.

The `Ingress Route Canary Apply` workflow is the matching apply proof path. It
uses the same canary-scoped product/context grant, accepts a canary route key,
and requires an explicit idempotency key plus the confirmation phrase
`apply ingress canary`. The service resolves the canary key through DB-backed
Launchplane ingress canary route records. Those records own the canary domain,
expected provider host id, certificate id, and edge endpoint key; the workflow
must not pass those route topology values directly. The canary apply path fixes
the provider toggles instead of accepting arbitrary provider options, and asks
the service to reject the request unless the expected provider host's domain set
exactly matches the stored canary domain set. Dry-run and apply responses write
Launchplane-owned ingress route audit records that preserve the trace id, mode,
status, requested domains, expected/provider host ids, edge endpoint key,
operations, reason, and idempotency key. Each plan operation includes high-level
change categories such as `route`, `upstream`, `certificate`, `tls`,
`access_list`, `identity_access`, `provider_options`, and `enabled`; those
categories make identity/access drift visible without storing provider topology
or secret values in the public contract. Keep this workflow canary-scoped until
broader route ownership and approval UX are explicit.

Callers with `ingress_route.plan` for the target product/context can inspect
those audit records through the native FastAPI service reads. A caller with only
`route_binding.read` reads them without provider host ids, the certificate
reference or the request's reason, and cannot filter by `provider_host_id`. List records with
`GET /v1/ingress/route-audits/records?product=launchplane&context=example-prod`
and optional `status`, `mode`, `provider_host_id`, `trace_id`,
`idempotency_key`, and `limit` filters. Read one record with
`GET /v1/ingress/route-audits/records/{record_id}?product=launchplane&context=example-prod`.
Both list and single-record endpoints require `product` and `context` so audit
browsing stays scoped.

The `Ingress Route Audit Read` GitHub workflow is the service-mediated OIDC
reader for the same audit records. It accepts product/context plus optional list
filters or a single record id, calls the service with `GET`, and receives only
the canary-scoped `ingress_route.plan` grant. The workflow writes only a
redacted response artifact and summary; raw audit records stay in runner-local
temporary storage because records may include private route or provider details.
Keep private provider identifiers and live topology in private infra docs;
public Launchplane workflow inputs and artifacts should stay limited to
sanitized record ids, trace ids, statuses, operation counts, and
admin-selected filters.

## Product Repo Boundary

Driver development should make product repos thinner, not larger. When a new
driver needs a product workflow, the workflow should only build/test/publish the
artifact and its build manifest; Launchplane picks it up from the build event
([event-driven-deploys.md](event-driven-deploys.md)). See
[product-repo-contract.md](product-repo-contract.md) for the approval gate.

Legacy product repos may still carry scripts that shape Launchplane evidence or
call provider APIs directly. Treat those as migration candidates: classify them,
move the durable behavior into Launchplane, then delete or shrink the product
repo scripts.

When product-specific smoke checks still run in the product repo, keep the
follow-up contract thin: the repo reports the primitive result facts, and the
driver translates them into Launchplane records. Do not leave rendered evidence
payload construction in the product repo.

Generic runtime health, public page readiness, and build identity checks belong
in the driver once Launchplane has the lane profile, target, health path, and
expected artifact identity. Product repos should not keep their own URL
derivation or health polling scripts.

Event-driven previews record provider results and use Launchplane's health check
of the expected build to mark the preview ready. See
[event-driven deploys](event-driven-deploys.md).

Lifecycle declarations are qualified with the deployed artifact, not merely the
provider's image status. Before pausing recurring jobs, the shared deploy writes
a target- and artifact-bound receipt containing their original definitions.
An interrupted attempt resumes from that receipt under the same provider
operation. A failed migration or canary keeps recurring jobs disabled on the
unqualified image; promotion fails and restores the previous artifact. Rollback
skips the new artifact's maintenance commands and canaries the recorded previous
schedules before enabling them. Removing a declaration also removes its
previously managed schedule after qualification, while unrelated jobs are left
alone. Editing a profile cannot remove the qualification required by an existing
deployment receipt.

For declared preview transport, `preview.app_name_suffix` is part of the resource
name used by creation, inventory and destruction. Existing VeriReel applications
use `-app`; record that suffix explicitly when switching its profile. The named
`verireel-postgres` transport retains VeriReel's Prisma, seed and environment
interfaces while its template lane, product identity and resource prefixes come
from the profile. It does not imply compatibility with other applications.

A reservation keeps its original lifecycle declaration. Changing a declaration
requires a new operation after exact recovery of the previous attempt; refusal
does not erase that attempt's receipt. Held snapshots remain available even
when the interrupted attempt never reached environment inventory.

A refusal before an image change records whether schedule undo succeeded. A
restored attempt does not make the still-running release unqualified. Standalone
rollback keeps a separate snapshot of the current jobs so a pre-image refusal
restores them; rollback after a failed promotion keeps jobs paused. Older rollback
targets reuse the historical receipt that actually captured that artifact's
baseline, rather than borrowing the current artifact's declarations. A profile
declaration alone does not add lifecycle work to a pre-extension rollback.

If provider deployment evidence is absent but the exact attempt's receipt proves
that a pre-image refusal restored all schedules, recovery may retry the original
operation. A different attempt or an unqualified/failed-restoration receipt does
not supply that proof. Testing rollback selects the same declared qualification
executor as a forward testing deploy.

### VeriReel extension repair and history

VeriReel uses the shared deploy, promotion, rollback and backup paths. Its
PostgreSQL preview persistence adapter is internal to the declared transport;
it does not expose a parallel native preview API. Legacy deployment, promotion
and backup schemas and migrations remain readers for stored history.

The VeriReel app-maintenance endpoint and source-control smoke-client action are
retired. Fixture setup remains internal to the testing qualification extension
until the product checks move into the product repository. No product workflow
calls a Launchplane maintenance route.

### Profile maintenance and explicit testing reset

Testing lanes and `preview.generic_web_extensions` declare their own maintenance
commands and recurring schedules. Preview refresh pauses declared schedules,
runs maintenance after deploying, verifies runtime health, then canaries each
enabled schedule before enabling it. Preview deployment receipts preserve owned
job names and baselines across refreshes, including interrupted attempts and
removal of declarations. A failed refresh after an image effect
keeps jobs paused. Destroying the preview application removes its attached jobs.

Commands normally run on deploy. A command with `on_reset: true` runs only when
an explicit generic-web testing deploy includes `reset: true`; ordinary deploys
skip it. Reset uses the existing deploy reservation, exact recorded application,
artifact provenance, effect fence, verification and schedule lifecycle. It also
requires current disposable-lane evidence and refuses production or rollback.
The reset command must include all required schema and seed work. It executes
inside the new image, so resetting never needs a second deploy or a call-in
workflow. Scheduled jobs are canaried after reset just as after a normal deploy.

Stored profiles carrying the retired testing-verification or PostgreSQL preview
extensions are converted once to editable declarations. The conversion changes
only testing and preview declarations; production lanes are preserved. The
schema migration performs no provider calls. Rollback leaves declarations
intact because they may have been edited since conversion. Subsequent profile
edits, including removing commands, are never replaced by legacy defaults.
A stale v1 or unversioned write carrying the retired extension markers is
refused: read the current profile and use the existing reviewed generic-web
transition to preserve or remove its declarations explicitly.

Required Compose environment substitutions such as `${APP_SECRET:?required}`
are admin-supplied inputs regardless of product. They do not name a Launchplane
secret binding. The product profile's `expected_config.managed_secret_bindings`
owns actual managed bindings; no product-specific allowlist lives in the audit.
