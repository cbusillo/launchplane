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
  Secret versions are encrypted before Launchplane stores them.
- New deployments must use `LAUNCHPLANE_SECRET_KEYS_JSON` with explicit, canonical
  high-entropy Fernet roots to manage encryption keys. Generate roots with a
  cryptographically secure secret manager or `Fernet.generate_key()` and store
  the JSON value in the Launchplane service bootstrap secret, never in the repo.
  Existing deployments can temporarily keep `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`
  as a migration-only historical root.
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
- Runner-host hygiene cross-repository evidence uses a dedicated GitHub App.
  Store its non-secret client ID in the documented repository variable and its
  private key in `LAUNCHPLANE_RUNNER_HOST_HYGIENE_GITHUB_APP_PRIVATE_KEY`.
  The workflow passes the private key only to the commit-pinned official token
  action, requests read-only Actions and Administration permissions for the
  runtime-derived repository set, and uses the resulting installation token
  only in the executor step. The token is revoked at job completion and is not
  persisted in artifacts, audit records, logs, or Launchplane managed secrets.
  Do not retain a PAT fallback.
- Conventional product onboarding uses a dedicated read-only GitHub App to
  resolve immutable repository and owner ids before protected review. Store its
  client id in `LAUNCHPLANE_ONBOARDING_GITHUB_APP_CLIENT_ID` and its private key
  in `LAUNCHPLANE_ONBOARDING_GITHUB_APP_PRIVATE_KEY`. Install it only on product
  repositories that operators may onboard and grant only repository Contents
  read plus the GitHub App's mandatory metadata read access. Contents read is
  the minimum permission that lets an installation be scoped to selected
  private repositories; the workflow requests that exact permission when it
  mints each repository-scoped token.
  The workflow passes the key only to the commit-pinned official token action,
  uses the short-lived token only for repository metadata lookup, and does not
  persist the token or private key in plan/apply artifacts. Do not use a PAT or
  the Launchplane service GitHub App as a fallback.
- Advisory engineering and Owner check-run projection uses its own dedicated
  GitHub App. Store its numeric id as the DB-backed Launchplane service-context
  runtime value `LAUNCHPLANE_ADVISORY_GITHUB_APP_ID` and its private key as the
  managed-secret value `LAUNCHPLANE_ADVISORY_GITHUB_APP_PRIVATE_KEY`. Install
  it only on repositories that receive advisory governance checks and grant
  only Checks write plus mandatory Metadata read. Launchplane mints a
  repository-scoped installation token, verifies exact App, installation,
  repository, and permission identity, revokes the token after use, and never
  persists or logs the token.
  Do not reuse onboarding, runner-host-hygiene, PAT, or ordinary service tokens.
- The protected
  `LAUNCHPLANE_AUTHZ_GENERIC_WEB_ONBOARDING_MANAGED_SET_JSON` secret contains
  the permanent exact Launchplane workflow grants for onboarding and preview
  authorization maintenance. It is generic worker authority, not per-product
  policy: do not place product repositories, contexts, targets, domains, or
  generated preview caller rules in it. Product rules are planned from typed
  runtime input and persisted in the DB-backed `operator.generic-web-preview`
  managed set.
- The protected `LAUNCHPLANE_AUTHZ_OWNER_ACCEPTANCE_MANAGED_SET_JSON` secret
  contains the complete `operator.owner-acceptance` GitHub-human desired set.
  Bind every rule to immutable numeric GitHub user IDs and grant only the
  `read_only` role, `launchplane` product, and `owner-acceptance` context.
  Engineering viewer rules may contain only `owner_acceptance.read`; Owner
  candidate rules may also contain `owner_acceptance_event.write`. Keep product
  Owner membership in the independently managed Owner policy; this secret grants
  workbench access but cannot satisfy Owner authority.
- The protected
  `LAUNCHPLANE_AUTHZ_PRODUCT_OWNER_POLICY_ADMIN_MANAGED_SET_JSON` secret contains
  the complete `operator.product-owner-policy-admin` local-operator desired set.
  Every rule must bind one exact operator subject and token label to one exact
  product/system scope and exactly the Product Owner policy and requirement
  read/write actions. Product identities, repository identities, and Owner
  memberships remain DB-backed runtime records and do not belong in this secret.

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

## Managed Secret Model

Managed secrets are the durable value boundary for secret-shaped runtime and
provider inputs. A secret record names the stable Launchplane secret identity;
secret versions hold encrypted value payloads and rotation metadata; bindings map
runtime-facing keys to the current allowed secret version for a product context
or Launchplane-owned integration.

`secret_id`, `version_id`, and `encryption_key_id` are identifiers, not secret
material. They must be stable, opaque, unique within their record family, and
safe to show in redacted audit or operator status surfaces. They must not encode
real plaintext values, provider tokens, operator identities, tenant values,
domains, or topology. `current_version_id` points to the active secret-value
version; it is not the encryption-key id and must not be overloaded as rotation
state for the master encryption root.

New writes create a new version and move the current-version pointer only after
the encrypted payload, metadata, binding checks, and audit record are durable.
Old versions remain evidence until an explicit retirement or retention policy
marks them unusable. Missing, disabled, ambiguous, or unlabeled versions fail
closed rather than falling back to process env, local files, previous ciphertext,
or provider-side env dumps.

## Encryption Key IDs And Rotation

Every encrypted managed-secret version should record the non-secret
`encryption_key_id` that identifies which Launchplane decryption root encrypted
that version. The active key id is used for new writes. Allowed historical key
ids may decrypt old versions only during an explicit rotation or recovery window.

The target rotation model is:

1. Introduce a new bootstrap decryption root or platform-secret reference and an
   active `encryption_key_id` in `LAUNCHPLANE_SECRET_KEYS_JSON`.
2. Keep the previous decryption root available only as an allowed historical key
   in the JSON keys map for versions that still carry its key id.
3. Run the deployed Launchplane service re-encryption endpoint in dry-run mode.
   The response reports unreadable versions, the active-key usage summary, keys
   blocked from retirement, and a digest bound to the current secret versions.
4. Apply through the same service endpoint with the dry-run digest, an operator
   reason, and an idempotency key. Launchplane atomically writes every new
   ciphertext version, current-version pointer, audit event, and apply
   idempotency completion record.
5. Run dry-run again and verify the previous key id is reported as ready for
   retirement.
6. Retire the previous root by removing it from the service bootstrap key ring.
   Later reads fail closed if any active secret still depends on that id.

`LAUNCHPLANE_SECRET_KEYS_JSON` has this bootstrap-only shape:

```json
{
  "active_key_id": "root-2026-07",
  "keys": {
    "root-2026-07": "<canonical-url-safe-base64-fernet-key>",
    "root-2026-04": "<historical-canonical-url-safe-base64-fernet-key>"
  }
}
```

Key ids use 1-64 ASCII letters, digits, dots, underscores, or hyphens. Each key
must be the exact URL-safe base64 encoding of 32 high-entropy bytes. Launchplane
rejects passphrases, whitespace-normalized values, low-diversity test material,
unknown JSON fields, missing active keys, and mismatches between the JSON legacy
entry and the legacy bootstrap variable.

### Migrating The Legacy Root

Existing secret-version payloads without an explicit historical label resolve
to the compatibility id `launchplane-master-key`. To migrate without deriving or
printing the old root:

1. Keep the existing `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` set on the deployed
   Launchplane service.
2. Add `LAUNCHPLANE_SECRET_KEYS_JSON` with a new canonical active key. Do not
   copy a legacy passphrase into the JSON map. Launchplane loads the legacy env
   value as the historical `launchplane-master-key` only for this migration
   window.
3. Run dry-run and stop if any version is unreadable or the reported plan does
   not include the expected legacy-key usage.
4. Apply with the matching digest and verify the next dry-run reports
   `launchplane-master-key` as ready for retirement.
5. Remove `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` and restart the service. A final
   dry-run must remain clean before the old bootstrap secret is destroyed.

Old ciphertext versions and audit metadata retain the old/new key ids and
version ids as rollback evidence. To roll back before destroying an old root,
restore that root as an allowed active key and run the same audited dry-run/apply
flow in reverse; Launchplane creates new versions instead of mutating history.

Rotation is a service/storage operation, not a product workflow shortcut. It
must not copy plaintext into GitHub issues, workflow logs, checked-in files,
operator-local env files, provider env dumps, or docs. Ambiguous key ids,
missing key ids, missing decryption roots, or mismatched active/historical key
state block the read or write instead of silently trying another source.

## Secret Provider Boundary

The accepted provider is Launchplane-managed secrets backed by Launchplane
storage and a minimal bootstrap decryption root. Future Vault, HSM, KMS, or
cloud-secret-manager integrations are deferred provider candidates. They require
a named Launchplane problem, local/dev bootstrap plan, operational owner,
failure mode, rollback posture, and proof that live secret values and assignments
remain out of checked-in files.

Provider adapters expose generic operations only: write encrypted version,
resolve metadata, resolve plaintext for an authorized in-process use,
re-encrypt/rotate, disable/retire, and append audit evidence. Drivers, workers,
and product-specific code request resolved secret bundles from Launchplane; they
must not query secret tables, inspect ciphertext, choose encryption keys, or
carry provider credentials as their own authority. Provider adapters do not own
product, lane, topology, authz, or runtime configuration authority.

## Plaintext Exposure And Audit

Plaintext exists only at the last responsible moment for an authorized
service-side use, such as rendering a provider request body, preparing a worker
environment, or applying a runtime payload after authorization and runtime
key-safety checks pass. Routine service, CLI, workflow, UI, and agent responses
return metadata only.

The product/environment managed-secret form keeps plaintext only in uncontrolled
password inputs and the immediate request local variable. It clears every value
before dispatch and again on secret-input validation failure, HTTP failure,
route change, and unmount. A successful dry-run retains only redacted plan
evidence, the operation fingerprint/idempotency identity, and trace metadata;
the operator must re-enter the same values for apply. Persisted product-config
continuity and idempotency fingerprints that cover secret input use a
server-keyed, purpose-separated HMAC derived from the active managed-secret
root, never an unkeyed secret verifier. Secret values must not enter React state,
URLs, browser storage, operation receipts, rendered errors, console or telemetry
events, fixtures, or live-target next-action evidence.

Product-config dry-run does not decrypt an existing secret to compare equality.
Submitting a binding that already exists plans and applies a new encrypted
version as an explicit rotation. This keeps dry-run free of plaintext resolution
and avoids retaining or auditing a value comparison solely to report
`unchanged`.

Any plaintext resolution or reveal attempt must append redacted audit evidence.
Audit payloads may include actor or subject type, reason, trace id, operation or
intent id, binding id, secret id, version id, encryption key id, destination
class, and finding codes. They must not include plaintext, ciphertext, token
prefixes, provider env dumps, request bodies that contain secrets, or values
derived from secret material.

Trusted operator reveal paths, if added later, must be deliberate, reasoned,
scoped, audited, and separate from routine metadata reads. Missing authorization,
missing runtime key-safety approval, missing secret version metadata, or missing
decryption key state denies the reveal or resolution.

## Runtime Key-Safety Gates

- Runtime key-safety gates classify managed secret bindings by binding key and
  Launchplane metadata, not by plaintext value. The initial classification
  contract is `prod_only`, `testing`, `preview`, `non_prod`, and `shared_safe`.
- Deploy-time runtime key-safety reconciliation accepts operator-supplied
  `LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON` metadata for runtime secret
  bindings that need Launchplane-managed storage. It writes binding key,
  `secret_class`, and target-scope metadata only; operators still supply or
  rotate secret values through product-config managed secret writes.
- Shared and production runtime mutations must execute through the deployed
  Launchplane service API or an operator UI path backed by that API. Do not use
  local CLI live-target mutation commands from arbitrary checkouts as a fallback
  when the service API is missing; add the service boundary first so the
  deployed runtime resolves DB-backed target authority and records sanitized
  audit evidence.
- Live target runtime sync uses `POST /v1/live-target-runtime/apply` or the
  `live-target-runtime.yml` workflow wrapper. Dry-run and apply both evaluate
  runtime key-safety policy before returning sanitized key/count evidence.
- Live target runtime sync through the service API filters the resolved runtime
  payload to the product profile's expected runtime-environment keys and
  runtime managed-secret binding keys for the selected lane. Shared/global
  runtime records can provide values, but they are not synced to an unrelated
  product target unless that product profile declares the key.
- Odoo stable lanes declare their compose runtime contract in product onboarding
  seed material: `ODOO_DB_NAME`, `ODOO_DB_USER`, `ODOO_DATA_VOLUME`,
  `ODOO_LOG_VOLUME`, `ODOO_DB_VOLUME`, and managed secret bindings for
  `ODOO_ADMIN_PASSWORD`, `ODOO_DB_PASSWORD`, and `ODOO_MASTER_PASSWORD`. CM prod
  uses DB `cm` with `cm_prod_odoo_*` volumes; OPW prod uses DB `opw_prod` with
  `opw_prod_odoo_*` volumes.
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
- The Launchplane deploy workflow may reconcile known runtime binding
  classifications through `POST /v1/runtime-key-safety/policies/apply`. That
  service path is OIDC-authenticated, DB-backed, additive by binding key, and
  carries only binding metadata such as class, context, and instance scope. It
  must not carry secret plaintext or provider env dumps.
- Evaluation reads only Launchplane managed secret bindings for the requested
  context and instance. If no active policy record exists, the gate fails closed
  instead of falling back to service-host env or product-local scripts.
- Policy rules can allow dynamic preview instances with paired `allowed_targets`
  entries that combine an exact preview context with `instance_patterns`, for
  example `pr-*`. Use paired patterns for reusable preview lanes instead of
  adding one-off PR instance names to policy records or broadening stable scope.
- Product-specific preview drivers that derive runtime secrets from a template,
  such as VeriReel's preview database bootstrap, must run the same metadata-only
  gate before creating databases, rendering preview env, or starting preview
  instances. Template secret-shaped keys copied into the preview must resolve to
  managed template-lane bindings, and the active policy must allow those
  bindings for the preview target. Template values that the driver rewrites for
  each preview, such as VeriReel's generated `DATABASE_URL`,
  `BETTER_AUTH_SECRET`, `VERIREEL_SECRETS_MASTER_KEY`,
  `VERIREEL_CRON_SECRET`, and `VERIREEL_SMOKE_MAINTENANCE_SECRET`, are not
  copied template secrets.
- Delegated worker workflows that overlay managed runtime secrets into
  subprocess environments, such as VeriReel prod backup and rollback workers,
  must evaluate the managed bindings for the worker target before the worker
  process starts. The worker receives plaintext only after the metadata gate has
  confirmed the active policy allows those bindings for that runtime class.
- Worker gates must evaluate every effective managed binding scope accepted by
  runtime secret resolution: global, context, and context-instance. A narrower
  binding query must not let an inherited managed secret bypass classification.
- A denied worker gate may report non-secret binding keys plus the active policy
  record id and policy digest so operators can repair the classification without
  exposing secret values. Validate the repaired metadata with a read-only target
  dry run or scoped write-intent preflight before retrying the side effect.
- Product-specific workflows that sync resolved runtime environment values into
  live Dokploy targets, such as Odoo prod rollback target env updates, must
  evaluate managed runtime secret bindings before writing the live env payload.
- Product-specific artifact/build workflows that pass resolved runtime
  environment payloads to delegated tooling, such as Odoo artifact publish,
  must evaluate managed runtime secret bindings before starting that tooling.

## Bootstrap-Only Env

- Treat these as bootstrap/process concerns, not product runtime truth:
  - `LAUNCHPLANE_DATABASE_URL`
  - `LAUNCHPLANE_SECRET_KEYS_JSON`
  - `LAUNCHPLANE_MASTER_ENCRYPTION_KEY` (legacy fallback)
  - policy/bootstrap selectors such as `LAUNCHPLANE_POLICY_*`
  - service-ingress bearer secrets such as
    `LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN` and
    `LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN`
  - server-owned engineering-review worker identity settings
    `LAUNCHPLANE_ENGINEERING_REVIEW_WORKER_RUNTIME_ID` and
    `LAUNCHPLANE_ENGINEERING_REVIEW_WORKER_HOST`; request bodies cannot
    override them
  - route-specific webhook ingress secrets such as
    `LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET` and
    `LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET`
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
- Missing `LAUNCHPLANE_SECRET_KEYS_JSON` (and its legacy fallback `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`)
  is a hard error when Launchplane needs to read or write DB-backed managed secrets.
- The live Launchplane Dokploy target should expose bootstrap env such as
  `LAUNCHPLANE_DATABASE_URL` and `LAUNCHPLANE_SECRET_KEYS_JSON`, while
  Dokploy credentials and runtime/product values should resolve from
  Launchplane-managed records instead of target env.
- Use `uv run launchplane service inspect-dokploy-target ...` to verify that
  the live Launchplane target has the required secret-backed contract without
  printing plaintext secret values.

## Local Runtime Contract

- `uv run launchplane environments resolve --context <ctx> --instance
<instance> --json-output`
  emits the resolved runtime environment payload for a tenant environment with
  secret-shaped values redacted by default. Use `--include-secret-values` only
  from a trusted operator shell when plaintext resolved values are required.
- `uv run launchplane environments put --scope <scope> --set KEY=VALUE --allow-direct-db-mutation`
  is an explicit local/bootstrap repair path for non-secret runtime values in
  DB-backed runtime-environment records and redacts values from command output.
  Secret-shaped keys are rejected and should be written with `secrets put`.
  Routine shared and production config changes should use product-config
  dry-run/apply through the deployed service route or operator UI instead of
  arbitrary local runtime-environment writes.
- `uv run launchplane secrets put ... --allow-direct-db-mutation` is the
  matching explicit local/bootstrap repair path for direct managed-secret
  writes. Routine shared and production secret changes should use product-config
  dry-run/apply through the deployed service route or operator UI instead of
  arbitrary local secret writes.
- `uv run launchplane secrets reencrypt --allow-direct-db-mutation` is a
  bootstrap/recovery-only dry-run. A direct apply additionally requires
  `--expected-plan-digest`, `--reason`, and `--apply`. Routine shared and
  production root rotation must use the deployed service endpoint rather than
  an arbitrary checkout.
- `uv run launchplane product-config apply --input-file bundle.json --dry-run`
  previews an approved product runtime/secret bundle without printing plaintext
  values or writing records. `--apply` writes non-secret runtime keys and
  managed secret values through the same DB-backed authority bundle. Runtime
  records, encrypted secret versions, current secret pointers, bindings, audit
  events, and applicable idempotency evidence commit together or roll back
  together. Run this command only
  from a trusted Launchplane context with current `LAUNCHPLANE_DATABASE_URL` and,
  when secrets are present, `LAUNCHPLANE_SECRET_KEYS_JSON` (or the legacy
  `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`). Dry-run and
  apply both reject invalid secret scopes or scope/context/instance mismatches
  before any managed secret write starts. Runtime-environment secret bundles
  also require an active runtime key-safety policy that allows each requested
  binding for the target runtime class.
- Trusted local agents that need to call the deployed service instead of a
  browser session should source `~/.config/launchplane/local-operator.env` and
  use `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN` for routine owner-agent writes. Exact
  authority is DB-backed by `local_operators` authz policy rules. Rare privileged
  owner-agent writes can use `LAUNCHPLANE_LOCAL_ADMIN_TOKEN` only when matching
  `local_admins` authz policy rules grant the action. Write requests sent with
  either token must include a reason. Product-config apply also requires a
  previously recorded matching dry-run. They must still send plaintext secret
  values only in the request body over the Launchplane service API. Do not copy
  those request bodies into logs, GitHub issues, PR bodies, or docs.
- Product onboarding may create disabled managed-secret binding placeholders for
  expected runtime secrets. Once product-config writes the configured managed
  secret for the same integration, binding key, context, and instance, Launchplane
  retires the disabled placeholder from active runtime-secret lookups. Later
  onboarding imports preserve the configured binding instead of recreating the
  disabled placeholder.
- `uv run launchplane environments unset --scope <scope> --key KEY --allow-direct-db-mutation`
  removes stale keys from DB-backed runtime-environment records without reading
  or printing plaintext values. Use it only for explicit local/bootstrap repair.
- `uv run launchplane environments relabel --scope <scope> --source-label ... --allow-direct-db-mutation`
  updates stale source metadata without changing runtime values. Use it only for
  explicit local/bootstrap repair.
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
`LAUNCHPLANE_SECRET_KEYS_JSON`, then write the durable DB-backed secret
and runtime records through the normal Launchplane commands. Dokploy
credentials belong in Launchplane-managed secrets before Dokploy operations run.
