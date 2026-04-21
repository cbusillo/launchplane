# launchplane

Odoo control-plane repo for release records, environment operations,
Launchplane preview state, and promotion orchestration.

## Purpose

- Own the current Odoo Launchplane operator surface for durable deployment truth.
- Own artifact, backup-gate, deployment, promotion, and inventory records
  outside the code and local-DX repos.
- Own ship and promotion orchestration behind explicit control-plane
  contracts.
- Keep code and local DX in `odoo-devkit`, tenant repos, and shared-addons,
  with only explicit artifact and operator handoffs into this repo.

This repo's docs describe the implemented Odoo control-plane contracts that
exist today. Launchplane is the operator surface name used inside this repo today;
this repository is still the Odoo-specific implementation, not a standalone
general Launchplane repo. Broader Launchplane product direction stays in saved plans
until matching generic code and operator surfaces exist.

The target shape is now explicit: Launchplane should become a long-running control
plane service with authenticated ingress, rather than treating the repo-local
CLI as the permanent cross-product boundary.

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
- GitHub Actions OIDC for workflow-to-Launchplane trust.
- Launchplane-owned product drivers plus thin repo extensions.
- CLI tools that act as local/operator clients of Launchplane contracts rather than
  defining the contract themselves.

The first implemented ingress slice now exists in this repo as a local Launchplane
service command with GitHub OIDC verification, a static workflow policy, and
evidence ingress for deployments, promotions, and the full preview lifecycle.
Shared-service core records can now be backed by Postgres with
`LAUNCHPLANE_DATABASE_URL` or `uv run launchplane service serve --database-url ...`.
The same service boundary now exposes authenticated operator read endpoints for
deployment, promotion, inventory, preview, preview history, and recent
context-scoped operations. Launchplane-managed secrets now use the same Postgres
backend, with encrypted secret values stored in DB and a bootstrap import path
from the existing `dokploy.env` and runtime-environment file surfaces.

## Quick Start

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/launchplane"
cp config/dokploy-targets.toml.example config/dokploy-targets.toml
uv run launchplane --help
uv run launchplane service serve --help
uv run launchplane storage import-core-records --help
uv run launchplane secrets import-bootstrap --help
uv run python -m unittest
```

Runtime secrets should come from the current process environment or from
external Launchplane config files such as
`${XDG_CONFIG_HOME:-$HOME/.config}/launchplane/dokploy.env` and
`${XDG_CONFIG_HOME:-$HOME/.config}/launchplane/runtime-environments.toml`, not from
repo-local secret files.

Once Launchplane-managed secret records exist in Postgres, Launchplane reads those
encrypted DB-backed values first for Dokploy credentials and runtime
environment secret keys, then falls back to the older file/env surfaces only
when no Launchplane-managed secret has been written yet.

For the first local Launchplane service run, copy
`config/launchplane-authz.toml.example` to a real local policy file such as
`${XDG_CONFIG_HOME:-$HOME/.config}/launchplane/launchplane-authz.toml`, then replace the
example repo/workflow values and adjust the product, context, and action
allow-lists to match the workflows you want Launchplane to trust.

The tracked Dokploy route catalog lives in `config/dokploy.toml`, with
operator-local target IDs supplied through `config/dokploy-targets.toml`.
The stable remote lane catalog is now `testing` plus `prod`; pull requests use
Launchplane-managed preview identities and ephemeral preview stacks instead of a
durable shared `dev` lane.

## Service Container Deploy

The repo now includes a containerized Launchplane service entrypoint for Dokploy or
similar long-running hosts:

- `Dockerfile`
- `docker-compose.yml`
- `scripts/start-launchplane-service.sh`

The compose service now expects the Launchplane container image through
`DOCKER_IMAGE_REFERENCE`. Dokploy should set that to an immutable GHCR digest
for real Launchplane deploys. For purely local compose usage, build a local image
first, for example `docker build -t launchplane:local .`, then run `docker compose up`.

The entrypoint can bootstrap operator-local files from environment variables so
the deployed service does not need checked-in secret or target-id files:

- `LAUNCHPLANE_DATABASE_URL`
- `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`
- `LAUNCHPLANE_POLICY_TOML` or `LAUNCHPLANE_POLICY_B64`
- `LAUNCHPLANE_DOKPLOY_TARGET_IDS_TOML` or `LAUNCHPLANE_DOKPLOY_TARGET_IDS_B64`
- `LAUNCHPLANE_RUNTIME_ENVIRONMENTS_TOML` or `LAUNCHPLANE_RUNTIME_ENVIRONMENTS_B64`

Launchplane now fails closed at startup when no explicit policy input is provided.
Do not point `LAUNCHPLANE_POLICY_FILE` at `config/launchplane-authz.toml.example`; copy
the example to a non-`.example` path first and replace the placeholder repo
identities.

Regular runtime env such as `DOKPLOY_HOST`, `DOKPLOY_TOKEN`, and any
`DOKPLOY_SHIP_MODE_*` overrides can still be passed directly as process
environment variables. `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` must be present whenever
Launchplane needs to read or write DB-backed managed secrets.

The intended first real Launchplane bring-up path is GitHub-driven deploy, not a
manual laptop-side image swap. The current operator posture is:

- `CI` remains the separate test gate and must pass before Launchplane deploy
  automation replaces the live Dokploy app.
- Launchplane currently targets a single real Dokploy-hosted service instance,
  without a separate Launchplane testing lane yet.
- Deploys should update Dokploy by immutable image digest and capture the
  previously running digest before replacement.
- Deploy automation should verify Launchplane health after rollout and immediately
  restore the previous digest when the new image fails health checks.
- Until Launchplane has a formal schema migration system, Postgres schema changes
  must remain additive and backward-compatible so code rollback stays viable.

The repo now includes `.github/workflows/deploy-launchplane.yml` for that path.
Configure these GitHub settings before enabling it:

- repository variables:
  - `LAUNCHPLANE_DOKPLOY_TARGET_TYPE`
  - `LAUNCHPLANE_DOKPLOY_TARGET_ID`
  - `LAUNCHPLANE_DEPLOY_HEALTH_URLS`
  - optional `LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS`
  - optional `LAUNCHPLANE_DEPLOY_HEALTH_TIMEOUT_SECONDS`
  - optional `LAUNCHPLANE_IMAGE_REPOSITORY`

The deploy workflow now uses GitHub OIDC plus Launchplane's own service API to
request a self-deploy. Dokploy credentials should live in Launchplane-managed
secrets inside the shared store, not in GitHub repository secrets.

`LAUNCHPLANE_DEPLOY_HEALTH_URLS` must point at Launchplane URLs that GitHub-hosted
runners can reach, typically the public `https://.../v1/health` endpoint.

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

It also reports warnings when the live target lacks a policy input, target-id
catalog input, runtime-environment catalog input, or an existing
`DOCKER_IMAGE_REFERENCE` rollback baseline.

The deploy path still depends on two Dokploy-side prerequisites that Launchplane can
document but cannot fully validate through the current Dokploy API surface:

- Dokploy must have a working saved registry credential for the Launchplane GHCR
  image repository.
- The dedicated Postgres service referenced by `LAUNCHPLANE_DATABASE_URL` must
  already be deployed and reachable on the Dokploy network before Launchplane is
  redeployed.

Manual `workflow_dispatch` may also deploy an explicit prior image reference,
which acts as the first operator rollback path while Launchplane still has only one
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
