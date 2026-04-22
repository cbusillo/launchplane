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
- Keep bootstrap values only in process env long enough to write the real
  Launchplane-managed secret records.
- Runtime environment truth should live in Launchplane DB records in steady
  state.
- Live Dokploy `target_id` values belong in Launchplane DB-backed target-id
  records.
- Optional ship-mode overrides such as `DOKPLOY_SHIP_MODE` now belong in
  runtime-environment records instead of the service host env surface.
- Launchplane preview routing now uses a dedicated `LAUNCHPLANE_PREVIEW_BASE_URL`
  runtime-environment value instead of piggybacking on ordinary live-instance
  web base URLs.
- VeriReel prod rollback worker dispatch now resolves
  `LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND`,
  `VERIREEL_PROD_PROXMOX_HOST`, `VERIREEL_PROD_PROXMOX_USER`, and
  `VERIREEL_PROD_CT_ID` from the `verireel/prod` runtime-environment contract,
  with managed-secret overlays still available for secret-looking keys.

## DB-Backed Secret Resolution

- Launchplane reads DB-backed managed secrets first when matching secret records
  exist for:
  - Dokploy `DOKPLOY_HOST`
  - Dokploy `DOKPLOY_TOKEN`
  - runtime-environment keys that look like secrets, such as `*_PASSWORD`,
    `*_TOKEN`, `*_SECRET`, and `*_KEY`
- Runtime environment records do not fall back to repo or XDG files.
- Dokploy credentials do not fall back to repo files, XDG files, or process
  env. Missing managed bindings are a hard error.
- Secret status surfaces return metadata only. Launchplane does not expose routine
  plaintext read commands or service endpoints.

## Bootstrap-Only Env

- Treat these as bootstrap/process concerns, not product runtime truth:
  - `LAUNCHPLANE_DATABASE_URL`
  - `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`
  - policy/bootstrap selectors such as `LAUNCHPLANE_POLICY_*`
- Treat these as DB-backed Launchplane-owned data instead of live service-host
  env once the shared store is available:
  - `DOKPLOY_HOST`
  - `DOKPLOY_TOKEN`
  - `DOKPLOY_SHIP_MODE`
  - per-context/runtime values such as `LAUNCHPLANE_PREVIEW_BASE_URL`,
    `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`, and tenant/product env keys
  - rollback worker values such as
    `LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND`,
    `VERIREEL_PROD_PROXMOX_HOST`, `VERIREEL_PROD_PROXMOX_USER`, and
    `VERIREEL_PROD_CT_ID`

## Rules

- Do not keep real secret files in the repo checkout.
- Never commit alternate secret files or rendered env artifacts.
- Do not rely on a repo-local `.env` for control-plane-owned secrets.
- Missing Dokploy credentials are a hard error, not a silent fallback.
- Missing `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` is a hard error when Launchplane needs to
  read or write DB-backed managed secrets.
- The live Launchplane Dokploy target should expose bootstrap env such as
  `LAUNCHPLANE_DATABASE_URL` and `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`, while
  Dokploy credentials and runtime/product values should resolve from
  Launchplane-managed records instead of target env.
- Use `uv run launchplane service inspect-dokploy-target ...` to verify that the
  live Launchplane target has the required secret-backed contract without printing
  plaintext secret values.

## Local Runtime Contract

- `uv run launchplane environments resolve --context <ctx> --instance
<instance> --json-output`
  emits the resolved runtime environment payload for a tenant environment.
- In steady state that payload comes from Launchplane DB-backed runtime
  environment records.
- Launchplane preview write/build helpers read `LAUNCHPLANE_PREVIEW_BASE_URL` from the
  shared plus context-scoped runtime environment contract, with shared values
  providing the default and context values allowed to override it.
- `odoo-devkit` may consume that contract when the operator points
  `ODOO_CONTROL_PLANE_ROOT` at a valid `launchplane` checkout.
- When `odoo-devkit` is configured to use the control-plane contract, legacy
  devkit-local `.env` / `platform/.env` / `platform/secrets.toml` files should
  be removed so environment authority stays single-source and fail-closed.

## Bootstrap

Bring up the service with bootstrap env such as `LAUNCHPLANE_DATABASE_URL` and
`LAUNCHPLANE_MASTER_ENCRYPTION_KEY`, then write the durable DB-backed secret
and runtime records through the normal Launchplane commands. Dokploy
credentials belong in Launchplane-managed secrets before Dokploy operations run.
