---
title: Secrets
---

## Purpose

- Define the control-plane-owned secret contract for deploy and operator
  workflows.

## Current Contract

- Dokploy credentials belong to `harbor`.
- Keep real Dokploy values in the current process environment or in an
  external Harbor config file such as
  `${XDG_CONFIG_HOME:-$HOME/.config}/harbor/dokploy.env`.
- Local runtime environment truth should live outside the repo checkout, such
  as `${XDG_CONFIG_HOME:-$HOME/.config}/harbor/runtime-environments.toml`.
- Live Dokploy `target_id` values belong in the control-plane repo's untracked
  `config/dokploy-targets.toml`.
- `DOKPLOY_HOST` and `DOKPLOY_TOKEN` may also be provided through the current
  process environment.
- Optional ship-mode overrides such as `DOKPLOY_SHIP_MODE` and
  `DOKPLOY_SHIP_MODE_<CONTEXT>_<INSTANCE>` are also read from the same
  control-plane env surface.
- Harbor preview routing now uses a dedicated `HARBOR_PREVIEW_BASE_URL`
  runtime-environment value instead of piggybacking on ordinary live-instance
  web base URLs.
- If you need a non-default secret file location, set
  `ODOO_CONTROL_PLANE_ENV_FILE` to an alternate untracked env file path.
- If you need a non-default runtime environments file location, set
  `ODOO_CONTROL_PLANE_RUNTIME_ENVIRONMENTS_FILE`.

## Rules

- Do not keep real secret files in the repo checkout.
- Never commit alternate secret files or rendered env artifacts.
- Never commit a real runtime environments file; keep it outside the repo and
  start from `config/runtime-environments.toml.example`.
- Never commit `config/dokploy-targets.toml`; keep the real file untracked and
  start from `config/dokploy-targets.toml.example`.
- Do not rely on a repo-local `.env` for control-plane-owned secrets.
- Missing Dokploy credentials are a hard error, not a silent fallback.

## Local Runtime Contract

- `uv run harbor environments resolve --context <ctx> --instance
<instance> --json-output`
  emits the resolved runtime environment payload for a tenant environment.
- Harbor preview write/build helpers read `HARBOR_PREVIEW_BASE_URL` from the
  shared plus context-scoped runtime environment contract, with shared values
  providing the default and context values allowed to override it.
- `odoo-devkit` may consume that contract when the operator points
  `ODOO_CONTROL_PLANE_ROOT` at a valid `harbor` checkout.
- When `odoo-devkit` is configured to use the control-plane contract, legacy
  devkit-local `.env` / `platform/.env` / `platform/secrets.toml` files should
  be removed so environment authority stays single-source and fail-closed.

## Bootstrap

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/harbor"
cp config/dokploy-targets.toml.example config/dokploy-targets.toml
cp config/runtime-environments.toml.example \
  "${XDG_CONFIG_HOME:-$HOME/.config}/harbor/runtime-environments.toml"
cp .env.example "${XDG_CONFIG_HOME:-$HOME/.config}/harbor/dokploy.env"
```
