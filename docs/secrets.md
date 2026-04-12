---
title: Secrets
---

## Purpose

- Define the control-plane-owned secret contract for deploy and operator
  workflows.

## Current Contract

- Dokploy credentials belong to `odoo-control-plane`.
- Keep real values in the control-plane repo's untracked `.env` by default.
- Local runtime environment truth may live in the control-plane repo's
  untracked `config/runtime-environments.toml`.
- Live Dokploy `target_id` values belong in the control-plane repo's untracked
  `config/dokploy-targets.toml`.
- `DOKPLOY_HOST` and `DOKPLOY_TOKEN` may also be provided through the current
  process environment.
- Optional ship-mode overrides such as `DOKPLOY_SHIP_MODE` and
  `DOKPLOY_SHIP_MODE_<CONTEXT>_<INSTANCE>` are also read from the same
  control-plane env surface.
- If you need a non-default secret file location, set
  `ODOO_CONTROL_PLANE_ENV_FILE` to an alternate untracked env file path.

## Rules

- Never commit `.env`, alternate secret files, or rendered env artifacts.
- Never commit `config/runtime-environments.toml`; keep the real file
  untracked and start from `config/runtime-environments.toml.example`.
- Never commit `config/dokploy-targets.toml`; keep the real file untracked and
  start from `config/dokploy-targets.toml.example`.
- Do not rely on a separate code repo's `.env` for control-plane-owned secrets.
- Missing Dokploy credentials are a hard error, not a silent fallback.

## Local Runtime Contract

- `uv run control-plane environments resolve --context <ctx> --instance <instance> --json-output`
  emits the resolved runtime environment payload for a tenant environment.
- `odoo-devkit` may consume that contract when the operator points
  `ODOO_CONTROL_PLANE_ROOT` at a valid `odoo-control-plane` checkout.
- When `odoo-devkit` is configured to use the control-plane contract, legacy
  devkit-local `.env` / `platform/.env` / `platform/secrets.toml` files should
  be removed so environment authority stays single-source and fail-closed.

## Bootstrap

```bash
cp .env.example .env
cp config/dokploy-targets.toml.example config/dokploy-targets.toml
cp config/runtime-environments.toml.example config/runtime-environments.toml
```
