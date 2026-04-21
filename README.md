# harbor

Odoo control-plane repo for release records, environment operations,
Harbor preview state, and promotion orchestration.

## Purpose

- Own the current Odoo Harbor operator surface for durable deployment truth.
- Own artifact, backup-gate, deployment, promotion, and inventory records
  outside the code and local-DX repos.
- Own ship and promotion orchestration behind explicit control-plane
  contracts.
- Keep code and local DX in `odoo-devkit`, tenant repos, and shared-addons,
  with only explicit artifact and operator handoffs into this repo.

This repo's docs describe the implemented Odoo control-plane contracts that
exist today. Harbor is the operator surface name used inside this repo today;
this repository is still the Odoo-specific implementation, not a standalone
general Harbor repo. Broader Harbor product direction stays in saved plans
until matching generic code and operator surfaces exist.

The target shape is now explicit: Harbor should become a long-running control
plane service with authenticated ingress, rather than treating the repo-local
CLI as the permanent cross-product boundary.

That rename has now started at the repo and CLI surface. The internal Python
module layout still uses `control_plane` for continuity during the transition,
but the public repo and command naming should now be treated as Harbor-first.

## Bootstrap Scope

- File-backed artifact manifests, backup gates, deployment records, promotion
  records, inventory records, and Harbor preview records.
- A CLI for records, inventory, backup gates, Harbor preview operations, and
  ship/promotion planning and execution.
- Repo-local docs, policies, CI, and dependency automation.

These are the current implementation surfaces, not the final Harbor product
shape. The intended direction is:

- Harbor service ingress over authenticated HTTP.
- GitHub Actions OIDC for workflow-to-Harbor trust.
- Harbor-owned product drivers plus thin repo extensions.
- CLI tools that act as local/operator clients of Harbor contracts rather than
  defining the contract themselves.

The first implemented ingress slice now exists in this repo as a local Harbor
service command with GitHub OIDC verification, a static workflow policy, and
evidence ingress for deployments, promotions, and the full preview lifecycle.
Shared-service core records can now be backed by Postgres with
`HARBOR_DATABASE_URL` or `uv run harbor service serve --database-url ...`.
The same service boundary now exposes authenticated operator read endpoints for
deployment, promotion, inventory, preview, preview history, and recent
context-scoped operations. Harbor-managed secrets now use the same Postgres
backend, with encrypted secret values stored in DB and a bootstrap import path
from the existing `dokploy.env` and runtime-environment file surfaces.

## Quick Start

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/harbor"
cp config/dokploy-targets.toml.example config/dokploy-targets.toml
uv run harbor --help
uv run harbor service serve --help
uv run harbor storage import-core-records --help
uv run harbor secrets import-bootstrap --help
uv run python -m unittest
```

Runtime secrets should come from the current process environment or from
external Harbor config files such as
`${XDG_CONFIG_HOME:-$HOME/.config}/harbor/dokploy.env` and
`${XDG_CONFIG_HOME:-$HOME/.config}/harbor/runtime-environments.toml`, not from
repo-local secret files.

Once Harbor-managed secret records exist in Postgres, Harbor reads those
encrypted DB-backed values first for Dokploy credentials and runtime
environment secret keys, then falls back to the older file/env surfaces only
when no Harbor-managed secret has been written yet.

For the first local Harbor service run, copy
`config/harbor-authz.toml.example` to a local policy file and adjust the repo,
workflow, product, context, and action allow-lists to match the workflows you
want Harbor to trust.

The tracked Dokploy route catalog lives in `config/dokploy.toml`, with
operator-local target IDs supplied through `config/dokploy-targets.toml`.
The stable remote lane catalog is now `testing` plus `prod`; pull requests use
Harbor-managed preview identities and ephemeral preview stacks instead of a
durable shared `dev` lane.

## Service Container Deploy

The repo now includes a containerized Harbor service entrypoint for Dokploy or
similar long-running hosts:

- `Dockerfile`
- `docker-compose.yml`
- `scripts/start-harbor-service.sh`

The compose service now expects the Harbor container image through
`DOCKER_IMAGE_REFERENCE`. Dokploy should set that to an immutable GHCR digest
for real Harbor deploys. For purely local compose usage, build a local image
first, for example `docker build -t harbor:local .`, then run `docker compose up`.

The entrypoint can bootstrap operator-local files from environment variables so
the deployed service does not need checked-in secret or target-id files:

- `HARBOR_DATABASE_URL`
- `HARBOR_MASTER_ENCRYPTION_KEY`
- `HARBOR_POLICY_TOML` or `HARBOR_POLICY_B64`
- `HARBOR_DOKPLOY_TARGET_IDS_TOML` or `HARBOR_DOKPLOY_TARGET_IDS_B64`
- `HARBOR_RUNTIME_ENVIRONMENTS_TOML` or `HARBOR_RUNTIME_ENVIRONMENTS_B64`

Regular runtime env such as `DOKPLOY_HOST`, `DOKPLOY_TOKEN`, and any
`DOKPLOY_SHIP_MODE_*` overrides can still be passed directly as process
environment variables. `HARBOR_MASTER_ENCRYPTION_KEY` must be present whenever
Harbor needs to read or write DB-backed managed secrets.

The intended first real Harbor bring-up path is GitHub-driven deploy, not a
manual laptop-side image swap. The current operator posture is:

- `CI` remains the separate test gate and must pass before Harbor deploy
  automation replaces the live Dokploy app.
- Harbor currently targets a single real Dokploy-hosted service instance,
  without a separate Harbor testing lane yet.
- Deploys should update Dokploy by immutable image digest and capture the
  previously running digest before replacement.
- Deploy automation should verify Harbor health after rollout and immediately
  restore the previous digest when the new image fails health checks.
- Until Harbor has a formal schema migration system, Postgres schema changes
  must remain additive and backward-compatible so code rollback stays viable.

The repo now includes `.github/workflows/deploy-harbor.yml` for that path.
Configure these GitHub settings before enabling it:

- repository secrets:
  - `DOKPLOY_HOST`
  - `DOKPLOY_TOKEN`
- repository variables:
  - `HARBOR_DOKPLOY_TARGET_TYPE`
  - `HARBOR_DOKPLOY_TARGET_ID`
  - `HARBOR_DEPLOY_HEALTH_URLS`
  - optional `HARBOR_DOKPLOY_DEPLOY_TIMEOUT_SECONDS`
  - optional `HARBOR_DEPLOY_HEALTH_TIMEOUT_SECONDS`
  - optional `HARBOR_IMAGE_REPOSITORY`

`HARBOR_DEPLOY_HEALTH_URLS` must point at Harbor URLs that GitHub-hosted
runners can reach, typically the public `https://.../v1/health` endpoint.

Before a real Dokploy deploy, Harbor now exposes a sanitized preflight check:

```bash
uv run harbor service inspect-dokploy-target \
  --target-type compose \
  --target-id "$HARBOR_DOKPLOY_TARGET_ID"
```

That preflight fails closed when the live Harbor target is missing critical
runtime contract pieces such as:

- `HARBOR_DATABASE_URL`
- `HARBOR_MASTER_ENCRYPTION_KEY`
- `DOKPLOY_HOST`
- `DOKPLOY_TOKEN`
- a Dokploy SSH key for private `git@github.com:...` compose sources

It also reports warnings when the live target lacks a policy input, target-id
catalog input, runtime-environment catalog input, or an existing
`DOCKER_IMAGE_REFERENCE` rollback baseline.

The deploy path still depends on two Dokploy-side prerequisites that Harbor can
document but cannot fully validate through the current Dokploy API surface:

- Dokploy must have a working saved registry credential for the Harbor GHCR
  image repository.
- The dedicated Postgres service referenced by `HARBOR_DATABASE_URL` must
  already be deployed and reachable on the Dokploy network before Harbor is
  redeployed.

Manual `workflow_dispatch` may also deploy an explicit prior image reference,
which acts as the first operator rollback path while Harbor still has only one
real Dokploy-hosted service instance.

## Public Readiness

The repo is not ready to flip public blindly yet. The current gap list and the
cleanup needed before that move live in [docs/public-readiness.md](docs/public-readiness.md).

## Docs

- [docs/README.md](docs/README.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/service-boundary.md](docs/service-boundary.md)
- [docs/operations.md](docs/operations.md)
- [docs/records.md](docs/records.md)
- [docs/public-readiness.md](docs/public-readiness.md)
