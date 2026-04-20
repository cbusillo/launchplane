# odoo-control-plane

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

That also means the current `odoo-control-plane` name should be treated as
transitional. Once Harbor has a real service/API/OIDC boundary, the main
repo/package/CLI naming should move to Harbor-first naming.

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

## Quick Start

```bash
cp .env.example .env
cp config/dokploy-targets.toml.example config/dokploy-targets.toml
uv run control-plane --help
uv run python -m unittest
```

The tracked Dokploy route catalog lives in `config/dokploy.toml`, with
operator-local target IDs supplied through `config/dokploy-targets.toml`.
The stable remote lane catalog is now `testing` plus `prod`; pull requests use
Harbor-managed preview identities and ephemeral preview stacks instead of a
durable shared `dev` lane.

## Docs

- [docs/README.md](docs/README.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/operations.md](docs/operations.md)
- [docs/records.md](docs/records.md)
