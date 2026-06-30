---
title: Records
---

## Storage Policy

- Persist local-dev records as JSON files in a local state directory.
- Use Postgres-backed Launchplane core-record tables for shared-service ingress
  when Launchplane is running with `LAUNCHPLANE_DATABASE_URL` or
  `launchplane service serve --database-url ...`.
- Use Postgres-backed Launchplane secret tables for managed secret records when
  Launchplane is running with `LAUNCHPLANE_DATABASE_URL` and
  `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`.
- Manage shared-service Postgres schema changes with Alembic migrations. The
  current baseline revision captures the SQLAlchemy ORM schema that earlier
  deployments created through `create_all`; future GUI/write-flow schema changes
  need explicit migrations instead of relying on implicit table creation.
- Keep git history separate from operational history.
- Favor append-style writes for promotion records.

## Schema Migrations

Launchplane uses SQLAlchemy ORM models as the persistence boundary and Alembic as
the versioned migration mechanism for shared-service Postgres databases. Hosted
service startup runs Alembic to the checked-in head revision before starting the
HTTP service; schema or payload changes must therefore land as explicit Alembic
revisions. Runtime code can still call `ensure_schema()` for compatibility, but
it only creates tables for local SQLite/test databases. For shared-service
database URLs, `ensure_schema()` verifies that Alembic has already created the
required tables and columns and fails closed when migrations are missing.

For a fresh database, apply the current schema with:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run alembic upgrade head
```

For an existing Launchplane database that already has the tables created by the
pre-migration `create_all` path, adopt the baseline by stamping the database at
the current revision only after the automated schema adoption verifier passes.
The verifier inspects existing Launchplane-owned tables and fails closed when a
live table is missing an ORM-managed column or has an unexpected column. A
failure means the operator must stop and reconcile the schema before stamping;
do not hand-edit the Alembic version table or skip the check.

The hosted startup wrapper runs the verifier before stamping and then applies
migrations:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... scripts/start-launchplane-service.sh
```

For a manual adoption rehearsal, run the verifier directly. It prints the
revision to stamp when adoption is safe and exits non-zero when the live schema
does not match the ORM-managed table shape:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run python -m control_plane.storage.schema_adoption
```

After a passing verifier result, stamp the printed revision and upgrade:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run alembic stamp <printed-revision>
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run alembic upgrade head
```

JSONB `payload` columns remain durable evidence envelopes and original typed
payload snapshots. Fields that the GUI or drivers need to filter, order, join,
authorize, constrain, display regularly, or act on should be promoted into ORM
columns/tables and migrated explicitly while keeping the payload copy as
historical evidence.

## ORM Query Boundary

Launchplane's Postgres storage layer should expose GUI and driver reads through
typed repository methods, not through service/UI code that reaches into JSONB
payloads. The first GUI-facing repository projections are:

- `LaunchplaneLaneSummary`: lane inventory, release tuple, latest deployment,
  latest promotion, latest backup gate, provider-neutral deployed target
  metadata, runtime environment records, Odoo override metadata, and secret
  binding status.
- `LaunchplanePreviewSummary`: preview identity plus recent/latest generation
  state.

These summaries are read models, not new durable record families. They compose
existing ORM rows and contract payloads behind the storage boundary so the next
driver descriptor and GUI slices can consume a stable API shape.

- `ProtectedArtifactSet`: registry-cleanup read model built from current
  environment inventory, release tuples, active preview generations, ready
  preview feedback, product profiles, and artifact manifests. It is not a new
  durable record family; it is the Launchplane-owned liveness projection that
  cleanup consumers must load before deleting registry artifacts. Missing
  manifests for live inventory, release, or preview artifacts are returned as
  warnings while the artifact id remains protected, so cleanup stays fail-closed
  instead of treating unresolved live images as deletable.

## Field Promotion Audit

The current ORM tables already model the first layer of queryable operational
state. Use this audit when deciding whether a new GUI or driver field belongs in
an ORM column/table or remains only in the evidence payload.

- Artifact manifest: modeled fields are `artifact_id`, `source_commit`,
  `image_repository`, and `image_digest`. The payload also carries typed Odoo
  build provenance for base images and build tools such as `odoo-devkit`.
  Source input details, addon selectors, support-repo provenance, and provider
  evidence stay payload-only until they become normal query or action fields.
- Backup gate: modeled fields are `record_id`, `context`, `instance`,
  `created_at`, and `status`. Concrete backup paths and provider-specific backup
  evidence stay payload-only.
- Deployment: modeled fields are `record_id`, `context`, `instance`,
  `artifact_id`, `source_git_ref`, deploy timestamps, and an optional structured
  expected runtime identity. Destination health evidence may also record an
  observed runtime identity and classify it as `match`, `mismatch`, `missing`,
  `unverifiable`, or `unchecked`. Resolved provider evidence, health detail, and
  post-deploy product facts stay payload-only.
- Promotion: modeled fields are `record_id`, `context`, `from_instance`,
  `to_instance`, `artifact_id`, and deploy timestamps. Rollback annotations,
  backup evidence detail, and provider health envelopes stay payload-only.
- Inventory: modeled fields are `context`, `instance`, `artifact_id`,
  `source_git_ref`, `updated_at`, linked deployment/promotion ids, and the
  expected runtime identity copied from the current deployment record. Inventory
  carries the destination health runtime-identity evidence from that deployment
  so read models can show whether the live app reported the expected breadcrumb.
  Full deploy evidence and product-specific live facts stay payload-only.
- Preview: modeled fields are `preview_id`, `context`, `anchor_repo`,
  `anchor_pr_number`, `state`, and `updated_at`. Canonical URLs, lifecycle
  notes, and provider route evidence stay payload-only.
- Preview generation: modeled fields are `generation_id`, `preview_id`,
  `sequence`, `state`, `requested_at`, `finished_at`, and `artifact_id`. Source
  map, PR summaries, deploy/verify evidence, and failure details stay
  payload-only.
- Release tuple: modeled fields are `context`, `channel`, `tuple_id`,
  `artifact_id`, `minted_at`, and `provenance`. Repo SHA maps and source
  provenance details stay payload-only.
- Authz policy: modeled fields are `record_id`, `status`, `source`,
  `updated_at`, `policy_sha256`, and optional service-owned `audit` metadata.
  The parsed GitHub Actions and human grant policy stays payload-only until
  Launchplane needs per-rule filtering or browser-side policy editing. Authz
  grant audit metadata records the operator identity, reason, related issue,
  previous/new policy ids and shas, trace id, mode, and requested grant details;
  service responses redact that requested-grant detail to counts and scope
  summaries. During a future OpenFGA migration, these DB-backed policy records
  remain the source evidence for dry-run tuple proposals and parity checks.
  After a proven cutover, records should store import/audit/model-version
  evidence rather than remain a second live authorization source.
- Merge train stack collapse plan: modeled fields are `record_id`, `status`,
  `source`, `updated_at`, `repository`, `base_branch`, `collapse_id`,
  `root_pull_request_number`, and `plan_status`. The payload carries the typed
  stack entries, expected SHAs, planned child-to-parent mutations, policy digest,
  and intent source. The initial intent source is the root PR's merge-train
  enqueue label; no file-backed or hardcoded repository config participates in
  live collapse authority.
- Dokploy target id: modeled fields are `context`, `instance`, `target_id`, and
  `updated_at`. Provider lookup/import evidence stays payload-only.
- Dokploy target: modeled fields are `context`, `instance`, and `updated_at`.
  Provider-specific names, domains, policies, schedule, and app details stay
  payload-only until a provider-neutral target model needs them.
- Deployment records carry provider-neutral deployed target evidence in the
  payload (`provider_id`, `target_category`, `target_id`, and `display_name`).
  Existing Dokploy-shaped `resolved_target` and deploy-mode fields remain
  readable compatibility evidence and are translated into the neutral target
  reference when no explicit provider-neutral target is present.
- Provider target records define the neutral target inventory contract:
  `context`, `instance`, `provider_id`, `target_category`, `target_id`,
  `display_name`, `provider_target_type`, `updated_at`, and payload-only
  provider evidence. DB-backed storage uses `launchplane_provider_targets` as
  the explicit provider-neutral target authority for current reads. Paired
  Dokploy target and target-id records still provide audit/backfill comparison
  material and provider execution configuration, but they no longer synthesize
  steady-state provider-target authority when an explicit row is missing.
- Product onboarding, Dokploy target adoption/creation, product context cutover,
  and tracked Dokploy target metadata commands now dual-write explicit
  provider-target rows when a complete Dokploy target and target-id pair exists.
  The dual-write is identity-only: Dokploy route/runtime execution metadata such
  as domains, health policy, source metadata, env keys, and product policies
  remains in the Dokploy target record.
- Product context audit, cutover, and legacy cleanup responses expose target
  copy/delete summaries under provider-neutral `provider_targets` and
  `provider_target_ids` keys. Dokploy target and target-id records can still be
  the provider-specific source records copied or deleted by those workflows, but
  they are not exposed as Dokploy-named response buckets.
- `uv run launchplane storage provider-target-audit` is the read-only preflight
  for this record family. It compares explicit provider-target rows
  with the neutral projection from paired Dokploy target and target-id records,
  reports missing halves and mismatches, and exits nonzero when unresolved
  blockers would make backfill or authority cutover unsafe.
- `uv run launchplane storage provider-target-backfill` is the seeding path for
  explicit provider-target rows. Dry-run is the default; `--apply`
  writes only complete Dokploy target/id projections that have no conflicting
  physical row. Existing matching physical rows are skipped without churn,
  incomplete pairs are reported but not guessed, and conflicts or unsupported
  provider rows are never overwritten automatically.
- Shared and production backfill uses the deployed service route
  `POST /v1/provider-targets/operations`, normally through the manual
  `Provider Target Operations` workflow. The workflow records per-route audit,
  dry-run, or apply evidence as artifacts and uses DB-backed
  `provider_target.audit` or `provider_target.backfill` authz grants instead of
  local checkout writes.
- Shared ship and promotion request contracts require canonical flat target
  fields (`target_name`, `target_type`, `provider_id`, `target_category`, and
  `provider_target_type`) and reject `target_reference` compatibility input.
  Persisted deployment and promotion evidence still accepts `target_reference`
  while loading historical records, but writes flat compatibility fields. Full
  retirement of evidence compatibility remains blocked on explicit record schema
  migration criteria and evidence that existing shared-service payloads have
  been migrated; mixed neutral and legacy target facts fail closed when they
  disagree.
- Private health endpoint records define Launchplane-owned private monitor URL
  authority for a lane: `endpoint_key`, `product`, `context`, `instance`,
  `url`, `status`, `updated_at`, and payload-only provenance such as
  `source_label`. The service applies them through
  native FastAPI `POST /v1/private-health-endpoints/apply` with
  product/context-scoped `private_health_endpoint.apply` authorization, exact
  apply-mode confirmation, retry-safe idempotency for mutations, private URL
  validation, and cross-scope endpoint-key conflict protection. The service
  reads them through
  `GET /v1/private-health-endpoints/records` with
  `private_health_endpoint.read`. The stored URL must be private/internal;
  product profiles reference it by `private_endpoint_key` and do not own the
  mutable URL value.
- Runtime environment: modeled fields are `scope`, `context`, `instance`, and
  `updated_at`. Individual key/value settings stay payload-only until GUI
  filtering or editing requires a setting table.
- Odoo instance override: modeled fields are `context`, `instance`, and
  `updated_at`. Typed Odoo override payloads stay payload-only until
  cross-driver settings need generic structure.
- Secret: modeled fields are `secret_id`, `scope`, `integration`, `name`,
  `context`, `instance`, `status`, `current_version_id`, and `updated_at`.
  `current_version_id` points to the active secret-value version and is separate
  from the non-secret `encryption_key_id` recorded on encrypted version payloads.
  Version ids, key ids, descriptions, validation detail, and encrypted version
  payloads stay payload-only until rotation or operator views need queryable
  columns.
- Secret binding: modeled fields are `binding_id`, `secret_id`, `integration`,
  `binding_key`, `context`, `instance`, `status`, and `updated_at`. Binding
  implementation details beyond status and lookup stay payload-only.
- Secret audit event: modeled fields are `event_id`, `secret_id`, `event_type`,
  and `recorded_at`. Event categories should cover version writes, rotation or
  re-encryption, disable/retire, bind/unbind, plaintext resolution, reveal
  denial, and key-safety gate evaluation without overloading one event type for
  all secret access. Actor, reason, trace or idempotency metadata, binding ids,
  version ids, encryption key ids, destination class, detail, and finding codes
  stay payload-only until audit filtering needs more columns. Audit payloads
  must never contain plaintext, ciphertext, token prefixes, provider env dumps,
  or request bodies that contain secrets.
- Runner host hygiene audit: modeled fields are `audit_record_key`,
  `host_name`, `action`, `status`, and `mutate`. The payload carries the typed
  request, plan, pre/post hygiene reports, retained warm-builder evidence, and
  operator message. Docker toolchain evidence, host-command output, Docker
  summaries, and rollout notes stay payload-only until they need queryable
  operational views.
- Runner lane registration audit: modeled fields are `audit_record_key`,
  `repository`, `host_name`, `lane_name`, `status`, and `mutate`. The payload
  carries the typed request, registration plan, pre/post runner inventory, and
  operator message. GitHub registration token values are never persisted; token
  metadata and host command evidence stay payload-only until operator views need
  them.
- Ingress route audit: modeled fields are `record_id`, `product`, `context`,
  `mode`, `status`, `provider_host_id`, and `recorded_at`. The payload carries
  the typed requested domains, expected provider host id, dry-run/apply mode,
  plan operations, high-level change categories, trace id, idempotency key, and
  operator reason. Provider payload details and comparison evidence stay
  payload-only until route ownership gets a broader operator UI. Apply requests
  first write a `pending` audit intent before provider mutation and then replace
  it with the final `applied` or `unchanged` outcome.
- Edge endpoint: modeled fields are `endpoint_key`, `provider`, `server_name`,
  `upstream_host`, `upstream_scheme`, `upstream_port`, `status`, and
  `updated_at`. `endpoint_key` and `server_name` are human-facing operator
  identity. `upstream_host` is the provider data-plane value and must be an IP
  address for NPMplus-backed routes so a bad hostname cannot become a runtime
  Nginx startup dependency. Native FastAPI `POST /v1/edge-endpoints/apply`
  writes this Launchplane-owned authority with `edge_endpoint.apply`, exact
  apply-mode confirmation, and retry-safe idempotency for mutations. Product
  repositories must not own provider topology, edge IPs, NPMplus host ids, or
  Dokploy server routing facts.
- Ingress canary route: modeled fields are `canary_key`, `product`, `context`,
  `domain_name`, `expected_host_id`, `edge_endpoint_key`, `certificate_id`,
  `status`, and `updated_at`. The record is Launchplane-owned route authority
  for canary applies; workflows pass the canary key and the service resolves the
  stored domain, provider guard, certificate, and edge endpoint values before
  calling the ingress provider. Native FastAPI
  `POST /v1/ingress/canary-routes/records/apply` writes this authority for
  apply mode and plans it for dry-run mode. Native FastAPI
  `POST /v1/ingress/canary-routes/apply` consumes the stored record, records an
  ingress route audit, and preserves the existing idempotency replay/conflict
  contract.

Promote a payload field into ORM structure when Launchplane needs to filter,
order, join, authorize, constrain, display it regularly, or drive an action from
it. Keep unstable provider envelopes, replay/debug context, and raw evidence in
JSONB until they graduate into normal product behavior.

## Product Profiles

Product profile records are DB-backed Launchplane configuration for product
identity and driver selection. They hold product key, display name, owning repo,
driver id, image repository, runtime port, health path, stable lane bindings,
preview context policy, and the pull-request label that enables previews for the
product. Generic-web preview policy can also name the source template lane,
required template env keys, copied or omitted settings, preview URL/domain env
keys, required provider fields, and the declared data transport mode so
readiness can fail before Launchplane mutates a provider.

Product profiles may also declare expected config requirements for stable lanes:
runtime-environment key names and managed secret binding keys by context and
instance. These requirements are declarative intent for operator readiness
views. Actual configured, missing, disabled, stale, or unsupported status is
derived from runtime-environment records, managed secret binding records, driver
support, and trust metadata; expected config requirements do not store runtime
values, managed secret IDs, secret plaintext, or ciphertext.

Stable lanes declare synthetic monitoring through `health_monitoring.checks[]`.
Each check has a stable name and kind. `public_http` checks use an explicit URL
or the lane `health_url`, or derive one from lane `base_url` plus product
`health_path`. `private_http` checks monitor internal service endpoints without
publishing an ingress route by carrying a `private_endpoint_key` that resolves
through a DB-backed private health endpoint record. Product profiles own what to
monitor, while
`launchplane_private_health_endpoints` owns the mutable private endpoint URL for
a product/context/instance lane. Private endpoint records reject public URLs,
and endpoint-key-backed checks fail closed when the record is missing, disabled,
or scoped to another lane. `provider` checks record provider-health intent and
fail closed until a provider-specific monitor implementation is wired. The
monitor records HTTP reachability, redirect failures, private/internal URL skips
for public checks, and runtime identity comparison when current lane inventory
or deployment evidence provides an expected identity. Check policy may carry an
enabled flag and provider-specific routing details, but alert destinations are
not lane text fields. Public ingress incident notifications are routed through
DB-backed notification policy records. Legacy product-profile payloads that
still contain `alert_issue_url` are tolerated during record reads and stripped
from the migrated health-monitoring shape instead of being treated as runtime
authority.

The product key is the durable workspace identity. For example,
`sellyouroutboard` is the SellYourOutboard product workspace; `testing`, `prod`,
and the preview inventory all appear under that workspace in the operator UI.
The `context` fields on lane and preview profile entries are technical routing
and record lookup identifiers, not user-facing product names. Stable
generic-web lanes should converge on the product context, such as
`sellyouroutboard` for both `testing` and `prod`, so promotion and runtime reads
resolve one product stack. A separate preview context may remain while preview
apps are isolated from stable lane records.

When cleaning up a legacy context such as `sellyouroutboard-testing`, first
copy or reseed only the mutable current-authority records needed by live
resolution: runtime environments, managed secrets and bindings, tracked targets,
tracked target IDs, inventories, and release tuples. After the product profile
points at the canonical context, cleanup can delete legacy runtime environment
records and Dokploy target lookups, and can disable legacy managed secret records
and bindings. It should not delete inventory records, release tuples,
deployments, promotions, backup gates, or preview history; those records are
historical evidence and should continue to describe the route that produced
them. Product profiles retain legacy route names in `historical_contexts` after
cutover so product activity read models can continue to include that preserved
evidence without making the legacy context current authority again.

Before changing a product profile or deleting legacy rows, audit both route
families with:

```bash
uv run launchplane product-profiles audit-context-cutover \
  --product sellyouroutboard \
  --source-context sellyouroutboard-testing \
  --target-context sellyouroutboard
```

The audit reports key names, record ids, counts, target names, and binding
metadata only. It does not print runtime values, managed secret plaintext,
secret ciphertext, or full provider env text.

The same redacted audit is exposed through the Launchplane service at
`GET /v1/product-profiles/{product}/context-cutover-audit` with
`source_context`, `target_context`, and optional `preview_context` query
parameters. The manual `Product Context Cutover Audit` GitHub workflow calls
that service route through GitHub OIDC and uploads the redacted JSON artifact.
After cutover, the source context is historical evidence rather than a current
product boundary, so this pre-cutover audit will reject the legacy context. Use
the `Product Legacy Context Cleanup` workflow in `dry_run=true` mode for
post-cutover SYO evidence, then validate live runtime against the canonical
`sellyouroutboard` testing and prod lanes.
The manual `Product Legacy Context Cleanup` GitHub workflow calls the matching
write route through GitHub OIDC. It defaults to `dry_run=true`, refuses cleanup
while the source context is still product-owned, blocks individual mutable
records without target-context replacements, and preserves historical evidence
rows.

These records replace repo-local Launchplane lifecycle manifests. Product repos
still own their normal app/runtime contract, such as Dockerfile, image publish,
health endpoint, tests, and source/build inputs. Launchplane owns the product
profile that maps those app facts into preview, deploy, promotion, and evidence
behavior.

Simple service products deployed as Dokploy applications use the same product
profile shape. For a bot or worker service with an HTTP bridge or health
endpoint, `runtime_port` is the internal HTTP port, `health_path` names the
product-level health route, and lane `health_url` can point at an internal URL
reachable by Launchplane. Source-backed compose workers may use `runtime_port=0`
and empty product-level image/health fields when any observable health surface is
an explicit lane `health_url`; source-backed workers without an HTTP surface use
that shape only when preview, public ingress monitoring, and provider health
checks are disabled. See
[dokploy-service-deployments.md](dokploy-service-deployments.md) for the
service-specific contract.

The service exposes product profile records through `GET /v1/product-profiles`,
`GET /v1/product-profiles/{product}`, and `POST /v1/product-profiles`. Writes
require the `product_profile.write` action for the target product in the
Launchplane service context; reads use `product_profile.read`.

For initial seed or repair work, operators can write the same DB-backed record
directly with `uv run launchplane product-profiles upsert --database-url ...`.
That command is an operator tool for creating the Launchplane record; it is not
a repo-local manifest and should not become product repo authority.

## Public Ingress Observation Records

Public ingress observations are append-only Launchplane records under
`launchplane_public_ingress_observations`. Each record is keyed by product,
context, instance, and observation time. It stores the checked base and health
URLs, pass/fail/skipped status, failure code, redirect and HTTP evidence,
runtime identity match detail when available, and whether Launchplane delivered
a configured transition notification.

These records are the source for the product environment read model's
`public_ingress` summary. A passing observation is verified evidence, a failing
observation marks the lane stale/unhealthy, and a skipped private URL is treated
as unsupported rather than silently healthy.

## Public Ingress Incident Records

Public ingress incidents are Launchplane-owned lifecycle records under
`launchplane_public_ingress_incidents`. They are derived from public-ingress
observations and keyed by product, context, instance, and incident open time. An
open incident records the first failing observation and the latest failing
observation for that lane. A recovery observation resolves the incident by
recording the resolving observation and resolved timestamp.

Incident records are the source of truth for whether Launchplane currently
considers a lane to be in a public-ingress incident. Notification routing and
delivery are separate record families: observations say what was measured,
incidents say whether there is active operator state, and delivery records say
where Launchplane attempted to notify operators.

## Public Ingress Notification Records

Public ingress notification policy records are DB-backed Launchplane records
under `launchplane_public_ingress_notification_policies`. They select enabled
destinations for incident events by product, context, and instance. The initial
destination drivers are GitHub issues, email, and Discord. Policies store
routing intent and managed secret references only; they must not store Discord
webhook URLs, SMTP passwords, or production destination values as code defaults.

Public ingress notification attempt records are append-only evidence under
`launchplane_public_ingress_notification_attempts`. Each attempt is keyed by the
incident, event, policy, destination, and observation. Attempts record delivered,
skipped, or failed status plus provider-safe external ids or URLs. Delivery
attempts are the idempotency boundary for notifications, while incident records
remain the source of truth for active public-ingress state.

## Every Code Notification Records

Every Code notification policy records are DB-backed Launchplane records under
`launchplane_every_code_notification_policies`. They select enabled
destinations for Every Code operator events, currently `work_request_blocked`,
optionally scoped by repository. Policies store routing intent and managed
secret references only; they must not store Discord webhook URLs or real
operator destination values as source, workflow defaults, or checked-in config.

Every Code notification attempt records are delivery evidence under
`launchplane_every_code_notification_attempts`. Each attempt is keyed by the
work-request id, event, policy, and destination. Attempts record pending,
delivered, skipped, or failed status plus a bounded, provider-safe action or
error. Launchplane writes a pending attempt before calling an external Discord
webhook, then updates it after delivery returns, so retries have durable dispatch
evidence instead of blindly sending duplicate notifications. When a worker
reports a request as `blocked`, Launchplane writes the terminal work request
first and then records each notification attempt so bot-auth or Discord delivery
failures remain inspectable even when no Every Code session starts.

## Preview PR Feedback Notification Records

Preview PR feedback notification policy records are DB-backed Launchplane
records under `launchplane_preview_pr_feedback_notification_policies`. They
select enabled destinations for skipped or failed `/v1/previews/pr-feedback`
delivery by product, context, and repository. Policies store routing intent and
managed secret record ids only; Discord webhook URLs and operator destination
values must stay in managed secrets, not checked-in workflow defaults, examples,
or service-host env.

Preview PR feedback notification attempt records are delivery evidence under
`launchplane_preview_pr_feedback_notification_attempts`. Each attempt is keyed
by the preview feedback record, event, policy, and destination. Missing runtime
GitHub credentials produce `delivery_skipped`; GitHub API failures produce
`delivery_failed`. Attempts record pending, delivered, skipped, or failed status
plus bounded provider-safe action/error text. Launchplane writes a pending
attempt before calling Discord and updates it after the provider returns, so
idempotent retries have durable dispatch evidence instead of silently re-sending
or silently dropping operator feedback.

Odoo stable bootstrap eligibility is lane-owned product-profile data. A lane's
`odoo_stable_bootstrap` policy defaults to disabled and must explicitly carry
an issue-backed approval URL, the destructive confirmation phrase,
`data_source_mode`, expected Dokploy target name, expected domains, and required
verification checks. The lane's `odoo_data_policy` must also allow that rebuild
source; `unknown` data authority allows no destructive rebuild source. Launchplane
treats the policy, data authority, and stored/observed target proof as the
authority for whether a bootstrap can proceed; request product/context/instance
alone is not sufficient. The approval issue is the implementation signal, not a
launch tracker: close it when the policy is encoded and keep launch/cutover
retirement in a separate issue or explicit expiration record.

Odoo prelaunch rebuild eligibility is also lane-owned product-profile data. A
lane's `odoo_prelaunch_rebuild` policy defaults to disabled and must explicitly
carry an issue-backed approval URL, confirmation phrase, data source mode,
expected Dokploy target name, and expected domains. The initial data source
modes are `empty` and `upstream_restore`. Target replacement plan/apply requests
that set a prelaunch data source must match this policy and the lane's
`odoo_data_policy.allowed_rebuild_sources` before Launchplane will treat missing
Odoo volume keys as intentional. This keeps provisional lanes such as OPW
testing/prod auditable without making environment names like `prod` destructive
authority by themselves.

`odoo_data_policy` records the lane's data authority separately from operational
driver defaults. `resettable` lanes may explicitly allow `empty` rebuilds;
`restorable` lanes may explicitly allow `upstream_restore` and must name an
`upstream_source`; `authoritative` lanes require backup-before-destroy and
restore-proof safeguards. Routine Odoo probe details belong in the Odoo driver,
not in tenant-owned product config, unless a lane has a real exception that
needs an explicit override. Launchplane-managed Odoo lanes use the runtime
identity endpoint `/launchplane/health` for lane verification; provider-local
container liveness checks may continue to use Odoo's `/web/health` endpoint.

This file layout describes today's local Launchplane implementation, not the
final cross-product communication boundary. The stable long-term contract should
be Launchplane's authenticated service ingress plus the durable record semantics
those API payloads map onto.

These records are the durable Odoo-first Launchplane truth for this repo today.
Stable lane records (`testing`, `prod`) and preview records are separate on
purpose: previews are not another long-lived environment lane.

The current cross-product posture is evidence-first. A second product such as
VeriReel should first land in these existing Launchplane record shapes through
deployment, promotion, inventory, and preview evidence ingestion before this
control plane takes over product-specific runtime actions.

Under the target Launchplane shape, product workflows and drivers should speak in
typed evidence payloads. Launchplane may still store those facts in file-backed
JSON for local development, but the shared-service path should write the same
record nouns into Postgres-backed tables without inventing a second record model.

## Layout

```text
state/
  artifacts/
    <artifact-id>.json
  backup_gates/
    <record-id>.json
  deployments/
    <record-id>.json
  launchplane_preview_generations/
    <generation-id>.json
  launchplane_preview_enablements/
    <enablement-id>.json
  launchplane_previews/
    <preview-id>.json
  promotions/
    <record-id>.json
  inventory/
    <context>-<instance>.json
  odoo_instance_overrides/
    <context>-<instance>.json
  release_tuples/
    <context>-<channel>.json
```

## Artifact Manifest

- One file per immutable artifact identifier.
- Record the public app commit, private enterprise digest, and final image
  identity.
- Preserve build-affecting addon, OpenUpgrade, and flag inputs alongside the
  image identity so the control plane owns the full manifest instead of a thin
  image pointer.
- Use generic artifact vocabulary at the record level, but keep Odoo-specific
  source inputs explicit in the stored evidence.

## Promotion Record

- One file per promotion attempt.
- Record source, destination, artifact id, gate evidence, deploy evidence, and
  destination health.
- Promotion records can also carry `deployment_record_id` so Launchplane can
  refresh current inventory from externally produced promotion evidence
  without guessing which deployment record established the promoted state.
- Promote inputs should reference the immutable artifact id directly.
- Promotion records also persist the authorizing `backup_record_id` so
  current inventory can be traced back to the exact stored backup-gate record
  that authorized the live promotion.
- Promotion execution should normalize backup-gate evidence from a stored
  backup-gate record instead of trusting ad-hoc inline request payloads.
- Promotion execution also resolves the deployable ship request natively in
  `launchplane` from this repo's Dokploy source-of-truth, instead of
  shelling out for a pre-rendered JSON request.
- Promotion execution requires the source lane to have a current release tuple
  record for the requested artifact before it can deploy to the destination.
- For a second product such as VeriReel, promotion evidence from the existing
  production-promotion workflow is the smallest proof point that this record
  shape works beyond Odoo.

## Backup Gate Record

- One file per backup gate run that can authorize a promotion.
- Record the destination environment, evidence source, pass/fail status, and
  concrete backup evidence such as snapshot or archive identifiers.
- Odoo prod backup-gate records are created by the Launchplane Odoo driver after
  a real compose-local DB dump and filestore archive capture. They should not be
  synthesized with generic operator assertions for release drills.
- VeriReel prod backup-gate records remain the promotion evidence and replay
  authority, but long-running backup-gate execution is queued separately in
  `launchplane_verireel_prod_backup_gate_operations` for DB-backed storage and
  `verireel_prod_backup_gate_operations` for file-backed local state. The HTTP
  route writes a pending backup-gate record plus a typed operation record; the
  supervised `verireel-workers` process claims the operation, heartbeats its
  lease, runs the delegated backup worker, writes the terminal backup-gate
  evidence, and completes the operation record. Expired operations retry only
  before the external backup side-effect boundary; once the phase reaches
  `backup_gate`, lease expiry fails closed for operator review.
- Promotion execution should fail closed unless the referenced backup-gate
  record exists, targets the same destination environment, and has `status`
  `pass`.

## Deployment Record

- One file per direct ship attempt owned by `launchplane`.
- Record the requested source git ref, target, deploy status, recorded
  executor, post-deploy update evidence, and destination health evidence.
- Ship execution no longer delegates runtime deploy/update work back to
  another repo; the durable deploy record belongs entirely here.
- The final deployment status also reflects control-plane-owned health
  verification rather than relying on delegated runtime steps to make that final
  readiness call.
- Deployment records also persist the resolved Dokploy target so the
  control plane owns the exact runtime target identity used for the deploy.
- The recorded executor reflects control-plane-owned Dokploy execution,
  including the compose post-deploy update schedule workflow when it applies.
- Deploy execution drives the Dokploy image selection from stored artifact
  manifests when possible by syncing an exact
  `DOCKER_IMAGE_REFERENCE=<repo>@<digest>` override before the deploy starts.
- Native ship/deploy records do not persist branch-mutation evidence because
  branch movement is not part of artifact-backed execution.
- When no stored artifact manifest is available for a direct ship, deploy
  execution fails closed.
- Deployment records make the native follow-up step explicit by
  recording whether the Odoo-specific compose post-deploy update was skipped,
  pending, passed, or failed.
- Odoo stable bootstrap writes normal deployment records with additional
  `bootstrap` evidence because the provider action is a dedicated data workflow
  schedule, not a fresh artifact deploy. `bootstrap.run_status` captures whether
  the destructive bootstrap schedule ran, while `bootstrap.readiness_status`
  separates post-deploy/public verification failures from bootstrap execution.
  Successful bootstrap refreshes inventory and sets `bootstrap_record_id` to the
  same record as `deployment_record_id`; failed or partially verified bootstrap
  attempts leave the current deployment inventory unchanged and update only
  `bootstrap_record_id` so operators can see the latest bootstrap attempt.
- Odoo stable bootstrap also writes durable operation records under
  `odoo_stable_bootstrap_operations`. These operation records are the
  create/read/poll boundary for the service-backed workflow: they store the
  original request, idempotency key, request fingerprint, active status/phase,
  deployment-record id when available, final driver result, and terminal error.
  A `pending` or `running` record is the single-flight guard for that
  product/context/instance.
- Odoo stable target replacement apply writes durable operation records under
  `odoo_stable_target_replacement_operations`. These records mirror the stable
  bootstrap operation boundary for the guarded `recreate-in-place` replacement
  path: the service stores the apply request, `Idempotency-Key`, request
  fingerprint, caller idempotency scope, status/phase, deployment-record id
  when available, final apply result, and terminal error. Reusing the same key
  with the same request and caller identity replays the existing operation;
  reusing it for a different request is rejected; an active `pending` or
  `running` operation blocks another apply for the same product/context/instance
  through a storage-owned lane reservation. Filesystem reservations wait briefly
  for a concurrent owner id to settle, then give that owner record its own
  bounded settle window before clearing abandoned empty or orphaned reservations
  so an interrupted writer cannot block the lane forever.
- The target execution model for these Odoo long-running operation records is a
  dedicated Launchplane worker process backed by DB leases and heartbeats. The
  HTTP route creates or replays the operation record and returns the poll URL;
  execution is owned by the supervised worker process, not by request-process
  daemon threads. Operation
  records carry execution fields for `attempt`, `lease_owner`,
  `lease_expires_at`, and `heartbeat_at`; terminal writes are guarded by the
  current lease owner so stale workers cannot overwrite recovered work. Worker
  entry points require DB-backed storage: `uv run launchplane service
odoo-workers run-once` performs one recovery/claim/execution pass, `uv run
launchplane service odoo-workers reconcile` performs the same expired-lease
  recovery without claiming new work, `uv run launchplane service odoo-workers
run` is the foreground loop intended for an external process supervisor, and
  `uv run launchplane service odoo-workers status` reports pending, running,
  stalled, and recent terminal operation counts without exposing request
  payloads. `status` is read-only observation; stale lease mutation remains
  storage-owned recovery in `reconcile`, `run-once`, and the worker loop.
  The deployed service exposes the same redacted read model at
  `GET /v1/service/odoo-workers/status` for callers authorized to
  `launchplane_service.read` on product/context `launchplane`, so operators can
  prove worker queue state without shelling into provider containers. The
  deployed service also exposes `POST /v1/service/odoo-workers/reconcile` for
  callers authorized to `launchplane_service.reconcile_odoo_workers` on the same
  service context, so expired-lease reconciliation can be proven through
  Launchplane itself rather than via provider shell access.
  The checked-in Launchplane compose topology starts a separate
  `launchplane-odoo-workers` process with the same image, runtime volume, and
  operator-supplied environment as the HTTP service. That process runs
  `/app/scripts/start-launchplane-odoo-workers.sh`, refuses startup without
  `LAUNCHPLANE_DATABASE_URL`, and only accepts generic worker timing knobs as
  process wiring; live operation selection remains in Launchplane records.
  VeriReel backup-gate operation execution follows the same deployment model via
  `launchplane-verireel-workers` and
  `/app/scripts/start-launchplane-verireel-workers.sh`.
  Production operation remains observable through the `launchplane service
  verireel-workers status` and `launchplane service verireel-workers reconcile`
  operator commands, and through
  `GET /v1/service/verireel-workers/status` and
  `POST /v1/service/verireel-workers/reconcile` on the deployed Launchplane
  service. Live operation selection remains in Launchplane records.
  Other long-running work should use typed worker operation or
  lease records that reference business evidence records, rather than making
  business evidence records themselves the queue lease, unless a future ADR
  explicitly says otherwise.
- Odoo prod rollback delegates the rollback deploy to stable target replacement,
  which writes the deployment record and prod release tuple. Rollback then
  refreshes prod inventory with rollback provenance and annotates the current
  prod promotion record's `rollback` and `rollback_health` fields. The selected
  rollback source is the DB-backed `testing` release tuple unless the operator
  supplies an explicit DB-backed artifact ID; operators must not supply
  unrecorded image refs or source SHAs.
- Generic-web rollback planning writes `GenericWebRollbackPlanRecord` entries
  under `generic_web_rollback_plans` in file-backed state and
  `launchplane_generic_web_rollback_plans` in DB-backed state. These records are
  safe-write plans when written by the plan route and audit records when written
  by the apply route before mutation. They store the destination lane, selected
  deployment-record id, immutable artifact identity, source git ref, planned
  deploy payload, backup-gate evidence, target health evidence, and any blockers
  that prevent a rollback apply. Generic-web rollback apply then writes the
  normal deployment and inventory records through the generic deploy path.
- Direct `ship` and `promote` execution fail closed if the referenced artifact
  id does not already have a stored manifest in control-plane state.
- Artifact manifests may also carry `addon_selectors` metadata so operators can
  inspect the original selector intent, but `addon_sources` remains the exact
  SHA-backed release truth used for tuple minting and deploy execution.
- Odoo stable target replacement also treats artifact `odoo_install_modules` as
  required-module availability evidence. Managed Odoo artifacts must declare
  Launchplane-required modules such as `launchplane_settings` and
  `disable_odoo_online`; deployment fails closed before provider mutation when
  that evidence is absent.
- Artifact manifests may carry `build_provenance` metadata for Odoo runtime and
  devtools base images plus build tools such as `odoo-devkit`. That provenance
  is artifact evidence, not addon ownership: `odoo-docker`, `odoo-devkit`,
  `odoo-shared-addons`, and `disable_odoo_online` stay support/dependency repos,
  while Launchplane records the immutable image/build refs used to produce an
  artifact.
- `odoo-docker` owns Odoo base-image build and promotion across its own
  candidate, testing, and stable image tracks. Launchplane does not create a
  separate base-image promotion record today; it consumes the selected
  base-image digest, tag, source repository, and source ref from the artifact
  manifest and carries that evidence through deploy, inventory, and read-model
  records. Add a Launchplane-owned base-image promotion record only if
  Launchplane starts deciding or executing those image-track promotions itself.
- Lane and driver read models expose the current lane's stored artifact manifest
  when the inventory or latest deployment points to one. Operators can inspect
  the Odoo base-image digests/tags/source refs and `odoo-devkit` provenance from
  Launchplane read evidence without treating support repos as release-tuple
  owners.
- For a second product such as VeriReel, the first Launchplane onboarding slice
  should ingest deployment evidence from that product's existing release
  workflows into this record shape before Launchplane owns the deploy execution.

## Release Tuple Record

- Release tuple records are keyed by long-lived environment channel.
- Successful waited `ship` executions for `testing` and `prod` mint the current
  channel tuple from the stored artifact manifest after deploy evidence passes.
- Tuple minting requires artifact manifest source refs to be exact git SHAs;
  branch names such as `main` or `origin/testing` are rejected instead of being
  written as release truth.
- Tuple minting uses tenant and addon source SHAs only. Base-image and build-tool
  provenance remains on the artifact manifest payload and does not expand the
  release tuple `repo_shas` map.
- Promotion execution copies the source channel tuple to the destination
  channel after the destination deploy passes, retaining the promotion and
  deployment record ids that established the promoted state.
- Externally produced promotion evidence can mint the same destination tuple
  through `release-tuples write-from-promotion` when the stored promotion
  record carries explicit `deployment_record_id` linkage and Launchplane already
  has the current source tuple for the promoted-from lane.
- Launchplane previews are not long-lived release-tuple channels; they derive
  their baseline from stored tuple evidence plus preview generation records.
- Local-dev tuple records live under `state/`; shared-service runtime baseline
  authority comes from the same release-tuple record shape in Postgres-backed
  storage. Neither path rewrites any tracked TOML catalog implicitly.

## Dokploy Target Record

- One record per tracked stable Dokploy route (`context` plus `instance`).
- Record the stable target definition fields Launchplane owns for that route,
  such as target type, project/target names, source metadata, env keys,
  domains, health policy, and typed product policies.
- Live `target_id` values remain a sibling DB-backed record so operators can
  update route metadata and route identity independently when needed.
- Paired Dokploy target and target-id records project to the neutral
  `ProviderTargetRecord` shape for audit and backfill comparison only. Missing
  halves remain missing; Launchplane does not fabricate provider-neutral target
  identity from only route metadata or only a live id. Explicit
  `launchplane_provider_targets` rows are the provider-target read authority.
  Dokploy records remain provider-specific execution configuration and cannot
  override provider-target identity after cutover.
- Shopify guard values such as protected store keys now belong in
  `policies.shopify.protected_store_keys` on this record instead of a route map
  hardcoded in Python.
- The operator write path for this record family is the Launchplane CLI,
  including `dokploy-targets list`, `show`,
  `put-shopify-protected-store-key`, and
  `unset-shopify-protected-store-key`. Direct local writes, including Shopify
  protected-store-key mutation, relabel, adoption apply, and application-create
  apply, require `--allow-direct-db-mutation` and are explicit local/bootstrap
  repair only; routine shared/live target setup should use the deployed service
  route or operator workflow.
- Repo-local Dokploy target TOML files are not a supported runtime authority or
  mutation surface for these records.
- For service-shaped products, persistent volume mounts remain operator-owned
  Dokploy target configuration. The product repo may document the expected mount
  path, but Launchplane records own the live target identity and mutation path,
  and managed secrets remain separate from volume contents.

## Odoo Instance Override Record

- One record per Odoo context and stable instance.
- Record Odoo application intent in typed fields instead of treating
  `ENV_OVERRIDE_*` names as the durable contract.
- `config_parameters` stores explicit `ir.config_parameter` writes such as
  `web.base.url`.
- `addon_settings` stores addon-shaped intent such as Authentik SSO or Shopify
  settings without coupling Launchplane records to environment variable names.
- `website_bootstrap` stores the typed devkit website bootstrap payload,
  including site identity, canonical URL, logo path, source metadata, and route
  definitions. Product repos remain the source of that intent; Launchplane
  persists the typed payload and renders it during Odoo post-deploy.
- Stable bootstrap normalizes the persisted `website_bootstrap.canonical_url`
  to the Launchplane-resolved stable target base URL before post-deploy renders
  the payload, so local tenant bootstrap defaults do not become stable lane URL
  authority.
- `apply_on` records the phases where the override is intended to apply, and
  `last_apply` records the latest driver result without making the addon layer
  the durable audit surface.
- Secret-shaped values must reference Launchplane managed secret bindings; list
  and show commands return only keys and counts, not plaintext values or binding
  ids.
- This record is the target authority for the Odoo driver. Runtime-environment
  `ENV_OVERRIDE_*` keys remain a migration input to retire, not the final
  override model.
- The compose post-deploy bridge renders one typed, workflow-intent-aware Odoo
  post-deploy payload for the data-workflow runner, then serializes it to the
  compatible v1 JSON/base64 wire shape. Launchplane evidence records the
  redacted payload digest, counts, required container keys, and whether
  website bootstrap was included; it does not make legacy `ENV_OVERRIDE_*`
  names the deploy-time contract.
- Secret-backed values still avoid Dokploy schedule plaintext. The payload
  points at the already-present neutral `ODOO_OVERRIDE_SECRET__*` container
  environment key for each managed secret binding, and the driver asserts those
  keys before invoking Odoo.

## Runtime Key-Safety Policy Record

- One record per imported runtime key-safety policy version under
  `launchplane_runtime_key_safety_policies`.
- Store policy metadata, status, source, timestamp, and binding-key
  classifications only. Do not store secret plaintext, ciphertext, provider env
  dumps, token prefixes, or operator-local overrides in these records.
- Active policy records are the Launchplane-owned authority for deciding whether
  a managed secret binding may be used by a target runtime class. Evaluation
  fails closed when no active policy record exists or when a required binding is
  missing, disabled, ambiguous, unclassified, or outside the allowed
  context/instance.
- Rules may restrict stable scope with exact `allowed_contexts` and
  `allowed_instances` values. Dynamic preview lanes should use paired
  `allowed_targets` entries with an exact context and explicit
  `instance_patterns` such as `pr-*`, so a preview pattern never broadens a rule
  to a different product or stable context.

## Launchplane Preview Record

- One file per stable Launchplane preview identity.
- Record the anchor PR identity, deterministic preview label, canonical preview
  URL, lifecycle timestamps, current preview state, and the active/serving/
  latest generation links.
- Preview records model the durable Launchplane identity for PR review, while the
  underlying preview runtime remains ephemeral and replaceable.
- Destroyed previews should remain readable durable evidence instead of being
  removed from state.
- Preview records should preserve one stable identity per anchor PR even when
  Launchplane replaces the serving generation over time.
- The initial explicit mutation surface is `launchplane-previews write-preview`,
  which builds the stored record from typed request input plus the dedicated
  Launchplane preview base-url runtime contract.
- Preview mutations may also carry an explicit `canonical_url` when the live
  preview route is produced outside Launchplane, so a second product can land
  preview evidence in the same record shape without first adopting Launchplane-
  managed routing.
- Higher-level transition commands may also rewrite preview records through the
  tested Launchplane transition helpers so operators do not have to hand-edit link
  fields for common lifecycle states.
- For a second product such as VeriReel, preview-control-plane and cleanup
  workflow evidence is the first candidate source for proving this preview
  model without forcing Launchplane to provision or destroy those previews itself
  on day one.
- `launchplane-previews write-destroyed` is the matching cleanup-evidence ingest
  surface for that model: it accepts typed teardown evidence and applies the
  stored destroyed transition without implying Launchplane executed the cleanup.
  Under the target Launchplane service shape, that same payload should enter through
  authenticated API ingress rather than a repo-local CLI command.

## Launchplane Preview Generation Record

- One file per Launchplane preview generation.
- Record the resolved manifest fingerprint, exact repo-to-SHA source map,
  baseline release tuple, artifact identity, health evidence, and failure
  details when a replacement does not become ready.
- Generation history should remain ordered and inspectable even when the latest
  generation failed and an older generation is still serving.
- Launchplane read models should derive status/list/history payloads from these
  durable generation facts rather than storing separate page blobs.
- The initial explicit mutation surface is `launchplane-previews write-generation`,
  which requires an existing preview record and can assign the next sequence
  automatically when the input does not pin one.
- Higher-level transition commands such as generation request/ready/failed
  reuse the same stored generation records while updating preview linkage
  semantics through the Launchplane transition helpers.
- `launchplane-previews write-from-generation` is the first explicit
  evidence-ingest surface for that path: it accepts typed preview plus
  generation evidence, writes the generation record, and refreshes the preview
  linkage according to the ingested generation state.
- Together with `launchplane-previews write-destroyed`, Launchplane can now
  ingest the full external preview lifecycle: create or refresh route evidence,
  persist generation outcome, and record confirmed cleanup.
- Those CLI surfaces should be treated as temporary adapters for the target
  Launchplane API payloads, not as the final integration boundary external
  products are expected to couple to forever.

## Launchplane Preview Enablement Record

- One file per tenant PR enablement snapshot.
- Record the anchor PR identity, enablement state, normalized preview-request
  metadata, candidate/request evidence, and timestamps.
- PR ingest and `launchplane-previews write-enablement` write the same typed record
  shape so webhook and non-webhook flows preserve comparable evidence.

## Launchplane Preview Inventory Scan Record

- One append-only record per provider inventory scan for a preview context.
- Record the scan id, context, scanned timestamp, source, pass/fail status,
  observed preview slugs, and failure message when the scan could not complete.
- A zero-preview scan is valid evidence and should be distinguished from missing
  inventory. Read models and readiness checks should use the latest scan to
  decide whether an empty preview inventory is verified or unknown.

## Launchplane Preview Lifecycle Plan Record

- One append-only decision record per preview lifecycle planning run.
- Record the desired preview anchors submitted by a product repo, the latest
  desired-state discovery record when present, the latest inventory scan used as
  current provider state, and the derived keep/orphaned/missing slug sets.
- The plan record is the required input for cleanup execution. Product repos
  should eventually submit thin desired-state adapters to this boundary instead
  of each owning a separate preview janitor implementation.

## Launchplane Preview Desired State Record

- One append-only record per Launchplane discovery of desired preview anchors.
- Record the product/context/source, GitHub repository, label, anchor repo,
  preview slug prefix, discovered timestamp, discovered desired previews, and
  pass/fail status.
- Desired-state records let Launchplane own the recurring PR label discovery
  loop before it plans cleanup against provider inventory.

## Launchplane Preview Lifecycle Cleanup Record

- One append-only cleanup record per lifecycle cleanup request.
- Record the source plan id, inventory scan id, requested source, whether
  `apply=true` was explicitly requested, the planned orphan slugs, and per-slug
  cleanup results.
- `apply=false` is the default report-only mode. Destructive provider cleanup is
  only allowed through an authorized workflow request with `apply=true` and an
  existing passing lifecycle plan.

## Launchplane Preview PR Feedback Record

- One append-only record per attempt to publish preview status back to an anchor
  pull request.
- Record the product/context/source, anchor repository and PR, preview status,
  rendered comment markdown, delivery status, delivery action, GitHub comment id
  and URL, and any skip/failure reason.
- Product repos should send outcome facts rather than hand-rendering or upserting
  GitHub comments themselves. This keeps PR feedback aligned with Launchplane's
  durable preview lifecycle records.

## Every Code Work Request Record

See [agent-context-boundary.md](agent-context-boundary.md) for the agent-facing
rules that compose these records into public-safe context and scoped intent
preflights.

- One durable request per approved Every Code automation trigger.
- Record the source, repository, issue number and URL, trigger label, trigger
  actor, optional GitHub delivery id, queue/update timestamps, claim host, run
  timestamps, PR URL, result summary, and blocked error message.
- State is `queued`, `claimed`, `running`, `done`, or `blocked`. Workers claim a
  queued request before reporting progress. Terminal states are immutable through
  the service status route.
- The local worker handoff is `uv run launchplane every-code run` for polling or
  `uv run launchplane every-code run-once` for a single scan. Each pass applies
  trusted PR feedback, reconciles preview gates and ready preview labels, removes
  stale source-issue queue labels for closed requests that can no longer reach
  preview readiness, routes failed checks back to the owning session, then claims
  at most one queued request. Request handoff opens or reuses deterministic
  visible tmux sessions for local checkouts, records `running` or immediate
  `blocked` status, and wraps the visible command so terminal success or failure
  calls `uv run launchplane every-code finish`.
- A Mac host can leave the poller running with
  `uv run launchplane every-code start`, inspect it with
  `uv run launchplane every-code status`, and stop it with
  `uv run launchplane every-code stop`. The supervisor writes a pid file and log
  under `state/every-code-worker/` by default while the worker-created tmux
  sessions remain visible and independently attachable.
- Worker prompts require closeout hygiene, including the Love Gate, before a PR
  can be merged or an issue can close. After a terminal session is gone, worker
  maintenance removes clean Every Code worktrees and their local `every-code/*`
  branches from the source checkout. Dirty or suspicious worktrees are left in
  place for operator inspection.
- Operators can reconcile older local cleanup residue with
  `uv run launchplane every-code reconcile-cleanup`. The command inventories
  saved session JSON, worker worktree directories, registered Git worktrees, and
  linked local `every-code/*` branches. It defaults to dry-run/report mode;
  `--apply` is required before it removes terminal, worker-owned, clean state.
  Missing records, active tmux sessions, dirty or uninspectable worktrees,
  unknown source checkouts, unlinked branches, and paths outside the worker
  state root are preserved and reported with skip reasons.
- For a remote Launchplane database, run the Mac worker through the service API
  instead of sharing DB credentials with the local host. Configure the service
  and worker with the same `LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN`, then start the
  worker with `uv run launchplane every-code start --service-url https://...`.
  The foreground `run`, `run-once`, and `finish` commands support the same
  `--service-url` mode. Direct `--database-url` and file-backed state remain
  local/dev fallbacks.
- Missed or manually inspected issue labels can be reconciled without polling by
  running `uv run launchplane every-code reconcile-issue` with the known issue
  repository, number, URL, title, and current labels. Reconciliation creates the
  same deterministic request id when the trigger label is present, dedupes an
  existing request without overwriting worker state, and skips issues that do
  not currently carry the trigger label.
- Launchplane owns this coordination record so GitHub webhooks, reconciliation,
  local Mac workers, and the future operator UI share one inspectable source of
  truth instead of relying on GitHub API polling or local shell lock files.
- GitHub webhook ingress accepts signed `issues.labeled` deliveries for the
  `every-code` label through `POST /v1/every-code/github-webhook`. The route
  uses `X-Hub-Signature-256`, requires `X-GitHub-Delivery`, and dedupes repeated
  deliveries by the deterministic repository/issue/label request id without
  overwriting a request that is already claimed or finished. Re-applying the
  same label to the same issue returns the existing request; a fresh retry model
  should use a new trigger label or explicit retry record rather than mutating a
  terminal request in place.
- The same webhook ingress accepts signed pull-request `closed` deliveries to
  terminalize linked Every Code requests. The close handler matches records by a
  stored result PR URL, by recorded PR feedback, or by GitHub closing references
  to linked issues. A single pull request can close multiple Every Code requests;
  queued matches are terminalized with service-owned claim metadata so terminal
  records still satisfy the work-request contract.
- Every Code PR feedback webhooks and `/preview ok` or `/preview changes ...`
  source-issue comments are actor-gated before they become pending work for a
  local session. The repository owner is trusted, the source issue author is
  trusted for preview validation, and configured managers from the local planning
  manager map are accepted as a bootstrap policy source until Launchplane owns a
  mutable repo trust-policy record. Bot-authored and untrusted human comments are
  accepted-but-skipped so webhook delivery remains idempotent without sending
  automation chatter to Every Code.
- Agent callers should prefer `GET /v1/every-code/summary` over raw work-request
  reads when they only need status. The summary projection links back to the
  issue and result PR, reports whether work is active, stuck, complete, or
  rerunnable, and includes safe rerun guidance without exposing webhook delivery
  ids, blocked error messages, issue bodies, prompt text, local checkout paths,
  or local worker hostnames. Summary entries include compact agent-context
  provenance and evidence for the source issue and recorded work-request state.
- Agent callers should prefer `GET /v1/previews/readiness` over raw preview-gate
  reads when they only need preview gate status. The readiness projection maps
  gate state to waiting, ready, needs-attention, or cancelled statuses with
  source links, freshness/provenance, and safe request-preview guidance. Detail
  fields are bounded and redacted so provider-only internals, local paths, and
  secret-shaped values are not copied into agent context payloads.
- Agent callers can use `GET /v1/agent/context` for a single read-only preflight
  context. It aggregates the existing repo-product mapping, work graph snapshot,
  Every Code summary, and preview readiness projections, then reports each
  section as available, unauthorized, or unavailable. It is not a persisted
  record and must not fetch or store issue bodies.
- Agent-consumer authorization diagnostics use a compact subject model for
  GitHub Actions, terminal agents, and GitHub humans. The model records the
  requested action, product, context, safety family, read-only-context status,
  access profile, and approval-capable status without replacing exact
  policy-rule authorization. Limited remote-user profiles fail closed to read and
  safe-write action families even when a human policy rule is too broad.
- Agent-facing authorization diagnostics include an `agent_audit` response
  provenance envelope with decision, safe reason code, subject, action, product,
  context, policy source, policy digest, and `authz_policy` source kind.
- Agent write-intent evaluations are persisted as
  `launchplane_agent_write_intents` records. Each record stores the request,
  evaluation result, `agent_audit` envelope, trace id, optional idempotency key,
  and recorded timestamp so later action routes can link to durable evidence.
  Execution routes treat these records as provenance, not credentials: they must
  perform their normal route-specific authorization and fail closed when the
  linked record is denied, stale, the wrong intent family, or mismatched on
  product, context, route action, source, or idempotency binding. The first
  consumer is Every Code rerun, which requires approved `every_code_rerun`
  evidence before requeueing a terminal work request.
- Merge train repository policy is persisted as
  `launchplane_merge_train_policies` records. The active policy record is the
  Launchplane-owned authority for supported repository/base branch pairs,
  enqueue labels, merge method, service authz, and token source metadata.
  Service routes fail closed when no active policy record exists or when the
  requested repository/base branch is not represented in the active policy.
- Merge train service runs are persisted as `launchplane_merge_train_runs`
  records. Each record stores the repository/base branch, mode, status, policy
  key and digest, fresh GitHub snapshot, dry-run decision, selected pull request
  metadata, trace id, recorded timestamp, and optional one-step worker result.
  The record is evidence for a single Level 1 ordered-queue service call, not
  queue authority for a later pass.
- Merge train pull-request feedback is persisted as
  `launchplane_merge_train_pr_feedback` records. Each record stores the
  repository/base branch, PR number/url, feedback event, hidden managed-comment
  marker, rendered public markdown, policy key and digest, controller action
  metadata, delivery status, GitHub comment id/url, and error detail when
  delivery fails. These records are audit evidence for the PR-facing feedback
  surface; the current PR comment remains managed through GitHub by marker.
- Full batch train candidates are persisted as
  `launchplane_merge_train_batch_candidates` records. Each record stores the
  repository/base branch, observed base SHA, ordered PR entries, candidate ref,
  candidate SHA when available, policy key and digest, candidate status, check
  status summary, source, and update timestamp. After successful landing is
  persisted, Launchplane attempts best-effort cleanup of the generated GitHub
  candidate ref; cleanup failure leaves the persisted landing result intact and
  the candidate record remains as durable evidence for the speculative batch
  candidate, not checked-in configuration.
- Full batch train landing plans are persisted as
  `launchplane_merge_train_batch_landing_plans` records. Each record stores the
  candidate identity, repository/base branch, candidate SHA, policy key and
  digest, and ordered PR-native landing entries with expected head/base SHAs,
  merge method, and per-entry landing status.
- The full batch train may add more persisted records for explicit queue entries
  and detailed candidate check evidence. Those records must preserve the same
  DB-backed authority boundary: runtime train state belongs in Launchplane
  storage, not checked-in config, service-host env, logs, or product-repo
  conditionals.
- Runner lane baseline readiness is represented by typed policy, observation,
  violation, and readiness contracts in
  `control_plane.contracts.runner_lane_baseline`. These contracts are evidence
  about whether a self-hosted runner lane satisfies Launchplane's host baseline,
  including Docker credential isolation and Docker toolchain/version policy;
  they are not product deploy authority and they do not replace route-specific
  authorization, promotion, backup-gate, or provider safety checks.
- Runner lane registration audit records are the durable record for the first
  Launchplane-controlled repository-runner registration host adapter. They live
  in `launchplane_runner_lane_registration_audits`, are written through
  `POST /v1/evidence/runner-lane-registration/audits`, and preserve dry-run or
  apply evidence without storing GitHub runner registration tokens.
- Scoped agent write-intent evaluation is exposed at
  `POST /v1/agent/write-intents/evaluate`. It validates intent shape, maps the
  intent to an exact existing policy action, evaluates authorization, and returns
  status/evidence links without executing runtime mutations or returning
  credentials.
- Secret-backed write-intent evaluation is metadata-only. Requests may include
  managed secret binding keys and a runtime destination, but responses include
  only binding keys, runtime key-safety policy ids/digests, and finding codes.
  They must not include plaintext, ciphertext, token prefixes, or provider env
  dumps.

## Inventory

- Inventory records are keyed by environment.
- Inventory may be replaced in place because it represents current state rather
  than append-only event history.
- Inventory records capture the current deployed source git ref, artifact
  identity when known, deploy evidence, post-deploy update evidence,
  destination health, and the deployment/promotion records that established the
  current state.
- The CLI status/read-model commands are expected to compose inventory with the
  linked promotion, deployment, and backup-gate records rather than forcing
  operators to open those files directly.
- Successful waited `ship` executions refresh inventory directly from the final
  deployment record.
- Successful waited `promote` executions refresh the same inventory record and
  add promotion linkage so the current state can still be tied back to the
  controlling promotion and deployment records.
- Launchplane service evidence ingress now applies the same pattern for external
  evidence: accepted deployment evidence refreshes inventory immediately, and
  accepted promotion evidence refreshes destination inventory when the
  promotion record carries explicit deployment linkage.
- For a second product such as VeriReel, inventory should first be derived from
  ingested deployment/promotion evidence before Launchplane becomes the runtime
  executor for that product. The first explicit mutation surfaces for that are
  `inventory write-from-deployment` and `inventory write-from-promotion`.
