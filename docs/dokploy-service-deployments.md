---
title: Dokploy Service Deployments
---

## Purpose

Use this contract for a simple product service that Launchplane deploys to a
Dokploy application target. The service may be a bot or worker instead of a
public website, but it still follows the same Launchplane-owned lifecycle:
publish an immutable image, resolve product profile and lane records, mutate the
Dokploy target through Launchplane, and write deployment evidence here.

Discord Blue is the first target product for this service-shaped contract. It
should not keep long-term host-managed `systemd` or `uv` deployment ownership
once this contract is live.

## Supported Model

Launchplane supports a simple service when all of these are true:

- the product publishes one immutable container image per release candidate
- the runtime is a single Dokploy application target per stable lane
- required runtime settings can be represented by Launchplane runtime
  environment records and managed secret bindings
- persistent state is mounted by the Dokploy target and documented as part of
  the product runtime contract
- readiness can be represented by either a small HTTP health endpoint or an
  explicitly skipped health check when the lane has no reachable health URL

Use `driver_id="generic-web"` for this model. The driver name is historical: for
service deployments it means reusable image deploy, health evidence, preview
policy when enabled, promotion evidence, and Launchplane-owned Dokploy
mutation. It does not require the product to expose a public website.

Create a product-specific driver only when the service needs behavior beyond
this contract, such as data migrations, product-specific smoke checks,
destructive repair actions, backup gates, or custom rollback state.

## Launchplane Records

The Launchplane-owned product profile is the service identity and lane map. It
must declare:

- `product`: durable product key, for example `discord-blue`
- `repository`: owning GitHub repo, for example `cbusillo/discord-blue`
- `driver_id`: `generic-web`
- `image.repository`: immutable image repository, for example
  `ghcr.io/cbusillo/discord-blue`
- `runtime_port`: the service's internal HTTP port when it exposes one, or `0`
  when the product has no HTTP runtime surface
- `health_path`: a path beginning with `/` when Launchplane, Dokploy, or public
  ingress monitoring should verify HTTP health; leave it empty only for
  non-HTTP workers with health checks and public ingress monitoring disabled
- `lanes`: stable instances such as `testing` and `prod`, each with a
  Launchplane context and optional `base_url` or `health_url`

Each stable lane also needs DB-backed Dokploy target records:

- `DokployTargetRecord` keyed by `context` plus `instance`
- `target_type="application"`
- `target_name` or `project_name` matching the operator-owned Dokploy route
- `deploy_timeout_seconds` when the service needs a longer rollout window
- `healthcheck_*` fields when Dokploy should independently monitor the service
- `env` only for non-secret provider settings that Launchplane owns
- sibling `DokployTargetIdRecord` carrying the live Dokploy application id

For an existing Dokploy app, use `launchplane dokploy-targets adopt` to create
or refresh those target and target-id records from the live provider id. The
adoption command is dry-run by default and requires `--apply` to write records;
local `--apply` also requires `--allow-direct-db-mutation` and is explicit
local/bootstrap repair only. It does not copy provider env text into
Launchplane. Provider application creation for application targets is available through
`launchplane dokploy-targets create-application`. That command is also dry-run
by default; with local `--apply --allow-direct-db-mutation`, it can create or
reuse the Dokploy project and environment, create the application, and
immediately persist the matching target records. Routine shared/live target
setup should use the deployed service route or operator workflow instead. It
still leaves runtime env, managed secrets, volumes, ports, and health behavior
as explicit setup rather than inferred provider state.

Product repos may document these expected facts, but they must not store live
Launchplane product profiles, target ids, provider credentials, or lifecycle
records as repo-local authority.

## Image Strategy

Product repos own image build and publish. Launchplane owns deploy selection and
evidence after the image exists.

The stable product-repo integration surface for this contract is image-backed
generic-web deploy through `POST /v1/drivers/generic-web/deploy`, normally via
the shared `cbusillo/launchplane/.github/actions/launchplane-request` action.
The product repo submits immutable image identity plus the tested source SHA;
Launchplane resolves lane, provider target, runtime environment, managed
secrets, and deployment records from DB-backed authority.

Publish images to the profile's `image.repository` and prefer digest deployment:

```text
ghcr.io/cbusillo/discord-blue@sha256:<digest>
```

Mutable tags such as `latest`, branch names, or environment names are display or
debug labels only; they are not stable deploy inputs. If a product workflow also
emits a human-readable tag, the Launchplane trigger should still send the exact
digest image reference or an artifact id that Launchplane can resolve to that
digest.

For the current generic-web application deploy path, the submitted
`artifact_id` is the deployable immutable image reference used to update the
Dokploy application's Docker image. Do not submit a mutable tag for service
deploys. If a later slice adds generic artifact-manifest resolution for this
path, the manifest must still resolve to the same `repository@sha256:digest`
identity before Dokploy is mutated.

RepairShopr Sync is the first live canary for this stable shape. The
`cbusillo/repairshopr_api` product workflow built an immutable GHCR image,
called deployed Launchplane, and received `deploy_status: pass` for deployment
record `deployment-20260630T034901Z-repairshopr-sync-prod` after Launchplane PR
#1503 deployed. Treat that run as proof that service-shaped worker products can
use this contract without source-ref deploy or direct Dokploy mutation in the
product repo.

The inherited source-ref deploy bridge is retired. Services that still deploy a
Dokploy compose target from Git must migrate the provider target to immutable
image deploy before using the generic-web service contract; do not use the bridge.
Product repositories must publish immutable images for Launchplane-owned stable
deploys.
Product repos must not keep Dokploy host, token, compose id, provider source-ref
rewrites, or other direct provider mutation scripts as a replacement for the
retired route.

The stable target is immutable image deploy. If the target is a background
worker with no HTTP listener, its product profile may leave `health_path` empty
and set `runtime_port=0`, but every stable lane must disable public ingress
monitoring and omit HTTP URLs, and every provider target must omit domains and
disable Dokploy health checks. Image deploy, preview creation, public ingress
monitoring, provider domains, and provider health checks still require real
image and HTTP health metadata before they can mutate provider state.

Launchplane also injects a non-secret runtime identity into Dokploy env during
Launchplane-owned deploys. The standard keys are
`LAUNCHPLANE_RUNTIME_IDENTITY_JSON`, `LAUNCHPLANE_DEPLOYMENT_RECORD_ID`,
`LAUNCHPLANE_ARTIFACT_ID`, and `LAUNCHPLANE_SOURCE_GIT_REF`. Product health
endpoints should expose that identity when adopted so Launchplane can compare
expected inventory against observed runtime state.
The preferred JSON health payload is bounded and non-secret:

```json
{
  "status": "ok",
  "version": "ghcr.io/example/product@sha256:...",
  "source_git_ref": "<commit-sha>",
  "image_reference": "ghcr.io/example/product@sha256:...",
  "runtime_identity": { "schema_version": 1 }
}
```

`runtime_identity` should be the parsed value from
`LAUNCHPLANE_RUNTIME_IDENTITY_JSON`. Worker-like products can still use this
contract; their endpoint should report product-owned freshness in `status` and
`summary` rather than only process liveness.

## Config, Secrets, And Volumes

Non-secret runtime settings belong in Launchplane runtime-environment records.
Secret settings belong in Launchplane managed secret records and bindings. A
product workflow may pass the product key, source ref, run URL, and immutable
image reference; it should not pass secret values or render a Dokploy env file.
Launchplane live-target runtime sync evaluates runtime key-safety policy for
managed runtime secret bindings before updating Dokploy environment variables,
and records only key-safety status and policy hash evidence.
Generic-web preview refresh applies the same rule to secret-shaped env keys
copied from a template lane: the copied key must resolve to a managed runtime
secret binding on the template lane and the active runtime key-safety policy
must allow that binding for the preview target before Launchplane writes the
preview app env.

Dokploy-owned persistent volumes are allowed for service state, but the volume
mapping is provider target configuration, not product-repo lifecycle state.
Document the mount path in the product runtime contract and record the live
target through Launchplane's Dokploy target records. For Discord Blue, the state
volume should cover product config and bot runtime state that must survive image
replacement; secrets still remain managed secret bindings, not files committed
to the product repo.

Volume contents are not Launchplane release artifacts. If a future service
needs backup, restore, or data migration gates, add a product-specific driver or
typed product policy instead of overloading the generic-web deploy request.

## Ports And Health

`runtime_port` is the container port the service listens on when it exposes an
HTTP endpoint. For Discord Blue, use port `8787` for the Every Code bridge or a
small health/control endpoint if that is the only HTTP surface.

Stable lanes can verify health in two ways:

- set `lane.health_url` to an explicit URL reachable by Launchplane, such as an
  internal bridge health endpoint
- set `lane.base_url` and rely on `profile.health_path` to derive the health URL

If neither value is set, generic-web promotion records skip health verification
for that lane. Skipping health is acceptable only for an initial migration slice
with other evidence, such as a successful Dokploy deployment plus product-owned
log or smoke evidence. The preferred Discord Blue target is a reachable health
URL on port `8787` so promotion can fail closed on a bad rollout.
For sync or worker products, the same endpoint should report whether the worker
is actually fresh. For example, RepairShopr Sync should mark health unhealthy
when the last successful sync is stale even if the process is running.

Do not expose service ports publicly just to satisfy Launchplane. Keep public,
private, and internal-only routing as Dokploy/provider configuration and record
only the product-level health URL or runtime port in Launchplane records.

## Deploy, Promote, And Roll Back

A normal service deploy does this:

1. Product CI builds, tests, and publishes an immutable image.
2. The product workflow calls Launchplane with product key, stable lane,
   source ref, and immutable image reference.
3. Launchplane resolves the product profile lane and DB-backed Dokploy target.
4. Launchplane updates the Dokploy application image and triggers deployment.
5. Launchplane writes a deployment record with the resolved target and status.

Generic-web deploy records post-deploy evidence as `skipped` by default. A
driver that inherits from generic-web can provide a product post-deploy
extension for work that must happen after the provider deployment succeeds. That
extension writes terminal post-deploy evidence without changing the underlying
deploy status, so operators can distinguish "image deploy failed" from "image
deploy passed but product maintenance failed".

Odoo profiles that execute generic-web deploy or rollback apply use this
extension to run the Odoo post-deploy driver after the provider deploy succeeds.
The Odoo-specific rollback apply route remains available for rollback flows that
still need Odoo release tuple and promotion-state updates.

Promotion uses the same artifact identity and target records. When health URLs
exist, Launchplane verifies the source and destination lane health around the
deployment and writes promotion evidence.

Rollback begins with a Launchplane-owned rollback plan. The generic-web planner
is exposed through `POST /v1/drivers/generic-web/prod-rollback-plan` as a
safe-write contract: it reads the product profile, destination lane, a
Launchplane deployment record selected as the rollback target, and optional
backup-gate evidence, then writes a rollback-plan record. It does not mutate
Dokploy or trigger a product workflow.

Launchplane applies generic-web rollback through
`POST /v1/drivers/generic-web/prod-rollback`. The apply route rebuilds and
persists the rollback plan from current records, then calls the normal
generic-web deploy path with the selected previous immutable artifact. Drivers
such as Odoo can keep a product-specific rollback action while they still need
extra gates around backups, release tuples, manifests, migrations, or post-deploy
validation.

Odoo rollback planning uses
`POST /v1/drivers/generic-web/prod-rollback-plan`. The former Odoo-shaped
rollback-plan alias is retired; this does not change Odoo's product-specific
`POST /v1/drivers/odoo/prod-rollback` apply route.

Required planner input:

- `product`
- destination `instance`, usually `prod`
- `rollback_deployment_record_id`, pointing at the previous good deployment
  record for that same context and instance
- optional `backup_record_id` when the product/operator requires backup-gate
  evidence before stable recovery

The planner fails closed when the rollback target is missing, belongs to a
different context or instance, has a failed deploy, has failed health evidence,
or does not reference the product image repository by immutable `@sha256:`
digest. If `backup_required=true`, the request must name a stored backup-gate
record, and that record must match the destination lane and have `status=pass`.

The produced `GenericWebRollbackPlanRecord` stores the selected immutable
artifact identity, source git ref, planned generic-web deploy payload, backup
gate evidence, target health evidence, blockers, and summary. A later explicit
apply path can consume the ready plan and call the normal generic-web deploy
route, but operators must not roll back by clicking Dokploy to a mutable tag or
changing product-repo workflow state. The durable rollback source remains the
Launchplane deployment record and its immutable image digest.

## Discord Blue Target

Discord Blue should target this minimum contract before leaving the hardened
host-managed deployment:

- product key: `discord-blue`
- driver: `generic-web`
- image repository: `ghcr.io/cbusillo/discord-blue`
- stable lanes: at least `testing`; add `prod` before production cutover
- Dokploy target type: `application`
- runtime port: `8787` when the bridge or health service is enabled
- health path: a stable path such as `/health` or `/v1/health`
- persistent volume: one Dokploy-mounted state/config path documented in the
  product repo and represented by the operator-owned target configuration
- secrets: Discord tokens and other credentials managed through Launchplane
  secret bindings, never passed in workflow payloads
- deploy input: immutable image reference, preferably
  `ghcr.io/cbusillo/discord-blue@sha256:<digest>`

The migration is unblocked when Launchplane can read the product profile and
target records, deploy a testing image through the generic-web route, and record
either a passed health check on port `8787` or an explicitly documented skipped
health check with replacement evidence for the first cutover.
