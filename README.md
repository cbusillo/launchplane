# harbor

Private Odoo control-plane repo for release records, environment operations,
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

## Quick Start

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/harbor"
cp config/dokploy-targets.toml.example config/dokploy-targets.toml
uv run harbor --help
uv run harbor service serve --help
uv run python -m unittest
```

Runtime secrets should come from the current process environment or from
external Harbor config files such as
`${XDG_CONFIG_HOME:-$HOME/.config}/harbor/dokploy.env` and
`${XDG_CONFIG_HOME:-$HOME/.config}/harbor/runtime-environments.toml`, not from
repo-local secret files.

For the first local Harbor service run, copy
`config/harbor-authz.toml.example` to a local policy file and adjust the repo,
workflow, product, context, and action allow-lists to match the workflows you
want Harbor to trust.

The tracked Dokploy route catalog lives in `config/dokploy.toml`, with
operator-local target IDs supplied through `config/dokploy-targets.toml`.
The stable remote lane catalog is now `testing` plus `prod`; pull requests use
Harbor-managed preview identities and ephemeral preview stacks instead of a
durable shared `dev` lane.

## Docs

- [docs/README.md](docs/README.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/service-boundary.md](docs/service-boundary.md)
- [docs/operations.md](docs/operations.md)
- [docs/records.md](docs/records.md)
