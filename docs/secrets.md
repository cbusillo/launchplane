---
title: Secrets
---

## Purpose

- Define the control-plane-owned secret contract for deploy and operator
  workflows.

## Current Contract

- Dokploy credentials belong to `launchplane`.
- Launchplane can now persist managed secret values in the Postgres shared-service
  backend when `LAUNCHPLANE_DATABASE_URL` is configured.
- Managed secret values are encrypted before Launchplane stores them; the master key
  stays outside the database in `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`.
- Keep existing bootstrap values in the current process environment or in an
  external Launchplane config file such as
  `${XDG_CONFIG_HOME:-$HOME/.config}/launchplane/dokploy.env` until they have been
  imported into Launchplane-managed secret records.
- Local runtime environment truth should live outside the repo checkout, such
  as `${XDG_CONFIG_HOME:-$HOME/.config}/launchplane/runtime-environments.toml`.
- Live Dokploy `target_id` values belong in the control-plane repo's untracked
  `config/dokploy-targets.toml`.
- `DOKPLOY_HOST` and `DOKPLOY_TOKEN` may also be provided through the current
  process environment.
- Launchplane can import the current `DOKPLOY_HOST` / `DOKPLOY_TOKEN` values with
  `uv run launchplane secrets import-bootstrap --database-url ...` so the first
  shared-service bring-up does not require manual secret re-entry.
- Optional ship-mode overrides such as `DOKPLOY_SHIP_MODE` and
  `DOKPLOY_SHIP_MODE_<CONTEXT>_<INSTANCE>` are also read from the same
  control-plane env surface.
- Launchplane preview routing now uses a dedicated `LAUNCHPLANE_PREVIEW_BASE_URL`
  runtime-environment value instead of piggybacking on ordinary live-instance
  web base URLs.
- If you need a non-default secret file location, set
  `ODOO_CONTROL_PLANE_ENV_FILE` to an alternate untracked env file path.
- If you need a non-default runtime environments file location, set
  `ODOO_CONTROL_PLANE_RUNTIME_ENVIRONMENTS_FILE`.

## DB-Backed Secret Resolution

- Launchplane reads DB-backed managed secrets first when matching secret records
  exist for:
  - Dokploy `DOKPLOY_HOST`
  - Dokploy `DOKPLOY_TOKEN`
  - runtime-environment keys that look like secrets, such as `*_PASSWORD`,
    `*_TOKEN`, `*_SECRET`, and `*_KEY`
- Launchplane falls back to the older file/env surfaces only when no managed secret
  record exists for a requested binding.
- Secret status surfaces return metadata only. Launchplane does not expose routine
  plaintext read commands or service endpoints.

## Rules

- Do not keep real secret files in the repo checkout.
- Never commit alternate secret files or rendered env artifacts.
- Never commit a real runtime environments file; keep it outside the repo and
  start from `config/runtime-environments.toml.example`.
- Never commit `config/dokploy-targets.toml`; keep the real file untracked and
  start from `config/dokploy-targets.toml.example`.
- Do not rely on a repo-local `.env` for control-plane-owned secrets.
- Missing Dokploy credentials are a hard error, not a silent fallback.
- Missing `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` is a hard error when Launchplane needs to
  read or write DB-backed managed secrets.
- The live Launchplane Dokploy target should expose `LAUNCHPLANE_DATABASE_URL`,
  `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`, `DOKPLOY_HOST`, and `DOKPLOY_TOKEN` through
  target env or an equivalent mounted runtime contract.
- Use `uv run launchplane service inspect-dokploy-target ...` to verify that the
  live Launchplane target has the required secret-backed contract without printing
  plaintext secret values.

## Local Runtime Contract

- `uv run launchplane environments resolve --context <ctx> --instance
<instance> --json-output`
  emits the resolved runtime environment payload for a tenant environment.
- Launchplane preview write/build helpers read `LAUNCHPLANE_PREVIEW_BASE_URL` from the
  shared plus context-scoped runtime environment contract, with shared values
  providing the default and context values allowed to override it.
- `odoo-devkit` may consume that contract when the operator points
  `ODOO_CONTROL_PLANE_ROOT` at a valid `launchplane` checkout.
- When `odoo-devkit` is configured to use the control-plane contract, legacy
  devkit-local `.env` / `platform/.env` / `platform/secrets.toml` files should
  be removed so environment authority stays single-source and fail-closed.

## Bootstrap

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/launchplane"
cp config/dokploy-targets.toml.example config/dokploy-targets.toml
cp config/runtime-environments.toml.example \
  "${XDG_CONFIG_HOME:-$HOME/.config}/launchplane/runtime-environments.toml"
cp .env.example "${XDG_CONFIG_HOME:-$HOME/.config}/launchplane/dokploy.env"
export LAUNCHPLANE_MASTER_ENCRYPTION_KEY="replace-me"
uv run launchplane secrets import-bootstrap --database-url "$LAUNCHPLANE_DATABASE_URL"
uv run launchplane secrets list --database-url "$LAUNCHPLANE_DATABASE_URL" --integration dokploy
```

After bootstrap import succeeds, verify Launchplane can resolve the managed secret
status you expect before removing older operator-local bootstrap files such as
`~/.config/launchplane/dokploy.env`.
