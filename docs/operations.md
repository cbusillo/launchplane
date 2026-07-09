---
title: Operations
---

## Command Groups

Use `uv run launchplane --help` for the complete CLI surface. The current
top-level groups are:

Today this CLI is the local Launchplane operator/client surface around the
service API. Shared or production mutations must prefer the deployed service API
with GitHub OIDC or the operator UI that calls it. Local core-record write
commands are file-backed rehearsal helpers, not DB-backed shared-state mutation
paths. If a live mutation still exists only as a local CLI command, stop and add
or use a service API path instead of running the local command from an arbitrary
checkout.

- `artifacts`: rehearse artifact manifest writes, inspect artifact manifests,
  and emit protected artifact inventories for registry cleanup deny sets.
- `backup-gates`: rehearse and inspect backup-gate records.
- `deployments`: rehearse and inspect deployment records.
- `environments`: write, list, and resolve DB-backed runtime environment
  contracts.
- `launchplane-previews`: inspect, mutate, render, ingest, and replay
  Launchplane preview state.
- `inventory`: inspect current environment inventory.
- `promote`: record, resolve, and execute artifact-backed promotions.
- `promotions`: rehearse and inspect promotion records.
- `product-config`: dry-run and apply trusted product runtime/secret config
  bundles from a live Launchplane context.
- `release-tuples`: inspect state-backed tuple records and explicitly export a
  TOML catalog from minted state.
- `service`: run the first local Launchplane HTTP ingress slice.
- `ship`: plan, resolve, and execute artifact-backed deploy requests.
- `storage provider-target-audit`: run the read-only provider-target parity
  preflight before backfill or provider-target authority cutover.
- `storage provider-target-backfill`: dry-run explicit provider-target row
  projections from complete Dokploy target/id pairs; shared/live apply uses the
  service-backed Provider Target Operations workflow.

`deployments write`, `promotions write`, `inventory write-from-deployment`,
`inventory write-from-promotion`, and `release-tuples write-from-promotion`
are local rehearsal commands. Shared-service evidence ingress uses
authenticated Launchplane service routes so Launchplane can accept externally
produced deployment and promotion facts without claiming it executed that
product's runtime action itself.

Those commands are current implementation scaffolding. The Launchplane boundary
is a long-running service with authenticated HTTP ingress. The CLI should remain
a client of Launchplane's stable API contract or an explicit DB-backed operator
tool, not a separate live-state authority.

Provider-target data changes for shared or production lanes must use the
deployed Launchplane service. The manual `Provider Target Operations` workflow
calls `POST /v1/provider-targets/operations` with GitHub OIDC and is authorized
through DB-backed `provider_target.audit` and `provider_target.backfill` grants
for product/context `launchplane`. Run it first in `audit` or
`backfill-dry-run` mode, review the artifact, then run `backfill-apply` only
with the exact confirmation phrase and an operator reason. Specific rollout
lane sets and ordering belong in issue-backed plans and workflow inputs, not in
checked-in docs.

After the provider-target audit is clean, use the manual
`Product Environment Evidence` workflow to collect read-model evidence through
the deployed service. The workflow calls `GET
/v1/products/{product}/environments/{environment}` with GitHub OIDC, records
only sanitized provider, target-type, trust-state, and count summaries, and
fails closed if any requested lane lacks recorded provider-target authority.
Raw product environment responses are not uploaded; the artifact contains only
the route list and sanitized summaries.

The local CLI remains a DB-backed inspection and rehearsal helper. Local
provider-target data changes must start with a read-only audit:

```bash
uv run launchplane storage provider-target-audit \
  --database-url "$LAUNCHPLANE_DATABASE_URL"
```

Use `--provider-id`, `--context`, or `--instance` for narrower inspection. The
command emits JSON and exits nonzero when explicit provider-target rows are
missing, partial Dokploy pairs exist, or explicit rows disagree with the
Dokploy-derived projection. Backfill and authority cutover should not proceed
until the audit has no unresolved blockers for the affected lanes.

Use the backfill command to preview missing physical rows after reviewing the
audit output and after dual-write is deployed:

```bash
uv run launchplane storage provider-target-backfill \
  --database-url "$LAUNCHPLANE_DATABASE_URL"
```

The local command is report-only. It emits dry-run JSON for `would-create`,
`skipped-exists`, `skipped-incomplete`, `skipped-conflict`, and
`unsupported-provider` rows without writing anything. Review incomplete pairs and
conflicts before applying; conflicts are never overwritten automatically.
Shared/live apply runs through the deployed service route and is normally
launched by the manual `Provider Target Operations` workflow:

```bash
gh workflow run provider-target-operations.yml \
  -f mode=backfill-apply \
  -f target_set=configured-json \
  -f routes_json='[{"context":"<context>","instance":"<instance>"}]' \
  -f provider_id='<provider-id>' \
  -f confirmation='APPLY PROVIDER TARGET BACKFILL' \
  -f reason="issue-backed provider-target backfill"
```

Service-backed backfill writes only complete, non-conflicting Dokploy-derived
rows and is idempotent. Re-run `storage provider-target-audit` after apply and
require clean evidence before provider-target authority cutover.

Provider-target dual-write is active for Launchplane-owned target identity
mutations: product onboarding, Dokploy target adoption/creation, product context
cutover, and tracked Dokploy target metadata commands. These paths validate an
existing explicit provider-target row before mutating the Dokploy pair, write the
Dokploy target/id records, then write the matching provider-target row. A stale
explicit provider-target row blocks the mutation instead of being overwritten.

## Target Launchplane Ingress

The target communication model is:

- Launchplane runs as a long-running service behind an operator-owned stable
  host.
- Product workflows communicate with Launchplane over authenticated HTTP.
- GitHub Actions OIDC is the default machine-to-machine trust boundary.
- Launchplane authorizes requests from GitHub-issued identity claims such as repo,
  workflow, ref, environment, and event context.
- Typed evidence payloads are the stable contract; CLI commands are service
  clients, DB-backed operator tools, or explicit local-only rehearsal and
  inspection helpers.

Launchplane should eventually expose API ingress for at least:

- deployment evidence
- promotion evidence
- inventory refresh triggers or derived writes
- preview generation evidence
- preview destroyed evidence
- driver-triggered runtime actions where Launchplane owns execution

The first explicit version of that boundary, including the OIDC claim mapping
and endpoint list, lives in [`service-boundary.md`](service-boundary.md).
Agent-facing read context and scoped write-intent consumption rules live in
[`agent-context-boundary.md`](agent-context-boundary.md).

The first implemented service command is:

```bash
uv run launchplane service serve \
  --state-dir ./state \
  --database-url "$LAUNCHPLANE_DATABASE_URL" \
  --policy-file ./bootstrap-policy.toml \
  --audience "$LAUNCHPLANE_SERVICE_AUDIENCE"
```

The service needs an explicit minimal bootstrap policy input, but the repo no
longer tracks the live policy. Product and workflow grants should be represented
as DB-backed authz policy records. Omitting `--database-url` always fails closed,
including loopback local development, rather than using file-backed JSON state
as authority. The GitHub OIDC audience is explicit operator/process wiring;
production code must not default it to a live domain.

Current implementation scope:

- `GET /v1/health`
- `GET /v1/artifacts/protected`
- `POST /v1/evidence/backup-gates`
- `POST /v1/evidence/deployments`
- `POST /v1/evidence/promotions`
- `POST /v1/evidence/previews/generations`
- `POST /v1/evidence/previews/destroyed`
- `POST /v1/authz-policies/github-actions/grants`
- `POST /v1/authz-policies/github-actions/removals`
- `POST /v1/authz-policies/github-humans/grants`
- `POST /v1/authz-policies/terminal-agents/grants`
- `POST /v1/authz-policies/local-operators/grants`
- `POST /v1/authz-policies/local-admins/grants`
- `POST /v1/provider-targets/operations`
- `POST /v1/product-profiles/context-cutover/apply`
- `POST /v1/products/public-ingress-monitor/run-once`
- `POST /v1/every-code/notification-policies/apply`
- `POST /v1/public-ingress/notification-policies/apply`
- `POST /v1/previews/lifecycle-plan`
- `POST /v1/drivers/verireel/preview-refresh`
- `POST /v1/drivers/verireel/preview-destroy`
- `POST /v1/drivers/verireel/testing-deploy`
- `POST /v1/drivers/verireel/testing-verification`
- `POST /v1/drivers/verireel/prod-deploy`
- `POST /v1/drivers/verireel/prod-backup-gate`
- `POST /v1/drivers/verireel/prod-promotion`
- `POST /v1/drivers/verireel/prod-rollback`
- `POST /v1/drivers/odoo/post-deploy`
- `POST /v1/drivers/odoo/prod-backup-gate`
- `POST /v1/drivers/odoo/prod-promotion`
- `POST /v1/drivers/odoo/prod-rollback`

Privileged product rollback routes should stay behind narrow delegated-worker
contracts rather than being absorbed into the main API host. Product-private
runtime memos belong in product repos; this repo keeps only the shared driver
and record contracts.

The service uses GitHub OIDC bearer tokens and DB-backed authz policy records.
Additional evidence routes should land against the same authn/authz boundary
rather than creating separate ad hoc ingress patterns.

Authz policy grant and removal routes are native FastAPI service routes. They
require DB-backed policy storage, enforce `authz_policy_grant.write` through
the active runtime policy, preserve signed-in GitHub human-session callers,
store `Idempotency-Key` replay/conflict evidence for apply requests, and keep
dry-runs stateless. The retired compatibility branches for these paths are not
production surfaces.

Operators should mutate shared or production authz through the deployed service,
not by running arbitrary local DB writes from a checkout. Use
`uv run launchplane authz-policies grant-workflow --service-url ... --dry-run`
or `uv run launchplane authz-policies grant-human --service-url ... --dry-run`
to inspect grant diffs. Use
`uv run launchplane authz-policies remove-workflow-rule --service-url ... --dry-run`
to inspect exact GitHub Actions rule removals, such as broad workflow
`launchplane_service_deploy.execute` rules left behind after route narrowing.
Removal requests match the complete persisted rule; partial selectors do not
remove broader or narrower rules. Rerun with `--apply --reason ...` and an
idempotency key only after the dry-run diff is reviewed. The CLI is a thin
service client: it sends a short-lived bearer token or a Launchplane browser
session cookie, and the service validates the caller's
`authz_policy_grant.write` authority, writes any new active policy record,
stores audit metadata, and reloads the current service worker's active policy.
Launchplane self-deploy authority is separate and does not authorize authz
policy grant maintenance.

The Launchplane deploy workflow also reconciles configured signed-in operator
grants for product-config writes. Set
`LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_LOGINS`,
`LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_PRODUCTS`, and
`LAUNCHPLANE_PRODUCT_CONFIG_OPERATOR_CONTEXTS` as comma-separated repository
variables. During deploy, `scripts/deploy/ensure-authz-grants.sh` writes
DB-backed GitHub-human grants for `product_config.plan` and
`product_config.apply` through `/v1/authz-policies/github-humans/grants`.
Leave those variables unset to skip reconciliation; do not hard-code human
logins or product-specific operator grants in source.

Product-specific GitHub Actions grants are not authored in the deploy script.
When a temporary deploy-time bridge is needed, provide
`LAUNCHPLANE_AUTHZ_GRANTS_JSON` as an explicit operator-controlled JSON array of
grant requests with repository, workflow file, product, context, action, source
label, and idempotency suffix fields. The script only submits that configured
payload through the service; checked-in shell tuples must not become the live
workflow grant catalog. Prefer the operator UI or service-backed authz policy
management routes for steady-state shared and production grant changes.

The deploy workflow also reconciles its own `authz_policy_grant.write` grants
for product/context `launchplane`, covering both manual dispatches and automatic
CI-success deploys. Those grants keep future grant reconciliation separate from
Launchplane self-deploy authority.

The #1049 compatibility cleanup removed stale
`launchplane_service_deploy.execute` GitHub Actions rules for policy import
workflows through the service-backed removals route. Do not reintroduce those
broad rules; keep those workflows paired with narrow action grants such as
`merge_train.policy_import`.

The deploy workflow also reconciles the manual `Provider Target Operations`
workflow grants. `provider_target.audit` covers audit and dry-run requests;
`provider_target.backfill` covers apply requests. The route is intentionally
Launchplane-scoped and single-route per request, so production rows are seeded
through explicit audited workflow runs rather than local live-target commands.

Routine local-operator product-config grants are scoped, not wildcard, and the
deploy reconciliation skips them unless explicit product/context scopes are
configured. Set `LAUNCHPLANE_LOCAL_OPERATOR_PRODUCT_CONFIG_SCOPES_JSON` only for
operator-reviewed routine write access; use local-admin grants for rare broader
repair authority instead of widening routine local-operator access. Checked-in
catalogs are not deploy-time authority for these operator scopes.

Private health endpoint grants follow the same scoped local-operator pattern.
Set `LAUNCHPLANE_PRIVATE_HEALTH_ENDPOINT_SCOPES_JSON` to an array of
product/context objects when the local operator should manage DB-backed private
health endpoint records for those scopes. Scopes must use explicit product and
context values; wildcard/glob scopes fail closed. Leave it unset to skip
reconciliation, and do not place endpoint URLs in repo files.

The `Ingress Route Canary Apply` workflow grant is also scoped by operator input,
not a checked-in product catalog. By default the deploy reconciliation grants the
workflow `ingress_route.apply` only for `launchplane`/`launchplane`. Set
`LAUNCHPLANE_INGRESS_CANARY_ROUTE_SCOPES_JSON` to an array of product/context
objects when DB-backed canary route records are intentionally scoped elsewhere;
the workflow still passes only the canary key while the service resolves route
topology from records.

Grant requests return only authz policy record metadata and rule counts; they do
not echo workflow refs, human logins, or the full policy body.

Lane health monitoring is Launchplane-owned synthetic monitoring for
generic-web stable lanes, including drivers that inherit generic-web behavior.
The `Public Ingress Monitor` workflow owns both the recurring schedule and
manual operator reruns, so its GitHub OIDC identity stays scoped only to
`public_ingress_monitor.run_once`. Both paths call
`POST /v1/products/public-ingress-monitor/run-once` through GitHub OIDC and are
authorized in the Launchplane service context. Lanes opt in by declaring
`health_monitoring.checks[]`: `public_http` checks validate public reachability,
`private_http` checks resolve a Launchplane-owned private endpoint record by
`private_endpoint_key`, and `provider` checks fail closed until a
provider-specific monitor is wired. The public-ingress route and record
names are compatibility names for the existing health-monitor observation
family. Observations are sensor evidence; incident records are keyed by product,
lane, and health-check name so public, private, and provider failures do not
overwrite each other.
Notification routing is a separate service-backed policy and delivery concern,
not lane-owned text config. The initial notification destinations are GitHub
issues, email, and Discord; each is selected by DB-backed policy and evidenced
by delivery-attempt records.
GitHub issue notification delivery uses the managed automation token projected
as `LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN`; it does not fall back to active
local `gh` authentication. Verify the configured actor with a token-scoped
GitHub `/user` API read outside Launchplane, and never print or paste the token
itself into logs, issues, or records.

The manual Product Context Cutover workflow plans or applies the same
current-authority record move through the Launchplane service. The workflow
does not carry product/context defaults; operators must provide the product,
legacy source context, canonical target context, and display name explicitly.
Run it first with `dry_run=true`; run with `dry_run=false` only after the
artifact shows the expected key/count metadata for runtime records, managed
secrets, Dokploy targets, target IDs, inventories, release tuples, and the
product profile lane contexts. The workflow intentionally leaves append-only deployments,
promotions, backup gates, and preview history on their original contexts.

After a cutover has been applied and the product profile no longer references
the legacy context, run the manual Product Legacy Context Cleanup workflow with
`dry_run=true`. The artifact reports mutable source records that can be cleaned:
runtime environment records and Dokploy target lookups are deleted only when the
matching target-context record already exists, and managed secret records and
bindings are disabled rather than deleted. Run with `dry_run=false` only when
`blocked=false`. Inventory records, release tuples, deployments, promotions,
backup gates, and preview history are preserved as historical evidence.
Launchplane keeps the legacy context in the product profile's
`historical_contexts` metadata after cutover so product activity queries still
show pre-cutover evidence while deploy and config authority use the current lane
contexts.

Render an explicit emergency bootstrap policy or import a policy into DB-backed
records with:

```bash
uv run launchplane service render-authz-policy --policy-file ./bootstrap-policy.toml
uv run launchplane service render-authz-policy \
  --policy-file ./bootstrap-policy.toml \
  --format b64
uv run launchplane authz-policies import-toml \
  --policy-file ./bootstrap-policy.toml \
  --allow-direct-db-mutation
```

When operators need to preview or apply an explicit emergency bootstrap policy to
the live Launchplane Dokploy target without editing any rendered host-side env
file, use:

```bash
uv run launchplane service sync-bootstrap-policy \
  --target-type "$LAUNCHPLANE_DOKPLOY_TARGET_TYPE" \
  --target-id "$LAUNCHPLANE_DOKPLOY_TARGET_ID" \
  --policy-file ./bootstrap-policy.toml

uv run launchplane service sync-bootstrap-policy \
  --target-type "$LAUNCHPLANE_DOKPLOY_TARGET_TYPE" \
  --target-id "$LAUNCHPLANE_DOKPLOY_TARGET_ID" \
  --policy-file ./bootstrap-policy.toml \
  --apply
```

Preview workflows should normally authorize by workflow path with a wildcard
ref suffix such as `.../preview-control-plane.yml@*`, because pull-request runs
execute from branch-specific workflow refs rather than a fixed `main` ref.

The Launchplane container entrypoint now fails closed unless one of
`LAUNCHPLANE_POLICY_TOML`, `LAUNCHPLANE_POLICY_B64`, or
`LAUNCHPLANE_POLICY_FILE` is supplied. It also refuses to start from the
checked-in `.example` policy path.

## Launchplane Service Deploy Posture

The first real Launchplane service deployment should be GitHub-driven and
Dokploy-hosted.

- Keep test and deploy automation separate.
- `CI` is the gate for Launchplane code changes and must pass before a deploy
  workflow replaces the live Launchplane app.
- The first real Launchplane bring-up should target a single Dokploy-hosted Launchplane
  instance rather than introducing a separate Launchplane testing instance during
  bootstrap.
- Launchplane deploy automation should publish an immutable image artifact, update
  Dokploy by digest, and record the previously running digest before
  replacement.
- The current repo workflow for that posture is
  `.github/workflows/deploy-launchplane.yml`.
- Launchplane image builds use Docker Hub public mirror-qualified base images
  and the CI container scan pulls Trivy from GHCR so self-hosted runners do not
  fail closed on Docker Hub anonymous pull limits before Launchplane code is
  built or scanned.
- Before adding or controlling self-hosted runner lanes, use
  `uv run launchplane work-graph runner-inventory --repository owner/name` to
  read GitHub's current repository runner list. The command is read-only and
  reports lane count, online/busy/idle/offline counts, labels, host hints, and a
  capacity-constrained heuristic from the observation timestamp.
- Treat [runner-lane-baseline.md](runner-lane-baseline.md) as the readiness
  contract before routing shared product automation onto a lane. The baseline
  fails closed without positive per-job Docker credential isolation evidence, so
  product repositories should not carry local `DOCKER_CONFIG` workaround retries
  as the long-term safety mechanism.
- To prove repo-scoped runner registration, start with
  `.github/workflows/runner-lane-registration.yml` in `mutate=false` mode. For
  the cm-website pilot, capture `runner-inventory` evidence for
  `cbusillo/odoo-tenant-cm-website`, run the registration workflow with an audit
  key under `runner-lane-registration/<date>/...`, inspect the uploaded JSON
  artifact, and only rerun with `mutate=true` after the dry-run evidence and
  confirmation text have been reviewed. The workflow writes service-backed
  runner-registration audit evidence and also uploads the local JSON artifact
  for operator review.
- Deploy verification should probe Launchplane's live health endpoint, currently
  `GET /v1/health`, after the Dokploy update.
- When rollout health fails, deploy automation should restore the previous
  digest automatically instead of requiring a manual Dokploy click path.
- Keep a manual rollback path too, so operators can redeploy a known-good
  digest even after a technically successful rollout.

This posture is the current safety net while Launchplane still lacks a dedicated
testing environment of its own.

Required GitHub configuration for that workflow:

- repository variables:
  - `LAUNCHPLANE_PUBLIC_URL`
  - optional `LAUNCHPLANE_SERVICE_AUDIENCE`; when unset, trusted workflows
    derive the GitHub OIDC audience from `LAUNCHPLANE_PUBLIC_URL`'s host
  - `LAUNCHPLANE_DOKPLOY_TARGET_TYPE`
  - `LAUNCHPLANE_DOKPLOY_TARGET_ID`
  - `LAUNCHPLANE_DEPLOY_HEALTH_URLS`
  - optional `LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS`
  - optional `LAUNCHPLANE_DEPLOY_HEALTH_TIMEOUT_SECONDS`
  - optional `LAUNCHPLANE_IMAGE_REPOSITORY`
  - product target ids are not consumed from deploy-time seed files; create or
    repair product onboarding records through Launchplane service routes with
    scoped authorization

The workflow should use GitHub OIDC to call Launchplane's own service API and
update the image digest plus known OAuth env only. DB-backed authz policy records
own live product/workflow grants; keep Dokploy host/token authority in
Launchplane-managed secrets instead of duplicating those credentials in GitHub
repository secrets for normal deploy execution. Automatic rollback also uses the
Launchplane service route. If a failed rollout makes that route unable to accept
its own rollback request, direct Dokploy rollback is available only through the
manual break-glass inputs on the Deploy Launchplane workflow: provide the exact
confirmation text, previous image reference, and operator reason.
Keep the workflow configured with break-glass
`LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST` and
`LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN` repository secrets for that manual
emergency path. When direct Dokploy break-glass rollback runs, the workflow
writes a redacted `launchplane-break-glass-rollback` artifact and summary so the
provider mutation remains reviewable after the emergency.
Before rollback, the workflow uses those same break-glass credentials to capture
redacted Dokploy target, container, and recent log diagnostics for the failed
rollout. Diagnostics failures are non-blocking so rollback remains the priority.

Product onboarding manifests and runtime key-safety policies are DB-backed
Launchplane records. Create or repair them through the Launchplane service API
or operator UI with scoped authorization and operator evidence; do not load
product/runtime truth from checked-in catalogs.

`LAUNCHPLANE_DEPLOY_HEALTH_URLS` must resolve from the runner that executes the
deploy workflow. Use a Launchplane `GET /v1/health` endpoint reachable from that
runner rather than an internal-only provider hostname.

The manual Merge Train Policy Import workflow uses GitHub OIDC with
`merge_train.policy_import` authority for product/context `launchplane`. It does
not inherit Launchplane self-deploy authority; use that workflow for DB-backed
merge-train policy imports instead of direct DB writes from a local checkout.

The Dokploy-hosted Launchplane target should consume `DOCKER_IMAGE_REFERENCE` from
its env so deploy automation can switch the service by immutable digest and
roll back to the prior digest when verification fails.

Before a real Launchplane deploy, run the sanitized preflight check against the
live Dokploy target:

```bash
uv run launchplane service inspect-dokploy-target \
  --target-type compose \
  --target-id "$LAUNCHPLANE_DOKPLOY_TARGET_ID"
```

That command reports only non-secret metadata and fails closed when the live
Launchplane target is missing critical runtime pieces such as
`LAUNCHPLANE_DATABASE_URL`, `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`,
Launchplane-managed Dokploy secret bindings, or a Dokploy SSH key for a private
`git@github.com:...` compose source.

The intended live service contract is now bootstrap-only target env plus
DB-backed Launchplane records:

- keep bootstrap/process inputs such as `LAUNCHPLANE_DATABASE_URL`,
  `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`, and policy selectors on the service
  target
- move Dokploy credentials into Launchplane-managed secret records
- move per-context runtime values, ship-mode overrides, preview base URLs, and
  product-specific worker config into Launchplane runtime-environment records
- use target-id records in the shared store when possible instead of relying on
  env-carried target-id catalogs
- inspect tracked stable-lane Dokploy target records with
  `uv run launchplane dokploy-targets list` / `show`
- mutate tracked target Shopify guard policy with
  `uv run launchplane dokploy-targets put-shopify-protected-store-key ... --allow-direct-db-mutation`
  and `unset-shopify-protected-store-key ... --allow-direct-db-mutation` only
  for explicit local/bootstrap repair instead of editing repo-local target
  catalogs or ad-hoc DB rows. Routine shared/live target setup and
  provider-target authority changes should use the deployed service route or
  operator workflow.

Two deployment prerequisites remain Dokploy-side operational contracts rather
than Launchplane CLI validations:

- Dokploy must already have a working saved registry credential that can pull
  Launchplane's GHCR image.
- The Postgres service referenced by `LAUNCHPLANE_DATABASE_URL` must already be
  deployed and reachable on the Dokploy network before Launchplane is redeployed.

The Launchplane service entrypoint applies `uv run alembic upgrade head` with
the container's `LAUNCHPLANE_DATABASE_URL` before starting HTTP service. This
keeps hosted service startup fail-closed on schema drift while running
migrations from inside the Dokploy network that can resolve the shared Postgres
service hostname. The self-deploy workflow should not run shared database
migrations from the GitHub runner; keep Launchplane migrations additive and
rollback-aware so a failed health check can still return to the previous image.
The service route keeps OIDC authz and idempotency at the HTTP boundary, then
delegates provider target env mutation and deployment triggering to the
Launchplane self-deploy workflow module.
If a shared database already has Launchplane tables but no `alembic_version`
table from the pre-migration `create_all` era, the entrypoint stamps the newest
revision that matches the detected legacy table set before applying later
migrations. Empty databases still run the full migration chain from the first
revision.

Current derived-state behavior:

- accepted deployment evidence also refreshes current environment inventory for
  that `context/instance`
- accepted promotion evidence refreshes destination inventory when the
  promotion record includes valid `deployment_record_id` linkage
- Launchplane can now execute the first explicit VeriReel driver actions
  directly: `POST /v1/drivers/verireel/testing-deploy` and
  `POST /v1/drivers/verireel/prod-deploy` trigger the shared testing and prod
  deploys, `POST /v1/drivers/verireel/prod-backup-gate` captures the prod
  backup gate and writes the backup-gate record, and the promotion / rollback
  drivers own the remaining stable-lane execution path. The prod-promotion
  driver writes the promotion record from the backup gate, deploy result,
  migration result, destination health check, and primitive testing-lane health
  status sent by the product workflow. The testing-verification route accepts
  primitive migration, browser verification, and owner-route statuses and
  updates the existing testing deployment record plus current inventory.
  VeriReel maintenance operations that need Dokploy authority, such as testing
  migrations, preview owner-admin verification helpers, reset-testing, and
  preview inventory, also flow through Launchplane driver routes instead of
  product-repo workflow secrets. Stable testing/prod base URLs and target
  identity are resolved from Launchplane's DB-backed target/runtime records
  through the stable-environment route. Those routes return durable record
  identifiers, topology metadata, or timing/status for the caller to thread into
  later verification or promotion evidence.
- Launchplane can also execute the Odoo stable-lane driver path directly:
  `POST /v1/drivers/odoo/prod-promotion-inputs` resolves the current testing
  artifact, source ref, and deterministic backup-gate record ID from DB-backed
  release tuple and artifact records, `POST /v1/drivers/odoo/prod-backup-gate`
  captures DB and filestore backup evidence, `POST /v1/drivers/odoo/prod-promotion`
  validates the stored artifact, source release tuple, and required backup gate
  before promoting `testing` to `prod`, and `POST /v1/drivers/odoo/prod-rollback`
  deploys an explicit previous artifact. These routes resolve target identity,
  runtime values, override inputs, and managed secrets from DB-backed
  Launchplane records; tenant workflows should only send thin
  OIDC-authenticated requests and record returned IDs.
- Generic web products can use the common
  `POST /v1/drivers/generic-web/prod-promotion` route for testing-to-prod image
  promotion when product-specific gates are not needed. The route resolves
  product profile lanes, deploys the submitted image to the prod lane, records
  source and destination health evidence, writes promotion/deployment linkage,
  and refreshes prod inventory after a verified deploy. Product-specific
  drivers can wrap this base path with stricter backup, migration, rollout, or
  tenant checks instead of reimplementing the shared promotion record flow.
- Operators should promote generic web products through the product-owned GitHub
  workflow bridge. The UI first calls the generic-web prod-promotion route with
  `dry_run=true`, then dispatches
  `POST /v1/drivers/generic-web/prod-promotion-workflow`. Launchplane resolves
  the product repository/workflow from the DB-backed product profile and the
  managed `GITHUB_TOKEN` from runtime records; the product workflow still owns
  release/tag creation and any product-specific safeguards.

## Core Rules

- Promotions and deploys reference explicit artifact identifiers.
- Missing control-plane config is a hard error, not a silent fallback.
- Operator-local runtime records belong under `state/` or another explicit
  state directory outside git.
- Artifact manifests handed off from build/export steps are persisted here
  before later workflows depend on them.
- The normal split-repo build/export handoff comes from `odoo-devkit`
  `platform runtime publish`, which writes a control-plane-compatible artifact
  manifest JSON file after it stages tenant/shared source inputs, pushes the
  image, and resolves the pushed digest.
- Promotion execution validates a stored passing backup-gate record for the
  destination environment before ship execution begins.
- Deploy execution prefers immutable artifact image references by syncing
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` to Dokploy when a stored artifact
  manifest is available.
- VeriReel stable deploys update the Dokploy Application docker provider to the
  exact immutable artifact id before triggering deploy; product workflows do not
  publish mutable prod tags as the promotion authority.
- Direct `ship` and `promote` execution fail closed when the referenced
  artifact manifest is missing.
- Direct artifact-backed execution also fails closed when the Dokploy target
  still points at a legacy monorepo source or carries mutable addon repository
  refs instead of exact git SHAs.
- Successful waited `ship` executions for `testing` and `prod` mint current
  release tuple records from stored artifact manifests.
- `promote execute` requires the source lane's current release tuple to match
  the requested artifact, then promotes that exact tuple to the destination
  lane after the deploy passes.
- `promote execute` and `ship execute` require `--database-url` or
  `LAUNCHPLANE_DATABASE_URL` for DB-backed execution and must also pass
  `--allow-direct-db-mutation`; explicit offline filesystem execution must opt
  in with `--local-rehearsal`.
- Current environment inventory is refreshed from successful waited `ship` and
  `promote` executions.
- Externally produced promotion evidence can also refresh current inventory
  when the stored promotion record carries explicit `deployment_record_id`
  linkage to the deployment record that established the promoted state.
- The same promotion evidence can also mint the destination stable-lane tuple
  when Launchplane already has the source tuple state for the promoted-from lane.
- `artifacts protected` and `GET /v1/artifacts/protected` compose Launchplane's
  current protected artifact inventory from stable environment inventory,
  release tuples, active preview generations, and ready preview feedback.
  Registry cleanup jobs for every product must consume this inventory before
  deleting package versions and fail closed when the read fails or warns about a
  live artifact without a stored manifest. Service callers must include
  `product=`; callers that request a whole product without `context=` need an
  `artifact_protection.read` grant with wildcard context. The CLI requires
  `--database-url` or
  `LAUNCHPLANE_DATABASE_URL` unless `--local-rehearsal` is explicitly passed for
  non-authoritative local rehearsal.
- The tracked Dokploy route catalog is only for stable remote lanes. If a pull
  request needs runtime state, Launchplane models that through preview records and
  preview generations instead of adding another long-lived route.
- Operator read models compose inventory, deployment, promotion, and
  backup-gate records instead of requiring operators to inspect raw JSON first.
- DB-backed schema changes must land as Alembic revisions. Keep revisions
  additive and rollback-aware so image rollback remains a valid recovery path.
- Local CLI/file-backed compatibility paths are local-development, rehearsal, or
  emergency-inspection surfaces only. Product workflows should use service
  routes once matching OIDC-authenticated routes exist; production-capable
  compatibility paths need an issue-backed removal condition.

## Dokploy Contracts

- Tracked Dokploy route definitions live in Launchplane DB-backed target
  records.
- Tracked route definitions are expected to be stable remote lanes only:
  `testing` and `prod`.
- Live Dokploy `target_id` values should come from Launchplane DB-backed
  target-id records in steady state.
- Dokploy source loading fails closed when target ids are missing, duplicate
  routes are present, or the tracked target records omit a required target id.
- `environments logs --context <context> --instance <instance> --lines <n>`
  resolves the DB-backed tracked Dokploy target and target id before fetching
  bounded logs. It supports Dokploy `application` and `compose` targets,
  includes route/target/app/server metadata, accepts optional `--since` and
  `--search`, and redacts likely secret values from returned log lines.
- `GET /v1/contexts/{context}/instances/{instance}/logs?lines=200` exposes the
  same tracked-target log reader through a native FastAPI service route using
  action `target_logs.read`.
- The manual Tracked Target Logs workflow calls that service route with GitHub
  OIDC and uploads the redacted JSON result, so operators can inspect compose
  target boot failures without local Dokploy credentials. Tenant contexts need a
  matching `target_logs.read` GitHub Actions grant in
  `LAUNCHPLANE_AUTHZ_GRANTS_JSON`; do not broaden this workflow to read all
  contexts by default.

## Runtime Environment Contracts

- `environments put` remains an explicit local/bootstrap repair path for
  non-secret `KEY=VALUE` runtime settings in DB-backed runtime-environment
  records. It requires `--allow-direct-db-mutation`, rejects secret-shaped keys,
  and returns key metadata only, not plaintext values. Routine shared and
  production runtime config changes should use product-config dry-run/apply
  through the deployed service route or operator UI.
- `product-config apply --dry-run --input-file <json>` remains a local planning
  helper for trusted product runtime config bundles. Direct local DB
  `--apply` is restricted after the service boundary and requires
  `--allow-direct-db-mutation`; use it only for explicit local/bootstrap repair,
  not routine shared or production mutation from an arbitrary checkout. The
  service route below is the normal shared/prod path. Product-config apply
  writes non-secret values to runtime-environment records and secret-shaped
  values to managed secret records while returning only key names, actions,
  counts, actor, and source metadata. Bundles with secrets require
  `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`. Runtime and secret scopes default from
  the top-level `context`/`instance`; nested `runtime_env` and secret routes must
  match that top-level target. Dry-run validates secret scope/route
  compatibility and runtime key-safety policy before reporting a plan, so apply
  does not discover invalid secret scopes or disallowed runtime secret bindings
  after writing earlier secrets.
- `POST /v1/product-config/apply` exposes the same planner/writer through the
  authenticated service API for operator UI use. Submit `mode: "dry-run"` to
  preview with `product_config.plan`, then `mode: "apply"` with
  `product_config.apply` after review. Signed-in GitHub human operators can use
  this route when their session has the exact product/context/action grant.
  Trusted owner terminals can also use the dedicated
  `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN` from
  `~/.config/launchplane/local-operator.env` when exact DB-backed
  `local_operators` policy rules grant the product/context/action. Owner-agent
  requests must include a non-empty `reason`, and owner-agent apply is rejected
  until the service has recorded a matching dry-run. Terminal-agent read bearer
  credentials stay read-only and cannot apply product config. The service
  response is redacted and the route rejects nested runtime or secret targets
  that differ from the authorized top-level context/instance. It fails closed
  when secret writes are requested without the Launchplane master encryption key
  in the service runtime or when no active runtime key-safety policy allows the
  requested binding. When apply changes runtime-environment keys for a tracked
  Dokploy target, the response includes a required `live_target_runtime_apply`
  `next_actions` item. Treat product-config apply as a record mutation only
  until that next action has been dry-run and applied through
  `/v1/live-target-runtime/apply`; redeploying the same app image does not sync
  the live Dokploy target environment.
- The operator UI uses the same service route. It requires a successful dry-run
  result before enabling apply, clears rendered secret input values after each
  submit, and shows only key/action/count metadata from Launchplane responses.
- `environments unset` removes named keys from a DB-backed runtime-environment
  record without reading or printing plaintext values. It requires
  `--allow-direct-db-mutation` and is intended only for explicit local/bootstrap
  repair.
- `environments delete-record --dry-run|--apply` deletes a whole mistaken
  runtime-environment record for `global`, `context`, or `instance` scope. The
  dry-run and apply responses include record identity, source label, update
  timestamp, key names, key count, actor, and delete-event metadata only. Apply
  requires `--allow-direct-db-mutation` and refuses records that can affect a
  tracked Dokploy target unless `--allow-tracked-target` is provided. Apply also
  fails closed if the target record changes after the command reads it; re-run
  the command after reviewing the current record.
- `environments relabel` updates runtime-environment record source metadata
  without reading or printing plaintext values. It requires
  `--allow-direct-db-mutation` and is intended only for explicit
  local/bootstrap repair.
- `environments list` shows DB-backed runtime-environment record metadata and
  keys without echoing plaintext values.
- `environments resolve` reads the control-plane-owned runtime environment
  contract for a context and instance. Output redacts secret-shaped keys by
  default; use `--include-secret-values` only in a trusted operator shell.
- `POST /v1/live-target-runtime/apply` is the deployed service path for shared
  and production live changes. It resolves the DB-backed runtime environment
  and managed secret overlay for a tracked Dokploy target, compares it against
  the live target env by key, and can apply those keys without requiring an
  artifact manifest. It preserves unrelated live env keys, verifies persistence
  by key/count metadata only, and never prints plaintext env or secret values.
  Use `mode: "dry-run"` first, confirm the returned `changed_keys` are expected,
  then use `mode: "apply"` through an authorized workflow or operator API
  caller. The `live-target-runtime.yml` workflow wraps this route with the
  shared Launchplane request action and uploads sanitized request/response
  artifacts.
- TOML/env files are not runtime import surfaces; use DB-native
  runtime-environment records and managed secrets instead.
- Product repos and GitHub issues must not contain product secret values. Put
  the JSON bundle on an operator-controlled machine or inside the hosted
  Launchplane execution context, run `--dry-run` first, then run `--apply` only
  after the key/action summary matches the approved change.

Example product config bundle shape:

```json
{
  "schema_version": 1,
  "product": "sellyouroutboard",
  "context": "sellyouroutboard",
  "instance": "prod",
  "runtime_env": {
    "CONTACT_EMAIL_MODE": "smtp",
    "CONTACT_FROM_EMAIL": "owner@example.com"
  },
  "secrets": [
    {
      "name": "smtp-password",
      "binding_key": "SMTP_PASSWORD",
      "value": "operator-supplied-secret"
    }
  ]
}
```

`runtime_env` values are non-secret scalar values. `secrets` default to the
`runtime_environment` integration and the top-level context/instance, which
makes them available as managed runtime environment overlays. Secret scope
routes must be compatible: `global` has no context or instance, `context` has
context only, and `context_instance` has both context and instance.

- `environments show-live-target` reads the live Dokploy target payload for a
  tracked route and reports whether the target is ready for artifact-backed
  split-repo execution.
- Shared and production live target runtime changes use
  `POST /v1/live-target-runtime/apply` or the `live-target-runtime.yml` workflow
  instead of a local checkout command.
- `ship execute` and `promote execute` can take an explicit `--env-file` overlay
  for the compose post-deploy update path.
- The post-deploy overlay supports only `ODOO_DB_NAME`, `ODOO_FILESTORE_PATH`,
  and `ODOO_DATA_WORKFLOW_LOCK_FILE`.
- When multiple healthcheck URLs are resolved for a lane, Launchplane treats
  them as alternate verification surfaces and accepts the first `2xx` response
  instead of requiring every URL to succeed.

## Odoo Instance Override Contracts

- `POST /v1/drivers/odoo/post-deploy` is the first Launchplane-owned Odoo
  driver route. It executes the remote compose post-deploy data-workflow runner
  for a stable Odoo target and applies DB-backed instance override records when
  the requested phase matches `apply_on`. Routine post-deploy runs use the
  devkit post-deploy maintenance mode: update addons, apply typed Launchplane
  settings and website bootstrap payloads, normalize the configured admin user,
  and re-provision derived Odoo service-user API keys. Full database
  sanitization such as disabling mail servers and cron remains tied to explicit
  restore/bootstrap workflows, not ordinary prod image deploys.
- Reusable Odoo GitHub workflows resolve the Launchplane product before calling
  driver routes. Callers may pass `product` explicitly; otherwise the default is
  `odoo-tenant-${context}` after normalizing underscores to dashes, so context
  `cm_website` resolves to product `odoo-tenant-cm-website`. Post-deploy
  workflow outputs include `website_bootstrap_included` so callers can prove
  whether the typed website bootstrap payload was rendered into the remote data
  workflow request. These reusable workflow jobs use GitHub-hosted runners so
  tenant repositories do not need direct access to Launchplane self-hosted
  runners; privileged provider mutations still execute inside the deployed
  Launchplane service.
- `POST /v1/drivers/odoo/prod-rollback` rolls a prod-named Odoo lane back to
  the DB-backed `testing` release tuple for the same context. The driver owns
  rollback intent and promotion-record annotation, but the provider mutation runs
  through the stable target replacement executor so deploy, runtime identity,
  Odoo post-deploy maintenance, canonical/logo verification, deployment, and
  release-tuple evidence stay on the canonical stable path. After the delegated
  replacement passes, rollback refreshes prod inventory with rollback provenance
  and annotates the current prod promotion record.
- `POST /v1/drivers/odoo/prod-backup-gate` captures the DB and filestore backup
  evidence required before Odoo prod promotion. It resolves `ODOO_DB_NAME`,
  `ODOO_FILESTORE_PATH`, and `ODOO_BACKUP_ROOT` from DB-backed runtime
  environment records, runs a Dokploy schedule against the compose lane, stops
  the web service while capturing, and writes the backup-gate record only after
  the capture succeeds.
- Odoo rollback is image/release-tuple rollback, not VM snapshot rollback. Do not
  invent artifact ids, source commits, backup gates, or env-file overlays to make
  a rollback proceed; write or import the real Launchplane records first.
- `odoo-overrides put-config-param --allow-direct-db-mutation` writes a typed
  Odoo `ir.config_parameter` override for a context and instance. This direct
  local DB path is explicit local/bootstrap repair only; routine shared/live
  changes should use the trusted service route or operator workflow.
- For shared/live targets, use the trusted `Odoo Config Parameter Override`
  workflow instead of local CLI writes. It calls
  `POST /v1/drivers/odoo/config-parameter-override` with GitHub Actions OIDC and
  requires explicit product, context, instance, key, and value inputs instead of
  workflow-local product topology defaults. The current workflow only exposes
  the non-secret `web.base.url` key; product/context/instance authority is
  enforced by service authz and DB-backed product/runtime records.
  Service-written `web.base.url` records are always marked for `deploy` and
  `promotion` application so Odoo post-deploy and stable-bootstrap drivers can
  apply the canonical URL before verification.
- `odoo-overrides put-addon-setting --allow-direct-db-mutation` writes
  addon-shaped Odoo override intent such as Authentik or Shopify settings for a
  context and instance. Use it only for explicit local/bootstrap repair.
- `odoo-overrides put-website-bootstrap --allow-direct-db-mutation` writes typed
  website-bootstrap override intent for explicit local/bootstrap repair only.
- Secret-shaped override names, including `*_TOKEN`, `*_PASSWORD`, and
  `*_KEY`, must use `--secret-binding-id`; plaintext secret writes are rejected.
- `odoo-overrides list` and `odoo-overrides show` return keys, counts, source
  labels, and timestamps only. They do not echo literal values or managed secret
  binding ids.
- `odoo-overrides migrate-secret-transport --apply` and `odoo-overrides
  mark-apply` require `--allow-direct-db-mutation` before they persist local DB
  changes. `migrate-secret-transport` dry-run, `odoo-overrides list`, and
  `odoo-overrides show` stay read-only inspection paths.
- Compose post-deploy updates consume deploy-phase overrides from these records
  and pass them to Odoo as one typed payload env var. Deploy-phase payloads are
  persisted to the Dokploy compose target environment before the web container
  is redeployed, and the same payload is passed to the Odoo data-workflow
  runner for post-deploy maintenance.
- Odoo stable target replacement also merges the required Launchplane-managed
  operational modules into `ODOO_INSTALL_MODULES` before redeploying the web
  container. Artifact inputs or base images make addon files available, but the
  target env install list is what activates modules such as
  `launchplane_settings` and `disable_odoo_online` in already-initialized
  databases.
- When a deploy-phase payload is expected, Launchplane also persists generic
  runtime assertion flags that tell the Odoo runtime to fail closed if managed
  instance overrides or website bootstrap data are missing. Launchplane re-reads
  the provider target environment after writing it and fails the operation if
  the typed payload or assertion flags did not persist.
- Target replacement requests with `data_source_mode="upstream_restore"` use the
  guarded post-deploy schedule in destructive restore mode after image deploy so
  the devkit restore path performs restore sanitization, website bootstrap,
  admin normalization, and service-user API-key replacement before readiness
  verification.
- Launchplane passes one typed payload to the Odoo settings apply path; legacy
  `ENV_OVERRIDE_*` values are migration input only, not the deploy-time
  settings contract.
- Secret-backed overrides are still not rendered into schedule scripts as
  plaintext. The payload references the already-present neutral
  `ODOO_OVERRIDE_SECRET__*` script-runner environment key for each managed
  secret binding, and the workflow asserts those keys before Odoo starts.
- This keeps record authority in Launchplane while moving Odoo toward the
  typed payload contract. The remaining legacy `ENV_OVERRIDE_*` inputs are now
  compatibility-only and can be deleted once the DB-backed override records are
  fully migrated.
- `odoo-devkit` remains the local runtime/workspace surface. Launchplane driver
  routes should not be inserted into the local PyCharm or local container loop;
  use them only for remote stable lanes and promotion/deploy evidence.

## Odoo Rollback And Re-Promote Waterfall

- Confirm Launchplane health reports `storage_backend=postgres`.
- Confirm the target context has DB-backed artifact manifests, `testing` and
  `prod` release tuples, Dokploy target records, target-id records, and current
  prod inventory.
- For Odoo artifacts, the stored artifact manifest carries `odoo_install_modules`.
  Stable target replacement merges that list into `ODOO_INSTALL_MODULES` with
  Launchplane's required safety modules before deploying the target.
- For the first harmless drill, call the Odoo prod rollback driver with no
  explicit artifact id. The driver selects the current `testing` release tuple
  for that context and fails closed if the tuple or artifact manifest is missing.
- For a real rollback after `testing` has advanced, call the same driver with
  an explicit DB-backed artifact id for the previous known-good prod artifact.
  The driver reads the artifact manifest directly from Launchplane records and
  writes rollback evidence with an `artifact:<artifact_id>` source marker.
- A passing rollback delegates deployment and prod release-tuple writes to stable
  target replacement, then writes inventory rollback provenance, promotion
  rollback, and rollback-health evidence. Verify the target
  `/launchplane/health` endpoint and `inventory status` before taking another
  action.
- A real destructive rollback drill requires a second known-good artifact
  manifest. Do not synthesize artifact ids or source SHAs to create one.
- A re-promote drill should use the normal prod promotion path with a fresh
  backup gate for the current prod-named lane. Do not reuse old bootstrap backup
  gates as authorization for a new re-promote.

Artifact handoff example:

```bash
uv run launchplane odoo-artifacts publish \
  --context example \
  --instance testing \
  --manifest ../product-repo/workspace.toml \
  --devkit-root ../devkit \
  --image-repository ghcr.io/example/product \
  --image-tag example-20260416-deadbeef
```

The Odoo artifact publish driver is the control-plane-owned handoff. It
resolves the DB-backed runtime environment and managed secrets in Launchplane,
derives product-profile publish metadata such as preview slug, image repository,
and image tag, passes the runtime payload to `odoo-devkit` as a one-shot runtime
payload for the publish subprocess, validates the returned artifact belongs to
the requested context, and writes the artifact manifest back to Launchplane
records. Do not point a local devkit checkout directly at the live Launchplane
database or recreate runtime env files to publish artifacts.

## Launchplane Preview Operations

Launchplane commands operate on durable preview, generation, and enablement records.
The current command group supports:

- inventory and detail reads: `list`, `show`, `history`, `show-tenant`
- static rendering: `render-status-page`, `render-index-page`,
  `render-policy-page`, `render-site`
- direct record writes: `write-preview`, `write-generation`,
  `write-enablement`
- external evidence ingest: `write-from-generation`, `write-destroyed`
- lifecycle transitions: `request-generation`, `mark-generation-ready`,
  `mark-generation-failed`, `destroy-preview`
- PR/webhook ingest: `ingest-pr-event`, `ingest-github-webhook`
- captured delivery replay: `replay-github-webhook`,
  `build-github-webhook-replay-envelope`

`show-tenant`, `render-index-page`, and `render-site` now resolve stable-lane
baseline tuples from Launchplane's DB-backed release-tuple records. Cockpit and
local renders should run with `LAUNCHPLANE_DATABASE_URL` pointed at the same
shared store that owns the current stable-lane tuple state.

Preview mutation, ingest, replay, and lifecycle transition commands require
`--database-url` or `LAUNCHPLANE_DATABASE_URL` plus
`--allow-direct-db-mutation` for DB-backed execution. Offline JSON writes are
local rehearsals only and must opt in with `--local-rehearsal`; read and render
commands may still inspect a local `--state-dir`.

Any exported release-tuple catalog is seed/reference material now, not live
runtime authority. Pull requests flow through Launchplane preview records
instead of a tracked long-lived `dev` tuple lane.

Odoo PR previews use the Odoo isolated compose planner/apply routes over the
same shared preview lifecycle records. Product workflows call
`POST /v1/drivers/odoo/preview-apply-inputs` with product, PR, source, and
manifest facts, then call `POST /v1/drivers/odoo/preview-apply` with the ready
dry-run plan for both refresh and destroy. Launchplane resolves the Odoo product
profile preview context, runtime bindings, template compose, public preview URL,
and provider operations inside the service boundary. Odoo-specific
`preview-refresh` and `preview-destroy` compatibility routes are retired.

Stage-MVP Odoo previews may point at a Dokploy `compose` template lane only when
the product profile uses `driver_id="odoo"` and `preview.data_transport_mode` is
`bootstrap`. In that mode Launchplane reuses the configured compose target,
renders the Odoo raw compose file for the requested immutable image, overlays
runtime-environment records when present, requires the Odoo raw-compose safety
env keys already present on the live target, including non-default Odoo master
and admin password values, applies profile-owned preview override env such as
`ODOO_INSTALL_MODULES`, applies preview URL env keys, deploys the compose
target, and records the requested PR URL as the preview generation. This is
intentionally not generic compose preview support:
generic-web application previews still require application template lanes, and
stable deploy/promotion routes do not inherit Odoo's compose preview behavior.
Inventory and destroy for the staged compose MVP inspect compose-attached domains
whose hostnames match the product preview slug template. Destroy deletes only the
matching preview domain and must not delete the shared compose target or stable
hostnames.

The long-term Odoo preview target is isolated per-PR runtime state, either by a
provider-supported compose clone/create/delete path or by a dedicated
application-backed template if Odoo can be safely reduced to an application
shape. Until that follow-up lands, CM previews are a single active staged target
behind pre-existing DNS/nginx routing and are intended to make the client-visible
Odoo system usable before full ephemeral preview infrastructure exists.

For stable Odoo target replacement planning, use the read-only dry-run command
before considering any provider mutation:

```sh
uv run launchplane odoo-targets replacement-plan \
  --database-url "$LAUNCHPLANE_DATABASE_URL" \
  --product odoo-tenant-cm \
  --instance testing
```

For live shared targets, prefer the trusted workflow
`Odoo Target Replacement Plan`. It calls the deployed Launchplane service route
`POST /v1/drivers/odoo/target-replacement-plan` with GitHub Actions OIDC so the
service resolves DB-backed records and managed Dokploy secrets inside the normal
runtime boundary. The local CLI form is for operator debugging from an already
trusted runtime with `LAUNCHPLANE_DATABASE_URL` configured.

The plan reads the product profile, Launchplane Dokploy target/id records,
current inventory, live Dokploy target payload, domains, volume env keys, latest
deployment, and expected runtime identity. It does not create, delete, deploy,
or change routes.

For an already-tracked Dokploy compose target, use `Dokploy Target Setup` with
`operation=reconcile-compose-domain` to reconcile the provider domain route
without creating or adopting the target again. The service reads the
Launchplane-owned Dokploy target/id records, requires the target to be a compose
target, and applies each requested domain to Dokploy's `web` service on the
requested runtime port. Dry-run mode reads the tracked records and reports the
planned domain/port tuple without calling Dokploy.

Use `operation=prune-compose-domain` only to remove stale provider domain routes
from an already-tracked Dokploy compose target. The service reads the tracked
target/id records, requires a compose target, looks up matching Dokploy domain
records by compose id, reports matched domain ids in dry-run mode, and deletes
only the explicitly requested matching domain ids in apply mode. Apply also
removes those hosts from the tracked target record when present.

When a plan is `ready`, the trusted `Odoo Target Replacement Apply` workflow can
call `POST /v1/drivers/odoo/target-replacement-apply` for the guarded
`recreate-in-place` path. The service creates a durable operation record and
returns immediately; the workflow polls
`GET /v1/drivers/odoo/target-replacement/operations/{operation_id}` until the
operation status is `pass` or `fail`, then uploads the final operation payload as
the workflow artifact. `Idempotency-Key` is required: a repeated request with the
same key from the same caller identity returns the existing operation, while a
different key for the same product/context/instance is rejected while a
`pending` or `running` operation is active. Storage owns that active-lane
reservation so the worker starts only after the lane is claimed; abandoned
filesystem reservations recover after a bounded settle window if the owner or
owner record never appears. The first apply surface is testing-only and keeps
the existing compose
target, explicit Odoo volume env keys, and expected hostnames; the operation
worker re-syncs the Launchplane-rendered compose source, reconciles each expected
Dokploy compose domain route to the `web` service on the product runtime port,
renders the matching HTTP and HTTPS Traefik router labels into the raw compose,
defaults the web process to the devkit startup wrapper so public runtimes write
their generated config, initialize the selected database, and pin HTTP DB
selection to `ODOO_DB_NAME`, injects the platform context/instance and runtime
identity breadcrumb, triggers Dokploy deploy, runs Odoo post-deploy, verifies
health/canonical/logo, and
writes deployment plus inventory records. The explicit raw-compose routers keep
the runtime reachable even when Dokploy stores the compose domain record
separately from the raw source update. By default it deploys the artifact
already recorded in current inventory. Operators may supply both `artifact_id`
and `source_git_ref` to deploy
a newly published stored artifact before it has become inventory; the service
refuses mismatches against the stored artifact manifest. Do not manually delete
canonical stable targets as a replacement shortcut; add the missing Launchplane
apply coverage first, then use the service-backed workflow.

Runtime identity is a driver-owned breadcrumb, not tenant config. Launchplane
injects `LAUNCHPLANE_RUNTIME_IDENTITY_JSON`, `LAUNCHPLANE_DEPLOYMENT_RECORD_ID`,
`LAUNCHPLANE_ARTIFACT_ID`, and `LAUNCHPLANE_SOURCE_GIT_REF` into supported
Dokploy targets. Product health endpoints may echo the JSON payload as
`runtime_identity`, `launchplane_runtime_identity`, `launchplaneRuntimeIdentity`,
or `LAUNCHPLANE_RUNTIME_IDENTITY_JSON`; Launchplane records whether the observed
payload matches, mismatches, is missing, or cannot be parsed. Missing observed
identity is evidence for adoption work, not a reason to hardcode product-specific
health or logo probe URLs. Runtime identity mismatches and malformed identity
evidence remain hard public-ingress failures because they indicate the public
route may be serving a different artifact than Launchplane expects. Missing or
unverifiable identity is advisory unless the lane explicitly requires runtime
identity after adopting the health echo contract.

Target replacement only reconciles the provider target and runtime envelope. If
an intentionally empty CM testing lane needs its Odoo database and filestore
rebuilt, use the trusted `Odoo Stable Bootstrap` workflow instead of repairing
state in Dokploy or a local checkout. The workflow calls
`POST /v1/drivers/odoo/stable-bootstrap` and requires the lane's product-profile
`odoo_stable_bootstrap` policy to be enabled. That policy carries the destructive
confirmation phrase, issue-backed approval URL, expected Dokploy target name,
expected domain set, data source mode, and required verification checks. The
approval issue is the operator signal to encode prelaunch/resettable policy; it
should close after the policy lands, while launch or cutover retirement belongs
in a separate follow-up. The service proves the request, product lane, stored
target record, and target domains before contacting Dokploy. The Dokploy runner
also refuses if the live compose name no longer matches the expected target. After
proof passes, Launchplane runs the existing compose-local devkit data workflow in
`--bootstrap` mode through a dedicated manual Dokploy schedule, then runs Odoo
post-deploy and verifies health, canonical URL, and logo routes before writing
deployment and inventory evidence. Bootstrap evidence keeps the destructive data
workflow status separate from public readiness: if the bootstrap runs but
post-deploy or verification fails, inventory keeps its prior
current deployment pointer and records the failed attempt in `bootstrap_record_id`.
Do not use this path for prod until Launchplane has explicit backup/restore
policy evidence for that lane.

The service route creates a durable Odoo stable-bootstrap operation and returns
an operation id immediately. The GitHub workflow polls
`GET /v1/drivers/odoo/stable-bootstrap/operations/{operation_id}` until the
operation status is `pass` or `fail`, then uploads the final operation payload as
the workflow artifact. `Idempotency-Key` is required: a repeated request with the
same key returns the existing operation, while a different key for the same
product/context/instance is rejected while a `pending` or `running` operation is
active. The operation record stores the request, status, phase,
deployment-record linkage when known, final bootstrap result, and any terminal
error message so operators can inspect progress after the original HTTP request
has ended.

For OPW prelaunch lanes that should be rebuilt from the current upstream
non-Docker source, encode `odoo_prelaunch_rebuild` on the product profile lane
with `data_source_mode="upstream_restore"`, the approval issue URL, expected
target name, expected domains, and the confirmation phrase. Target replacement
plan/apply requests must set the matching `data_source_mode`, confirmation, and
`allow_empty_data=true` before Launchplane treats absent Odoo data/log/database
volume keys as intentional. Unknown lanes, mismatched target proof, missing issue
approval, or plain `prod` names still fail closed.
During OPW prelaunch, the prod proof follows the temporary Dokploy host
`opw-prod.shinycomputers.com`; update the issue-backed policy to the final
public hostname only after the live target actually exposes that domain.

`launchplane-previews write-from-generation` and `launchplane-previews write-destroyed`
are local preview-evidence ingest adapters that mirror the service ingress
payload shape. Generic-web, Odoo, and VeriReel preview runtime now flow through
Launchplane drivers: product repos send PR/image intent, Launchplane derives the
live preview URL from `LAUNCHPLANE_PREVIEW_BASE_URL`, and evidence stores that
returned URL with generation status and cleanup outcome.

Launchplane now owns the preview lifecycle planning boundary. The scheduled
Launchplane `Preview Lifecycle` workflow calls `POST /v1/previews/lifecycle-sweep`;
the service derives the participating products from product profiles where
`preview.enabled=true`, refreshes provider inventory, discovers desired preview
anchors from GitHub PR label state, writes lifecycle plans, and records cleanup
results. Cleanup defaults to report-only and destructive provider cleanup still
requires explicit `apply=true` from an authorized GitHub Actions workflow.
Preview-disabled products are excluded from the sweep. PR feedback goes through
`POST /v1/previews/pr-feedback`; Launchplane renders and upserts the anchored PR
comment when runtime GitHub credentials are available, then records delivery
status. Refresh-capable workflows can publish neutral pending feedback before
preview publish/provision/verify outcomes are known, then replace it with ready
or failed feedback after the actual result. Product repos remain thin adapters
for labels, artifact build facts, and product-specific health/config hints.

Preview PR feedback delivery failures are operator-visible when a preview PR
feedback notification policy is configured. Missing context-scoped runtime
GitHub credentials record `delivery_status="skipped"` on the feedback record and
emit a `delivery_skipped` notification attempt; GitHub API failures record
`delivery_status="failed"` and emit `delivery_failed`. Discord destinations
resolve webhook URLs from managed secrets scoped to `launchplane` /
`preview-feedback`, and attempt records live under
`launchplane_preview_pr_feedback_notification_attempts` for DB-backed stores or
`state/launchplane_preview_pr_feedback_notification_attempts/` for filesystem
stores. Repair missing credentials by configuring the canonical product preview
context's managed-secret-backed `GITHUB_TOKEN` in Launchplane runtime records;
do not add repo-local defaults or service-host env fallbacks for product GitHub
tokens.

### VeriReel Preview Evidence Handoff

VeriReel already computes the route, PR slug, image tags, and workflow run URL
inside `.github/workflows/preview-control-plane.yml` and
`.github/workflows/preview-cleanup.yml`. The scheduled orphan backstop in
`.github/workflows/preview-janitor.yml` should use the same Launchplane destroy
and evidence contract rather than keeping a second repo-local teardown path.
Launchplane's reusable preview lifecycle contract is OIDC-authenticated HTTP into
Launchplane. The local CLI examples below pin the payload shape for local
rehearsal and repair; shared product workflows should use the service ingress.

For a successful or failed preview refresh, emit two JSON payloads and hand
them to Launchplane's preview-generation evidence ingress. The current local
rehearsal adapter is `launchplane-previews write-from-generation`:

```json
{
  "context": "verireel-testing",
  "anchor_repo": "verireel",
  "anchor_pr_number": 123,
  "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
  "canonical_url": "https://pr-123.preview.example.com",
  "state": "active",
  "updated_at": "2026-04-16T08:10:00Z",
  "eligible_at": "2026-04-16T08:10:00Z"
}
```

```json
{
  "context": "verireel-testing",
  "anchor_repo": "verireel",
  "anchor_pr_number": 123,
  "anchor_pr_url": "https://github.com/example-org/verireel/pull/123",
  "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
  "state": "ready",
  "requested_reason": "external_preview_refresh",
  "requested_at": "2026-04-16T08:02:00Z",
  "ready_at": "2026-04-16T08:10:00Z",
  "finished_at": "2026-04-16T08:10:00Z",
  "resolved_manifest_fingerprint": "verireel-preview-manifest-pr-123-6b3c9d7",
  "artifact_id": "ghcr.io/example-org/verireel-app:pr-123-6b3c9d7",
  "deploy_status": "pass",
  "verify_status": "pass",
  "overall_health_status": "pass"
}
```

That evidence maps directly from the VeriReel workflow outputs:

- `preview_url` -> `preview.canonical_url`
- `pr_number` -> `anchor_pr_number`
- `pr_sha` -> `anchor_head_sha`
- `run_url` should be retained in the calling workflow logs or wrapper script
  alongside the payload write for traceability
- immutable preview image tag or digest -> `artifact_id`

For cleanup, emit the destroy payload and hand it to
Launchplane's preview-destroyed evidence ingress once the preview teardown has
actually completed. The current local rehearsal adapter is
`launchplane-previews write-destroyed`:

```json
{
  "context": "verireel-testing",
  "anchor_repo": "verireel",
  "anchor_pr_number": 123,
  "destroyed_at": "2026-04-16T09:04:00Z",
  "destroy_reason": "external_preview_cleanup_completed"
}
```

That cleanup payload should be written only after the preview URL, Dokploy app,
and backing database teardown has succeeded. If cleanup fails, Launchplane should
keep the preview record live and instead receive a failed generation or workflow
signal later, rather than a premature destroyed transition.

The scheduled janitor backstop uses the same payload shape with
`destroy_reason: external_preview_janitor_cleanup_completed` so its retries stay
separate from the pull-request cleanup workflow's idempotency key.

Use `release-tuples export-catalog --state-dir <state>` to render those minted
state records as catalog TOML when an operator is ready to review and
materialize a new tracked baseline.

GitHub PR feedback uses one Launchplane-owned marker comment per PR. The
comment is a review surface over durable Launchplane records: preview URL/state,
manifest and baseline tuple, source inputs, artifact identity when present,
health status, next action, and apply outcome.

Launchplane treats product PRs as preview anchors. Companion, infra, and tooling
repos should remain source inputs only unless a product explicitly maps them to
a preview context.

Preview enablement records retain the anchor PR head SHA plus any resolved
companion PR head SHA snapshots from ingest. Tenant renders use those stored
snapshots for preview request recipes and keep unresolved companion requests
blocked instead of guessing source inputs.

## Launchplane Boundary

- GitHub remains the engineering workflow surface: issues, branches, pull
  requests, labels, checks, PR comments, releases, and CI execution.
- `launchplane` owns the durable operational truth behind those
  workflows: artifacts, release tuples, previews, deployments, promotions,
  backup gates, and inventory.
- Launchplane should converge on a separate long-running service boundary even while
  the first implementation still lives inside this repo.
- The stable Launchplane contract should be service ingress plus Launchplane-owned
  drivers, not repo-local shell wrappers around file writes.
