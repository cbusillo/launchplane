---
title: Dokploy Service Deployments
---

## Purpose

Use this contract for a simple product service that Launchplane deploys to a
Dokploy application target. The service may be a bot or worker instead of a
public website, but it still follows the same Launchplane-owned lifecycle:
publish an immutable image, resolve product profile and lane records, mutate the
Dokploy target through Launchplane, and write deployment evidence here.

Discord Blue is the first target product for this service-shaped contract. It
should not keep long-term host-managed `systemd` or `uv` deployment ownership
once this contract is live.

## Supported Model

Launchplane supports a simple service when all of these are true:

- the product publishes one immutable container image per release candidate
- the runtime is a single Dokploy application target per stable lane
- required runtime settings can be represented by Launchplane runtime
  environment records and managed secret bindings
- persistent state is mounted by the Dokploy target and documented as part of
  the product runtime contract
- readiness can be represented by either a small HTTP health endpoint or an
  explicitly skipped health check when the lane has no reachable health URL

Use `driver_id="generic-web"` for this model. The driver name is historical: for
service deployments it means reusable image deploy, health evidence, preview
policy when enabled, promotion evidence, and Launchplane-owned Dokploy
mutation. It does not require the product to expose a public website.

## Adapter Implementation Boundaries

The Dokploy adapter keeps one import-compatible package surface while its
implementation is divided by ownership:

- `control_plane.dokploy.source` owns checked-in bootstrap parsing, DB-backed
  target/source resolution, credentials, and provider target models.
- `control_plane.dokploy.api` owns generic HTTP requests, deployment lifecycle,
  logs, environment text, and schedules.
- `control_plane.dokploy.compose` owns compose rendering, validation, route
  labels, and raw-source synchronization.
- `control_plane.dokploy.post_deploy` owns product-specific post-deploy and data
  workflow orchestration plus generated provider scripts.

Production callers import the canonical owning module. The
`control_plane.dokploy` package root preserves established imports, but tests
and new code patch dependencies at the canonical module so the compatibility
surface does not become a second implementation authority.

### Environment wire formats

Dokploy application targets consume one dotenv layer. Docker Compose targets
consume two: Dokploy parses the stored environment and writes those values to
the generated `.env`, which Compose then reads for interpolation and `env_file`.
The adapter adds the second layer of quoting and dollar escaping for Compose,
with a format comment so readback, merges, credential checks and retirement
recover the literal values. Legacy unmarked environments keep their first-stage
meaning and move to this encoding on the next ordinary environment write. The
wire quotes are transport data, not extra quotes in product settings. Swarm
stack targets retain their separate shell-export format.

Render the target-specific environment before recording its write checkpoint.
Multiline text, surrounding quotes, whitespace, backslashes and ordinary dollar
expressions are supported when both provider stages can represent them. NUL,
some combinations of quote delimiters and escape sequences, and literal
Dokploy substitution expressions are refused without printing values or issuing
the environment write. For a Compose-only encoding refusal, use an application
target through the supported lane setup; do not flatten or encode product
settings. Dokploy's reserved substitution expressions require a provider that
can consume them literally if they are intended as literal input.

`scripts/qualify-dokploy-compose-environment.sh` exercises the tracked pinned
Dokploy generated-file functions and actual Compose `config`, covering both
interpolation and service `env_file` consumption with synthetic values only.
Provision Node through the repository's CI Node setup and Compose through
`scripts/setup-ci-compose.sh` using the CI-declared version before running it.
The frontend validation CI job runs this qualification; ordinary unit discovery
does not depend on a developer's installed Node or Compose. No containers start.

Odoo post-deploy sender verification follows the deployed image's bootstrap
contract. On a rollback or exact artifact re-deploy, when a company email is
requested, the maintenance schedule reads and
parses that image's `odoo_website_bootstrap.py` after the module update. A
recognized historical bootstrap without the company-email readback marker may
pass on its existing module-update and website-bootstrap proof; evidence records
`website_bootstrap_company_email_skip_reason=artifact_bootstrap_predates_company_email_readback`.
This means the sender was **not verified**, not that it matched. Rollbacks and
re-deploys retain this compatibility without rebuilding the pinned artifact.
Current bootstrap scripts still require the sender-match marker, including on
rollback. New forward artifacts always require the marker, regardless of the
bootstrap script's contents. An exact re-deploy must match the existing runtime
identity's product, lane, artifact, source commit and immutable image reference,
and the passing inventory's artifact and source commit. Identity written by a
failed forward attempt does not qualify its retry as a historical re-deploy.
Maintenance, standalone post-deploy and backup-restore verification also support
the existing artifact's historical contract; these calls do not select a new
forward artifact. The override record's last-apply detail records the sender
verification caveat, alongside deployment evidence.
Missing capability evidence, an unreadable or unrecognized
script, incomplete bootstrap proof, or an explicit sender mismatch never earns
the historical exception. Other post-deploy, integration and health gates remain
in force.

## Managed Application Schedules

Dokploy application schedules are provider-owned commands that run in the
target application container. Launchplane therefore uses the application's
loopback HTTP listener only when the product contract explicitly exposes an
in-container route. It does not pass runtime secrets in a schedule command or
argv. Every managed application schedule is read back after create or update;
the readback must exactly match its name, cron expression, type, shell,
command, application id, enabled state, and timezone. Duplicate managed names
for one application fail closed instead of being selected arbitrarily.

Testing and preview maintenance and billing commands are product-profile
declarations under the [generic-web lifecycle](driver-development.md#profile-maintenance-and-explicit-testing-reset).
Launchplane pauses owned schedules before changing the image, runs maintenance,
checks health, and canaries enabled jobs before activation. Pre-image failure
restores the baseline only with proved application restoration. After an image
effect, failure holds jobs disabled. Preview refresh saves the same deployment
receipts used by stable lanes, so removing or renaming a declaration pauses and
removes the previous job without touching unrelated provider schedules. Preview
destroy removes the application and its attached schedules together.

Command timeouts belong to each profile declaration. Dispatch is attempted once:
a timeout or ambiguous schedule result is not authority to replay a potentially
completed migration. A caller can retry through the recorded operation's
supported recovery after resolving its provider state. Production declarations
and schedules are outside this nonproduction conversion.

Create a product-specific driver only when the service needs behavior beyond
this contract, such as data migrations, product-specific smoke checks,
destructive repair actions, backup gates, or custom rollback state.

## Launchplane Records

The Launchplane-owned product profile is the service identity and lane map. It
must declare:

- `product`: durable product key, for example `discord-blue`
- `repository`: owning GitHub repo, for example `cbusillo/discord-blue`
- `driver_id`: `generic-web`
- `image.repository`: immutable image repository, for example
  `ghcr.io/cbusillo/discord-blue`
- `runtime_port`: the service's internal HTTP port when it exposes one, or `0`
  when the product has no HTTP runtime surface
- `health_path`: a path beginning with `/` when Launchplane, Dokploy, or public
  ingress monitoring should verify HTTP health; leave it empty only for
  non-HTTP workers with health checks and public ingress monitoring disabled
- `lanes`: stable instances such as `testing` and `prod`, each with a
  Launchplane context and optional `base_url` or `health_url`

Each stable lane also needs DB-backed Dokploy target records:

- `DokployTargetRecord` keyed by `context` plus `instance`
- `target_type="application"`
- `target_name` or `project_name` matching the admin-owned Dokploy route
- `deploy_timeout_seconds` when the service needs a longer rollout window
- `healthcheck_*` fields when Dokploy should independently monitor the service
- `env` only for non-secret provider settings that Launchplane owns
- sibling `DokployTargetIdRecord` carrying the live Dokploy application id

For an existing Dokploy app, use `launchplane dokploy-targets adopt` to create
or refresh those target and target-id records from the live provider id. The
adoption command is dry-run by default and requires `--apply` to write records;
local `--apply` also requires `--allow-direct-db-mutation` and is explicit
local/bootstrap repair only. It does not copy provider env text into
Launchplane. Provider application creation for application targets is available through
`launchplane dokploy-targets create-application`. That command is also dry-run
by default; with local `--apply --allow-direct-db-mutation`, it can create or
reuse the Dokploy project and environment, create the application, and
immediately persist the matching target records. Routine shared/live target
setup should use the deployed service route or admin workflow instead. It
still leaves runtime env, managed secrets, volumes, ports, and health behavior
as explicit setup rather than inferred provider state.

### Testing application state volume

`POST /v1/dokploy-targets/setup`, `operation: create-application`, optionally
accepts `state_volume_path`, the product's reviewed absolute container state
directory. This option creates only a new testing application in a new project
and environment on the supplied server. Existing target replacement keeps the
usual exact `expected_current_provider_target`, never-deployed evidence,
isolation-staging hold and deployment lock. It accepts no volume name, host
path, mount id, existing application id or caller-selected `app_name`.

An empty Compose may retain the provider's GitHub source defaults. Replacement
checks the complete live source with the existing empty-source validator, both
when planning storage and at each apply checkpoint. A configured or partial
repository, credential binding, image or raw Compose source refuses; unavailable
or mismatched provider evidence also refuses. No stored source normalization is
needed. Unavailable source reads report `testing_target_source_evidence_unavailable`
rather than a source-policy refusal. Earlier allocation effects, if any, still
need the existing partial-setup recovery path. Live reads run outside the global
authority write transaction; its final checkpoint rechecks the recorded lane
authority.

Start with `mode: dry-run`. The service saves only planning evidence in the DB
and returns `state_volume_plan_id`, `plan_sha256`, the exact target intent and
`state_volume: {name, mount_path}`. The service generates the new volume and
application names, and returns `allocation` with the effective generated project,
environment, target and application names. Caller names remain reviewed labels;
only the generated names identify recoverable provider placement. No provider
resource is created by a dry run. Keep the
private request and response together. For `mode: apply`, add the returned
`state_volume_plan_id`, `expected_plan_sha256: <plan_sha256>`, the normal setup
confirmation and reason, and an `Idempotency-Key`. The same caller must use the
saved plan. Changed input, lane authority, ownership or provider binding refuses
before effects. A fresh dry run is needed after an intentional change.

The durable provider runner fences the lane and records each creation phase.
The new application must have empty mounts and match its generated identity and
reviewed placement. Its single named volume is attached and independently read
back before the application becomes the tracked target. The ordinary image and
runtime-input update path preserves this mount across testing deployments.
Creation does not deploy an artifact, copy state, or prove runtime retention.

Use `mode: readback` with the unchanged private request, original plan/digest and
original `Idempotency-Key` for observational recovery evidence. Readback reports
`operation_state`, durable `provider_effect_phase`, `configuration_matches`,
`configuration_evidence` (`matches`, `differs` or `unavailable`) and `custody_sha256`.
Provider failures and missing targets return value-free `unavailable` evidence.
Readback never creates a resource or adopts a record.

Repeating **apply with the original key** can recover a lost project, environment
or application response from exactly one original generated marker in complete
provider inventory. It resumes only the next undispatched step under a renewed
lease and the lane lock. A lost mount response resumes only when independent
readback proves the exact mount exists. Positive mount proof survives recovery;
if that mount is later removed or changed, the original key refuses rather than
attaching it again. Retain custody and review a fresh plan to continue.
Separate lease-fenced progress is bound to the original operation key and
survives runner retries and process restarts. A completed plan refuses use under
a new key before effects or a held reservation can remain. Final target adoption
checks and locks the same live reservation and rechecks reviewed lane authority
inside its record transaction, so retained, expired or stale workers cannot
publish a lane target. Duplicate markers, incomplete inventory,
changed identities and an absent uncertain mount preserve the unresolved operation.
An empty inventory does not prove a dispatched provider create cannot finish later.
A different key or plan cannot bypass the held lane reservation. All testing mutations through the service setup route, including adoption and
source completion, share the lane lock and refuse unresolved storage custody.
Direct-DB break-glass and separate recovery paths are outside this route; their
authority changes make an in-flight creation plan stale, but do not prevent
retaining its exact custody under current context ownership.

For an uncertain allocation that cannot resume, keep its resources and explicitly
retain its custody through the service. Obtain current original-key readback.
Send the unchanged reviewed setup request with `mode: apply`,
`retain_state_volume_allocation: true`,
`expected_state_volume_custody_sha256: <custody_sha256>`, the original key/plan,
and the normal exact confirmation and reason. The service requires the original
execution lease to have expired, current active exclusive context ownership
and exact reviewed custody. Retention performs no provider effect and does not
require the original profile bytes or provider binding to remain unchanged.
A display-name or policy update therefore cannot strand the old allocation.
It atomically fences the old worker and records
`allocation_state: retained_uncertain` and `applied: false`. Ordinary apply
replay under the original key returns that same retained receipt, not a created
target; callers must check `applied` and `allocation_state`. If the original
allocation is already the tracked target, retention refuses; use original-key
recovery to complete its receipt. When a verified-mount checkpoint and the exact
generated target are already committed, original-key recovery completes that
receipt from database evidence without requiring current provider availability.
It reports `allocation_state: adopted`, `applied: true` and
`configuration_evidence: unavailable`; this receipt does not prove the current
mount configuration. Use readback to inspect that separately.
This performs no provider mutation, deployment or deletion. Its original intent,
known ids and pending phase remain retained even if a late create materializes.
The retained allocation can never resume, including under another key. To keep
working, request a fresh dry-run and apply its independent names with a new key.
Do not delete or recreate partial resources as a retry. Retention is available
under existing setup authority; it grants no access to a mounted resource.

The mount path must contain mutable state only. The image author must keep
application code outside that directory. Syntactic path validation cannot inspect
an image or prove that a directory contains no code. The reviewed path is echoed
in the saved plan; creation does not qualify the image's storage layout.

Recovery uses the original caller scope, including its token label. Rotating
that label does not transfer access to the original plan or release its lane
reservation. Preserve the original receipts if the caller becomes unavailable; changing
the caller scope requires a separate authority decision, and this operation
provides no cross-scope transfer. Applications with mounts also fail
the existing agent retirement guard; use a supported admin retirement/data
custody path, rather than an unmounted-target deletion command, when retiring
this testing lane. Creating storage does not authorize its later deletion.

Retention refusals expose stable error codes for caller decisions:

| Code | Next action |
| --- | --- |
| `application_state_volume_execution_active` | Wait for the original lease to expire. |
| `application_state_volume_lane_busy` | Retry the original operation after the executor releases the lane. |
| `application_state_volume_custody_changed` | Obtain new original-key readback and review its custody digest. |
| `application_state_volume_already_adopted` | Apply the original key to complete the adoption receipt. |
| `application_state_volume_owner_changed` | Resolve current ownership through the existing authority process. |
| `application_state_volume_unqualified` | Correct the request or its reviewed plan before retrying. |

An HTTP 202 alone does not prove creation: callers must inspect both `applied`
and `allocation_state`, including retained replay responses.

This option is service-only. The local/bootstrap CLI has no storage apply path.
Actual state retention and isolation still require testing-lane qualification.

Product repos may document these expected facts, but they must not store live
Launchplane product profiles, target ids, provider credentials, or lifecycle
records as repo-local authority.

## Image Strategy

Product repos own image build and publish. Launchplane owns deploy selection and
evidence after the image exists.

A product repository never calls Launchplane (see
[DIRECTION.md](../DIRECTION.md)). It builds the image in
`.github/workflows/build.yml` and uploads the build manifest described in
[artifact-provenance.md](artifact-provenance.md). Launchplane verifies that
build and deploys the image to `testing` itself
([event-driven-deploys.md](event-driven-deploys.md)), through the generic-web
deploy route's provider operation; it resolves lane, provider target, runtime
environment, managed secrets, and deployment records from DB-backed authority.
Transitional: `POST /v1/drivers/generic-web/deploy` through the
`cbusillo/launchplane/.github/actions/launchplane-request` action is the old
call-in path some product workflows still use until #2606 deletes it; a new
product repository does not call it.

Publish images to the profile's `image.repository` and preserve digest identity:

```text
ghcr.io/cbusillo/discord-blue@sha256:<digest>
```

Mutable tags such as `latest`, branch names, or environment names are display or
debug labels only; they are not stable deploy inputs. If a product workflow also
emits a human-readable tag, the build manifest still carries the exact digest.

For generic-web and VeriReel application targets, `artifact_id` remains the
canonical artifact identity and should be the digest-pinned
`repository@sha256:digest` reference. Dokploy applications with a saved Registry
Swarm cannot save that digest reference as the Docker image because Dokploy
re-tags application images internally. On the old call-in path, product
workflows that publish a stable SHA tag, such as `repository:sha-<commit>`, pass
that tag as `deploy_reference`; the build manifest and the event-driven testing
deploy carry no `deploy_reference`. Launchplane validates that `deploy_reference` is a
non-floating tag in the same repository, uses it only for the provider-facing
Docker image, and records runtime identity with `artifact_id` as the digest and
`image_reference` as the provider tag. Compose targets remain backward
compatible with digest image references in `artifact_id`.

RepairShopr Sync is the first live canary for this stable shape. The
`cbusillo/repairshopr_api` product workflow built an immutable GHCR image,
called deployed Launchplane, and received `deploy_status: pass` for deployment
record `deployment-20260630T034901Z-repairshopr-sync-prod` after Launchplane PR
#1503 deployed. That run proved image deploy for a service-shaped worker
without source-ref deploy or direct Dokploy mutation in the product repo; its
product-repo call into Launchplane is the retired call-in path.

The inherited source-ref deploy bridge is retired. Services that still deploy a
Dokploy compose target from Git must migrate the provider target to immutable
image deploy before using the generic-web service contract; do not use the bridge.
Product repositories must publish immutable images for Launchplane-owned stable
deploys.
Product repos must not keep Dokploy host, token, compose id, provider source-ref
rewrites, or other direct provider mutation scripts as a replacement for the
retired route.

The stable target is immutable image deploy. If the target is a background
worker with no HTTP listener, its product profile may leave `health_path` empty
and set `runtime_port=0`, but every stable lane must disable public ingress
monitoring and omit HTTP URLs, and every provider target must omit domains and
disable Dokploy health checks. Image deploy, preview creation, public ingress
monitoring, provider domains, and provider health checks still require real
image and HTTP health metadata before they can mutate provider state.

Launchplane also injects a non-secret runtime identity into Dokploy env during
Launchplane-owned deploys. The standard keys are
`LAUNCHPLANE_RUNTIME_IDENTITY_JSON`, `LAUNCHPLANE_DEPLOYMENT_RECORD_ID`,
`LAUNCHPLANE_ARTIFACT_ID`, and `LAUNCHPLANE_SOURCE_GIT_REF`. Product health
endpoints should expose that identity when adopted so Launchplane can compare
expected inventory against observed runtime state.
The preferred JSON health payload is bounded and non-secret:

```json
{
  "status": "ok",
  "version": "ghcr.io/example/product@sha256:...",
  "source_git_ref": "<commit-sha>",
  "image_reference": "ghcr.io/example/product@sha256:...",
  "runtime_identity": { "schema_version": 1 }
}
```

`runtime_identity` should be the parsed value from
`LAUNCHPLANE_RUNTIME_IDENTITY_JSON`. Worker-like products can still use this
contract; their endpoint should report product-owned freshness in `status` and
`summary` rather than only process liveness.

## Config, Secrets, And Volumes

Non-secret runtime settings belong in Launchplane runtime-environment records.
Secret settings belong in Launchplane managed secret records and bindings. A
product workflow may pass the product key, source ref, run URL, and immutable
image reference on the old call-in path; it never passes secret values or
renders a Dokploy env file.
Launchplane live-target runtime sync delivers only the site's own environment
for the lane (see `docs/secrets.md`) and records only key names and counts.
Generic-web preview refresh applies the same rule to credentials copied from a
template lane: a value a managed secret delivers to the template (its own or
the site's shared one), a secret-shaped key, or a value that embeds a password
such as `SMTP_URL=smtp://user:password@host`. The copied key must resolve to a
managed runtime secret binding on the template lane and the active runtime
key-safety policy must allow that binding for the preview target before
Launchplane writes the preview app env. Odoo preview apply checks the integration credentials it
copies from the template lane (keys carrying an integration marker such as
`SHOPIFY` or `SMTP`) the same way, but instead of refusing it sets each one no
rule allows on previews to an empty value (every one when no key-safety policy
is active) and lists the key names in the plan's
`omitted_integration_credential_keys`. The integration read-back still runs.
The template lane's own classification and declared secret classes never carry
over to a preview; only a policy rule that allows previews does.

Dokploy-owned persistent volumes are allowed for service state, but the volume
mapping is provider target configuration, not product-repo lifecycle state.
Document the mount path in the product runtime contract and record the live
target through Launchplane's Dokploy target records. For Discord Blue, the state
volume should cover product config and bot runtime state that must survive image
replacement; secrets still remain managed secret bindings, not files committed
to the product repo.

Volume contents are not Launchplane release artifacts. If a future service
needs backup, restore, or data migration gates, add a product-specific driver or
typed product policy instead of overloading the generic-web deploy request.

## Ports And Health

`runtime_port` is the container port the service listens on when it exposes an
HTTP endpoint. For Discord Blue, use port `8787` for the Every Code bridge or a
small health/control endpoint if that is the only HTTP surface.

Stable lanes can verify health in two ways:

- set `lane.health_url` to an explicit URL reachable by Launchplane, such as an
  internal bridge health endpoint
- set `lane.base_url` and rely on `profile.health_path` to derive the health URL

If neither value is set, generic-web promotion records skip health verification
for that lane. Skipping health is acceptable only for an initial migration slice
with other evidence, such as a successful Dokploy deployment plus product-owned
log or smoke evidence. The preferred Discord Blue target is a reachable health
URL on port `8787` so promotion can fail closed on a bad rollout.
For sync or worker products, the same endpoint should report whether the worker
is actually fresh. For example, RepairShopr Sync should mark health unhealthy
when the last successful sync is stale even if the process is running.

Do not expose service ports publicly just to satisfy Launchplane. Keep public,
private, and internal-only routing as Dokploy/provider configuration and record
only the product-level health URL or runtime port in Launchplane records.

## Deploy, Promote, And Roll Back

A normal service deploy does this:

1. Product CI builds, tests, and publishes an immutable image and its build
   manifest.
2. Launchplane hears the build event and verifies the build; see
   [event-driven-deploys.md](event-driven-deploys.md).
3. Launchplane resolves the product profile lane and DB-backed Dokploy target.
4. Launchplane updates the Dokploy application image and triggers deployment.
5. Launchplane writes a deployment record with the resolved target and status.

Generic-web deploy records post-deploy evidence as `skipped` by default. A
driver that inherits from generic-web can provide a product post-deploy
extension for work that must happen after the provider deployment succeeds. That
extension writes terminal post-deploy evidence without changing the underlying
deploy status, so admins can distinguish "image deploy failed" from "image
deploy passed but product maintenance failed".

Odoo profiles that execute generic-web deploy or rollback apply use this
extension to run the Odoo post-deploy driver after the provider deploy succeeds.
The Odoo-specific rollback apply route remains available for rollback flows that
still need Odoo release tuple and promotion-state updates.

Promotion uses the same artifact identity and target records. When health URLs
exist, Launchplane verifies the source and destination lane health around the
deployment and writes promotion evidence.

A live generic-web promotion rolls production back on its own. Before it
changes production, it plans a rollback to the deployment record that
production's inventory names. If that deployment cannot be restored (it is
missing, not digest-pinned, or an application target without an immutable
deploy reference), the promotion and its dry run are refused before any change.
When there is no earlier production deployment, or production was already
failing its health check, the promotion goes ahead and records why no rollback
is possible. If the destination deploy fails after it touched the provider, or
the destination health or runtime-identity check fails, the same server-side
request redeploys the planned artifact. It then verifies health against that
deployment's runtime identity and restores production's inventory and release
lineage. The rollback runs inside the promotion's provider operation, so a
cancelled caller or a lost response cannot skip it. The promotion record's
`rollback` evidence names the failure, the `target_deployment_record_id`
production returned to, and the rollback's own `deployment_record_id`;
`rollback_health` holds its health check. A failed promotion's record also holds
`failure`: a code (`source_health_failed`, `destination_deploy_failed` or
`destination_health_failed`) and Launchplane's fixed description of it, so
`promotion.read` shows why without the synchronous response. The promotion response repeats them
as `rollback_status`, `rollback_target_deployment_record_id`, and
`rollback_deployment_record_id`. When the rollback restores and verifies
production, the promotion's provider operation completes with that failed
result, so the next promotion is not blocked. If the rollback fails, the deploy
raises instead of returning a result, or the operation's lease is lost, the
operation stays in `reconcile_required`. The lease fence is never bypassed,
because another operation may own the target. Recover it through generic-web
deploy recovery, using the rollback target the promotion record names.

Rollback begins with a Launchplane-owned rollback plan. The generic-web planner
is exposed through `POST /v1/drivers/generic-web/prod-rollback-plan` as a
safe-write contract: it reads the product profile, destination lane, a
Launchplane deployment record selected as the rollback target, and optional
backup-gate evidence, then writes a rollback-plan record. It does not mutate
Dokploy or trigger a product workflow.

Launchplane applies generic-web rollback through
`POST /v1/drivers/generic-web/prod-rollback`. The apply route rebuilds and
persists the rollback plan from current records, then calls the normal
generic-web deploy path with the selected previous immutable artifact. Drivers
such as Odoo can keep a product-specific rollback action while they still need
extra gates around backups, release tuples, manifests, migrations, or post-deploy
validation.

Odoo rollback planning uses
`POST /v1/drivers/generic-web/prod-rollback-plan`. The former Odoo-shaped
rollback-plan alias is retired; this does not change Odoo's product-specific
`POST /v1/drivers/odoo/prod-rollback` apply route.

Required planner input:

- `product`
- destination `instance`, usually `prod`
- `rollback_deployment_record_id`, pointing at the previous good deployment
  record for that same context and instance
- optional `backup_record_id` when the product/admin requires backup-gate
  evidence before stable recovery

The planner fails closed when the rollback target is missing, belongs to a
different context or instance, has a failed deploy, has failed health evidence,
or does not reference the product image repository by immutable `@sha256:`
digest. If `backup_required=true`, the request must name a stored backup-gate
record, and that record must match the destination lane and have `status=pass`.

The produced `GenericWebRollbackPlanRecord` stores the selected immutable
artifact identity, source git ref, planned generic-web deploy payload, backup
gate evidence, target health evidence, blockers, and summary. A later explicit
apply path can consume the ready plan and call the normal generic-web deploy
route, but admins must not roll back by clicking Dokploy to a mutable tag or
changing product-repo workflow state. The durable rollback source remains the
Launchplane deployment record and its immutable image digest.

## Discord Blue Target

Discord Blue should target this minimum contract before leaving the hardened
host-managed deployment:

- product key: `discord-blue`
- driver: `generic-web`
- image repository: `ghcr.io/cbusillo/discord-blue`
- stable lanes: at least `testing`; add `prod` before production cutover
- Dokploy target type: `application`
- runtime port: `8787` when the bridge or health service is enabled
- health path: a stable path such as `/health` or `/v1/health`
- persistent volume: one Dokploy-mounted state/config path documented in the
  product repo and represented by the admin-owned target configuration
- secrets: Discord tokens and other credentials managed through Launchplane
  secret bindings, never passed in workflow payloads
- deploy input: immutable image reference, preferably
  `ghcr.io/cbusillo/discord-blue@sha256:<digest>`

The migration is unblocked when Launchplane can read the product profile and
target records, deploy a testing image through the generic-web route, and record
either a passed health check on port `8787` or an explicitly documented skipped
health check with replacement evidence for the first cutover.
