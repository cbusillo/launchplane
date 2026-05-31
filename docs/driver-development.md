---
title: Driver Development
---

## Purpose

Launchplane drivers are the backend-owned boundary for product lifecycle
behavior. A driver declares what a product can do, validates requests, executes
or delegates provider work, writes durable records, and exposes read models for
operators and future UI actions.

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

## Driver Shape

Each driver should have these pieces:

- Descriptor metadata in `control_plane/drivers/registry.py`.
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
Do not move a product apply route onto generic deploy until its remaining
release, backup, promotion, migration, and post-deploy invariants are either
represented in generic contracts or still explicitly wrapped by the product
driver.

Generic-web deploy resolves and executes runtime targets through a deploy
provider adapter. The default adapter is Dokploy, but generic-web orchestration
must depend on the adapter protocol rather than importing provider clients
directly. Deployment records must carry the adapter's provider identity,
provider target reference, and delegated executor so operator evidence stays
accurate when a future deploy provider is introduced.

## Capability Design

Use capability names to describe operator-visible behavior, not implementation
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
Expose them in read models only when operators need them to decide or repair
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
4. Wire service routes and authz action checks in `control_plane/service.py`.
5. Write records through existing storage contracts when possible.
6. Add focused unit tests for validation, authorization, execution, and failure
   evidence.
7. Update docs and any product-repo trigger examples.
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
managed secret or operator configuration boundaries.

The NPMplus adapter in `control_plane/npmplus.py` is the first ingress-provider
slice. It models session-cookie authentication, proxy-host payloads, and the
proxy-host create/read/update/disable/enable/delete lifecycle that was proven
against a disposable canary route. Future ingress driver routes should use this
adapter instead of direct NPMplus SQLite or generated nginx config writes.

The service-backed ingress route is `POST /v1/drivers/ingress/route-apply`.
Callers send a product/context envelope plus a typed route request. `mode` is
`dry-run` by default for CLI callers; `apply` must be explicit. The route uses
`ingress_route.plan` for dry-run authorization and `ingress_route.apply` for
provider mutation authorization. The service constructs the NPMplus client from
environment keys named `LAUNCHPLANE_NPMPLUS_BASE_URL`,
`LAUNCHPLANE_NPMPLUS_IDENTITY`, and `LAUNCHPLANE_NPMPLUS_SECRET`; do not commit
real values or local operator overrides.

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
  --reason "Plan ingress route"
```

Use `--apply` only after reviewing a matching dry-run. Provider mutations require
an explicit `--idempotency-key`. The CLI accepts a bearer token from
`LAUNCHPLANE_SERVICE_TOKEN` by default or a signed browser session via
`--session-cookie`; it must not read NPMplus credentials locally.

Operators can also use the `Ingress Route Dry Run` GitHub workflow for a
service-mediated canary plan through GitHub OIDC. The workflow accepts the
product, context, domain, upstream target, existing certificate id, expected
provider host id, and route toggles as manual inputs, then calls the same route with
`mode: dry-run`. Its authz grant is plan-only; an apply still requires the
separate `ingress_route.apply` permission and an explicit mutation path.

The `Ingress Route Canary Apply` workflow is the matching apply proof path. It
uses the same canary-scoped product/context grant, reads the expected provider
host id and canary route tuple from repository variables, and requires an explicit
idempotency key plus the confirmation phrase `apply ingress canary`. It fixes the
current canary route toggles instead of accepting arbitrary provider options, and
asks the service to reject the request unless the expected provider host's domain
set exactly matches the requested canary domain set. Dry-run and apply responses
write Launchplane-owned ingress route audit records that preserve the trace id,
mode, status, requested domains, expected/provider host ids, operations, reason,
and idempotency key. Keep this workflow canary-scoped until broader route
ownership and approval UX are explicit.

Operators with `ingress_route.plan` for the target product/context can inspect
those audit records through the service. List records with
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
operator-selected filters.

## Product Repo Boundary

Driver development should make product repos thinner, not larger. When a new
driver needs a product workflow, the workflow should only build/test/publish the
artifact and send a minimal Launchplane trigger request. See
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
expected artifact identity. Product repos should call the driver route, not keep
their own URL derivation or health polling scripts.

For example, VeriReel preview refresh writes the initial preview generation from
the provider result, then the product repo reports only the product smoke result
to `/v1/drivers/verireel/preview-verification`. Launchplane updates the latest
preview generation to `ready` or `failed` and owns the durable record shape.
