---
title: Secrets
---

## Purpose

- Define the control-plane-owned secret contract for deploy and operator
  workflows.

## Current Contract

- Dokploy credentials belong to `launchplane`.
- Launchplane can now persist managed secret values in the Postgres
  shared-service backend when `LAUNCHPLANE_DATABASE_URL` is configured.
- Managed secret values are encrypted before Launchplane stores them; the master
  key stays outside the database in `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`.
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
- Product backup, rollback, maintenance, and preview drivers should resolve
  runtime worker commands, host/user metadata, and non-secret operation settings
  from DB-backed runtime-environment records. Private keys, tokens, and
  known-host material must come from managed secret bindings.
- GitHub workflows should not carry provider credentials such as `DOKPLOY_HOST`,
  `DOKPLOY_TOKEN`, or project names; they should call Launchplane with OIDC and
  operation intent.

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
- Secret status surfaces return metadata only. Launchplane does not expose
  routine plaintext read commands or service endpoints.

## Runtime Key-Safety Gates

- Runtime key-safety gates classify managed secret bindings by binding key and
  Launchplane metadata, not by plaintext value. The initial classification
  contract is `prod_only`, `testing`, `preview`, `non_prod`, and `shared_safe`.
- Gates fail closed when a required binding is missing, disabled, ambiguous,
  unclassified, or scoped outside the target context/instance. A target with an
  unknown environment class also fails closed.
- `prod_only` bindings are allowed only for `prod` runtime targets. `testing`
  targets may use `testing`, `non_prod`, or `shared_safe` bindings. `preview`
  targets may use `preview`, `non_prod`, or `shared_safe` bindings.
- Gate output may include binding keys, binding ids, secret ids,
  classifications, and finding codes. It must not include secret plaintext,
  ciphertext, provider env dumps, or token prefixes.
- Runtime key-safety policy records live in
  `launchplane_runtime_key_safety_policies`. Operators import JSON policy
  records with `launchplane runtime-key-safety import-policy`, inspect active
  records with `launchplane runtime-key-safety list-policies`, and run a
  metadata-only check with `launchplane runtime-key-safety evaluate` before a
  workflow mutates runtime keys.
- Evaluation reads only Launchplane managed secret bindings for the requested
  context and instance. If no active policy record exists, the gate fails closed
  instead of falling back to service-host env or product-local scripts.
- Product-specific preview drivers that derive runtime secrets from a template,
  such as VeriReel's preview database bootstrap, must run the same metadata-only
  gate before creating databases, rendering preview env, or starting preview
  instances. Template secret-shaped keys and semantic secret keys such as
  `DATABASE_URL` must resolve to managed template-lane bindings, and the active
  policy must allow those bindings for the preview target.
- Delegated worker workflows that overlay managed runtime secrets into
  subprocess environments, such as VeriReel prod backup and rollback workers,
  must evaluate the managed bindings for the worker target before the worker
  process starts. The worker receives plaintext only after the metadata gate has
  confirmed the active policy allows those bindings for that runtime class.

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
  - product rollback, backup-gate, and maintenance worker values, with private
    key/token material stored as managed secrets

## Rules

- Do not keep real secret files in the repo checkout.
- Never commit alternate secret files or rendered env artifacts.
- Do not rely on a repo-local `.env` for control-plane-owned secrets.
- Missing Dokploy credentials are a hard error, not a silent fallback.
- Missing `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` is a hard error when Launchplane
  needs to read or write DB-backed managed secrets.
- The live Launchplane Dokploy target should expose bootstrap env such as
  `LAUNCHPLANE_DATABASE_URL` and `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`, while
  Dokploy credentials and runtime/product values should resolve from
  Launchplane-managed records instead of target env.
- Use `uv run launchplane service inspect-dokploy-target ...` to verify that
  the live Launchplane target has the required secret-backed contract without
  printing plaintext secret values.

## Local Runtime Contract

- `uv run launchplane environments resolve --context <ctx> --instance
<instance> --json-output`
  emits the resolved runtime environment payload for a tenant environment.
- `uv run launchplane environments put --scope <scope> --set KEY=VALUE` writes
  non-secret runtime values directly to DB-backed runtime-environment records
  and redacts values from command output. Secret-shaped keys are rejected and
  should be written with `secrets put`.
- `uv run launchplane product-config apply --input-file bundle.json --dry-run`
  previews an approved product runtime/secret bundle without printing plaintext
  values or writing records. `--apply` writes non-secret runtime keys and
  managed secret values through the same DB-backed stores. Run this command only
  from a trusted Launchplane context with current `LAUNCHPLANE_DATABASE_URL` and,
  when secrets are present, `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`. Dry-run and
  apply both reject invalid secret scopes or scope/context/instance mismatches
  before any managed secret write starts. Runtime-environment secret bundles
  also require an active runtime key-safety policy that allows each requested
  binding for the target runtime class.
- `uv run launchplane environments unset --scope <scope> --key KEY` removes
  stale keys from DB-backed runtime-environment records without reading or
  printing plaintext values.
- `uv run launchplane environments relabel --scope <scope> --source-label ...`
  updates stale source metadata without changing runtime values.
- In steady state that payload comes from Launchplane DB-backed runtime
  environment records.
- Launchplane preview write/build helpers read `LAUNCHPLANE_PREVIEW_BASE_URL`
  from the shared plus context-scoped runtime environment contract, with shared
  values providing the default and context values allowed to override it.
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
