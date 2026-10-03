---
title: Secrets
---

## Purpose

- Define the control-plane-owned secret contract for deploy and admin
  workflows.

## Client credential input

An admin can request a credential by setting `owner_input` on one managed-secret
requirement in the stored product profile. It contains a human-readable `label` and
`instructions` and requires an explicit product context. An optional instance
restricts it to one environment; a context request shares one submission across
the profile's named environments in that context, which the form lists. Omitted input
declarations grant no submission surface. Real account names and provider details
belong in these records, not application defaults.

The named Client signs in with GitHub at
`/ui/owner-secrets?product=PRODUCT&environment=ENVIRONMENT`. The page reads
`GET /v1/owner-secret-inputs` and submits one write-only value to
`POST /v1/owner-secret-inputs/submit`. The service checks the immutable Client id,
the current request revision (including its environment set), CSRF, database storage and encryption availability.
The password input is cleared before dispatch and on unmount; responses contain
only request metadata and a receipt. Admins with product-profile read access
can inspect receipts but cannot submit as the Client.

Submissions reuse managed-secret encryption and atomic authority bundles, under
the `owner_secret_submission` integration with **no secret bindings**. The profile
that authorized the submission is checked under the product lock during commit.
A changed Client or request cannot produce a stale authorized write. No runtime
environment, active runtime secret, provider target, or deployment changes.

An admin separately selects the saved submission in the environment's Managed
secrets form. The existing product-config dry-run/apply route accepts
`owner_submission_version_id` instead of a plaintext `value`. It resolves only the
current version for the exact product, environment, declared binding and Client,
after admin authorization. Normal matching dry-run, confirmation, idempotency
and runtime key-safety checks still apply. Application copies the value to a
separate runtime secret; a later Client submission cannot rotate that active value.
Changing the profile during application aborts the atomic write. Live-target sync
and actual application verification remain separate admin work.

An exact retry of a completed application replays its stored receipt before
resolving the submitted credential. Client replacement or encryption-key retirement
cannot strand a credential-reference-only request after a lost apply response.

This is credential input, not a Client operational role or a release decision.
Product-profile write authority still controls which inputs are requested.

The receipt follows the submission audit event through recorded key re-encryption
events. Re-encryption changes the encrypted version without changing who supplied
the value or the displayed receipt time. Superseded requests remain encrypted
history and are not runtime-bound; automatic removal of retained secret history is
not part of this input flow.

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
  resolve immutable repository and repository-owner ids before protected review. Store its
  client id in `LAUNCHPLANE_ONBOARDING_GITHUB_APP_CLIENT_ID` and its private key
  in `LAUNCHPLANE_ONBOARDING_GITHUB_APP_PRIVATE_KEY`. Install it only on product
  repositories that admins may onboard and grant only repository Contents
  read plus the GitHub App's mandatory metadata read access. Contents read is
  the minimum permission that lets an installation be scoped to selected
  private repositories; the workflow requests that exact permission when it
  mints each repository-scoped token.
  The workflow passes the key only to the commit-pinned official token action,
  uses the short-lived token only for repository metadata lookup, and does not
  persist the token or private key in plan/apply artifacts. Do not use a PAT or
  the Launchplane service GitHub App as a fallback. Do not wait for a failed
  authorization run to discover a missing installation.
  Before dispatch, admins should verify this selected-repository installation
  instead of waiting for repository metadata token minting to fail.
- Advisory engineering and Client check-run projection uses its own dedicated
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
- `LAUNCHPLANE_AUTHZ_OWNER_ACCEPTANCE_MANAGED_SET_JSON` names a retired
  compatibility set. `operator.owner-acceptance` accepts only an empty desired
  policy for removal of existing grants. Current Client review uses the Client
  on the product profile and has no Client-grant secret prerequisite. This code
  retirement does not change deployed secrets or authorization records. See
  [`owner-acceptance.md`](owner-acceptance.md).

- The protected
  `LAUNCHPLANE_AUTHZ_PRODUCT_OWNER_POLICY_ADMIN_MANAGED_SET_JSON` secret contains
  the complete `operator.product-owner-policy-admin` `local_operators` desired set.
  Every rule must bind one exact admin subject and token label to one exact
  product/system scope and exactly the product Client policy and requirement
  read/write actions. Product identities, repository identities, and Client
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
- Credentials that only Launchplane's own jobs use for one lane, such as the
  production backup and VeriReel Proxmox SSH keys, live in the
  `launchplane_worker` integration, stored for exactly that lane. They are not
  part of the lane's runtime environment, so no deploy or sync can deliver them
  to an app. Store new worker credentials there with `context_instance` scope.
- Launchplane's own service credentials, its `GITHUB_TOKEN` for PR comments
  and release review and the advisory GitHub App private key, live in the
  `launchplane_service` integration at global or context scope. A context copy
  wins over the global one. No app environment resolves this integration.
- The GitHub App webhook secret that verifies `POST /v1/github/app-webhook`
  deliveries lives in the `github_app_webhook` integration, context
  `launchplane`, binding key `webhook_secret`: exactly one configured,
  context-scoped, write-only binding. It is resolved per request and used for
  nothing else. When it is missing the route returns `503` and records nothing.

## Managed Secret Model

Managed secrets are the durable value boundary for secret-shaped runtime and
provider inputs. A secret record names the stable Launchplane secret identity;
secret versions hold encrypted value payloads and rotation metadata; bindings map
runtime-facing keys to the current allowed secret version for a product context
or Launchplane-owned integration.

`secret_id`, `version_id`, and `encryption_key_id` are identifiers, not secret
material. They must be stable, opaque, unique within their record family, and
safe to show in redacted audit or admin status surfaces. They must not encode
real plaintext values, provider tokens, admin identities, tenant values,
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
4. Apply through the same service endpoint with the dry-run digest, an admin
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

Launchplane self-deploy must carry `LAUNCHPLANE_SECRET_KEYS_JSON` through its
reviewed bootstrap-secret path and validate the resulting target environment
before provider mutation. It may remove the migration-only legacy root only
when the remaining canonical configuration is valid; malformed, mismatched, or
rootless target state fails closed before deployment.

The `Deploy Launchplane` workflow accepts the canonical key ring only from the
repository secret `LAUNCHPLANE_SECRET_KEYS_JSON`. Manual dispatch chooses an
explicit `bootstrap_secret_operation` of `preserve`, `install`, or `remove`.
Automatic deployments always preserve the target value; a missing repository
secret never removes the active key ring. `install` requires non-empty JSON,
minifies it before the private self-deploy request is written, and may use an
admin-reviewed `self_deploy_idempotency_key`. `remove` is an explicit
rollback or retirement action and never follows merely from secret absence.
Both operations use exact target-state preconditions. A post-mutation workflow
failure automatically attempts the inverse key-ring change against the same
immutable image, guarded by the reviewed value and a unique forward deployment
marker; a distinct rollback marker proves restart completion. Ambiguous or
stale state fails closed for manual admin follow-up instead of applying a
blind inverse. The workflow removes private self-deploy payload files at job
completion and must not copy the key-ring value into outputs, summaries,
artifacts, or logs.

Old ciphertext versions and audit metadata retain the old/new key ids and
version ids as rollback evidence. To roll back before destroying an old root,
restore that root as an allowed active key and run the same audited dry-run/apply
flow in reverse; Launchplane creates new versions instead of mutating history.

Rotation is a service/storage operation, not a product workflow shortcut. It
must not copy plaintext into GitHub issues, workflow logs, checked-in files,
admin-local env files, provider env dumps, or docs. Ambiguous key ids,
missing key ids, missing decryption roots, or mismatched active/historical key
state block the read or write instead of silently trying another source.

## Secret Provider Boundary

The accepted provider is Launchplane-managed secrets backed by Launchplane
storage and a minimal bootstrap decryption root. Future Vault, HSM, KMS, or
cloud-secret-manager integrations are deferred provider candidates. They require
a named Launchplane problem, local/dev bootstrap plan, the party that operates it,
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
the admin must re-enter the same values for apply. Persisted product-config
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

Trusted admin reveal paths, if added later, must be deliberate, reasoned,
scoped, audited, and separate from routine metadata reads. Missing authorization,
missing runtime key-safety approval, missing secret version metadata, or missing
decryption key state denies the reveal or resolution.

## Runtime Key-Safety Gates

- Runtime key-safety gates classify managed secret bindings by binding key and
  Launchplane metadata, not by plaintext value. The initial classification
  contract is `prod_only`, `testing`, `preview`, `non_prod`, and `shared_safe`.
- Deploy-time runtime key-safety reconciliation accepts admin-supplied
  `LAUNCHPLANE_RUNTIME_KEY_SAFETY_RULES_JSON` metadata for runtime secret
  bindings that need Launchplane-managed storage. It writes binding key,
  `secret_class`, and target-scope metadata only; admins still supply or
  rotate secret values through product-config managed secret writes.
- Shared and production runtime mutations must execute through the deployed
  Launchplane service API or a Launchplane UI path backed by that API. Do not use
  local CLI live-target mutation commands from arbitrary checkouts as a fallback
  when the service API is missing; add the service boundary first so the
  deployed runtime resolves DB-backed target authority and records sanitized
  audit evidence.
- Live target runtime sync uses `POST /v1/live-target-runtime/apply` or the
  `live-target-runtime.yml` workflow wrapper. Dry-run and apply both return
  sanitized key/count evidence.
- Live target runtime sync and Odoo target replacement deliver the site's own
  environment for the lane:
  the site's context and lane settings, the tracked target's settings, secrets
  stored for exactly that lane, and, for testing and prod lanes only, secrets
  shared across the site. Global settings and secrets, other sites' values, and
  worker credentials (the `launchplane_worker` store) are never delivered.
  Previews and lanes with unrecognized names get only secrets stored for them.
  No declaration is needed to deliver a key; a managed secret the product
  profile does declare must be present, or the sync is refused.
- Odoo stable lanes declare their compose runtime contract in product onboarding
  seed material: `ODOO_DB_NAME`, `ODOO_DB_USER`, `ODOO_DATA_VOLUME`,
  `ODOO_LOG_VOLUME`, `ODOO_DB_VOLUME`, and managed secret bindings for
  `ODOO_ADMIN_PASSWORD`, `ODOO_DB_PASSWORD`, and `ODOO_MASTER_PASSWORD`. CM prod
  uses DB `cm` with `cm_prod_odoo_*` volumes; OPW prod uses DB `opw_prod` with
  `opw_prod_odoo_*` volumes.
- Gates fail closed when a required binding is missing, disabled, ambiguous,
  unclassified, or scoped outside the target context/instance. A target with an
  unknown environment class also fails closed.
- A binding stored for exactly the target's context and instance resolves for
  no other lane, so on a `prod`, `testing`, or `dev` target the lane is its
  classification and it needs no policy rule. An explicit rule for the key
  still applies when one exists. Preview targets never get this: previews copy
  template-lane values, and their check retargets the template's bindings to
  the preview, so a copied lane secret still needs an explicit rule. An Odoo
  preview receives a copied integration credential without such a rule as an
  empty value and its plan lists the key name instead of refusing to start.
- The writer of a secret stored for one exact lane can declare its class with
  `secret_class` on a product-config secret entry (scope `context_instance`
  only). Launchplane stores it on the binding as `declared_secret_class`, and
  key safety refuses it when the class is not allowed for the lane, for example
  `prod_only` on a `testing` lane. A declaration covers that binding only; a
  later write without `secret_class` clears it. A policy rule for the key still
  takes precedence.
- A production integration credential never takes its classification from a
  `testing` or `dev` lane, because the lane cannot tell a production key from a
  test key. A binding whose key names an integration needs a policy rule or a
  declared `secret_class` there, even when it is stored for exactly that lane.
  Code recognizes store, payment, outgoing-mail, printing and common
  business-system connector names (`DEFAULT_INTEGRATION_KEY_MARKERS` in
  `control_plane/runtime_key_safety.py`), matched on whole underscore-separated
  key parts. The active policy record's `integration_key_markers` add
  product-specific ones. Policy apply adds markers and never removes one. `prod`
  lanes keep the lane-exact shortcut.
- A production integration key may sit on a `testing` or `dev` lane only for a
  reason on an allowlist, recorded with evidence. Declaring such a key
  `shared_safe` on a non-production lane also needs a `sharing_reason` on the
  product-config secret entry: `kind` (`read_only_source`, `dev_store`,
  `pre_live` or `site_shared`, the lane integration allowance kinds plus
  site-shared keys), `reason`, and `evidence` saying who verified the key, when,
  and what they saw. `pre_live` is for `testing` and `dev` lanes only.
  Launchplane stores it on the binding with who recorded it and when, and a
  later write of the key needs it again. A product-config write that declares
  `shared_safe` on an integration key without one is refused
  (`sharing_reason_missing`). A key declared `testing` or `non_prod` is a
  non-production key and needs none.
- Nothing in Launchplane checks what a token can actually do. `read_only_source`
  is a person's statement that they verified the token's permissions in the
  provider and recorded it as evidence; Launchplane records it and shows it.
- The reason is metadata, never part of the value. The lane's integration
  allowances read (`GET /v1/product-config/integration-allowances`) lists the
  integration keys stored for that lane with their declared class and reason,
  and the product environment read shows both on each managed secret.
- Keys declared `shared_safe` before reasons existed keep working for one
  release: deploy-time checks, readiness and the read-back report them instead
  of refusing them. Record their reasons with a product-config write; the
  follow-up that ends the transition makes those paths refuse them too.
- Generic-web deploys, promotions and rollbacks read the lane's integration keys
  back after the deploy: the runtime key-safety rules run over every managed
  secret delivered to the lane whose key names an integration, and the result
  is stored on the deployment record as `integration_key_readback` (`pass`,
  `reported`, `fail`, `unavailable` or `skipped`, with key names and finding
  codes only). It records and does not refuse, because the deploy has already
  happened and a refused promotion would roll a live site back. Odoo lanes
  read their integration settings back from the database instead
  (`control_plane/integration_readback.py`).
- `prod_only` bindings are allowed only for `prod` runtime targets. `testing`
  targets may use `testing`, `non_prod`, or `shared_safe` bindings. `preview`
  targets may use `preview`, `non_prod`, or `shared_safe` bindings.
- Gate output may include binding keys, binding ids, secret ids,
  classifications, and finding codes. It must not include secret plaintext,
  ciphertext, provider env dumps, or token prefixes.
- Runtime key-safety policy records live in
  `launchplane_runtime_key_safety_policies`. Admins import JSON policy
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
- VeriReel previews copy the testing template's settings but never its secrets.
  The driver generates each preview's own `DATABASE_URL` (its own database
  role and password), `BETTER_AUTH_SECRET`, `VERIREEL_SECRETS_MASTER_KEY`,
  `VERIREEL_CRON_SECRET`, and `VERIREEL_SMOKE_MAINTENANCE_SECRET`, and drops
  every other template key that looks like a credential: a name containing
  `PASSWORD`, `PASSWD`, `TOKEN`, `SECRET`, `KEY`, or `CREDENTIAL`, or a URL value
  with an embedded password. `NEXT_PUBLIC_*` keys are browser-bundled and kept.
  Previews run unmerged code, so no key-safety policy is consulted.
- Delegated backup and rollback workers read their SSH credentials from the
  `launchplane_worker` store for exactly their lane. That store is never part of
  an app's environment, so no policy gate is needed before the worker starts.
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
  from a trusted admin shell when plaintext resolved values are required.
- `uv run launchplane environments put --scope <scope> --set KEY=VALUE --allow-direct-db-mutation`
  is an explicit local/bootstrap repair path for non-secret runtime values in
  DB-backed runtime-environment records and redacts values from command output.
  Secret-shaped keys are rejected and should be written with `secrets put`.
  Routine shared and production config changes should use product-config
  dry-run/apply through the deployed service route or Launchplane UI instead of
  arbitrary local runtime-environment writes.
- `uv run launchplane secrets put ... --allow-direct-db-mutation` is the
  matching explicit local/bootstrap repair path for direct managed-secret
  writes. Routine shared and production secret changes should use product-config
  dry-run/apply through the deployed service route or Launchplane UI instead of
  arbitrary local secret writes.
- `uv run launchplane secrets reencrypt --allow-direct-db-mutation` is a
  bootstrap/recovery-only dry-run. A direct apply additionally requires
  `--expected-plan-digest`, `--reason`, and `--apply`. Routine shared and
  production root rotation must use the governed privileged-operation UI and
  supervised worker rather than an arbitrary checkout or legacy service route.
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
  use `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN` for routine Director-agent writes. Exact
  authority is DB-backed by `local_operators` authz policy rules. Rare privileged
  Director-agent writes can use `LAUNCHPLANE_LOCAL_ADMIN_TOKEN` only when matching
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
- `odoo-devkit` may consume that contract when an admin points
  `ODOO_CONTROL_PLANE_ROOT` at a valid `launchplane` checkout.
- When `odoo-devkit` is configured to use the control-plane contract, legacy
  devkit-local `.env` / `platform/.env` / `platform/secrets.toml` files should
  be removed so environment authority stays single-source and fail-closed.

## Bootstrap

Bring up the service with bootstrap env such as `LAUNCHPLANE_DATABASE_URL` and
`LAUNCHPLANE_SECRET_KEYS_JSON`, then write the durable DB-backed secret
and runtime records through the normal Launchplane commands. Dokploy
credentials belong in Launchplane-managed secrets before Dokploy operations run.

## Re-Encryption Planning Evidence

The `managed-secret-reencryption` privileged-operation descriptor is the
routine human planning surface for root rotation. It calls the existing
re-encryption computation with `apply=False` and stores only bounded evidence:
digests, counts, key IDs required for human retirement review, compatibility
state, actor/source metadata, and lifecycle timestamps.

Managed-secret IDs, secret-version IDs, ciphertext, plaintext, and raw error
strings are neither persisted nor returned in the agent projection. The agent
projection also excludes key IDs and request/actor details.

`POST /v1/secrets/reencrypt` is legacy-only: its `mode: "dry-run"` response
refuses with `privileged_operation_planning_required`, and its `mode: "apply"`
response refuses with `privileged_operation_approval_required`. The legacy
`secret.reencrypt.dry-run` and `secret.reencrypt.apply` actions are not a
current authority path. Use the typed browser-human plan and approval flow, then
the supervised privileged-operation worker; approval may execute immediately,
and revocation is possible only before worker claim. See
`docs/privileged-operations.md` for canary activation and worker proof rules.

**Preserved history:** Phase 1 described planning evidence before a deployed
worker existed; it is not current root-rotation operating guidance.

## Copying a product's own managed runtime secret

`GET /v1/products/{product}/secret-bindings` returns the product's configured
runtime secret bindings: name, binding key, scope, context, instance, declared
class, sharing reason and current version ID. It reads no ciphertext or value,
excludes global and worker/service stores, and requires product-profile read
access. It returns only bindings covered by the caller's existing `secret.list`
access, so an empty result does not prove absence outside that access. Preview
and removed-lane bindings and contexts shared ambiguously between products are
excluded.

A product-config secret entry can use `copy_from` instead of `value`:

```json
{
  "binding_key": "INTEGRATION_TOKEN",
  "copy_from": {
    "context": "example-site",
    "instance": "prod",
    "version_id": "<version_id from the metadata read>"
  },
  "secret_class": "shared_safe",
  "sharing_reason": {
    "kind": "read_only_source",
    "reason": "Testing reads the same source",
    "evidence": "Client verified read-only permissions on <date>"
  },
  "description": "Read-only source verified by the Client"
}
```

The top-level product-config target is the destination lane. The source must be
a stable lane of that same product, and the destination must be lane-exact
in `runtime_environment`. The source binding key is the destination binding key;
renaming a token to bypass integration key safety is not supported. Neither a
source product nor another secret store can be supplied. Site-shared sources
may be selected when that source lane has no configured exact binding. Ambiguous,
disabled, missing or superseded sources are refused. Copies to previews are refused.
An explicit class on the source must allow the destination lane; a `prod_only`
source cannot be copied to testing by relabelling its destination `shared_safe`.

If a person verifies that such a source is shareable, a writer authorized for
the source lane can reclassify it without collecting its value: target that
same lane with `copy_from` pointing to its own current version, declare the
new class, reason and evidence, and dry-run/apply normally. This preserves the
value while writing a new encrypted version and its binding metadata. Reading
a source and writing testing do not authorize this production-lane write.

Every copy requires a declared class and an allowlisted sharing reason with
reason and evidence, in addition to normal runtime key safety. The caller needs
existing `secret.read` access to the resolved source record's scope (whole
context for a site-shared source, exact instance for a lane source) and
destination product-config access; the route creates no grant. The resolved
scope is authorized before decryption. Launchplane does not verify token permissions: a
person verifies them and records who, when and what they checked.

Use the normal product-config dry run first, then apply the same reviewed
request with its idempotency key. The source version ID pins the review; a
rotation requires reading metadata and reviewing a fresh request. Dry runs
resolve metadata without decrypting. Apply decrypts inside the service and
writes a separately encrypted destination secret atomically, leaving the source
unchanged. Product ownership, profile and source record/binding changes before
commit abort the copy. The audit records the source secret and version IDs.
Completed retries replay before resolving or decrypting the source. Request,
response and audit metadata contain no secret value; subsequent live runtime
sync or deployment remains a separate operation.
