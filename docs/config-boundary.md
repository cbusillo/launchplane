---
title: Config Boundary
---

## Purpose

- Make Launchplane's intended configuration authority explicit.
- Separate bootstrap/root-of-trust inputs from live mutable control-plane config.
- Keep the DB-backed config boundary explicit now that implicit file/env
  fallback readers have been removed.

## Final Boundary

Launchplane's long-term config model is:

- bootstrap/root-of-trust stays outside the database
- all other live mutable config is DB-backed
- checked-in repo files are examples, docs, schemas, tests, or explicit seed
  material only
- local files under `~/.config/launchplane/` are archive/import material only
- the service never silently falls back across multiple live authorities

In steady state, if a DB-backed config class is missing from Launchplane's
shared store, Launchplane should fail closed.

## Source-Of-Truth Matrix

### Bootstrap Env Only

These values remain outside the database because Launchplane needs them before
it can reach, trust, or decrypt DB-backed state.

| Class | Current surface | Final authority | Notes |
| --- | --- | --- | --- |
| Database connectivity | `LAUNCHPLANE_DATABASE_URL` | Bootstrap env | Required before Launchplane can read DB-backed config. |
| Secret decryption root | `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` | Bootstrap env | Must stay outside the DB it decrypts. |
| Authz bootstrap | `LAUNCHPLANE_POLICY_TOML`, `LAUNCHPLANE_POLICY_B64`, `LAUNCHPLANE_POLICY_FILE` | Bootstrap env/file | Current root of trust. May evolve later, but still needs a non-DB bootstrap path. |
| Launchplane self image ref | `DOCKER_IMAGE_REFERENCE` | Service target env | Needed for Launchplane self-deploy and rollback posture. |
| Process wiring | `LAUNCHPLANE_SERVICE_HOST`, `LAUNCHPLANE_SERVICE_PORT`, `LAUNCHPLANE_SERVICE_AUDIENCE`, `LAUNCHPLANE_STATE_DIR`, `LAUNCHPLANE_APP_ROOT` | Service target env | Runtime/process wiring, not product config. |

### DB Authoritative

These values are live mutable control-plane config and should resolve from
Launchplane records/secrets instead of repo files or operator-local env.

| Class | Current surface(s) | Final authority | Notes |
| --- | --- | --- | --- |
| Dokploy credentials | Launchplane managed secrets (`DOKPLOY_HOST`, `DOKPLOY_TOKEN`) | Launchplane managed secrets | Fail closed when the shared store does not have both bindings. |
| Runtime environment values | Runtime-environment records, with file input allowed only through explicit override/import flows | Launchplane runtime-environment records | Includes shared, context, and instance-scoped values. |
| Secret-shaped runtime keys | Managed runtime secrets overlay | Launchplane managed secrets | Includes `*_PASSWORD`, `*_TOKEN`, `*_SECRET`, `*_KEY`. |
| Ship mode overrides | `DOKPLOY_SHIP_MODE`, `DOKPLOY_SHIP_MODE_<CTX>_<INSTANCE>` | Launchplane runtime-environment records | Mutable operator behavior, not bootstrap. |
| Preview routing/config | `LAUNCHPLANE_PREVIEW_BASE_URL` | Launchplane runtime-environment records | Shared control-plane-owned runtime value. |
| GitHub workflow runtime integration values | `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` | Launchplane runtime-environment records and managed secrets | Current docs already classify these as DB-backed target state. |
| Product/tenant runtime env | `ENV_OVERRIDE_*`, Odoo runtime values, tenant-specific env keys | Launchplane runtime-environment records and managed secrets | Includes shared and per-instance overlays. |
| Worker/runtime-action config | `LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND`, `VERIREEL_PROD_PROXMOX_HOST`, `VERIREEL_PROD_PROXMOX_USER`, `VERIREEL_PROD_CT_ID`, `VERIREEL_PROD_GATE_LOCAL` | Launchplane runtime-environment records and managed secrets | Current rollback worker still reads env; target is DB-backed runtime authority. |
| Dokploy target-id overrides | DB records, with file input allowed only through explicit override/import flows | Launchplane target-id records | File catalogs are no longer implicit steady-state authority. |
| Stable target definitions | Launchplane DB-backed target records | Launchplane DB-backed target records | Repo catalogs should become example/seed material only. |
| Release tuple baseline authority | Launchplane release-tuple records | Launchplane record store | Repo catalogs should be treated as export/seed material, not live mutable authority. |

### Repo Only

These stay in git, but not as live mutable runtime authority.

| Class | Examples |
| --- | --- |
| Examples/templates | `config/runtime-environments.toml.example`, `config/dokploy-targets.toml.example`, `config/launchplane-authz.toml.example` |
| Docs/specs | `docs/*`, `README.md` |
| Schemas/migrations/tests | storage schema code, tests, fixtures, explicit export/import formats |

### Archive Only

These should remain available only as operator reference or explicit import
input, never as implicit service runtime fallback.

| Class | Final location | Notes |
| --- | --- | --- |
| Legacy operator env file | `~/.config/launchplane/archive/<date>/dokploy.env` | Reference/import only. |
| Legacy runtime environments file | `~/.config/launchplane/archive/<date>/runtime-environments.toml` | Reference/import only. |
| Legacy local policy copies after replacement | `~/.config/launchplane/archive/<date>/...` | Keep only if needed for historical reference. |

## Removed Runtime Fallbacks

The DB-backed cutover removed these surfaces as implicit runtime readers for
DB-backed config classes.

- repo-local `.env`
- repo-local `config/runtime-environments.toml`
- repo-local `config/dokploy-targets.toml` as live target authority
- `~/.config/launchplane/dokploy.env`
- `~/.config/launchplane/runtime-environments.toml`

These may still be useful as reference material, but only through explicit
import, audit, or archive flows.

## Current Code Reality

The codebase now points more directly at the final model, but it still mixes
live authority across DB, files, and process env:

- runtime environments fail closed unless DB-backed records exist, then overlay
  managed secrets
- Dokploy target-id overrides resolve from DB in steady state
- Dokploy credentials resolve from Launchplane-managed secrets only and fail
  closed when the shared store is missing either binding
- stable target definitions resolve from DB-backed tracked target records
- release tuple baseline resolution fails closed unless DB-backed release-tuple
  records exist

The remaining transition surface is legacy-path visibility and explicit export
material, not runtime fallback authority.

## Cutover Rules

- If a config class is listed as DB authoritative here, Launchplane should read
  it from DB only in steady state.
- Files are allowed only when the operator explicitly runs an import, export,
  audit, or archive flow.
- Launchplane should fail closed when DB-backed config is missing.
- Repo-owned config files should describe or seed config, not silently act as
  the live source of truth for production behavior.

## Inspection

Use the local inspection command to see which parts of the current config
contract are still DB-backed, file-backed, or mixed:

```bash
uv run launchplane service inspect-config-boundary --control-plane-root .
```

That payload is intended to make DB-backed authority and stale legacy files
visible without treating those files as runtime inputs.
