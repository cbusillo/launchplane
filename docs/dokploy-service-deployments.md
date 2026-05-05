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
- `runtime_port`: the service's internal HTTP port when it exposes one
- `health_path`: a path beginning with `/`, even when the lane uses an explicit
  `health_url` or skips health verification
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
it does not copy provider env text into Launchplane. Provider application
creation for application targets is available through
`launchplane dokploy-targets create-application`. That command is also dry-run
by default; with `--apply`, it can create or reuse the Dokploy project and
environment, create the application, and immediately persist the matching target
records. It still leaves runtime env, managed secrets, volumes, ports, and
health behavior as explicit setup rather than inferred provider state.

Product repos may document these expected facts, but they must not store live
Launchplane product profiles, target ids, provider credentials, or lifecycle
records as repo-local authority.

## Image Strategy

Product repos own image build and publish. Launchplane owns deploy selection and
evidence after the image exists.

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

Promotion uses the same artifact identity and target records. When health URLs
exist, Launchplane verifies the source and destination lane health around the
deployment and writes promotion evidence.

Rollback is another Launchplane-owned deployment to a previous immutable image
reference. Operators should choose the previous good image from Launchplane
deployment, promotion, inventory, or release-tuple evidence and redeploy that
exact digest. Do not roll back by clicking Dokploy to a mutable tag or by
changing product-repo workflow state. A future product-specific rollback route
can add one-click selection and product smoke evidence, but the durable rollback
source remains the previous immutable image identity.

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
