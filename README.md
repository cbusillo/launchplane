# launchplane

Launchplane control-plane repo for release records, environment operations,
preview state, and promotion orchestration.

## Purpose

- Own the Launchplane admin surface for durable deployment truth.
- Own artifact, backup-gate, deployment, promotion, and inventory records
  outside the code and local-DX repos.
- Own ship and promotion orchestration behind explicit control-plane
  contracts.
- Keep product code and local DX in product repos, with only explicit artifact
  and admin handoffs into this repo.

This repo's docs describe the implemented Launchplane contracts that exist
today. Some drivers are intentionally product-specific because they are the
current proving grounds for the shared control-plane boundary; live customer,
tenant, and runtime authority belongs outside git in Launchplane records and
private product repos.

The target shape is explicit: Launchplane is a long-running control-plane
service with authenticated ingress, rather than a repo-local CLI as the
permanent cross-product boundary.

That rename has now started at the repo and CLI surface. The internal Python
module layout still uses `control_plane` for continuity during the transition,
but the public repo and command naming should now be treated as Launchplane-first.

## Bootstrap Scope

- File-backed artifact manifests, backup gates, deployment records, promotion
  records, inventory records, and Launchplane preview records.
- A CLI for records, inventory, backup gates, Launchplane preview operations, and
  ship/promotion planning and execution.
- Repo-local docs, policies, CI, and dependency automation.

These are the current implementation surfaces, not the final Launchplane product
shape. The intended direction is:

- Launchplane service ingress over authenticated HTTP.
- GitHub Actions OIDC for Launchplane's own admin and self-deploy workflows.
- Launchplane-owned product drivers. Product repositories never call
  Launchplane: Launchplane reacts to source-control events and verifies each
  build's provenance (see [docs/event-driven-deploys.md](docs/event-driven-deploys.md)).
- CLI tools that act as local/admin clients of Launchplane contracts rather than
  defining the contract themselves.

The first implemented ingress slice now exists in this repo as a local Launchplane
service command with GitHub OIDC verification, DB-backed workflow policy records, and
evidence ingress for deployments, promotions, and the full preview lifecycle.
Shared-service core records can now be backed by Postgres with
`LAUNCHPLANE_DATABASE_URL` in the service environment.
The same service boundary now exposes authenticated admin read endpoints for
deployment, promotion, inventory, preview, preview history, and recent
context-scoped operations. Launchplane-managed secrets now use the same Postgres
backend, with encrypted secret values stored in DB and bootstrap limited to
process env for the first bring-up.

## Quick Start

```bash
uv run launchplane --help
uv run launchplane service serve --help
cd frontend && npx pnpm@10.10.0 validate
uv run --extra dev python -m unittest
```

Frontend validation runs the frontend unit tests, the OpenAPI drift check,
TypeScript, and the production Vite build. `pnpm --dir frontend test:browser`
runs the Playwright browser smoke. Frontend lint and formatting are not
introduced yet.

When checking the served Launchplane UI, use a browser or `GET /ui` request. Some
server paths may not answer `HEAD /ui` the same way as the app shell, so a
failed `HEAD` probe is not enough evidence that the UI is unavailable.

Runtime authority should come from Launchplane DB records in steady state.
Launchplane-managed secrets, runtime-environment records, tracked Dokploy
target records, and Dokploy target-id records are DB-backed concerns;
bootstrap stays in process env long enough to bring the service up and write
the real records.

Use product-config dry-run/apply through the deployed service route or Launchplane
UI for routine shared and production runtime config changes. Raw
`uv run launchplane environments put --scope ... --set KEY=VALUE --allow-direct-db-mutation`,
`uv run launchplane environments unset --scope ... --key KEY --allow-direct-db-mutation`,
and `uv run launchplane environments relabel --allow-direct-db-mutation` are
explicit local/bootstrap repair paths only; they reject secret-shaped keys or
avoid printing plaintext values. Use product-config dry-run/apply for routine
managed secret values; `uv run launchplane secrets put ... --allow-direct-db-mutation`
is the matching explicit local/bootstrap repair path for direct managed-secret
writes. TOML/env files are not supported runtime import surfaces outside minimal
bootstrap policy/env.

Live authz policy is DB-backed through `authz-policies` records. The service
still requires a minimal bootstrap policy input at startup so it can fail closed
and seed or repair DB-backed policy records, but the repo does not track a live
authz TOML file.

Steady-state tracked Dokploy route definitions and target IDs should come from
Launchplane DB-backed target records and target-id records. The stable remote
lane catalog is now `testing` plus `prod`; pull requests use Launchplane-managed
preview identities and ephemeral preview stacks instead of a durable shared
`dev` lane.

Use `uv run launchplane dokploy-targets list` or `show` to inspect the tracked
DB-backed target catalog without reading legacy TOML files. Use
`uv run launchplane dokploy-targets put-shopify-protected-store-key --context ... --instance ... --key ... --allow-direct-db-mutation`
and `unset-shopify-protected-store-key --allow-direct-db-mutation` only for
explicit local/bootstrap repair of Shopify protected-store-key policy carried by
a tracked target record. Routine shared/live target setup and provider-target
authority changes should use the deployed service route or admin workflow.

## Service Container Deploy

The repo now includes a containerized Launchplane service entrypoint for Dokploy or
similar long-running hosts:

- `Dockerfile`
- `docker-compose.yml`
- `scripts/start-launchplane-service.sh`
- `frontend/`

The service image builds the Vite/React Launchplane UI in a Node 22 stage, copies
only the static bundle into the Python runtime image, and serves it at `/` and
`/ui`. Built assets stay under `/ui/assets/...`; versioned API ingress remains
under `/v1`.
The compose service now expects the Launchplane container image through
`DOCKER_IMAGE_REFERENCE`. Dokploy should set that to an immutable GHCR digest
for real Launchplane deploys. For purely local compose usage, build a local image
first, for example `docker build -t launchplane:local .`, then run `docker compose up`.

The entrypoint accepts only the bootstrap inputs needed to start Launchplane and
reach DB-backed runtime authority:

- `LAUNCHPLANE_DATABASE_URL`
- `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`
- `LAUNCHPLANE_POLICY_TOML`, `LAUNCHPLANE_POLICY_B64`, or `LAUNCHPLANE_POLICY_FILE`

The browser Launchplane UI uses GitHub OAuth when these additional inputs are set:

- `LAUNCHPLANE_GITHUB_CLIENT_ID`
- `LAUNCHPLANE_GITHUB_CLIENT_SECRET`
- `LAUNCHPLANE_PUBLIC_URL`
- `LAUNCHPLANE_SESSION_SECRET`
- optional `LAUNCHPLANE_COOKIE_SECURE` for local HTTP development
- optional `LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS` for comma-separated verified
  GitHub email addresses that receive the initial `admin` role

Human browser sessions use signed cookies backed by the Launchplane database when
`LAUNCHPLANE_DATABASE_URL` is configured. Human roles are authorized through
DB-backed Launchplane authz policy records, with the bootstrap admin email list
available for first-access recovery. Machine writes continue to use GitHub
Actions OIDC bearer tokens. Hosted requests revalidate the active DB policy on
every request; GitHub-derived human organization/team claims must be refreshed
through OAuth at least every 24 hours, and new workflow grants bind both the
repository name and GitHub's immutable repository and repository-owner IDs.

Launchplane now fails closed at startup when no explicit policy input is provided.
The bootstrap policy input should be minimal; live product and workflow grants
belong in DB-backed policy records.

`LAUNCHPLANE_MASTER_ENCRYPTION_KEY` must be present whenever Launchplane needs
to read or write DB-backed managed secrets. Dokploy credentials now resolve
from Launchplane-managed secrets only, and ship-mode overrides belong in
runtime-environment records instead of process env.

The intended Launchplane bring-up path is GitHub-driven deploy, not a manual
laptop-side image swap. The current admin posture is:

- `CI` remains the separate test gate and must pass before Launchplane deploy
  automation replaces the live Dokploy app.
- Launchplane currently targets a single Dokploy-hosted service instance unless
  an admin configures additional service lanes.
- Deploys should update Dokploy by immutable image digest and capture the
  previously running digest before replacement.
- Deploy automation should verify Launchplane health after rollout and immediately
  restore the previous digest when the new image fails health checks.
- Launchplane applies formal Alembic migrations under a database advisory lock at
  service startup. Each rollout declares its target and compatible revisions so
  mixed-version deploys and code rollback remain explicit and verifiable.

The repo now includes `.github/workflows/deploy-launchplane.yml` for that path.
Configure these GitHub settings before enabling it:

- repository variables:
  - `LAUNCHPLANE_RUNNER_LABEL` (the self-hosted runner label both deploy jobs use)
  - `LAUNCHPLANE_DOKPLOY_TARGET_TYPE`
  - `LAUNCHPLANE_DOKPLOY_TARGET_ID`
  - `LAUNCHPLANE_DEPLOY_HEALTH_URLS`
  - optional `LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS`
  - optional `LAUNCHPLANE_DEPLOY_HEALTH_TIMEOUT_SECONDS`
  - optional `LAUNCHPLANE_IMAGE_REPOSITORY`
  - optional `LAUNCHPLANE_GITHUB_CLIENT_ID`
  - optional `LAUNCHPLANE_PUBLIC_URL`
  - optional `LAUNCHPLANE_COOKIE_SECURE`
  - optional `LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS`
- repository secrets:
  - optional `LAUNCHPLANE_GITHUB_CLIENT_SECRET`
  - optional `LAUNCHPLANE_SESSION_SECRET`
  - break-glass rollback `LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST`
  - break-glass rollback `LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN`

The deploy workflow now uses GitHub OIDC plus Launchplane's own service API to
request a self-deploy. It updates the immutable image reference and known OAuth
env keys while preserving the target's minimal bootstrap policy env. Live
product/workflow authz changes should move through DB-backed policy records, not
repo-local TOML. Normal Dokploy credentials should live in Launchplane-managed
secrets inside the shared store, not in GitHub repository secrets. The
break-glass rollback secrets are intentionally narrower: only the manual
`emergency-dokploy-rollback` job reads them, in the `launchplane-break-glass`
environment, when a `workflow_dispatch` types the `break_glass_confirm` phrase
with an exact prior image digest and a reason. A failed automatic rollout
restores the previous digest through the Launchplane service route instead.

`LAUNCHPLANE_DEPLOY_HEALTH_URLS` must point at Launchplane URLs that the
self-hosted deploy runner can reach, typically the public `https://.../v1/health`
endpoint.

Before a real Dokploy deploy, Launchplane now exposes a sanitized preflight check:

```bash
uv run launchplane service inspect-dokploy-target \
  --target-type compose \
  --target-id "$LAUNCHPLANE_DOKPLOY_TARGET_ID"
```

That preflight fails closed when the live Launchplane target is missing critical
runtime contract pieces such as:

- `LAUNCHPLANE_DATABASE_URL`
- `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`
- Launchplane-managed `DOKPLOY_HOST`
- Launchplane-managed `DOKPLOY_TOKEN`
- a Dokploy SSH key for private `git@github.com:...` compose sources

It also reports warnings when the live target still exposes legacy Dokploy
credentials in target env, when managed-store inspection is unavailable, or
when the target lacks a policy input, DB-backed target-id records, DB-backed
runtime-environment records, or an existing `DOCKER_IMAGE_REFERENCE` rollback
baseline.

Launchplane UI data is record-backed evidence rather than an unlabeled live provider
poll. Use the data freshness report to confirm visible surfaces carry provenance
before launch or handoff:

```bash
uv run launchplane service inspect-data-freshness \
  --context <product> \
  --preview-context <product-preview-context>
```

The first freshness gate reports lane and preview surfaces, their source record,
and whether provenance is present. The UI renders the same provenance as compact
`verified`, `recorded`, `stale`, `missing`, or `unsupported` trust labels.

The deploy path still depends on two Dokploy-side prerequisites that Launchplane can
document but cannot fully validate through the current Dokploy API surface:

- Dokploy must be able to pull the Launchplane GHCR image repository. Public
  images may not require a saved registry credential, while private images do.
- The dedicated Postgres service referenced by `LAUNCHPLANE_DATABASE_URL` must
  already be deployed and reachable on the Dokploy network before Launchplane is
  redeployed.

Manual `workflow_dispatch` may also deploy an explicit prior image reference,
which acts as the first admin rollback path.

## Public Posture

The repo is designed to be public source code. Runtime secrets, target IDs,
admin catalogs, and product-specific authorization policy stay outside git in
Launchplane records, GitHub environment/secret settings, or private product
repos. See [docs/public-readiness.md](docs/public-readiness.md).

## Docs

- [docs/README.md](docs/README.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/config-boundary.md](docs/config-boundary.md)
- [docs/service-boundary.md](docs/service-boundary.md)
- [docs/operations.md](docs/operations.md)
- [docs/records.md](docs/records.md)
- [docs/public-readiness.md](docs/public-readiness.md)
