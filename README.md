# odoo-control-plane

Private control-plane repo for Odoo release records, promotion orchestration,
and environment operations.

## Purpose

- Own artifact, backup-gate, deployment, promotion, and inventory records
  outside `odoo-ai`.
- Own ship and promotion orchestration behind explicit control-plane
  contracts.
- Keep `odoo-ai` focused on code and local DX, with only explicit artifact and
  operator handoffs between repos.

## Bootstrap Scope

- File-backed artifact manifests and promotion records.
- A CLI for records, inventory, backup gates, and ship/promotion planning and
  execution.
- Repo-local docs, policies, CI, and dependency automation.

## Quick Start

```bash
cp .env.example .env
uv run control-plane --help
uv run python -m unittest
```

The default Dokploy target catalog lives in `config/dokploy.toml`.

## Docs

- [docs/README.md](docs/README.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/operations.md](docs/operations.md)
- [docs/records.md](docs/records.md)
