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
the versioned migration mechanism for shared-service Postgres databases. Runtime
code can still call `ensure_schema()` for compatibility and ephemeral test/local
databases, but new production schema changes should land as Alembic revisions.

For a fresh database, apply the current schema with:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run alembic upgrade head
```

For an existing Launchplane database that already has the tables created by the
pre-migration `create_all` path, adopt the baseline by stamping the database at
the current revision after confirming the live table shape matches the ORM:

```bash
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://... uv run alembic stamp head
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
  latest promotion, latest backup gate, target metadata, runtime environment
  records, Odoo override metadata, and secret binding status.
- `LaunchplanePreviewSummary`: preview identity plus recent/latest generation
  state.

These summaries are read models, not new durable record families. They compose
existing ORM rows and contract payloads behind the storage boundary so the next
driver descriptor and GUI slices can consume a stable API shape.

## Field Promotion Audit

The current ORM tables already model the first layer of queryable operational
state. Use this audit when deciding whether a new GUI or driver field belongs in
an ORM column/table or remains only in the evidence payload.

- Artifact manifest: modeled fields are `artifact_id`, `source_commit`,
  `image_repository`, and `image_digest`. Source input details, addon selectors,
  and provider/build evidence stay payload-only until they become normal query
  or action fields.
- Backup gate: modeled fields are `record_id`, `context`, `instance`,
  `created_at`, and `status`. Concrete backup paths and provider-specific backup
  evidence stay payload-only.
- Deployment: modeled fields are `record_id`, `context`, `instance`,
  `artifact_id`, `source_git_ref`, and deploy timestamps. Resolved provider
  evidence, health detail, and post-deploy product facts stay payload-only.
- Promotion: modeled fields are `record_id`, `context`, `from_instance`,
  `to_instance`, `artifact_id`, and deploy timestamps. Rollback annotations,
  backup evidence detail, and provider health envelopes stay payload-only.
- Inventory: modeled fields are `context`, `instance`, `artifact_id`,
  `source_git_ref`, `updated_at`, and linked deployment/promotion ids. Full
  deploy evidence and product-specific live facts stay payload-only.
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
  `updated_at`, and `policy_sha256`. The parsed GitHub Actions and human grant
  policy stays payload-only until Launchplane needs per-rule filtering or
  browser-side policy editing.
- Dokploy target id: modeled fields are `context`, `instance`, `target_id`, and
  `updated_at`. Provider lookup/import evidence stays payload-only.
- Dokploy target: modeled fields are `context`, `instance`, and `updated_at`.
  Provider-specific names, domains, policies, schedule, and app details stay
  payload-only until a provider-neutral target model needs them.
- Runtime environment: modeled fields are `scope`, `context`, `instance`, and
  `updated_at`. Individual key/value settings stay payload-only until GUI
  filtering or editing requires a setting table.
- Odoo instance override: modeled fields are `context`, `instance`, and
  `updated_at`. Typed Odoo override payloads stay payload-only until
  cross-driver settings need generic structure.
- Secret: modeled fields are `secret_id`, `scope`, `integration`, `name`,
  `context`, `instance`, `status`, `current_version_id`, and `updated_at`.
  Descriptions, validation detail, and encrypted version payloads stay
  payload-only.
- Secret binding: modeled fields are `binding_id`, `secret_id`, `integration`,
  `binding_key`, `context`, `instance`, `status`, and `updated_at`. Binding
  implementation details beyond status and lookup stay payload-only.
- Secret audit event: modeled fields are `event_id`, `secret_id`, `event_type`,
  and `recorded_at`. Actor, detail, and metadata stay payload-only until audit
  filtering needs more columns.

Promote a payload field into ORM structure when Launchplane needs to filter,
order, join, authorize, constrain, display it regularly, or drive an action from
it. Keep unstable provider envelopes, replay/debug context, and raw evidence in
JSONB until they graduate into normal product behavior.

## Product Profiles

Product profile records are DB-backed Launchplane configuration for product
identity and driver selection. They hold product key, display name, owning repo,
driver id, image repository, runtime port, health path, stable lane bindings,
and preview context policy.

These records replace repo-local Launchplane lifecycle manifests. Product repos
still own their normal app/runtime contract, such as Dockerfile, image publish,
health endpoint, tests, and source/build inputs. Launchplane owns the product
profile that maps those app facts into preview, deploy, promotion, and evidence
behavior.

The service exposes product profile records through `GET /v1/product-profiles`,
`GET /v1/product-profiles/{product}`, and `POST /v1/product-profiles`. Writes
require the `product_profile.write` action for the target product in the
Launchplane service context; reads use `product_profile.read`.

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
- Odoo prod rollback writes a normal deployment record for the rollback deploy,
  refreshes prod inventory, mints the prod release tuple from the selected
  artifact manifest, and annotates the current prod promotion record's
  `rollback` and `rollback_health` fields. The selected rollback source is the
  DB-backed `testing` release tuple; operators must not supply unrecorded image
  refs or source SHAs.
- Direct `ship` and `promote` execution fail closed if the referenced artifact
  id does not already have a stored manifest in control-plane state.
- Artifact manifests may also carry `addon_selectors` metadata so operators can
  inspect the original selector intent, but `addon_sources` remains the exact
  SHA-backed release truth used for tuple minting and deploy execution.
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
- Shopify guard values such as protected store keys now belong in
  `policies.shopify.protected_store_keys` on this record instead of a route map
  hardcoded in Python.
- The operator write path for this record family is the Launchplane CLI,
  including `dokploy-targets list`, `show`,
  `put-shopify-protected-store-key`, and
  `unset-shopify-protected-store-key`.
- Repo-local Dokploy target TOML files are not a supported runtime authority or
  mutation surface for these records.

## Odoo Instance Override Record

- One record per Odoo context and stable instance.
- Record Odoo application intent in typed fields instead of treating
  `ENV_OVERRIDE_*` names as the durable contract.
- `config_parameters` stores explicit `ir.config_parameter` writes such as
  `web.base.url`.
- `addon_settings` stores addon-shaped intent such as Authentik SSO or Shopify
  settings without coupling Launchplane records to environment variable names.
- `apply_on` records the phases where the override is intended to apply, and
  `last_apply` records the latest driver result without making the addon layer
  the durable audit surface.
- Secret-shaped values must reference Launchplane managed secret bindings; list
  and show commands return only keys and counts, not plaintext values or binding
  ids.
- This record is the target authority for the Odoo driver. Runtime-environment
  `ENV_OVERRIDE_*` keys remain a migration input to retire, not the final
  override model.
- The compose post-deploy bridge renders one typed Odoo override payload for
  the data-workflow runner; it does not make legacy `ENV_OVERRIDE_*` names the
  deploy-time contract.
- Secret-backed values still avoid Dokploy schedule plaintext. The payload
  points at the already-present neutral `ODOO_OVERRIDE_SECRET__*` container
  environment key for each managed secret binding, and the driver asserts those
  keys before invoking Odoo.

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
