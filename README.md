# odoo-control-plane

Private control-plane repo for Odoo release records, promotion orchestration,
and environment operations.

## Purpose

- Own artifact and promotion records outside `odoo-ai`.
- Become the long-term home for promotion and deploy orchestration.
- Keep `odoo-ai` focused on code, local DX, and thin compatibility wrappers.

## Bootstrap Scope

- File-backed artifact manifests and promotion records.
- A minimal CLI for record persistence and promotion recording.
- Repo-local docs, policies, CI, and dependency automation.

## Quick Start

```bash
cp .env.example .env
uv run control-plane --help
uv run python -m unittest
```

## Docs

- [docs/README.md](docs/README.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/operations.md](docs/operations.md)
- [docs/records.md](docs/records.md)
