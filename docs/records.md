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
- Shared-service core-record writes use authenticated Launchplane service
  ingress or operator workflows. Local core-record write commands are
  file-backed rehearsal helpers only; `storage import-core-records` is removed
  and arbitrary-checkout core-record DB imports are not a supported v2 mutation
  path.
- Direct managed-secret writes through `secrets put` follow the same
  `--allow-direct-db-mutation` bootstrap/repair boundary.
- Keep git history separate from operational history.
- Favor append-style writes for promotion records.

## Schema Migrations

Launchplane uses SQLAlchemy ORM models as the persistence boundary and Alembic as
the versioned migration mechanism for shared-service Postgres databases. Hosted
service startup runs the Launchplane schema-migration helper under a
deployment-wide PostgreSQL advisory lock before starting HTTP service. The
helper advances only to the release's explicit migration target, which may be
behind the checked-in Alembic head during an expand/contract rollout. Runtime
code can still call `ensure_schema()` for compatibility, but it only creates
tables for local SQLite/test databases. For shared-service database URLs,
`ensure_schema()` verifies that Alembic has already created the required tables
and columns and fails closed when migrations are missing. Hosted Postgres
verification accepts only the release-declared compatible revisions and verifies
the critical JSONB/integer types, unique indexes, worker-claim indexes, and
partial active-operation predicates used by idempotency, claims, leases,
CAS-style owner checks, and active operation reservations.

For a fresh or existing hosted database, use the same serialized helper as the
service entrypoint:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run python -m control_plane.storage.schema_migration
```

For an existing Launchplane database that already has the tables created by the
pre-migration `create_all` path, adopt the baseline by stamping the database at
the current revision only after the automated schema adoption verifier passes.
The verifier inspects existing Launchplane-owned tables and fails closed when a
live table is missing an ORM-managed column, has an unexpected column, or has a
critical idempotency/active-operation index with missing uniqueness, columns, or
partial predicate. A failure means the operator must stop and reconcile the
schema before stamping; do not hand-edit the Alembic version table or skip the
check.

The hosted startup wrapper runs that helper before starting the service:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... scripts/start-launchplane-service.sh
```

For a manual adoption rehearsal, run the verifier directly. It prints the
revision to stamp when adoption is safe and exits non-zero when the live schema
does not match the ORM-managed table shape:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run python -m control_plane.storage.schema_adoption
```

The migration helper performs a safe adoption stamp and then advances to the
release target while holding the migration lock:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run python -m control_plane.storage.schema_migration
```

Do not run `alembic upgrade head` directly against the shared service database
during a staged rollout. The authorization compatibility image was deployed at
revision `f3b5d7e9a1c2` before this release advanced the migration target to the
fenced `f4c6e8a0b2d4` schema, then to `a1c3e5f7b9d2` for the dedicated Odoo
production backup-restore operation table, and then to `b3d5f7a9c1e4` for the
retained-volume backup-import operation table. The full reconciliation image
accepts older revisions only as serialized migration sources; its ORM and
runtime compatibility contract requires b3. After the database reaches b3,
rollback is supported only to an image that understands both production
recovery operation tables. The runtime status route reports the observed
database revision, the image's b3-only runtime compatibility set, and its
migration target so deployment verification can enforce this boundary.

JSONB `payload` columns remain durable evidence envelopes and original typed
payload snapshots. Fields that the GUI or drivers need to filter, order, join,
authorize, constrain, display regularly, or act on should be promoted into ORM
columns/tables and migrated explicitly while keeping the payload copy as
historical evidence.

The production schema proof runs against real PostgreSQL, not SQLite:

```bash
LAUNCHPLANE_TEST_POSTGRES_URL=postgresql+psycopg://... uv run launchplane ci postgres-integration
```

The test URL points at a disposable PostgreSQL service/database root. The harness
creates isolated databases, applies Alembic from empty schema to `head`, verifies
the exact schema head and critical invariants, and then drops the databases. It
must not use Launchplane runtime credentials or shared production databases.

## Mutation Reservations

The `launchplane_idempotency_records` table is also the durable mutation-
reservation boundary. Existing completed-response rows remain valid and are
backfilled as `completed`; reservation-backed routes add promoted state, lease,
attempt, owner, reconciliation-key, and timestamp columns while retaining the
typed payload as the complete evidence envelope.

The same table stores ready Odoo preview apply-inputs issuance evidence. The
service derives a plan id from caller scope plus the inputs idempotency key,
persists the normalized plan request, runtime plan, provider dry-run plan,
fingerprint, and expiry under that id, and requires the id as the later apply
key. This reuses completed-response storage rather than adding a second plan
table; blocked plans are not issuance records.

Reservation identity is `(scope, route_path, idempotency_key)` and remains
unique in PostgreSQL. The typed lifecycle is:

- `running`: one owner holds a bounded lease before effects begin.
- `completed`: the response status, trace, payload, and completion timestamp are
  durable replay evidence.
- `reconcile_required`: an external operation key was bound and the effect is
  now unknown; automatic re-execution is forbidden until domain reconciliation
  proves the provider state.

Provider-operation reservations additionally retain a stable
`provider_target_key`, plus `provider_effect_phase` and
`provider_effect_started_at`, in the typed payload.
The phase is checkpointed under the current lease immediately before a provider
write. It is part of transition identity, so a reservation snapshot from before
the checkpoint cannot renew, complete, release, or advance provider work after a
newer owner recovers the claim. The same rule covers compensating rollback
deletes and multi-write post-deploy work; each external write advances the phase
before dispatch instead of treating the enclosing workflow as one effect.

Reservation attempts return typed `acquired`, `replayed`, `conflict`,
`in_progress`, or `reconcile_required` decisions. A different request
fingerprint always conflicts, including while the first request is running. An
expired reservation without a reconciliation key may be reclaimed by a new
owner with an incremented attempt. Once a provider operation or reconciliation
key is bound, lease expiry transitions to `reconcile_required` instead of
repeating the effect. Lease renewal, completion, and reconciliation binding use
owner checks and fail closed for stale owners.

DB-only mutations should reserve and complete inside the same transaction as
their business write. `POST /v1/product-profiles/preview-tls/apply`,
`POST /v1/product-profiles/prelaunch-rebuild/apply`,
`POST /v1/route-bindings/reconcile`, and
`POST /v1/route-bindings/external/reconcile` use that boundary: the reservation
insert occurs before the domain write, and the domain record plus completed
response commit atomically. A no-op apply still commits the completed
reservation so concurrent and later same-key requests replay the original
response. If response-evidence persistence fails, the domain write and
reservation both roll back. A DB-only preflight may remove an expired unbound
orphan reservation when the route could not have committed the atomic domain
write; active or reconciliation-bound claims remain fail-closed. Persisted
reservation and completion timestamps come from the database clock.

Route-binding reconcile compares the full current record under a per-binding
PostgreSQL transaction lock. Expected-absent create, expected-current refresh,
and unchanged no-op all complete idempotency evidence atomically. A missing or
changed expected record removes the uncommitted reservation and returns a CAS
conflict without changing authority. PostgreSQL apply is the supported service
mutation boundary. Filesystem storage retains typed route-binding read/write
parity for local rehearsal, but the service does not emulate the PostgreSQL
transaction by performing a split filesystem apply.

The Odoo testing route-binding refresh controller is a bounded batch over that
same per-binding mutation boundary. Target discovery comes from DB-backed Odoo
product-profile testing lanes intersected with active service-owned route
bindings. Each due binding refresh and its child replay record commits
atomically under the existing per-binding lock. Before the first child write,
the controller acquires a parent mutation reservation that fences concurrent
same-key runs. It compare-and-completes that parent response only after the
bounded batch finishes. If an unexpected failure interrupts the batch, the
bounded parent lease keeps immediate retries in progress; after safe release or
expiry, retrying with the same controller key replans already-refreshed records
as unchanged and continues remaining due bindings. The controller never creates
a missing binding or selects production.

Product authority bundle writes are the same atomicity boundary for product
runtime/config ownership. PostgreSQL storage exposes a single
`write_product_authority_bundle` repository method for the authority graph that
can include product profiles, provider targets, legacy Dokploy target records,
runtime-environment rows and delete events, managed-secret versions/current
pointers/bindings/audit events, environment inventory, release tuples, and the
completed idempotency response. Product config, onboarding, context cutover,
and legacy cleanup plan their whole graph first and then commit through this
method once. A current managed-secret pointer must not advance unless the new
version, binding, audit evidence, required runtime-environment changes, and
applicable idempotency completion are in the same transaction. Cleanup deletes
compare the current row payload with the planned expected record under the
storage boundary and fail closed on missing or drifted authority instead of
publishing a partial graph. Provider-target writes likewise carry an
expected-current or expected-absent precondition so a concurrent route owner
cannot be overwritten after planning. Lane-summary reads hold a shared bundle
guard while assembling their multi-record view, so a bundle commit cannot split
one response across the old and new authority graphs.

Filesystem storage remains local rehearsal state, not shared runtime authority.
Its product authority bundle path stages every replacement under
`.product_authority_bundle_stages/` before touching live record files. A stage
left in `ready` state is discarded on the next store access because no live file
has been published yet. A stage left in `publishing` state is explicitly
resumable: the next store access completes remaining `os.replace` writes and
expected-payload deletes from the manifest, then removes the stage. If live data
changed from the manifest while recovery was pending, recovery fails closed and
leaves the stage for operator inspection rather than guessing at authority.
Ordinary filesystem reads, writes, creates, deletes, and composite promotion
evidence rollback hold the same bundle lock through their live-file access.

Provider-backed routes must durably reserve first, bind their stable provider
operation or reconciliation key before invoking the provider, and complete only
after durable local evidence is ready. A crash or timeout after key binding is
an unknown outcome, not permission to retry the provider mutation.

## Transactional Outbox

External deliveries that are not safe to perform inside the request transaction
use `launchplane_outbox_deliveries`. Business state and the pending outbox row
must commit in the same PostgreSQL transaction, so a crash before a worker runs
leaves durable intent instead of a lost notification or workflow dispatch. The
first migrated paths are generic-web promotion workflow dispatches and GitHub
public-ingress incident notifications.

Outbox rows are intentionally provider-neutral and secret-free. Payloads may
carry stable routing facts, bounded provider inputs, prior observation IDs,
hidden reconciliation markers, and safe credential context names, but they must
not carry bearer tokens, webhook URLs, encrypted secret blobs, cookies,
passwords, raw provider error bodies, or other secret-bearing fields. Validation
rejects sensitive payload key names so delivery records remain durable evidence,
not a secret store.

Workers claim due rows with bounded leases. PostgreSQL claims use `FOR UPDATE
SKIP LOCKED` over pending or expired work so multiple service instances can
claim independently without blocking on the same row. Completion remains
lease-owner fenced; stale owners cannot record terminal state after another
worker reclaims the delivery. Attempts are bounded by `max_attempts`.

Provider calls record a stable `provider_operation_key` and `provider_id` before
the external send. If a worker crashes after recording that marker, a later
worker reclaims the row and reconciles before resending. GitHub workflow
dispatch reconciliation checks for a new workflow run not present before the
original dispatch; public-ingress GitHub notifications include a hidden marker
in issue/comment bodies and search for that marker before posting again. Unknown
provider failures are stored only as bounded `error_code` values such as
`github_provider_error` or `invalid_outbox_payload`.
Retryable provider errors return to `pending` with bounded database-clock
backoff; provider markers remain attached so post-send uncertainty reconciles
before another effect. Dedupe keys identify one business transition rather than
the workflow parameters forever, allowing a later legitimate dispatch with the
same inputs to enqueue a distinct delivery.
Workflow dispatches only adopt an observed run when reclaiming an existing
provider marker; a first attempt records its marker and sends rather than
claiming an unrelated concurrent run. Resolved public-ingress notifications
also ensure the GitHub issue is closed after marker reconciliation.

## Durable Provider Operations

The shared durable provider-operation runner (`control_plane/provider_operations`)
implements this contract for external-effect routes. It reserves with the
deterministic provider reconciliation key bound from the start, so `acquired`
always means a brand-new attempt with no prior effect, while an expired bound
reservation always resolves to `reconcile_required`. On the reconcile path the
runner asks a provider adapter to observe the target: an observed effect is
adopted into a completed reservation through `adopt_reconciled_mutation`. The
adoption compares the exact observed attempt, phase, target key, and reservation
identity, so a second instance can adopt another instance's effect without a
stale observer completing a newer attempt. An unknown observation stays
`reconcile_required`. A proven-absent observation may instead atomically advance
the same reservation to a new attempt only when no provider phase was
checkpointed or the adapter explicitly proves the recorded phase is retry-safe.
An unknown observation never retries. A pre-effect rejection releases the fresh
reservation through `release_reserved_mutation` (owner- and identity-fenced) so
no poisoned claim is left behind. Odoo preview apply and generic web deploy are
the first migrated provider routes; both require an `Idempotency-Key` and a
database store. Provider-specific observation and reconciliation stay behind
the adapter boundary.

The `launchplane_idempotency_active_reconciliation_idx` partial unique index
fences each provider target while its reservation is `running` or
`reconcile_required`, regardless of caller idempotency key, using the promoted
`provider_target_key` rather than the richer reconciliation snapshot. Migration
backfills previously bound keys and aborts with an explicit reconciliation error
if duplicate active claims would make the target fence ambiguous. The runner renews
the bounded lease during blocking provider work and always completes from the
latest reservation snapshot. Generic-web reconciliation keys embed a versioned
snapshot of the resolved provider target, so restart recovery remains bound to
the original target even if current authority records change. Provider operation
markers are derived from the durable request identity and written into Dokploy
deployment titles; both the initial wait and later observation use that exact
marker rather than inferring success from similar target state. Adoption may
store terminal failure response evidence as well as success, but only after the
provider supplies the exact deployment id and start/finish timestamps. Fresh
completion has the same evidence requirement as restart adoption; completed
means the provider outcome is durable and replayable, not that the external
deployment passed.

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
  build provenance for base images and build tools such as `odoo-devkit`, plus
  schema-v2 dependency provenance for uv locks, platform-specific Python
  inventories, and exact-source external compatibility inputs. Source input
  details, addon selectors, dependency provenance, support-repo provenance, and
  provider evidence stay payload-only until they become normal query or action
  fields.
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
- Authz policy: modeled fields are `record_id`, monotonic `revision`, `status`,
  `source`, `updated_at`, `policy_sha256`, and optional service-owned `audit`
  metadata. PostgreSQL enforces unique revisions and at most one active row.
  Managed rules persist stable `(managed_set_id, managed_rule_id)` identities in
  the schema-v2 policy payload; content hashes describe versions rather than
  ownership. Managed reconciliation audit records the operator identity, reason,
  related issue, reviewed plan and desired-set digests, migration/adoption
  intent, previous/new revisions and policy digests, trace/request fingerprints,
  idempotency evidence, and a redacted rule-ID/hash diff. Policy CAS and
  completed replay evidence commit in one transaction; a no-op apply creates no
  policy-history row. Managed reconciliation can adopt matching unmanaged rules
  and retire covered name-only compatibility rules in the same reviewed policy
  transaction. Its diff also reports bounded operational-readiness blockers for
  desired managed GitHub Actions rules: the managed rule ID, affected readiness
  actions, and selector-shape reason codes, but never repository IDs, workflow
  refs, products, contexts, or instances. A rule containing old and new worker
  SHAs in one `job_workflow_refs` selector may remain valid transitional
  authorization, but it reports `job_workflow_refs_not_singleton`. Readiness-safe
  expansion uses two separately identified exact rules, one immutable worker SHA
  per rule, followed by reviewed contraction of the old rule.
  Production tracked-log reads and website-bootstrap writes use separate exact
  caller/worker rule identities. Their workflow artifacts are scoped by run and
  attempt so retries preserve distinct evidence without turning observation or
  payload churn into new runtime authority.
  During a future OpenFGA migration, these DB-backed policy records remain the
  source evidence for dry-run tuple proposals and parity checks.
  After a proven cutover, records should store import/audit/model-version
  evidence rather than remain a second live authorization source.
- Human session: the DB-backed payload includes the GitHub identity snapshot,
  creation/expiry timestamps, and CSRF generation. Hosted authorization
  revalidates roles against the current DB policy on every request and rejects
  OAuth-derived organization/team claims after 24 hours, forcing a fresh GitHub
  sign-in before those mutable claims can authorize another hosted request.
- Merge train stack collapse plan: modeled fields are `record_id`, `status`,
  `source`, `updated_at`, `repository`, `base_branch`, `collapse_id`,
  `root_pull_request_number`, and `plan_status`. The payload carries the typed
  stack entries, expected SHAs, planned child-to-parent mutations, policy digest,
  and intent source. The initial intent source is the root PR's merge-train
  enqueue label; no file-backed or hardcoded repository config participates in
  live collapse authority.
- Merge train controller state: modeled fields are `controller_key`,
  `repository`, `base_branch`, `status`, `policy_key`, `policy_sha256`,
  `updated_at`, `lease_owner`, `lease_expires_at`, `active_action`, and
  `active_phase`. The payload carries the active record id, pull-request scope,
  step payload, last-transition evidence, and reconciliation status/detail.
  This row is Launchplane's repository/base-branch controller fence and resume
  checkpoint: it is the durable owner/phase record that lets a restarted
  controller adopt already-observed GitHub effects instead of continuing from
  stale in-memory assumptions. PostgreSQL acquisition and transition writes use
  one repository/base advisory lock, row-level compare-and-set checks, and the
  database clock for lease expiry; a stale owner cannot overwrite or release a
  successor lease. Runtime repository/base authority still comes from the active
  merge-train policy record and live request scope, not from checked-in config.
  The tenant admission controller reuses this repository/base row only as a
  shared mutation fence. Its `tenant_admission_merge` checkpoint carries the
  exact request candidate, base branch, admission decision, technical-check
  digest, and provider phase needed to reconcile an uncertain merge. That state
  does not become admission authority, does not enqueue the PR, and is cleared
  only after an exact merged result is confirmed or a pre-effect block is
  recorded cleanly. Every shared-fence acquisition atomically writes a
  controller-specific initial action and declares which active actions it can
  resume, so either controller rejects an unfinished foreign action before any
  row fields are rewritten.
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
- `uv run launchplane storage provider-target-backfill` is the local report-only
  preview for explicit provider-target row seeding. It emits dry-run output for
  complete Dokploy target/id projections, existing matching physical rows,
  incomplete pairs, conflicts, and unsupported provider rows without writing
  anything.
- Shared and production backfill uses the deployed service route
  `POST /v1/provider-targets/operations`, normally through the manual
  `Provider Target Operations` workflow. The workflow records per-route audit,
  dry-run, or apply evidence as artifacts, writes only complete non-conflicting
  projections, and uses DB-backed `provider_target.audit` or
  `provider_target.backfill` authz grants instead of local checkout writes.
- Shared ship and promotion request contracts and new deployment/promotion
  evidence ingress require canonical flat target fields (`target_name`,
  `target_type`, `provider_id`, `target_category`, and
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
  operator message. Observation timestamps, generic cache-class availability,
  source and measurement-basis metadata, filesystem apparent/allocated bytes,
  Docker logical/reclaimable bytes, bounded inventory counts and truncation,
  age buckets, numeric run ids, GitHub completion state, bounded worker and
  open-handle observations, cleanup history, hysteresis/cooldown evidence, and
  source-attributed idle convergence also stay payload-only. Idle convergence
  records public-safe scope and subject keys, source availability, state,
  sample count, nonzero observation window, timestamps, bounded counts,
  truncation/reason codes, and derived blockers. Absolute cache paths, raw
  command output, runner names, and source repository identity are
  executor-local runtime authority and are not persisted in the audit.
  Docker toolchain evidence, host-command output, Docker summaries, and rollout
  notes stay payload-only until they need queryable operational views. Bounded
  list, sanitized detail, and observation-history reads use the existing JSON
  payload and promoted host/action/status columns; no separate history table or
  inferred timestamp is required. Legacy reports without `observed_at` sort
  after timestamped evidence. The host-local audit-delivery envelope is a separate
  recovery record: it stores planned and optional terminal audit payloads,
  execution phase, delivery state, idempotency keys, bounded redacted errors,
  and attempt counts. It is written atomically under an explicit state
  directory and is not a substitute for the service-owned audit row.
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
- Environment route binding: modeled fields are `product`, `context`,
  `instance`, provider target summary, ingress provider/endpoint,
  termination kind, primary domain, TLS owner, `status`, freshness, and
  `updated_at`. The payload carries all typed desired domains, source record
  references, and provider evidence needed to explain the binding. The primary
  key is the neutral environment tuple, not a provider host id, certificate id,
  IP address, or Dokploy target id. Native FastAPI
  `GET /v1/route-bindings/records` and
  `GET /v1/route-bindings/records/current` return redacted read models that omit
  provider evidence and include an opaque full-record SHA-256 for compare-and-
  swap. Native FastAPI `POST /v1/route-bindings/reconcile` plans or writes one
  binding by
  comparing existing Launchplane provider-target, tracked Dokploy target, edge
  endpoint, and applied ingress audit records. The provider-target record must
  equal the projection of the Dokploy target plus target-id record; the latest
  matching apply audit must be terminal, include explicit TLS ownership, and
  name the active edge-endpoint record used for the route. The applied audit is
  the join authority for that edge endpoint; Dokploy project/display names are
  not treated as edge-server identities.
  Source record timestamps are retained as versions. Each successful service
  re-evaluation attests the derived binding for 24 hours; reconcile is an
  unchanged no-op while more than 12 hours remain and refreshes at half-life or
  when evidence changes. Invalid or future source timestamps remain blocked.
  Reconcile supports expected-absent create and expected-current evidence
  refresh, but any provider target, domain, ingress, TLS owner, lifecycle status,
  operator ownership, missing join, ambiguity, unresolved audit, bounded-scan
  exhaustion, or CAS drift is an explicit conflict or blocker rather than an
  overwrite.
  Native FastAPI
  `POST /v1/route-bindings/odoo-testing/controller/run-once` performs bounded
  testing-only discovery from product profiles and invokes this same planner for
  each active service-owned binding. Its contract contains no target selectors,
  caps discovery at 25 records, pre-authorizes every exact instance, and applies
  due refreshes sequentially. Absent, disabled, operator-owned, non-Odoo, and
  production records are not candidates. No schema migration is required.
  Externally managed ingress uses the separate native FastAPI
  `POST /v1/route-bindings/external/reconcile` contract. It derives the exact
  lane, public HTTPS domains, and provider target from DB-backed product-profile
  and provider-target records; callers cannot submit domains, provider ids,
  proxy host ids, certificates, upstreams, or edge addresses. The product lane
  must declare an enabled public HTTP health check that requires runtime
  identity. The resulting operator-owned binding records
  `ingress.provider = "external"`, edge termination, external TLS ownership,
  and no internal proxy evidence. Its deterministic endpoint key identifies the
  declared public edge without claiming a provider host identity.
  External authority is attested for 30 days and refreshes at 15-day half-life
  or when DB-backed source versions change. Public behavior remains separate:
  the public-ingress monitor independently verifies HTTP, exact runtime
  identity, and TLS, and its observations expire after two hours. Apply uses
  separate exact-instance `route_binding.external.apply` authority;
  dry-run uses `route_binding.external.plan`. An operator can explicitly set an
  external binding to `disabled` to relinquish authority. Managed reconcile may
  replace only that disabled external record after the managed provider route
  has terminal audit evidence; it never silently takes over active external
  authority.
  Product repositories must not own route bindings, TLS ownership, provider
  host ids, certificate ids, or edge topology.

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
keys, preview domain certificate policy, required provider fields, and the
declared data transport mode so readiness can fail before Launchplane mutates a
provider. `preview.domain_certificate_type` defaults to `none` for externally
managed wildcard TLS; `letsencrypt` delegates per-host certificate provisioning
to Dokploy.

Product profiles may also declare expected config requirements for stable lanes:
runtime-environment key names and managed secret binding keys by context and
instance. These requirements are declarative intent for operator readiness
views. Actual configured, missing, disabled, stale, or unsupported status is
derived from runtime-environment records, managed secret binding records, driver
support, and trust metadata; expected config requirements do not store runtime
values, managed secret IDs, secret plaintext, or ciphertext.

Stable lanes declare synthetic monitoring through `health_monitoring`, which
contains an explicit `monitoring_intent` of `public`, `private`, or `prelaunch`
plus `checks[]`. The intent is DB-backed product-profile authority, not a
checked-in product catalog. `public` requires an enabled `public_http` check;
`private` requires an enabled `private_http` check; `prelaunch` may retain
public checks so Launchplane can show readiness evidence before public
availability is expected. Missing intent on a policy with checks, unknown
values, and public/private intent without its required check fail validation.

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
monitor records HTTP reachability, redirect failures, private/internal URL
rejection for public checks, and runtime identity comparison when current lane
inventory or deployment evidence provides an expected identity. Public and TLS
probes are effective for `public` and `prelaunch`, but only `public` makes their
failures incident-eligible. `private` suppresses public/TLS probes while keeping
private and provider checks active; private/provider failures remain
incident-eligible in every intent mode. Public checks never fall back to the
private client. Check policy may carry an enabled flag and provider-specific
routing details, but alert destinations are not lane text fields. Public ingress
incident notifications are routed through DB-backed notification policy
records. Legacy product-profile payloads receive a versioned generic migration:
an enabled public check maps to `public`, otherwise an enabled private check maps
to `private`, and lanes without either map to `prelaunch`. Real product identity
does not participate in that migration.

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
reachable by Launchplane. Generic-web service profiles must name an immutable
image repository for stable deploys; source-backed compose onboarding is
retired with the generic-web source-ref deploy bridge. See
[dokploy-service-deployments.md](dokploy-service-deployments.md) for the
service-specific contract.

The service exposes product profile records through `GET /v1/product-profiles`,
`GET /v1/product-profiles/{product}`, and `POST /v1/product-profiles`. Writes
require the `product_profile.write` action for the target product in the
Launchplane service context; reads use `product_profile.read`.

Additive expected-config metadata changes use
`POST /v1/product-profiles/expected-config/apply`. The request carries
`mode: "dry-run"` or `mode: "apply"`, a product key, a reason, and runtime key
or managed secret binding requirements to append if absent. It does not accept
secret plaintext, runtime values, repositories, lanes, domains, or promotion
settings, and it never removes existing expected-config entries. The manual
`Product Expected Config` workflow is the operator path for shared/runtime
metadata changes; real product, context, instance, and binding values are
workflow inputs, not checked-in defaults. Because the route authorizes against
the target product in the Launchplane service context, product-specific workflow
authority must come from managed authz reconciliation through the service or
operator UI, not a checked-in product catalog.

Odoo preview certificate-policy changes use
`POST /v1/product-profiles/preview-tls/apply`. The route reads the current
DB-backed profile and can change only `preview.domain_certificate_type`; all
other profile fields are preserved from the service-owned record. Dry-run
always reads fresh state and returns the current value, requested value, profile
timestamp, and a canonical plan SHA-256 without storing idempotency evidence.
Apply requires that reviewed SHA-256 plus an idempotency key and fails stale if
the reviewed TLS plan inputs changed or the profile row changes during apply.
Apply inserts its mutation reservation before the profile write and commits the
profile plus completed response evidence in the same transaction, including
no-op applies. The manual `Product Preview TLS` workflow is the audited operator
surface for both modes and supplies the target product and requested `none` or
`letsencrypt` value as runtime input. Its
`product_profile.preview_tls.apply` grant is target-product scoped and must come
from service-backed or operator-supplied authz input rather than a checked-in
product catalog.

Stable-lane health policy and monitoring-intent changes use
`POST /v1/product-profiles/health-monitoring/apply`. The request identifies one
exact product/context/instance lane and one health-check name, then supplies the
desired `monitoring_intent`, `public_http` or `private_http` kind, enabled state,
and runtime-identity requirement. Public checks cannot carry endpoint URLs;
Launchplane preserves any existing public-check URL or derives it from lane-owned
`health_url`/`base_url`. Private checks carry only a registered
`private_endpoint_key`; apply validates that the record is active and belongs to
the exact product/context/instance without returning or logging its internal URL.
The request cannot carry a domain, provider target, proxy record, certificate
reference, or replacement product profile. Enabling strict public runtime
identity requires an HTTPS host already owned by that lane. Dry-run returns a
canonical plan bound to the complete current profile; apply requires the reviewed
digest and an idempotency key, then compare-and-writes only the selected check,
lane intent, and server-owned profile audit fields. Whole-profile service writes
cannot change existing health-monitoring authority, and onboarding updates
preserve it; operators use this bounded apply path instead. Concurrent profile
edits fail stale instead of being overwritten.

Odoo prelaunch rebuild policy changes use
`POST /v1/product-profiles/prelaunch-rebuild/apply`. The request identifies one
exact product/context/instance lane and can change only that lane's
`odoo_prelaunch_rebuild` policy plus server-owned profile audit fields. Enabling
the policy requires issue-backed approval, a typed `empty` or
`upstream_restore` source, a target-replacement confirmation phrase, and exact
target/domain proofs. The lane must still be in `prelaunch` monitoring intent,
and its separately owned `odoo_data_policy` must already authorize the source;
an `empty` rebuild additionally requires `resettable` data authority. The route
does not mutate data authority, health monitoring, routes, provider targets,
runtime settings, secrets, or volume identities. Dry-run returns typed current
and requested policies plus a plan SHA-256 bound to the complete profile. Apply
requires the reviewed digest and an idempotency key, then compare-and-writes only
the selected policy. Whole-profile writes cannot change existing prelaunch
rebuild authority, and onboarding updates preserve it.

For initial seed or repair work, operators can write the same DB-backed record
directly with
`uv run launchplane product-profiles upsert --database-url ... --allow-direct-db-mutation`.
That command is an explicit local/bootstrap repair tool for creating the
Launchplane record; it is not a repo-local manifest and should not become
product repo authority.

## Public Ingress Observation Records

Public ingress observations are append-only Launchplane records under
`launchplane_public_ingress_observations`. Each record is keyed by product,
context, instance, and observation time. It stores the checked base and health
URLs, pass/fail/skipped status, failure code, redirect and HTTP evidence,
runtime identity match detail when available, and whether Launchplane delivered
a configured transition notification. Observations recorded while an incident
is open carry that incident id; an observation that caused one material event
also carries the event id. Every observation remains durable even when its
evidence is equivalent to the previous cycle and therefore does not create a
notification. Failing observations carry the typed material fingerprint and
digest used by reconciliation. Records also carry the lane's typed
`monitoring_intent` and a `purpose` of `probe` or `reconciliation`.

These records are the source for the product environment read model's
`public_ingress` and `health_monitoring` summaries. Readiness projections select
the latest `probe` record so an intent transition cannot replace measured
reachability with administrative evidence. Passing and failing observations are
both verified evidence of the latest probe; failure marks the lane unhealthy
without mislabeling current evidence as stale. A public check whose literal or
resolved destination is non-public records a failing `private_url` observation.
The `skipped` status remains readable for historical records, but current public
checks do not treat a private destination as unsupported or silently healthy.

When a check leaves the incident-eligible set, Launchplane writes a distinct
`reconciliation` observation with skipped `monitoring_intent_changed` evidence
and a separate record id. This preserves the last real probe while giving the
incident transition an explicit durable cause. Canonical fingerprints for the
product profile and any private-endpoint or route-binding authority used by the
target are checked in the same database transaction as observation, incident,
and GitHub outbox writes. If monitoring authority changed after target
discovery, the stale probe is retained as evidence but cannot open, update, or
resolve an incident; the next monitor cycle reconciles against current
authority.

## Product Environment Topology Projection

Product environment detail and summary reads expose one provider-neutral
`topology` projection for every driver. The projection keeps three evidence
states separate:

- `desired` is product-profile URL and public-domain intent.
- `provider_recorded` is the current environment route-binding authority for
  placement, bound domains, ingress provider/path/termination, and TLS owner and
  terminator.
- `observed` is runtime identity, public-ingress, and per-domain active TLS
  evidence, including certificate status, issuer and validity window,
  public-name matching, bounded presented-name evidence, incident linkage, and
  a provider-neutral likely failure cause.

Every state or observed fact carries trust/freshness and provenance. A missing
route binding remains `missing` even when provider-specific target records
exist; product reads never synthesize reassuring placement or route authority
from Dokploy records, provider target ids, or other provider payloads. Fresh
failing probes remain verified observations with a failing status, while stale
route-binding or TLS evidence is called out separately.

Observed placement can remain current between real deploy or promotion events
when the newest public HTTP observation is a fresh passing strict health probe
for the exact configured check and its expected and observed runtime identities
match the recorded placement identity under the canonical runtime identity
comparison, whose recorded health is verified and passing. In that bounded case,
`observed.placement` uses the public observation as provenance and reports
verified placement trust. The environment inventory record and the environment
read model's inventory provenance remain unchanged and may still show their true
age. Legacy, non-strict, base-page-only, superseded, stale, failing, missing,
unchecked, unverifiable, or identity-divergent evidence leaves placement trust
fail closed. No observation writes or re-timestamps deployment or inventory
records.

Typed topology warnings identify missing or disabled authority, desired versus
recorded domain divergence, placement disagreement, ingress or TLS ownership
divergence, stale evidence, missing TLS observations, certificate mismatch,
expiry, trust-chain failure, unreachability, and unsupported TLS. This lets an
operator diagnose a wrong-certificate incident from the product read itself:
the requested domain, recorded ingress/TLS owner and terminator, observed
certificate names, failure code, incident, and likely cause remain visible
without direct provider database access.

The projection omits provider evidence maps, host ids, target ids, certificate
ids, edge addresses, private resolver details, and raw provider payloads. The
legacy `target` summary is backed by the same neutral route authority and
reports only whether a physical provider-target record exists; it no longer
returns the provider target id.

Shared environment summaries keep driver-specific policy under the
`driver_extensions` namespace. Odoo data-authority and prelaunch-rebuild policy
appear only in `driver_extensions.odoo`; generic-web, VeriReel, and future
drivers do not receive misleading Odoo defaults at the shared model's top
level.

## Product Operational Readiness Projection

Operational readiness is a read-only projection over existing Launchplane
records; it is not a new persisted record family. One request addresses an
exact product, context, instance, authorization action, and optional exact
artifact ID. The projection composes:

- product-profile lane ownership and the driver action's declared readiness
  requirements;
- the single active DB-backed authorization policy and the authenticated
  GitHub Actions caller's exact managed-rule match, including immutable
  repository and reusable-workflow identity;
- provider-target and provider-neutral route-binding records;
- expected runtime-environment keys and managed-secret binding metadata;
- the requested artifact manifest, current deployment/health/runtime identity,
  and the existing product topology projection.

Overall and per-dimension states are `ready`, `blocked`, `stale`, `missing`, or
`unsupported`, with non-ready states winning over ready evidence. Missing or
ambiguous active policy, a different instance grant, mutable workflow identity,
missing expected config, disabled bindings, absent artifact/deployment records,
stale route authority, or error-severity topology warnings all fail closed.
Warning-severity topology findings remain visible as bounded dimension details
without turning an otherwise current lane into a blocked result. Driver actions
without declared readiness requirements return `unsupported` rather than
borrowing requirements from another action.

Provider-target authority is classified from the exact mutable provider-target
record rather than deployment or inventory freshness. Deployment readiness uses
the same enriched lane snapshot for current-inventory fencing and deployment
freshness, while deployment health/runtime identity remains owned by the
deployment dimension. The topology dimension owns route, public ingress, and
TLS warnings and does not duplicate a deployment-health failure as a second
topology failure.

The matching workflow grant is not considered exact merely because normal
policy matching allows the request. Its product, context, instance, action,
caller workflow, and reusable workflow selectors must each be singleton exact
values for the requested tuple. Wildcards, sibling lanes, additional products or
contexts, extra actions, and additional mutable workflow refs keep authorization
readiness blocked.

The projection reads managed-secret binding keys and status only. It does not
read secret versions or decrypt values. Runtime-environment values,
managed-secret IDs, secret plaintext/ciphertext, provider target IDs, provider
evidence maps, internal hosts, certificate references, and raw identity claims
remain outside the response. Production lanes use the same generic projection;
missing production records remain non-ready and never trigger a write.

## Public Ingress Incident Records

Public ingress incidents are Launchplane-owned lifecycle records under
`launchplane_public_ingress_incidents`. They are derived from public-ingress
observations. One stable active key is product, context, instance, canonical
check name, and check kind, while `incident_id` is occurrence-scoped and includes
the open time. Repeated failures update one open occurrence; a later failure
after resolution opens a new record instead of overwriting history. A partial
database uniqueness fence permits only one open occurrence per active key.

The incident stores first/latest observation linkage, a monotonically changing
state version, severity, typed material fingerprint and digest, latest material
event identity, notification state, and recovery progress. The fingerprint is
deterministic per check kind. It includes failure code/layer, severity, affected
target kind, material route authority, TLS state, and bounded expected-runtime
mismatch identity. It excludes observation ids, timestamps, HTTP status churn
within one category, retry counters, summaries, certificate timing evidence,
and other equivalent sensor detail. Public HTTP fingerprints use canonical
public URLs; private HTTP fingerprints use the endpoint key plus a material
authority digest rather than the private URL; provider and TLS fingerprints use
their typed provider or route-binding authority.

Material lifecycle events are append-only records under
`launchplane_public_ingress_incident_events`. Event kinds are `opened`,
`updated`, `reminder`, `resolved`, and the non-deliverable `baseline` used only
to adopt pre-migration incidents. Opening and material-update identities derive
from the incident, previous material event, and new fingerprint. Reminder
identities derive from the incident, policy, material event, and bounded reminder
window. Resolution derives from the incident and previous material event. Raw
observation identity is evidence linkage, not event identity.

Reminder state is DB-backed under
`launchplane_public_ingress_incident_reminders`, one record per incident and
matching notification policy. It stores the material-event anchor, bounded
cadence, last delivered window, and next due time. A monitor pass emits at most
one reminder for the current overdue window, so downtime or worker backlog does
not cause a catch-up storm. A material update resets the anchor. Policy removal
marks prior state inactive; resolution marks it resolved. Suppressed state may
retain its internal due window for deterministic resumption, but product read
models expose no next-reminder timestamp until the state is active again.

Acknowledged incidents suppress reminders for the acknowledged material state.
A material change clears acknowledgement and remains immediately deliverable.
Silenced incidents preserve observations and events but suppress update and
reminder delivery only until their required `silenced_until`; expiry reactivates
delivery and the next unchanged failure may emit the current overdue reminder.
Resolution is always deliverable so external issue sinks can close. A recovery
observation resolves only after the health check's configured consecutive-pass
threshold and writes one `resolved` event with `resolution_reason = recovered`.
If monitoring authority makes a check ineligible, a typed reconciliation
observation resolves it immediately with
`resolution_reason = monitoring_intent_changed`; this is not endpoint recovery.

TLS certificate observations do not introduce a parallel storage family. They
reuse `launchplane_public_ingress_observations` with `check_kind = "tls"` and a
distinct per-domain `check_name`, so aliases can open or resolve incidents
independently without overwriting the legacy public HTTP lane observation. The
per-domain check identity includes a stable domain digest so punctuation-equivalent
aliases cannot collide in observation or incident primary keys.
The payload carries typed TLS evidence only: bounded issuer and subject strings,
public-name match evidence, validity window, days remaining, route-binding
ownership/source metadata, active-probe freshness, and provider-safe incident
linkage. Full certificate chains, private topology, local resolver artifacts,
and secrets are intentionally excluded from the durable record.

Incident records are the source of truth for whether Launchplane currently
considers a lane to be in a public-ingress incident. Notification routing and
delivery are separate record families: observations say what was measured,
incidents say whether there is active operator state, and delivery records say
where Launchplane attempted to notify operators.

The Product Ops incident projection is read-only and remains subordinate to
those durable records. The environment-scoped list and detail models resolve the
requested stable lane from the DB-backed product profile, then return incident
state plus typed links to observations, material events, reminder state,
notification attempts, and outbox deliveries for that exact product, context,
and instance. External GitHub, email, and Discord notifications are delivery
sinks, not incident authority. The projection returns destination kind,
provider-safe external links, delivery state, bounded failure state, and record
ids; it does not return destination or policy ids, raw outbox payloads, provider
operation keys, provider ids, raw target URLs, secret references, or provider
error text. Product summaries may aggregate active incident state across lanes,
but detail remains anchored to the lane-owned incident occurrence.

## Public Ingress Notification Records

Public ingress notification policy records are DB-backed Launchplane records
under `launchplane_public_ingress_notification_policies`. They select enabled
destinations for incident events by product, context, and instance and store a
bounded reminder cadence. The generic default is six hours; accepted policy
values are 15 minutes through seven days. The initial destination drivers are
GitHub issues, email, and Discord. Policies store routing intent and managed
secret references only; they must not store Discord webhook URLs, SMTP
passwords, or production destination values as code defaults.

Public ingress notification attempt records are append-only evidence under
`launchplane_public_ingress_notification_attempts`. Each attempt is keyed by the
incident event, policy, and destination; `observation_id` remains evidence only.
Attempts record delivered, skipped, or failed status plus provider-safe external
ids or URLs. GitHub outbox rows use the same material event or reminder-window
identity and include both an event marker and a stable incident marker. A worker
can therefore recover the existing issue before commenting or closing even when
the original opening effect completed before its attempt record committed.
Delivery attempts are the idempotency boundary for notifications, while incident
records remain the source of truth for active public-ingress state.

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
authority by themselves. Existing lane policy changes use the bounded
`Product Prelaunch Rebuild Policy` workflow and service endpoint rather than a
whole-profile write or onboarding replay.

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
  synthesized with generic operator assertions for release drills. Passing
  records include the request nonce, exact backup/database identity, non-empty
  artifact sizes, and SHA-256 values returned by the exact Dokploy schedule
  deployment. Dokploy terminal status alone is not backup evidence; a missing,
  duplicate, malformed, or mismatched bounded completion marker writes a failed
  gate record.
- A retained-volume backup import writes the same schema-v1 logical backup
  manifest and artifact evidence under a distinct backup-gate source. Its record
  additionally binds the reviewed import plan and operation, retained source
  volume identities and labels, source and destination database identities,
  PostgreSQL 17 control/checkpoint evidence, preserved staging-clone volume, and
  exact schedule deployment. Backup verification and guarded restore accept this
  source. Routine promotion explicitly rejects it so incident-recovery imports
  cannot satisfy the ordinary backup-before-promote gate.
- Odoo backup verification accepts only the exact required, passing
  `BackupGateRecord` written by the Odoo prod backup-gate source for the same
  context and prod instance. It recomputes backup paths from DB-backed runtime
  records and requires the record's path evidence to match before provider
  inspection. Every verification attempt also writes a separate
  `BackupGateRecord` from the Odoo backup-verification source. That durable
  record binds the exact backup record, request nonce, database identity,
  artifact paths, SHA-256 values, counts, sizes, and bounded per-check status.
  A passing verification record is evidence for the dedicated restore planner;
  it does not authorize routine target replacement or promotion.
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
  A pending operation can be cancelled through the deployed service. The
  storage transition is pending-only and writes terminal cancellation evidence
  together with a failed backup-gate record so promotion cannot treat an
  abandoned pending gate as usable authority.
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
  A `pending`, `running`, or `reconciliation_required` record is the
  single-flight guard for that product/context/instance. Unsafe lease expiry
  clears the stale lease but keeps the lane blocked until an authorized operator
  has inspected provider state and records cancellation evidence.
- Odoo stable target replacement apply writes durable operation records under
  `odoo_stable_target_replacement_operations`. These records mirror the stable
  bootstrap operation boundary for the guarded `recreate-in-place` replacement
  path: the service stores the apply request, `Idempotency-Key`, request
  fingerprint, caller idempotency scope, status/phase, deployment-record id
  when available, final apply result, and terminal error. The request preserves
  the expected current artifact separately from an optional replacement
  candidate so enqueue and worker execution can reject a stale lane snapshot.
  Reusing the same key with the same request and caller identity replays the
  existing operation;
  reusing it for a different request is rejected; an active `pending` or
  `running` operation blocks another apply for the same product/context/instance
  through a storage-owned lane reservation. Filesystem reservations wait briefly
  for a concurrent owner id to settle, then give that owner record its own
  bounded settle window before clearing abandoned empty or orphaned reservations
  so an interrupted writer cannot block the lane forever.
- Odoo production backup restore apply writes dedicated operation records under
  `odoo_prod_backup_restore_operations`. The immutable plan binds the exact
  product/context/prod instance, passing backup and verification record ids and
  timestamps, current artifact, old and fresh database volumes, unchanged data
  and log volumes, archive paths, hashes, counts, sizes, staging/quarantine
  paths, target, domains, and runtime identity. The operation stores the caller
  idempotency scope, request fingerprint, reviewed plan fingerprint,
  authorization provenance, lease ownership, monotonic phase checkpoints,
  terminal result, and bounded error. A partial unique index permits only one
  pending, running, or reconciliation-required restore for a
  product/context/instance. Expired work may be recovered only before a
  provider-effect phase; after any provider effect has started, lease recovery
  moves the operation to `reconciliation_required` and preserves the lane fence
  for explicit operator review.
- Odoo retained-volume backup import plan and apply write dedicated operation
  records under `odoo_prod_retained_volume_backup_import_operations`. Plan and
  apply have separate operation kinds, request fingerprints, idempotency scopes,
  and authorization actions. Apply stores the immutable reviewed plan and exact
  plan fingerprint. Both kinds retain lease ownership, monotonic checkpoints,
  terminal evidence, and bounded errors. One partial unique index reserves the
  product/context/instance lane across plan and apply while work is pending,
  running, or reconciliation-required. Expired work is recoverable only before a
  provider-effect checkpoint; later expiry preserves a reconciliation-required
  lane fence for explicit operator inspection. When the read-only provider
  inspection terminates unsuccessfully, its checkpoint may contain the exact
  inspection schedule/deployment ids when known, request nonce, backup-record
  id, and one allowlisted failure stage/code pair. Provider log or exception
  text is not persisted in the operation record. Pre-result failures use a
  `provider_control` stage with distinct target, schedule, trigger, wait, and
  identity codes; result-read and result-parse failures use bounded `result`
  codes.
- Bootstrap, target replacement, production backup restore, and retained-volume
  backup import creation and worker claim also share one storage-level
  stable-lane reservation.
  Filesystem storage serializes the exact product/context/instance with one lock;
  PostgreSQL uses a transaction-scoped advisory lock and checks all four blocking
  operation tables before inserting or claiming. Claims choose one deterministic
  owner across legacy cross-kind queue entries, prioritizing reconciliation and
  running work before the oldest pending record. Per-table partial indexes remain
  a second same-kind defense, but no operation kind can race another into the
  same lane. The schema migration refuses to activate this worker contract when
  an existing database already contains multiple blocking operation kinds for
  one lane, so rollout cannot silently inherit an ambiguous queue.
- Odoo target-replacement plan snapshots include the exact live values for
  `ODOO_DATA_VOLUME`, `ODOO_LOG_VOLUME`, and `ODOO_DB_VOLUME`. Existing-data
  plans compare those values with resolved DB-backed desired runtime authority
  and block on any difference before an apply operation can be created. Volume
  changes remain explicit rebuild/restore decisions rather than implicit
  `data_source_mode=existing` behavior.
- New records for all five durable driver queues use schema version 2 and
  persist authorization provenance in the canonical operation payload: action,
  product, context, exact instances, managed set/rule ids, policy record id,
  revision, schema version, digest, source, authorization time, and normalized
  caller identity. GitHub Actions evidence includes repository/workflow/reusable
  workflow/ref/event/subject facts but never the bearer token or raw claims.
  The payload remains the storage authority, so this contract does not require
  promoted SQL columns or an Alembic migration.
- A worker re-evaluates the recorded caller and the same managed rule against
  the current active policy after claim and again immediately before the first
  provider mutation. A later policy revision may authorize execution only when
  that same rule still permits the exact recorded target. Missing legacy
  provenance, a missing or ambiguous active policy, removed authority, narrowed
  instances, or caller mismatch terminates the operation with a stable
  `error_code` before provider mutation. Launchplane does not fabricate
  provenance for schema-v1 queued records.
- Retained-volume backup import additionally re-evaluates the recorded caller
  and exact managed rule immediately before its first provider effect. That
  authorization remains bound for later effects in the same operation so a
  policy revision cannot stop execution after partial provider mutation; lease
  loss after the effect boundary instead preserves a reconciliation-required
  fence.
- Pending Odoo bootstrap, Odoo target-replacement, Odoo backup-restore, Odoo
  retained-volume backup-import, and VeriReel backup-gate
  operations expose authenticated `POST .../operations/{operation_id}/cancel`
  endpoints. Cancellation is idempotent after it commits, records the normalized
  caller, reason, timestamp, and trace id, releases the active-lane predicate,
  and never rewrites `running` or terminal work. Odoo operations in
  `reconciliation_required` may also be cancelled only with a structured
  safe-release attestation that records an inspection timestamp between the
  fence and cancellation request, observed provider state, and an evidence
  reference. A claim that wins the race returns `409 operation_not_pending`;
  operators must inspect that running operation rather than assume cancellation
  prevented an effect.
- The target execution model for these Odoo long-running operation records is a
  dedicated Launchplane worker process backed by DB leases and heartbeats. The
  HTTP route creates or replays the operation record and returns the poll URL;
  execution is owned by the supervised worker process, not by request-process
  daemon threads. Operation
  records carry execution fields for `attempt`, `lease_owner`,
  `lease_expires_at`, and `heartbeat_at`; terminal writes are guarded by the
  current lease owner so stale workers cannot overwrite recovered work.
  Filesystem mutations and recovery share the exact operation lock, while SQLite
  starts an immediate write transaction before reading mutable operation state;
  the recovery fence and stale-worker heartbeat/checkpoint/completion are
  therefore one atomic order. Worker
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
  that evidence is absent. Post-deploy maintenance forces the deployed artifact
  module list through `ODOO_UPDATE_MODULES`, restores the managed Launchplane
  and Enterprise addon roots when an older runtime-environment record omitted
  them, and only records pass after provider schedule logs prove a matching
  web/script-runner artifact image, an explicit module list, and successful
  install/update completion. Missing or unavailable readback evidence fails the
  deployment instead of allowing health-only readiness against stale
  database-backed Odoo views. Stable bootstrap uses the same explicit artifact
  module list, addon-path normalization, and provider-log evidence gate before
  it can report completion.
- Artifact manifests may carry `build_provenance` metadata for Odoo runtime and
  devtools base images plus build tools such as `odoo-devkit`. That provenance
  is artifact evidence, not addon ownership: `odoo-docker`, `odoo-devkit`,
  `odoo-shared-addons`, and `disable_odoo_online` stay support/dependency repos,
  while Launchplane records the immutable image/build refs used to produce an
  artifact.
- Artifact manifest schema v1 remains readable without synthetic dependency
  evidence. Schema v2 requires both complete build provenance and
  `dependency_provenance`. The dependency payload records exactly one
  `support_runtime` uv lock and one `tenant` uv lock, each bound to a source
  repository, exact git commit, repo-relative `uv.lock` path, and SHA-256. The
  tenant lock commit must equal the manifest source commit; the support/runtime
  lock repository and commit must equal one exact-source build-tool entry.
- Schema-v2 Python environment evidence is keyed by every published OCI target
  platform. Each environment records an exact Python version, canonical package
  names and exact installed versions, sanitized registry-or-VCS source evidence,
  package count,
  and a SHA-256 over compact, key-sorted JSON for the package list. VCS source
  evidence contains only sanitized repository identity and the resolved git
  commit; raw direct URLs, local paths, URL userinfo, queries, and fragments are
  not persisted.
- The package-inventory digest sorts entries by canonical package name, dumps
  each complete `{name, source, version}` object with recursively sorted JSON
  keys, ASCII escaping, and separators `,` and `:` with no whitespace, then
  hashes the resulting UTF-8 JSON array. Registry sources use
  `{kind: "registry", repository: "", commit: ""}`; VCS sources use
  `{kind: "vcs", repository: <sanitized identity>, commit: <exact SHA>}`.
- Exact-source external compatibility inputs record their repository commit,
  repo-relative dependency-file path and SHA-256, dependency-file format, and
  whether resolution was locked or explicitly exact-source/unlocked. Launchplane
  validates and stores this producer evidence; it does not fetch repositories,
  read dependency files, contact package indexes, resolve versions, or install
  dependencies.
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
- Tuple minting uses tenant and addon source SHAs only. Base-image, build-tool,
  lockfile, package, and compatibility-input provenance remains on the artifact
  manifest payload and does not expand the release tuple `repo_shas` map.
- Promotion execution copies the source channel tuple to the destination
  channel after the destination deploy passes, retaining the promotion and
  deployment record ids that established the promoted state.
- Accepted service-backed promotion evidence can mint the same destination tuple
  when the stored promotion record carries explicit `deployment_record_id`
  linkage and Launchplane already has the current source tuple for the
  promoted-from lane. Local `release-tuples write-from-promotion` is a
  file-backed rehearsal helper only.
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
- New website-bootstrap writes through the service route enforce the devkit-safe
  contract: homepage and route URLs are local Odoo route paths,
  `primary_page_xmlid` is a dotted XML ID, and at most one route can be marked
  as the homepage. The reusable service workflow prevalidates the local-route
  portion of that contract for fast operator feedback, while the service remains
  the write authority. The local CLI applies the same validation for explicit
  repair writes only. Persisted record reads remain tolerant so older records
  can be inspected and repaired instead of becoming unreadable after validation
  hardening.
- The supported operator write path is a thin dispatch workflow pinned to an
  immutable reusable worker. The worker persists through the service route,
  records only redacted request-shape evidence, and treats the write as complete
  only when the response confirms `result.website_bootstrap=true`.
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

## Tenant Repository Classification Record

- Persisted as `launchplane_tenant_repository_classifications` records under DB authority.
- Records classify GitHub repositories by numeric `repository_id` as either `engineering` (normal merge flow) or `tenant_ui` (one exact tenant-admission path is required).
- Each revision is immutable and identified by a deterministic record ID (`tenant-repository-classification-<repository_id>-r<revision>`) and payload SHA-256 digest.
- Monotonically increasing revisions (`revision=1`, `revision=2`, ...) form an append-only classification ledger. Revision 1 must not specify `supersedes_record_id`; subsequent revisions must set `supersedes_record_id` equal to the active current record ID.
- Classification writes use CAS (compare-and-swap) operator recovery: callers supply `expected_current_record_id` (empty when no record exists). Mismatches fail closed with HTTP 409 conflict, and sequence gaps or invalid supersedes links fail closed with HTTP 400. Apply reserves durable DB idempotency, locks the repository classification stream, validates CAS, appends the revision, and completes the stored response in one PostgreSQL transaction. Exact same-key, same-payload retries replay that completed response; a different key must revalidate current state and cannot replay an already-applied revision. Dry-run results report `would_apply` or `would_replay` without writing.
- Filesystem storage is rehearsal/import input only. Both filesystem and DB writers validate the append-only revision chain, and filesystem-to-DB import orders revisions oldest-first before accepting them as authority.
- Classification records are pure factual classification authority without heuristics, wildcard matching, or PR label fallbacks. Identity matches require exact `repository_id`, `repository_owner_id`, `repository` owner/name, `product`, and `context`.
- Pure tenant merge eligibility evaluates candidates against this DB authority: engineering repos take the engineering fast path, while tenant UI repos require one satisfied exact-head path from manager preview approval, technical human waiver, or trusted-maintenance evidence.
- This record and pure evaluation remain separate from scheduler merge train admission (`merge_train_admission`).

## Repository Human Admission Contracts

- Repository human role-policy contracts bind one revision to exact numeric GitHub repository and owner IDs plus repository, product, and context. They name repository-owner humans, primary managers, optional backup managers, and direct time-bounded manager delegations without hard-coding people in code or checked-in configuration.
- A delegation is valid only while its current role-policy revision is active and effective, its grantor remains a primary or backup manager, and its start, expiration, and revocation timestamps permit it. Silence or elapsed review time never creates approval authority.
- Technical human waiver events are append-only create/revoke evidence. Creation
  requires a browser-authenticated GitHub human session whose numeric
  `github_id` is positive, whose ID is a current repository owner in exactly one
  active role policy for the candidate repository/product/context, and whose ID
  is explicitly present in exactly one managed schema-v2
  `tenant_technical_human_waiver.write` GitHub-human authorization rule. Login,
  org, team, role-only, local-admin/operator, GitHub Actions, terminal-agent, and
  Every Code identities are never write authority for this record type.
- Waiver evidence binds repository, product, context, pull request, exact head
  SHA, classification revision/digest, role-policy revision/digest, active
  authorization-policy revision/digest, human numeric identity, display login,
  source event, reason, authoritative database/server occurrence time,
  `recorded_at`, and optional creation expiration. Apply callers cannot provide
  `occurred_at`, author ID, or author login; Launchplane builds the binding,
  authorization provenance, event IDs, and digests inside the domain builder.
  `recorded_at` equals the authoritative occurrence time. New commits or any
  bound policy/classification/authz drift make prior evidence stale; revocation
  wins a same-timestamp tie.
- The role-policy read model is keyed by immutable `repository_id`, `product`,
  and `context`. It returns `missing`, `available`, or fail-closed
  `ambiguous` state plus the active current record when exactly one current tip
  exists. Authorization uses `repository_human_role_policy.read` against the
  submitted product/context and an explicit context target; repository names,
  paths, actor strings, logins, and changed files are never authority hints.
- Role-policy dry-run/apply accepts a strict envelope containing the candidate
  role-policy record plus the caller's expected current tip record ID and digest
  (both empty only for revision 1). Dry-run validates with filesystem or DB read
  stores and writes nothing. Apply is PostgreSQL-only, requires a non-empty
  `Idempotency-Key`, rejects terminal agents, authorizes
  `repository_human_role_policy.write` against the submitted product/context,
  and performs reservation, stream advisory lock, CAS/current-tip validation,
  supersede plus insert, stored-response completion, and commit in one database
  transaction. Same key plus same canonical request replays the stored HTTP 202
  response; same key plus changed request returns `idempotency_key_reused`.
  Repeating the exact currently active record under a new key also returns a
  replay without adding history, but the request must retain its original
  predecessor record ID and digest CAS.
- Role-policy apply fails closed on missing, ambiguous, stale, scope-drifted,
  conflicting, inactive, or sequence-invalid candidates. Request-provided
  superseded records are ignored; the database writer derives supersession from
  the locked current stream. The separate technical-human waiver apply route does
  not add trusted-maintenance evidence, unified status, controller changes,
  rollout decisions, UI controls, GitHub provider calls, or Launchplane
  authz-policy mutation.
- Filesystem storage can rehearse role-policy revision history and technical
  human waiver event history locally. Shared PostgreSQL storage now persists
  `launchplane_repository_human_role_policies` and
  `launchplane_tenant_technical_human_waiver_events` with canonical payloads,
  promoted filter/audit columns, serialized role-policy stream writes, one
  active role-policy tip per repository/product/context, and append-only waiver
  event replay/conflict semantics.
- `POST /v1/tenant-admission/technical-human-waivers/apply` accepts strict
  `mode: dry_run|apply` and `action: created|revoked` envelopes with candidate,
  expected classification/role-policy/authz record IDs plus digests, source event
  kind/id, reason, optional creation expiration, and revoke-only expected current
  waiver ID plus event digest. Dry-run uses the pure read/planning helpers and
  may run against rehearsal stores without writing. Apply is PostgreSQL-only,
  requires a non-empty `Idempotency-Key`, scopes idempotency by numeric GitHub ID
  (`github-human-id|<id>`), locks classification, role-policy, authz-policy, and
  waiver binding/history authority in deterministic order, revalidates all
  expected IDs/digests and lifecycle CAS under lock, appends the event, verifies
  the resulting path, stores the HTTP response, and commits once. Same key plus
  same canonical body replays the stored response with the original trace; same
  key plus a changed body returns conflict; a different key revalidates current
  authority and history.
- Manager-preview authorization can carry the same role-policy provenance for primary, backup, or delegated managers. Legacy approval records remain readable, but they cannot satisfy an evaluation once a repository role policy is explicitly enforced.
- Trusted-maintenance policy records are a separate contract, not a human role
  policy and not a generic authz-policy reuse. Each policy revision is keyed by
  immutable numeric `repository_id` plus `repository_owner_id`, `repository`,
  `product`, and `context`; has `status: active|superseded`, source, reason,
  `effective_at`, and optional evidence TTL; and uses CAS-friendly
  `supersedes_record_id` chaining. The canonical policy digest excludes mutable
  lifecycle `status` and audit-only display logins.
- Trusted-maintenance v1 actor rules require one explicit positive numeric
  GitHub PR author ID with actor type `Bot`, explicit positive numeric sender
  IDs with sender type `Bot`, and an explicit allow-list of signed GitHub event
  names and actions. Display logins are audit only and are never matching
  authority. The contract does not store or infer a GitHub App ID because this
  slice has no reliable GitHub fact source for it.
- Trusted-maintenance matching never uses repository name alone, branch/ref,
  changed files, labels, commits, PR title/body, semantic inference, actor or
  sender login strings, or blanket bot bypass. Same-repository PR head identity
  is required for v1.
- Trusted-maintenance evidence is append-only captured evidence bound to the
  exact candidate repository tuple, pull request, head SHA, current repository
  classification record/revision/digest, current trusted-maintenance policy
  record/revision/digest, matched actor rule ID, PR author numeric ID/type,
  signed-event sender numeric ID/type, same-repository head numeric IDs, event
  name/action, delivery/source ID, DB/server `occurred_at=recorded_at`, and
  source, signed request-body SHA-256 digest, audit-only delivery ID,
  DB/server `occurred_at=recorded_at`, and optional expiration derived only from
  policy TTL. Request-provided times, authors, Launchplane authz-policy IDs, and
  login strings are outside this evidence authority.
- Evidence identity is the normalized source plus the SHA-256 digest of the
  already signature-verified request body. Reprocessing the same signed body
  with the same trust-bearing binding replays the first persisted record even
  when the unsigned delivery header, audit-only logins, or later processing
  timestamp differ. Reusing that signed-body identity with a different
  repository, head, classification, policy, actor, sender, or event binding is
  a conflict. Delivery ID remains required audit metadata but is not identity or
  trust-bearing binding authority.
- Policy dry-run/apply accepts a strict envelope containing the candidate
  trusted-maintenance policy record plus the caller's expected current policy
  record ID and digest, both empty only for the first revision. `GET
  /v1/work-graph/tenant-admission/trusted-maintenance-policy` requires
  `trusted_maintenance_policy.read`; `POST
  /v1/tenant-admission/trusted-maintenance-policies/apply` requires
  `trusted_maintenance_policy.write`. Both actions are separate from repository
  human role-policy actions and are scoped to the submitted product/context.
  Apply is browser-GitHub-human-only, PostgreSQL-only, requires a non-empty
  `Idempotency-Key`, and reserves idempotency, locks the policy stream,
  validates CAS, writes/replays the response, and commits in one database
  transaction. Dry-run writes nothing and may use rehearsal/read stores.
- Policy apply compares and writes the expected active tip inside the same
  filesystem authority lock or PostgreSQL advisory/row-lock transaction.
  Current-authority reads validate the complete revision and supersession chain,
  and evidence evaluation re-derives expiration from the bound policy TTL rather
  than trusting a stored expiration value alone.
- Pure trusted-maintenance evaluation returns
  `TenantAdmissionPathResult(path_kind='trusted_maintenance')` and fails closed
  on head, repository identity, classification, policy, actor/sender/event
  provenance, expiration, or ambiguous/missing authority drift. Filesystem
  storage can rehearse trusted-maintenance policy history and evidence locally;
  shared PostgreSQL storage persists
  `launchplane_trusted_maintenance_policies` and
  `launchplane_trusted_maintenance_evidence` with canonical JSON payloads,
  promoted query/audit columns, one active policy tip per
  repository/product/context, exact-head/evidence/policy/actor indexes, and
  critical schema invariants.
- Trusted-maintenance evidence capture is invoked only after existing signed
  GitHub webhook verification in both current ingress surfaces: `POST
  /v1/manager-preview-approval/github-webhook` and `POST
  /v1/every-code/github-webhook`. This common post-signature handler adds no new
  route, webhook secret, durable raw receipt table, or runtime config. It only
  considers authenticated `pull_request` deliveries with an explicit policy
  event/action match. Before any GitHub API call it uses the signed numeric
  repository tuple, PR number, sender ID/type/login, PR author ID/type/login,
  and head SHA as structural pre-filter facts; if no current `tenant_ui`
  classification authority or exact signed rule candidate exists, the delivery
  is accepted/skipped and no provider call or evidence write occurs.
- For relevant deliveries, Launchplane resolves the GitHub token from the
  DB-authoritative repository classification product/context, re-fetches the
  current PR, and persists only re-fetched current facts. The base repository
  numeric ID, owner, and full name must exactly match the signed tuple; the PR
  must still be open; the re-fetched PR author numeric ID and type must match the
  signed author identity, while the current login is stored for audit only; the
  re-fetched head SHA must equal the signed head SHA; and the head repository
  numeric ID/owner/full name must exactly equal the base repository, preserving
  same-repository-only v1 evidence. Missing,
  indeterminate, forked, stale, closed, non-Bot, login-only, or mismatched facts
  fail closed with no evidence.
- PostgreSQL capture uses one transaction and one database/server timestamp. It
  locks and re-reads repository classification and the full trusted-maintenance
  policy history at write time, requires the expected authority record IDs,
  revisions, and digests to remain exact, evaluates the numeric actor, sender,
  event, and action rule, then appends or deterministically replays evidence in
  that same transaction. Policy or classification drift never produces success.
  The evidence source is the fixed canonical generic GitHub webhook source;
  signed-body replay is deterministic even if the unsigned delivery header
  changes, while changed trust-bearing binding conflicts are rejected.
- Invalid signatures, missing deliveries, and malformed payloads stop at the
  existing ingress boundary and never call the trusted-maintenance handler.
  Unsupported, nonmatching, non-Bot, fork, closed, and stale cases return
  accepted/skipped. Transient token resolution, GitHub API, or database
  uncertainty on an otherwise relevant delivery returns retryable 503 and writes
  no evidence; exact GitHub redelivery or existing signed replay-envelope
  tooling is the reconcile path. Responses do not expose policy actor IDs or
  logins.
- Unified tenant admission is a recomputed read model, not a fourth durable
  approval record. It resolves the current numeric repository classification
  and, for `tenant_ui`, evaluates the exact candidate against current manager
  preview approval, technical human waiver, and trusted-maintenance evidence.
  One satisfied path admits the candidate; missing, ambiguous, stale, denied,
  expired, or unavailable authority cannot create success.
- The public read model exposes only the candidate, classification binding,
  decision, path states, generation time, and one category: `engineering`,
  `pending`, `manager-approved`, `technical-waived`, `maintenance-admitted`,
  `stale`, `denied`, or `unavailable`. It does not expose manager identities,
  policy memberships, private provider topology, tokens, or secret values.
- The classic GitHub `tenant-admission` commit status is a non-authoritative
  projection of that recomputation. Reconciliation first re-fetches the open PR
  and verifies its numeric base-repository ID, numeric owner ID, full name, and
  exact head SHA. It then recomputes from DB records and writes or replays the
  status on that exact SHA. GitHub read/write uncertainty returns retryable
  failure and never manufactures a passing decision. Engineering candidates do
  not require or receive this tenant-only projection.
- Legacy manager-preview records that store only a bare repository name remain
  compatible only when their bound PR URL is the canonical
  `https://github.com/OWNER/REPO/pull/N` URL for the exact candidate. A different
  owner, host, PR number, query, or fragment cannot satisfy tenant admission.
- Merge-controller enforcement, branch protection, portfolio rollout, UI, and
  real repository policy values remain separate follow-up work. Blanket Bot
  bypass and changed-file, repository-name, branch, title, or label heuristics
  are not supported. Preview refresh, verification, destroy, and cleanup remain
  independent from every admission path and from GitHub projection delivery.

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
- Ready generation evidence preserves the runtime identity checked by the
  preview health verifier so downstream approval can bind the exact observed
  product, context, source ref, preview runtime, deployment, artifact, and image.
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

## Manager Preview Approval Event Record

- One append-only event per manager decision or lifecycle invalidation for an
  exact rendered preview identity. Events use deterministic ids derived from
  the exact binding, action, and source event so delivery retries replay without
  overwriting history; a conflicting replay is rejected.
- The binding captures product, context, repository, pull request, head SHA,
  preview and serving-generation ids, artifact id and immutable image digest,
  resolved manifest fingerprint, preview URL, and the full checked runtime
  identity plus canonical runtime/binding digests.
- Manager-authored `approved`, `changes_requested`, and `revoked` events require
  a stable GitHub numeric identity and exactly one schema-v2 managed
  authorization rule granting `manager_preview_approval.write` for the product
  and context. The event stores the display login only as audit presentation and
  records the managed rule ids plus authorization policy record id, revision,
  source, and digest.
- Lifecycle-authored `superseded` and `invalidated` events preserve teardown,
  PR-close, generation-replacement, and related history without impersonating a
  manager. Preview destroy and cleanup remain available independently of
  approval and never consult approval as an admission gate.
- The decision projection is computed from the append-only ledger and current
  preview/generation/policy evidence. It returns `pending`, `approved`,
  `changes_requested`, `revoked`, `stale`, or `unavailable` with a public-safe
  reason. Any head, serving generation, artifact digest, manifest, runtime
  identity, verification, preview state, or policy mismatch fails closed.
- People-based manager resolution is private agent routing for communication and
  planning only. It is not persisted in this record and is never runtime
  authorization. Launchplane's active managed policy is the authority; GitHub
  interaction and promotion-check projection are separate downstream adapters,
  and tenant repositories own only their thin workflow integration.
- Signed `issue_comment.created` delivery is the manager interaction adapter.
  Launchplane re-fetches the comment actor and current pull-request head, then
  accepts only an exact `/preview approve|changes|revoke <binding_sha256>`
  command. Delivery replay returns the existing append-only event, while actor,
  head, fingerprint, serving-generation, and policy mismatches write nothing.
- `manager-preview-approval` is a GitHub status projection of this record, not
  authority. The service updates only a marker comment owned by the authenticated
  Launchplane credential and projects `pending`, `success`, `failure`, or
  `error` on the current head. GitHub write failure never rewrites or deletes
  approval evidence.
- Preview refresh and verification routes reconcile the projection after their
  durable record changes. Pull-request synchronize, reopen, close, preview-label
  removal, isolated destroy, and managed-policy updates also re-project current
  evidence. Destroy and cleanup proceed even when GitHub is unavailable; an
  authenticated reconciliation request can retry the projection later.
- Policy-scoped live promotion joins the testing artifact digest and source SHA
  to exactly one active serving preview, includes the approval decision in the
  promotion evidence fingerprint, and denies before provider mutation unless
  the decision is `approved`. Removing the managed approval rule disables this
  admission requirement without deleting event history.

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
- Every claim is atomic and sets three lease fields: `lease_expires_at` (ISO
  timestamp when the lease lapses), `fencing_token` (monotonically increasing
  integer equal to the cumulative claim count for this request id), and `attempt`
  (same value, kept separately for readability). The fencing token starts at 1 on
  the first claim and increments on every subsequent requeue-and-reclaim cycle.
  Once a record has a non-zero fence, service status updates must carry that
  exact `fencing_token`; missing and stale tokens are rejected before the row is
  changed. Heartbeats enforce the same host-and-fence match, preventing
  stale-owner writes after a lease expires and a new worker reclaims.
- Workers send periodic heartbeats through `POST /v1/every-code/work-requests/heartbeat`
  to extend `lease_expires_at` before it lapses. A heartbeat is rejected (409)
  when the host or fencing token does not match, or when the request is already
  terminal. Workers that die without heartbeating leave the record with an expired
  lease and the recovery path handles it.
- `POST /v1/every-code/work-requests/recover-stale` scans for claimed or running
  records whose `lease_expires_at` has passed and applies a recovery policy:
  `safe_requeue` (attempt ≤ 3) resets the record to `queued` with all lease
  fields cleared so another worker can pick it up; `manual_review` (attempt > 3)
  marks the record `blocked` with an error message requiring operator inspection
  before any requeue. Recovery locks and compares the exact stale snapshot, so a
  concurrent heartbeat or status transition wins cleanly instead of being
  overwritten by a stale recovery decision.
- Service-backed workers invoke the same recovery route during their polling
  loop. Filesystem-backed workers serialize claim, heartbeat, status, and
  recovery transitions with per-request process locks and publish JSON records
  through atomic replacement.
- Requests that were already active before lease fields existed migrate with an
  expired lease and an exhausted safe-retry attempt. Their first recovery marks
  them `blocked` for manual review instead of risking duplicate execution.
- Worker-token claim and rerun requests use a stable synthetic idempotency scope.
  PostgreSQL claim commits the claimed record and completed replay evidence in
  one transaction; rerun uses compare-and-write with the completed response in
  the same transaction. Same-key retries replay the original response, changed
  payloads conflict, and a lost HTTP response does not permit a second claim or
  rerun mutation.
- The local worker handoff is `uv run launchplane every-code run` for polling or
  `uv run launchplane every-code run-once` for a single scan. Each pass applies
  trusted PR feedback, reconciles preview gates and ready preview labels, removes
  stale source-issue queue labels for closed requests that can no longer reach
  preview readiness, routes failed checks back to the owning session, then claims
  at most one queued request. Request handoff terminates any stale deterministic
  tmux session before launching a newly claimed attempt, records `running` or
  immediate `blocked` status, and wraps the visible command so terminal success
  or failure calls `uv run launchplane every-code finish` with the fencing token
  captured at launch. A recovered or superseded session therefore cannot read a
  newer token and finish as the new owner.
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
  trusted for Every Code source-issue validation. Those semantics are distinct
  from Launchplane manager approval of a rendered product preview: local People
  or planning maps never authorize runtime approval. Bot-authored and untrusted
  human comments are accepted-but-skipped so webhook delivery remains idempotent
  without sending automation chatter to Every Code.
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
  The fresh snapshot retains the PR author's immutable GitHub numeric user id so
  trusted-automation decisions remain auditable against the active policy; that
  id is not copied into the compact public queue response.
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
- Runner lane lifecycle audit records are the durable record for the narrow
  Launchplane-controlled repository-runner registration and retirement host
  adapters. They retain the compatibility name
  `launchplane_runner_lane_registration_audits`, are written through
  `POST /v1/evidence/runner-lane-registration/audits`, and distinguish
  `register` from `retire` through the typed operation field. Records preserve
  dry-run or apply evidence without storing GitHub runner registration tokens or
  other credentials.
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
- For a second product such as VeriReel, shared inventory should first be
  derived from accepted service-backed deployment/promotion evidence before
  Launchplane becomes the runtime executor for that product. Local
  `inventory write-from-deployment` and `inventory write-from-promotion` remain
  file-backed rehearsal helpers only.
