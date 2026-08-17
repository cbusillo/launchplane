---
title: Operations
---

## Merge Admission Recovery

Treat a merge admission without a terminal outcome as effect-unknown. Do not
rerun the merge mutation manually and do not infer success from checks,
candidate status, landing-plan progress, or a sent GitHub request. Re-run the
Launchplane controller so it observes the exact PR head, target base, merge
commit/tree, and branch containment through the guarded recovery path.

If the exact PR is already merged, Launchplane appends a `landed` observation.
If the exact PR remains open and the expected base did not advance, Launchplane
first appends `process_interrupted` when the attempt lacked an outcome, then
appends conclusive no-effect evidence with exact open PR, head/tree, and
base/tree observations; this evidence does not claim that GitHub rejected the
request. Only then may Launchplane recompute L2 for a fresh admission.
Contradictory or incomplete evidence remains `reconcile_required` and needs
operator investigation. Candidate-ref cleanup failures are separate from
landing truth and may be retried without changing the outcome record.

Use the controller phase when diagnosing a failed landing. An active
`admit_pull_request` phase means the failure occurred while reconciling or
evaluating Launchplane admission, before a GitHub merge request. An active
`merge_pull_request` phase means Launchplane persisted admission and crossed the
provider-effect checkpoint immediately before the GitHub merge request. Preserve
the response JSON and trace ID from protected workflow failures; an HTTP 409 by
itself does not distinguish controller fencing, admission refusal, admission
reconciliation, or a GitHub state conflict.

A controller response with `controller_action=block` and
`blocking_reason.code=merge_readiness_not_ready` is a normal fail-closed
pre-effect result. Read `merge_readiness.reason_codes` and the individual facets,
repair or record the missing evidence, and call the controller again. Do not
clear controller state or treat the result as a GitHub merge conflict: no
provider effect was attempted for the blocked entry and the controller lease is
released cleanly. Earlier entries in a multi-PR batch may already be merged; the
returned landing plan identifies their persisted status.

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
  `service outbox-workers run-once` and `service outbox-workers run` operate
  PostgreSQL transactional outbox deliveries for external workflow dispatch and
  notification effects.
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

Runner-host hygiene uses the protected `Runner Host Hygiene` workflow and the
deployed evidence API. Shared Docker mutation requires a runtime-only binding
manifest for every repository, lane, runner, and systemd service on the host;
the executor cross-checks that manifest against local service discovery and
persists only public hashes and counts. Missing or unauthorized GitHub sources,
manifest/service disagreement, stale evidence, or changed recovery inputs block
mutation. The workflow derives the exact evidence repository set from runtime
topology and mints a short-lived, read-only GitHub App installation token for
each run; it does not fall back to a PAT or runner-registration credential.
Every mutating action also requires the host-local durable audit spool described
in `docs/runner-host-hygiene.md`; do not invoke the executor from an arbitrary
checkout or bypass its planned/action-started/terminal evidence flow.

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
mutations: product onboarding, Dokploy target adoption/creation, and tracked
Dokploy target metadata commands. These paths validate an existing explicit
provider-target row before mutating the Dokploy pair, write the Dokploy target/id
records, then write the matching provider-target row. A stale explicit
provider-target row blocks the mutation instead of being overwritten.
A physical provider target identity already bound to another context/instance
also blocks onboarding or target adoption, even when the requested route
supplies matching replacement evidence. Product onboarding and target setup
additionally reject contexts retained as historical product evidence; repair the
current product profile before onboarding or target setup instead of reactivating
the historical route. The generic-web onboarding workflow is new-product-only;
existing product changes use the bounded profile or advanced manifest apply
surface.

## Mutation Reservation Recovery

Reservation-backed service mutations use the PostgreSQL
`launchplane_idempotency_records` table as a typed execution claim. Operators
and reconcilers should interpret states as follows:

- `running` with an unexpired lease means another owner is executing; do not
  bypass it or retry with a new key.
- `completed` is terminal replay evidence. Retry the same route, caller scope,
  key, and request fingerprint to receive the original accepted response.
- `reconcile_required` means a provider operation/reconciliation key was bound
  before the outcome became unknown. Do not repeat the provider mutation. Use
  the owning domain's service reconciliation workflow to inspect provider state
  and write terminal evidence.

Expired DB-only reservations without a reconciliation key may be reclaimed by
the storage API with an incremented attempt. Provider-backed work must bind its
stable provider operation key before effects; after that point lease expiry is a
reconciliation event, not a retry signal. Stale owners cannot renew or complete
another owner's lease or an earlier attempt owned by the same worker identity.
Lease timestamps and expiry decisions use the database clock rather than caller
time. Do not repair reservation rows with direct SQL or an arbitrary checkout;
shared/live recovery belongs in a typed Launchplane service endpoint or domain
reconciler.

Product preview TLS apply is the first migrated DB-only route. Its reservation,
profile compare-and-write, and response completion share one transaction, so a
completion-persistence failure rolls back the profile change and a no-op apply
still produces replay evidence. Before planning, the route uses the database
clock to reject active claims, replay completed claims, transition bound expired
claims to reconciliation, and release only expired unbound orphan claims that
cannot have committed the atomic profile write.

## Transactional Outbox Workers

External deliveries now use PostgreSQL outbox rows instead of request-thread
provider calls when the business transition must commit before the effect. Run
outbox workers only against Launchplane shared storage:

```bash
uv run launchplane service outbox-workers run \
  --database-url "$LAUNCHPLANE_DATABASE_URL"
```

For a bounded operational probe or smoke check, run one claim/delivery step:

```bash
uv run launchplane service outbox-workers run-once \
  --database-url "$LAUNCHPLANE_DATABASE_URL"
```

Outbox workers claim due rows with leases and PostgreSQL row locks. Multiple
workers may run concurrently; the claim query uses `FOR UPDATE SKIP LOCKED` so
one locked row does not block unrelated pending deliveries. A worker records a
provider operation marker before the external call. If it crashes after the
marker is recorded, the next worker reconciles provider state before resending.
Transient provider failures return the row to `pending` with database-clock
exponential backoff and retain any provider marker so the next attempt
reconciles before resending. Attempts are bounded by the row's `max_attempts`;
invalid payloads and exhausted retries become terminal failures. Do not update
outbox rows manually or clear provider markers to force a retry.

Generic-web promotion workflow dispatches are queued by
the automation route `POST /v1/drivers/generic-web/prod-promotion-workflow` or
the operator route
`POST /v1/products/{product}/environments/{environment}/promotion/workflow-dispatch`;
the HTTP response shows `dispatch_status=pending` and includes
`records.outbox_delivery_id`. The worker
resolves the managed GitHub token from Launchplane runtime records, sends the
dispatch, then marks the outbox delivery delivered when the corresponding run is
observed. Operator clients read
`GET /v1/products/{product}/environments/{environment}/promotion/workflow-deliveries/{delivery_id}`
for outbox and observed-run state; dispatch acceptance is not workflow
completion. The delivery dedupe key includes a hashed transition identity: replay
of one request reuses its row, while a later dispatch with the same workflow
inputs creates a new row instead of being suppressed forever.

The workflow input set includes `promotion_intent_id`, equal to the persisted
outbox delivery ID. Product workflows must pass it to the raw live promotion
route and use it as that request's `Idempotency-Key`; the service revalidates
the current evidence and target against the intent before execution. The
separate `generic_web_prod_promotion.execute_unreviewed` authorization action is
the only supported escape hatch for deliberately unreviewed automation.

GitHub workflow dispatch does not provide a provider idempotency token. Its
worker therefore persists the observation window and an in-doubt marker before
the POST and never automatically repeats the POST after that marker exists. A
crash or network ambiguity between marker persistence and run observation stays
`reconcile_required` with `workflow_dispatch_in_doubt`; operators must not clear
the marker to force a resend.

Public-ingress monitor GitHub issue notifications are also queued when the
record store supports the transactional outbox. Observation, incident, and
pending notification rows commit atomically. Mixed policies queue only the
GitHub destinations while continuing to deliver and record email and Discord
attempts directly. Resolved-incident reconciliation verifies the issue is
actually closed after finding the marker, covering a crash between the marker
comment and the close request. Email and Discord notification paths remain
direct-delivery until they are explicitly migrated.

## Durable Provider Operations

Provider-backed routes that mutate an external system share the durable
provider-operation runner (`control_plane/provider_operations.py`). The runner
reserves and binds a deterministic provider reconciliation key before the effect
begins, performs the effect through a provider adapter, and records terminal
evidence only after the effect returns. Odoo preview apply
(`/v1/drivers/odoo/preview-apply`) and generic web deploy
(`/v1/drivers/generic-web/deploy`) are the first migrated provider routes; the
former replaces the previous process-local apply lock. Both require an
`Idempotency-Key` header and Launchplane database storage, and fail closed with
`503 database_storage_required` on non-database stores.

- A concurrent request with the same key while the effect runs returns `409
  mutation_in_progress`; a completed effect replays; a different request
  fingerprint returns `409 idempotency_key_reused`.
- GitHub workflow retries must preserve the original operation key. The generic
  web stable-deploy workflow keeps the first-attempt `:<run_id>:1` suffix on
  every rerun so **Re-run failed jobs** can reconcile or replay the existing
  provider operation instead of colliding with its target fence under a new
  key.
- A stable provider-target key identifies the mutation target independently of
  request timeout or deploy settings. A partial unique database fence allows
  only one `running` or `reconcile_required` mutation for that target, including
  requests that use a different `Idempotency-Key`. Completed operations release
  the target fence. The separate reconciliation key can carry a richer
  generic-web target snapshot, so recovery continues to observe the originally
  selected target even if current authority records change before a restart.
- Running effects renew their lease in the background. Completion uses the
  latest fenced reservation snapshot; if the lease expires exactly as a
  successful provider result returns, Launchplane adopts that direct terminal
  evidence instead of repeating the effect.
- Immediately before every provider write, the adapter checkpoints a named
  provider-effect phase and renews the fenced reservation. A stale worker whose
  claim was recovered cannot advance to another provider write because its next
  checkpoint fails owner/reservation validation. Odoo preview create, update,
  deploy, and destroy share one compose-name reconciliation key for the complete
  lifecycle rather than changing the fence after Dokploy assigns a compose id.
  Compensating rollback deletes and generic-web post-deploy environment,
  deployment, schedule-upsert, and schedule-trigger writes use the same
  per-write checkpoint rule.
- A crash, timeout, or cancelled request after key binding leaves the durable
  reservation in place. The next attempt sees the bound expired reservation as
  `reconcile_required` and asks the provider adapter to observe the target. An
  observed effect is adopted as terminal evidence without re-running. When the
  adapter proves the exact operation marker is absent, Launchplane releases the
  reconciled claim and retries once only when no provider effect was checkpointed
  or the adapter explicitly marks the recorded phase safe to retry. Retry is an
  atomic in-place attempt transition under the same target fence; stale
  reconcilers cannot delete, retry, or adopt a newer attempt. A missing
  marker after a deployment-trigger checkpoint remains unknown. Genuinely
  unknown state stays `reconcile_required`; adapters are the only place
  provider-specific reconciliation lives.
- Migrated Dokploy adapters attach a deterministic, per-request operation title
  to the provider deployment. Both the initial deployment wait and restart
  reconciliation search for that exact marker; desired state or another recent
  deployment is not treated as proof that this operation ran. Any generic-web
  path that enables provider-effect checkpoints must supply this title before
  provider work begins. A crash during a
  checkpointed post-deploy phase stays `reconcile_required` unless the exact
  provider marker can be observed; Launchplane does not synthesize terminal
  success from the target's current desired state.
- Odoo compose-create recovery checks both project inventory and compose search,
  while destroy reconciliation reads the exact compose id. Generic-web recovery
  repairs the deterministic deployment record and environment inventory before
  adopting the operation, and preserves already-written post-deploy evidence.
- An exact-marker deployment that finished failed, cancelled, unhealthy, or
  timed out is terminal failure evidence, not a successful `202`. Launchplane
  completes both fresh and recovered operations only with the provider
  deployment id and provider start/finish timestamps, stores that evidence for
  idempotent replay, and returns `502 provider_mutation_failed` for provider
  failure. Incomplete terminal observations remain unknown.
- Blocked plans and other non-durable results release the reservation instead of
  storing replay evidence, so a corrected retry with the same key can proceed.
- Odoo preview destroy is the only current lifecycle operation allowed to
  supersede a different-key target fence. When the blocking preview apply is
  already `reconcile_required`, Launchplane waits until its recovery lease has
  been expired for 15 minutes and verifies that Dokploy has no running compose
  deployment or Odoo data-workflow schedule. It then atomically records that
  earlier apply as a terminal superseded failure while reserving the destroy.
  Odoo preview apply timeouts are capped at 10 minutes, so the additional grace
  outlives a supported in-flight provider request before cleanup can take over.
  Active leases remain fenced, stale reservation snapshots cannot supersede a
  newer attempt, and the original request keeps durable replay evidence
  explaining that cleanup won. Provider read failures remain fail-closed. Do not
  reproduce this transition with direct SQL or provider-side deletion.

### Generic-web deploy recovery dry-run/apply

Legacy generic-web deploy recovery starts read-only. Operators call
`POST /v1/admin/generic-web/deploy-recovery/dry-run` with the exact original
`GenericWebDeployEnvelope` under `original_deploy`, the original
`Idempotency-Key`, the product and instance, and a non-empty reason. The service
authorizes `generic_web_deploy.execute` for the current product/context/instance
before looking up any reservation. After lookup, it derives the original
operation target from the stored reconciliation snapshot, or from an exact
typed completed response, and authorizes the stored product/context/instance
again before returning evidence.

The lookup is existing-only and database-backed. It fingerprints the exact
original payload using `/v1/drivers/generic-web/deploy`, searches across the
original GitHub Actions scope, and fails closed for a missing, conflicting, or
multi-scope match. It never probes through the normal deploy route: calling
`/v1/drivers/generic-web/deploy` can reserve and start a new production deploy
when the assumed legacy reservation does not exist.

Dry-run uses the reservation's stored reconciliation and provider-target
identities to inspect the original provider operation. Reconciliation identity,
product, instance, provider-target, or completed-response drift returns a
conflict before provider inspection; provider read uncertainty alone produces
`hold_unknown`. It does not reserve,
release, supersede, retry, adopt, or write deployment, inventory, idempotency,
or provider state. The bounded response reports only reservation state and
timestamps, hashed identifiers, provider outcome/status, retry safety, one of
`replay_completed`, `wait_for_active_lease`, `adopt_observed`,
`retry_original_operation`, or `hold_unknown`, and a canonical recovery digest.
Raw scopes, idempotency keys, reconciliation keys, provider-target keys,
original payloads, target URLs, and provider payloads are never returned.
Product repositories that need an OIDC-authenticated inspection should use the
Launchplane-owned
`.github/actions/generic-web-deploy-recovery-dry-run` action. Its single request
object accepts only the exact legacy deploy coordinates, original GitHub Actions
run ID and attempt, operator reason, and optional connector-only
`launchplane_url`. The action strips the connector URL before constructing the
service payload, reconstructs the legacy idempotency key internally, calls only
the dry-run route through the shared request action, suppresses the raw response
body, and exposes only the seven bounded recovery fields documented above.
Product repositories whose authz grant is bound to the stable-deploy reusable
workflow may pass that request object through the optional
`recovery_request_json` input on
`.github/workflows/reusable-generic-web-stable-deploy.yml`. A non-empty recovery
request skips the stable-deploy job and runs only the bounded dry-run action;
the reusable workflow exposes no recovery apply input or route.

Stage 2 apply is explicit and digest-gated. Operators call
`POST /v1/admin/generic-web/deploy-recovery/apply` with the same request body as
the dry-run plus `expected_recovery_digest`. The service recomputes a fresh
inspection and requires an exact digest match before it writes anything. The
digest intentionally excludes `observed_at`, so routine database observation
time changes do not stale a reviewed recovery; all reservation identity,
provider classification, action, request, and bounded provider observation
inputs remain part of the digest.

Apply never creates a new reservation, releases a reservation, or supersedes a
provider target. If the reviewed reservation is an expired `running` mutation,
apply first CAS-transitions that exact reservation to `reconcile_required` via
the durable mutation store, then uses the existing exact-match adoption or retry
methods. `adopt_observed` stores deployment/inventory evidence from the reviewed
inspection without re-observing the provider. `retry_original_operation` resumes
the already-acquired reservation path and preserves `reconcile_required` for
pre-effect rejection, lease loss, uncertain, or non-durable outcomes. Successful
apply preserves the original deploy response evidence and adds only bounded
recovery metadata, `recovery_digest` and `recovery_action`, so a lost-response
apply retry can replay safely without exposing raw scope, key, target, or
provider payload data.

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
- `GET /v1/authz-policies/active`
- `POST /v1/authz-policies/managed-rule-sets/generic-web-preview/plan`
- `POST /v1/authz-policies/managed-rule-sets/reconcile`
- `POST /v1/authz-diagnostics/github-actions/evaluate`
- `GET /v1/route-bindings/records/current`
- `POST /v1/route-bindings/reconcile`
- `POST /v1/route-bindings/odoo-testing/controller/run-once`
- `POST /v1/route-bindings/external/reconcile`
- `POST /v1/provider-targets/operations`
- `POST /v1/product-onboarding/apply`
- `POST /v1/product-profiles/stable-lane-repair/apply`
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

`Generic Web Preview Authorization` preserves live GitHub App metadata lookup
for onboarding and rule rotation. Retirement is different: it can remove a
product's preview authorization after archival or GitHub App offboarding without
minting a product-repository token or calling the GitHub API. The service derives
and verifies the retired repository authority from active Launchplane records;
the caller's optional repository fields only assert that derived identity.

Privileged product rollback routes should stay behind narrow delegated-worker
contracts rather than being absorbed into the main API host. Product-private
runtime memos belong in product repos; this repo keeps only the shared driver
and record contracts.

The service uses GitHub OIDC bearer tokens and DB-backed authz policy records.
Additional evidence routes should land against the same authn/authz boundary
rather than creating separate ad hoc ingress patterns.

Managed authz policy reconciliation is a native FastAPI service route. It
requires DB-backed policy storage, enforces `authz_policy_grant.write`,
preserves signed-in GitHub human-session callers, and keeps dry-runs stateless.
Apply atomically commits the active-policy compare-and-swap and completed
`Idempotency-Key` replay evidence. Managed reconciliation is the sole policy
write contract for every principal type.

`POST /v1/authz-diagnostics/github-actions/evaluate` is a read-only diagnostic
route for GitHub Actions callers. It evaluates only the calling OIDC identity
against a requested action/product/context/target and requires the separately
scoped `authz_diagnostic.evaluate` action first. Its response returns the
active policy record/revision plus opaque identity and rule fingerprints and
failed selector categories; it never returns workflow refs, repositories,
policy values, or other principal rules. Ordinary route denials remain generic.

Operators mutate shared or production authz through the deployed service, not
through direct DB commands or a local CLI from an arbitrary checkout. Store the
complete desired rules for one `managed_set_id` in a protected repository
secret. `LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON` owns the primary operator set;
`LAUNCHPLANE_AUTHZ_POLICY_RECONCILE_MANAGED_SET_JSON` owns the exact immutable
policy-admin worker rules for the standalone authz wrapper and must declare the
`operator.authz-policy-reconcile` managed-set identity;
`LAUNCHPLANE_AUTHZ_MANAGER_PREVIEW_APPROVAL_MANAGED_SET_JSON` owns the generic
GitHub-human manager preview approval writer set and must declare the exact
`operator.manager-preview-approval` managed-set identity;
`LAUNCHPLANE_AUTHZ_OWNER_ACCEPTANCE_MANAGED_SET_JSON` owns only the dedicated
GitHub-human Owner Acceptance workbench grants and must declare the exact
`operator.owner-acceptance` managed-set identity. Bind its rules to immutable
numeric GitHub user IDs. Engineering viewer rules grant only
`owner_acceptance.read` and match both resolved human roles, `admin` and
`read_only`, so an existing global admin assignment cannot suppress the narrow
viewer grant. Owner candidate rules use only `read_only` and may grant that read
action plus `owner_acceptance_event.write`. Both shapes remain limited to
product `launchplane` and context `owner-acceptance`. Product Owner membership
remains a separate server-side requirement for event writes.
`LAUNCHPLANE_AUTHZ_PRODUCT_OWNER_POLICY_ADMIN_MANAGED_SET_JSON` owns the
dedicated local-operator policy administration boundary and must declare the
exact `operator.product-owner-policy-admin` managed-set identity. Each rule
binds one exact operator subject and token label to one exact product/system
scope and exactly the Product Owner policy and requirement read/write actions.
It cannot grant routing, deployment, production, secret, or unrelated policy
authority. Use this set only to apply DB-backed Owner membership and requirement
records through the deployed service with dry-run and compare-and-swap evidence.
Generic-web preview caller
grants are no longer sourced from a per-product repository secret. The
typed product-onboarding planner derives repository identity, caller workflow
refs, immutable Launchplane worker refs, products, contexts, events, and actions
and submits the complete `operator.generic-web-preview` desired set through the
same protected managed reconcile contract. Relative nested reusable-workflow
calls resolve at the caller's immutable Launchplane commit, so rotate lifecycle,
verification, notice, and feedback worker rules through the planner's
expand/contract operations before repinning product callers;
`LAUNCHPLANE_AUTHZ_GENERIC_WEB_ONBOARDING_MANAGED_SET_JSON` owns only the
permanent exact Launchplane workflow grants for typed onboarding planning,
protected target/record/authz apply, and preview-authz rotation/retirement. It
must declare `operator.generic-web-onboarding` and must not contain product
repository, product context, target, domain, or per-product preview rules;
`LAUNCHPLANE_AUTHZ_PRODUCT_HEALTH_MONITORING_MANAGED_SET_JSON` owns the generic
Product Health Monitoring wrapper's exact immutable worker grant;
`LAUNCHPLANE_AUTHZ_GENERIC_WEB_ROUTE_BINDING_MANAGED_SET_JSON` owns independent
exact-instance route-binding read/apply grants for generic-web stable lanes and
must declare `operator.generic-web-route-binding`. Keep real products, contexts,
instances, and immutable workflow identities only in that protected secret;
`LAUNCHPLANE_AUTHZ_GENERIC_WEB_TESTING_INGRESS_ROUTE_MANAGED_SET_JSON` owns the
separate exact-instance `ingress_route.plan` and `ingress_route.apply` grants
for generic-web testing lanes and must declare
`operator.generic-web-testing-ingress-route`; keep it independent from the
route-binding set so ingress provider access is never implied by record-only
route-binding authority;
`LAUNCHPLANE_AUTHZ_ODOO_ROUTE_BINDING_MANAGED_SET_JSON` owns the independent
Odoo stable managed route-binding set.
`LAUNCHPLANE_AUTHZ_PREVIEW_FEEDBACK_REMEDIATION_MANAGED_SET_JSON` owns the
local-operator-only VeriReel preview-feedback remediation plan/apply grants; and
`LAUNCHPLANE_AUTHZ_ODOO_EXTERNAL_ROUTE_BINDING_MANAGED_SET_JSON` owns the
testing-first external route-binding set.
`LAUNCHPLANE_AUTHZ_ODOO_TESTING_INGRESS_ROUTE_MANAGED_SET_JSON` owns narrow
testing-lane ingress inspection and reviewed no-op audit authority. The
`LAUNCHPLANE_AUTHZ_ODOO_TESTING_ROUTE_BINDING_REFRESH_MANAGED_SET_JSON` owns the
testing-only refresh controller and its exact-instance binding grants. The
`LAUNCHPLANE_AUTHZ_ODOO_TESTING_TARGET_REPLACEMENT_MANAGED_SET_JSON` owns the
separate exact-instance testing read, plan, and apply grants used by the pinned
Odoo target-replacement workers. The
`LAUNCHPLANE_AUTHZ_ODOO_OPW_PREVIEW_FEEDBACK_MANAGED_SET_JSON` owns the isolated
OPW preview feedback writer grant without requiring operators to read or replace
the primary managed-set secret. The
`LAUNCHPLANE_AUTHZ_ODOO_OPW_PRODUCTION_ENROLLMENT_MANAGED_SET_JSON` owns the
separate exact-instance OPW production inspection and enrollment grants so the
operator can reconcile that lane without replacing another unreadable managed
set. `LAUNCHPLANE_AUTHZ_ODOO_PRODUCTION_ENROLLMENT_MANAGED_SET_JSON` owns
additional exact-instance Odoo production inspection and enrollment grants
without expanding the OPW-specific set or replacing another unreadable managed
set. `LAUNCHPLANE_AUTHZ_ODOO_PRODUCTION_OPERATION_READ_MANAGED_SET_JSON` owns
separate exact-instance cross-operation read grants, such as allowing an
immutable production apply worker to read its reviewed plan operation without
replacing the broader production-enrollment set. The
`LAUNCHPLANE_AUTHZ_ODOO_PRODUCTION_BACKUP_RESTORE_MANAGED_SET_JSON` owns the
separate exact-instance destructive restore apply grant for the immutable
production restore wrapper and worker without replacing the broader
production-enrollment set. The
`Manage Launchplane Authorization` wrapper selects one of those explicit
secrets and forwards it into the reusable worker, whose OIDC-minting job remains
gated by the `launchplane-authz-admin` environment. Never replace the unreadable
primary secret with a partial set; use a distinct managed set and protected
secret instead. The JSON owns desired state only: `schema_version`, `product`,
`managed_set_id`, migration/adoption intent, and `desired_policy`.
Dispatch-time mode, reason, issue reference, and reviewed plan digest are
deliberately excluded from that secret.

The policy-admin worker set is self-rotating. Before repinning the standalone
wrapper, add a distinct exact rule for the next reusable-worker SHA through the
currently authorized worker, apply the reviewed expansion, then advance the
wrapper. A first-time recovery may temporarily expose
`authz_managed_set = authz-policy-reconcile` in the deploy workflow's existing
authz-only path so the previous immutable worker can add the standalone rule.
That selector is a bootstrap bridge only: verify the new wrapper, then remove
the selector in the next change. It must never replace or partially reconstruct
`LAUNCHPLANE_AUTHZ_MANAGED_SET_JSON`.

The reusable authz worker accepts an optional exact expected managed-set
identity from a reviewed wrapper. When provided, the worker rejects protected
configuration whose `managed_set_id` does not match before rendering or sending
the reconciliation request. Land this compatibility input before advancing a
pinned wrapper that depends on it; do not pass a new input to an older immutable
worker revision.

### Reusable workflow SHA rollout for readiness-gated actions

Operational-readiness actions require one exact managed rule for the current
caller, reusable worker, action, product, context, and instance. Roll a pinned
worker without creating an authorization gap by using two separate exact rules:

1. Add a new rule with a distinct `managed_rule_id` and the new immutable worker
   SHA while retaining the old exact rule unchanged. Each rule must keep a
   singleton `job_workflow_refs` selector and the same exact lane/action scope.
2. Run `Manage Launchplane Authorization` in `dry_run` mode and require
   `operational_readiness_blocked_rule_count=0` in the recorded diff before
   approving the protected `launchplane-authz-admin` job. Apply the reviewed
   expansion, then deploy the wrapper that calls the new worker SHA.
3. Run the read-only operational-readiness preflight from the new worker. It must
   select only the new exact rule and report authorization ready.
4. Remove the old rule through a second dry-run/review/apply cycle, then repeat
   the readiness preflight.

Do not place old and new worker SHAs together in one readiness-sensitive rule.
That overlap can be valid transitional authorization, but it is intentionally
not final operational-readiness shape and will report
`job_workflow_refs_not_singleton`. The managed-authz dry-run records bounded
rule IDs, affected readiness actions, and reason codes without exposing selector
values. Every expansion and contraction still requires the protected human
approval; diagnostics do not bypass or weaken that control.

`Tracked Target Logs` and `Odoo Website Bootstrap Override` follow this same
two-change rollout. Their dispatch files are thin operator entrypoints pinned to
reviewed reusable workers at a full commit SHA; production authorization binds
both the caller `workflow_ref` and immutable worker `job_workflow_ref`. The
workers resolve the Launchplane service URL and OIDC audience only from protected
repository configuration, not caller inputs. Land and validate worker behavior
first, then advance the wrapper pin, reconcile the exact managed rule, and run
the bounded action. Both workers retain attempt-scoped redacted evidence for 30
days; the website-bootstrap worker never uploads the literal bootstrap payload
and fails unless the service confirms that typed bootstrap intent was persisted.

`Product Retirement` uses the same worker-first sequence: land `Reusable
Product Retirement` without changing the protected dispatch wrapper, then pin
the wrapper to its merged full SHA in a separate change before authorizing or
using that immutable worker for an operator action.

`Ingress Route Dry Run` and `Ingress Route Apply` accept an optional exact
instance. When present, the service authorizes `ingress_route.plan` or
`ingress_route.apply` against that instance and verifies every requested domain
against the DB-backed product lane before any provider access. An instance-
scoped apply is intentionally adoption-only: it requires an existing host ID,
exact expected-host domains, a DB-backed edge endpoint, and `allow_create=false`.
When the authorized lane domain is one member of a shared provider host, the
service resolves that expected host from the lane-domain subset, then compares
the provider's complete live domain set internally. The audit's
`requested_domains` remains limited to the authorized lane; operation evidence
and the workflow response remain lane-scoped. Response records report whether
the service compared the full expected host and the provider-domain count
without disclosing sibling domain names. Those extra domains never become
Launchplane route authority for that lane.
The service performs a fresh provider dry-run and rejects any create, update,
enable, or disable before recording an unchanged audit. It repeats the provider
read with all mutation flags disabled so drift fails closed instead of changing
the route. Context-scoped callers retain the existing provider-mutation
contract; do not grant that broader scope merely to inspect one testing lane.

Use the `Manage Launchplane Authorization` workflow on the default branch.
Select the intended `managed_set`, then dispatch `mode=dry_run` with the final
single-line reason and related issue that will also be used for apply. The
environment gate protects the OIDC-minting job,
the worker is pinned to a reviewed immutable revision, and all authz runs share
one non-canceling concurrency group. During the one-time bootstrap before the
dedicated wrapper is present in active policy, dispatch `Deploy Launchplane`
with `authz_managed_mode=dry_run`; that job calls the same protected immutable
worker without building or deploying an image.

Babysit protected operator workflows with the installed GitHub workflow helper
instead of dispatching and then polling by workflow name:

```sh
skills_home="${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/skills"
uv run "$skills_home/github/scripts/github_workflow_babysit.py" dispatch \
  --repo OWNER/REPO \
  --workflow authz-policy-reconcile.yml \
  --ref main \
  --field mode=dry_run \
  --field managed_set=MANAGED_SET \
  --field reason=REASON \
  --field related_issue=ISSUE \
  --approve-environment launchplane-authz-admin \
  --timeout-seconds 1800
```

Do not substitute raw `gh workflow run`, `gh run watch`, or a generic Actions
run waiter for this helper. GitHub reports a protected-environment approval as
`status=waiting`; generic waiters commonly treat that as an ordinary
nonterminal state and may emit no approval diagnostic. The exact-run helper
reads `pending_deployments` on the first waiting poll, reports the eligible
reviewer state, and either submits the explicitly authorized approval or stops
with an actionable split-identity error.

The helper dispatches with the configured automation actor and reviews the
protected environment with the active local human GitHub account. Those
identities must be distinct; do not weaken the environment's self-review
protection. GitHub's returned run ID and URL are authoritative. If dispatch
does not return exact run details, stop rather than rediscovering a just-created
run through list filtering. An already-known run may be resumed with `watch
--run-id RUN_ID`.

Every `status=waiting` poll queries `pending_deployments` immediately and
reports the environment, eligible reviewers, wait timer, and
`current_user_can_approve`. Approval occurs only when the active human is
eligible and the exact environment name was supplied with
`--approve-environment`, which is the operator's explicit authorization for
that protected action. A triggering-actor self-review denial or ineligible
reviewer stops immediately with an actionable result. The run must also report
the configured automation actor as its triggering actor. Before approval, the
helper reads the active human's existing review history so resuming the same run
does not repeat a non-idempotent approval. Timer-only and custom protection
waits remain bounded; queued runner jobs and waiting runs with no pending
deployment are reported as distinct diagnostics. The default timeout is 30
minutes and the hard maximum is two hours, so workflow babysitting cannot
silently poll overnight.

Review the job summary and 30-day evidence artifact. The review must cover
`result.diff.plan_sha256`, previous and desired policy hashes, desired-set and
configuration hashes, migration/adoption intent, rule IDs/hashes, add/adopt/
update/remove counts, unmanaged compatibility candidate and retirement counts,
and whether authorization changed. Apply from the same default-branch workflow
with the identical protected desired-set secret, reason, and issue reference;
change only mode to `apply` and supply the reviewed plan SHA-256. The worker
derives the stable idempotency key from the managed set and reviewed plan. The
service recomputes the plan against the active policy and rejects drift before
mutation.

The artifact contains only request hashes/intent and the service's redacted
summary. Failed requests add a bounded error summary containing only the
service error code, at most 500 message characters, and the trace ID. The
worker removes the protected desired policy, raw request, and raw response
material before upload so repository-secret contents do not become
artifact-readable runtime authority.

Use the `Route Binding Reconcile` workflow on the default branch for shared or
production route authority. Supply only the product/context/instance tuple from
Launchplane records. The workflow reads the current redacted binding through the
deployed service, captures its opaque full-record SHA-256 or an explicit absent
expectation, and submits the same provider-neutral reconcile contract used by
the API. It never accepts domains, provider target IDs, ingress host IDs,
certificate references, edge addresses, or provider payloads as workflow
inputs, and it performs no provider mutation.

The latest terminal applied ingress audit is the authority linking a tracked
domain set to its active edge endpoint. Reconcile does not infer that link from
Dokploy project or display names. Missing audits or an audit that names no active
endpoint remain explicit blockers and require the ingress authority to be
recorded through its supported service workflow before route-binding apply.

Route-binding read and apply authority is instance-aware. Context-scoped rules
may list bindings only when no instance filter is supplied; current-record reads
and reconcile calls require the requested instance. Production-instance grants
must bind the dispatch wrapper and a reviewed reusable worker pinned to a full
commit SHA.

Dispatch `mode=dry-run` first. Review the create, unchanged, refresh, blocked, or
conflict outcome, source-record identities and versions, current/candidate
digests, and redacted route authority. Apply requires a unique
`idempotency_key`, an operator reason, and the exact confirmation
`APPLY LAUNCHPLANE ROUTE BINDING RECONCILE`. The service re-plans from current
DB-backed records and compare-and-writes the full record; a concurrent change
returns a conflict instead of overwriting newer authority.

Each successful reconcile attests the current source-record set for 24 hours.
Run reconciliation after any provider-target, tracked target, edge-endpoint, or
ingress-audit authority change and on a cadence no slower than every 12 hours
for active stable lanes. Calls made before half-life are unchanged no-ops; calls
at or after half-life refresh evidence even when the underlying source versions
are unchanged. A domain, target, ingress, TLS-owner, lifecycle, or operator-
ownership difference is not a refresh and must be resolved through the owning
authority workflow before retrying.

The Odoo testing refresh controller exposes
`POST /v1/route-bindings/odoo-testing/controller/run-once`. It accepts no
product, context, instance, domain, or provider selectors. The service derives
its bounded target set from DB-backed Odoo product profiles, keeps only exact
`testing` lanes with active service-owned route bindings, and skips absent,
disabled, externally managed, non-Odoo, and production bindings. The target
limit is 25 and exceeding it fails the whole plan before any write.

Controller dry-run requires the service-scoped
`route_binding.odoo_testing_refresh.plan` action plus `route_binding.read` for
every discovered exact instance. Apply requires the separate service-scoped
`route_binding.odoo_testing_refresh.apply` action, `route_binding.apply` for
every discovered exact instance, a unique `Idempotency-Key`, and confirmation
`APPLY ODOO TESTING ROUTE BINDING REFRESH`. All discovered binding grants are
checked before the first write. The controller then invokes the same
provider-neutral planner as manual reconcile and writes due refreshes
sequentially through each binding's PostgreSQL CAS/transaction lock. A parent
mutation reservation fences concurrent same-key controller runs and is completed
only after the bounded child loop. Unchanged, blocked, and conflict outcomes are
returned without mutation; one denied target blocks the whole batch. The
reusable workflow accepts only mode, reason, and an idempotency key, preserves a
30-day request/response artifact, and fails after artifact upload when any target
requires attention. Production enrollment is a separate change and cannot be
selected through this endpoint.

`Odoo Testing Route Binding Refresh` is the clock-only wrapper. It accepts no
target selectors and calls the reusable worker pinned to an immutable commit.
Manual dispatch defaults to `dry-run`; reviewed proof may select `apply`. The
schedule runs apply at minute 37 every six hours, so a half-life failure surfaces
with at least one additional scheduled attempt before the 24-hour attestation
expires. Workflow concurrency prevents overlapping controller runs while the
service's parent reservation and per-binding locks remain the authoritative
serialization boundary.

Use the `External Route Binding Reconcile` workflow when the public TLS
terminator or reverse proxy is owned outside Launchplane and no provider API is
available. This path adds external authority; it does not replace managed route
creation. When Launchplane owns a supported proxy adapter and managed
credentials, create or update the provider route through the normal ingress
apply workflow, record its terminal audit, and use `Route Binding Reconcile`.

The external workflow accepts only mode, product/context/instance, desired
active or disabled status, source label, reason, idempotency key, and
confirmation. The service derives domains from the DB-backed product profile
and placement from the DB-backed provider target. It requires an enabled public
HTTP health check with strict runtime-identity verification and records no proxy
host id, certificate id, upstream, edge address, or other internal provider
evidence. Dry-run requires `route_binding.external.plan`; apply requires the
separate exact-instance `route_binding.external.apply` action and confirmation
`APPLY EXTERNAL ROUTE BINDING RECONCILE`.

After external apply, run `Public Ingress Monitor` and read product topology.
The route binding is the recorded operator authority; it is not proof of proxy
configuration. Readiness requires a fresh successful public HTTP observation,
an exact expected runtime-identity match, and fresh valid TLS evidence for every
recorded domain. Public HTTP and TLS observations expire after two hours.
Launchplane reports the external proxy's internal state as unsupported while
continuing to verify customer-visible behavior. Runtime identity is strong
control-plane-correlated routing evidence, not cryptographic proof against a
malicious TLS terminator that can cache or rewrite downstream responses; that
terminator remains an explicit accepted trust boundary.

To move a lane from external to Launchplane-managed ingress, first dry-run and
apply the external workflow with `desired_status=disabled`. That explicit CAS
write relinquishes operator-owned authority. Create the managed provider route
and terminal audit through the supported provider workflow, then run managed
route-binding reconcile. Managed reconcile may replace a disabled external
record, but it continues to reject active external ownership. The reverse
transition is intentionally not added by this slice: external apply continues
to reject active or disabled service-owned bindings until a separately reviewed
managed-authority retirement contract exists.

Schema-v1 migration requires `schema_migration: "migrate_v1_to_v2"` in the
protected desired-set JSON. Taking ownership of an existing matching unmanaged
rule requires `unmanaged_adoption: "adopt_matching"`. The same explicit intent
retires one name-only GitHub Actions compatibility rule after the desired managed
rule already exists unchanged, but only when the managed rule is a demonstrable
authorization narrowing of that legacy rule. Action sets must match exactly;
immutable repository identity, caller/reusable-workflow, ref, environment,
product, context, and instance selectors may only narrow. Multiple candidates,
one candidate matching multiple desired managed identities (including identities
being updated), rules with repository IDs, and non-GitHub principals fail closed
or remain untouched. A retirement plan must also leave the applying identity
authorized to administer policy under the candidate policy, so stale immutable
IDs or workflow selectors cannot remove the final usable operator path. The
redacted diff reports candidate and retirement counts plus rule hashes without
returning selectors. The CLI remains only a thin service client for local/
rehearsal use. Direct authz policy list/import DB commands are not supported.
Launchplane self-deploy authority remains separate and does not authorize authz
policy administration.

Authz policy schema v1 remains the compatibility contract for an active v1
policy and retains its existing product/context breadth. Managed desired sets
must use schema v2; reconciling an active v1 policy requires the explicit
`migrate_v1_to_v2` plan intent. Schema v2 adds exact instance selectors to
every principal rule. Selectors are exact names or the lone `*` wildcard; an
empty schema-v2 selector authorizes non-instance scope only and is never an
implicit instance wildcard. Actions that can operate at either context or
instance scope, such as `driver.read` and `product_environment.read`, use
separate context and instance rules. Promotion and other multi-lane decisions
require every affected instance. Migration splits mixed or actionless v1 rules
into explicit context and `*` instance representations so their current breadth
is preserved without teaching new schema-v2 rules that an empty selector means
all instances.

Reviewed exact-instance workflow actions for external route binding and product
health monitoring require schema v2 and cannot be authorized by a schema-v1
compatibility rule.

When the CLI uses `--session-cookie`, it first reads `GET /v1/auth/session` and
then sends the returned single-use CSRF token with strict same-origin fetch
metadata. Use the configured public Launchplane URL so its origin matches
`LAUNCHPLANE_PUBLIC_URL`. Reusing a captured CSRF token, omitting `Origin`, or
calling through a different origin fails closed. Bearer-token and GitHub Actions
OIDC callers do not use this browser preflight.

Normal Launchplane deployment does not mutate authorization policy. It deploys
the reviewed image and may reconcile runtime key-safety policy, but authz grants
remain DB-backed service state. This prevents a checked-in catalog or ordinary
image rollout from silently expanding or replacing live authority.

The former append-oriented authz maintenance bridge is retired. Do not add
per-rule grant/removal endpoints, deploy-time grant catalogs, or workflow inputs
that bypass the managed desired-state review/apply contract. Use managed-set
reconciliation for GitHub Actions, GitHub human, terminal-agent, local-operator,
and local-admin policy changes.

When a GitHub Actions route fails closed and the operator needs to distinguish a
caller, reusable-worker, event, product, context, target, or action mismatch,
grant the caller the narrow `authz_diagnostic.evaluate` action through its
existing managed set. The diagnostic route evaluates only that calling identity
and returns redacted category/fingerprint evidence. Do not replace exact route
authority with wildcard execution grants merely to discover which selector
failed; remove temporary diagnostic authority once the exact operational rule
is confirmed.

The #1049 compatibility cleanup removed stale
`launchplane_service_deploy.execute` GitHub Actions rules for policy import
workflows. Do not reintroduce those broad rules; keep those workflows paired
with narrow managed actions such as `merge_train.policy_import`.

The operator-managed desired set owns workflow and local-operator authority for
`Provider Target Operations`, product config, private health endpoints, and
`Ingress Route Canary Apply`. Keep those rules explicitly scoped by product,
context, instance, action, and immutable workflow identity as applicable. Use
local-admin rules only for rare broader repair authority instead of widening
routine local-operator access. Checked-in catalogs and deploy-time variables are
not authority for these scopes.

Managed reconciliation responses return only authz policy record metadata,
rule counts, managed IDs and hashes, compact diffs, and redacted audit evidence;
they do not echo workflow refs, human logins, owner-agent subjects, or the full
policy body.

Lane health monitoring is Launchplane-owned synthetic monitoring for
generic-web stable lanes, including drivers that inherit generic-web behavior.
The `Public Ingress Monitor` workflow owns both the recurring schedule and
manual operator reruns, so its GitHub OIDC identity stays scoped only to
`public_ingress_monitor.run_once`. Both paths call
`POST /v1/products/public-ingress-monitor/run-once` through GitHub OIDC and are
authorized in the Launchplane service context. Each lane declares a DB-backed
`health_monitoring.monitoring_intent` of `public`, `private`, or `prelaunch` plus
`checks[]`: `public_http` checks validate public reachability, `private_http`
checks resolve a Launchplane-owned private endpoint record by
`private_endpoint_key`, and `provider` checks fail closed until a
provider-specific monitor is wired. `public` requires an enabled public check;
`private` requires an enabled private check; contradictory or missing intent
fails validation. The public-ingress route and record
names are compatibility names for the existing health-monitor observation
family. Observations are sensor evidence; incident records are keyed by product,
lane, and health-check name so public, private, and provider failures do not
overwrite each other. The active key is stable, but each opened occurrence has
its own incident id so recovery followed by a later failure does not replace the
resolved history.

Probe effectiveness and incident eligibility are separate. Public and TLS
checks run for `public` and `prelaunch`; their failures open or update incidents
only for `public`. Private intent suppresses public and TLS probes, while private
and provider checks remain active and incident-eligible in every intent mode.
Prelaunch failures therefore stay visible as readiness evidence without being
reported as public outages. Moving to `public` deterministically activates
incident eligibility on the next monitor cycle.

`public_http` uses the centralized public outbound HTTP policy. Every initial
destination and redirect hop is resolved independently; all IPv4 and IPv6
answers must be globally routable, and any private, loopback, link-local,
multicast, reserved, unspecified, unsupported-family, or mixed public/private
answer set fails closed before a connection is opened. Launchplane connects to
the validated socket address directly while retaining the original hostname for
the HTTP `Host` header and TLS SNI/certificate validation, so the connect step
does not perform a second DNS lookup. The monitor timeout is shared across
connect, request, response read, and redirect hops; the operating-system
resolver still owns its own DNS lookup timeout. `private_http` is intentionally
separate: only a scoped active private-health endpoint record can select the
explicit private client, and public checks never fall back to that path.

Use the `Product Health Monitoring` workflow for shared or production intent
changes. Run `dry-run` first with the exact product/context/instance, check name,
check kind, requested intent, and any registered private endpoint key; review the
returned plan SHA-256, then run `apply` with that digest, a unique idempotency
key, and the exact confirmation phrase. The service validates private endpoint
scope without returning its URL, rejects check-kind changes under an existing
name, and compare-and-writes only the selected check and lane intent. Whole
profile writes cannot change existing health-monitoring authority, and
onboarding updates preserve it.

When intent makes an open public or TLS incident ineligible, the monitor keeps
the real probe observation and writes a separate skipped reconciliation
observation. The incident resolves with `monitoring_intent_changed`, not a false
recovery. Observation, incident, and GitHub outbox rows are fenced by canonical
fingerprints of the product profile and any private endpoint or route binding
used by the target. The checks happen in one transaction, so an in-flight run
discovered under old authority cannot reopen or resolve operator state after a
record changes, even when its human timestamp is unchanged. Email and Discord
delivery behavior and DB-backed notification policies are unchanged; do not
disable destinations to perform an intent transition.

Every failing observation is retained and linked to its open incident. The
monitor derives a typed material fingerprint from failure layer/category,
severity, affected target, route authority, TLS state, and expected-runtime
mismatch. Observation ids, timestamps, retries, summaries, and equivalent HTTP
evidence do not participate. A new incident emits one `opened` event. An open
incident emits `updated` only when that fingerprint changes; otherwise it
updates latest evidence without an immediate delivery. The database serializes
the active incident key and compare-checks both the incident state version and
the full expected incident digest before committing observation, incident,
event, reminder, and outbox rows together. Concurrent acknowledgement, silence,
or monitor passes therefore re-plan against current state instead of overwriting
operator state, opening a second incident, or losing evidence. Any supported
non-monitor incident-state mutation must increment `state_version`.

Notification policies carry a reminder interval from 15 minutes through seven
days; the generic migrated/default interval is six hours. Each incident/policy
pair stores its material-event anchor, current reminder window, last reminder,
and next due time. An overdue pass emits only the current window, never every
missed window. Opening or material update resets the anchor. The outbox and
notification attempt ids use the material event id or reminder-window event id,
not the observation id.

Acknowledgement suppresses reminders for the acknowledged fingerprint. A
material change clears acknowledgement and is immediately eligible for delivery.
A bounded silence preserves all observations/events but suppresses material
updates and reminders until `silenced_until`; after expiry the next unresolved
failure can emit the current reminder. Resolution is never silenced because
GitHub issue sinks must be closed and other operators must see recovery. Public,
private, and provider checks use the configured consecutive-pass recovery
threshold; only the threshold-crossing pass creates the single recovery event.

Inspect active incident state from Product Ops before following an external
notification. The product index derives its active-incident summary from the
existing product read model. An environment's incident history is available at
`GET /v1/products/{product}/environments/{environment}/public-ingress/incidents`,
and one occurrence with its linked observations, material events, reminder
state, notification attempts, and outbox evidence is available at the matching
`/{incident_id}` route. Both reads require `product_environment.read`, resolve
the stable lane from its DB-backed product profile, and return `404` rather than
revealing an incident owned by another lane. Treat GitHub issues, email, and
Discord messages as delivery evidence only: do not infer recovery from a closed
or missing sink, and do not clear outbox rows or destination state to force a
new opening. The operator projection deliberately omits raw destination values,
policy identities, outbox payloads, provider operation details, target URLs,
and provider error text.

TLS observations reuse the same monitor run, record family, and incident
lifecycle. For each active environment route binding, Launchplane probes one
TLS target per bound domain so primary names and aliases are recorded
independently. The recorded route-binding evidence stays distinct from the live
probe evidence: route bindings contribute ownership, ingress, source, and
provider-safe certificate reference metadata, while the active probe contributes
validated-address counts, hostname match evidence, issuer/subject summaries,
and the observed validity window. Launchplane does not persist full chains,
private addresses, internal hostnames, raw socket topology, or secrets.

The TLS probe reuses `resolve_public_http_destination` and the validated-address
direct-connect path. Launchplane resolves the public name once, rejects any
non-public DNS answer, connects directly to the validated address set, and uses
the original domain for SNI and hostname verification. No redirect path exists
for TLS observations, no second DNS lookup is allowed during connect, and each
connect-plus-handshake phase recomputes its socket timeout from the same phase
deadline. DNS and IP subject-alternative-name evidence are both represented
truthfully when a route binding names a public host or public IP literal. Active
probes require TLS 1.2 or newer; obsolete protocol versions are unsupported
rather than silently weakening the client context.
Deterministic TLS states are `valid`, `expiring`, `expired`,
`hostname_mismatch`, `untrusted`, `self_signed`, `unreachable`, `unknown`, and
`unsupported`. DNS failure and timeout map to `unreachable`; private or malformed
destinations and unexpected handshake failures are failing evidence rather than
skips. Only explicit protocol/version incompatibility maps to `unsupported`.
Hostname mismatch, chain failure, expiry, self-signed, and unsupported protocol
each retain their own failure codes so the existing incident authority can open,
update, and resolve without a parallel TLS incident system.

The initial conservative thresholds are intentionally simple: a certificate is
`expiring` when it has 14 or fewer whole days remaining, and active TLS probe
evidence becomes stale after 2 hours. The monitor workflow currently runs twice
per hour, so the 2-hour freshness window tolerates one or two missed scheduled
passes without treating old evidence as current for an entire workday, while the
14-day expiry window gives operators enough time to review ownership and
rotation before a certificate crosses into an outage condition.

The product topology read model may use the newest fresh strict public health
probe to corroborate observed placement between deployments. This applies only
to a public-monitoring lane whose exact enabled check requires runtime identity,
when the health target passes and its expected and observed identities match
each other and the recorded placement identity under the canonical runtime
identity comparison, whose recorded health is also verified and passing. The
placement projection then
uses the public observation as its current provenance; the environment inventory
record, its timestamp, and the environment-level provenance remain unchanged.
Legacy, non-strict, base-page-only, older-than-latest, stale, failing, missing,
unchecked, unverifiable, or mismatched evidence never refreshes placement trust.
This is a read-only corroboration rule, not an inventory heartbeat or a substitute
for deployment, promotion, route, provider-target, or TLS authority.

Notification routing is a separate service-backed policy and delivery concern,
not lane-owned text config. The initial notification destinations are GitHub
issues, email, and Discord; each is selected by DB-backed policy and evidenced
by delivery-attempt records.
GitHub delivery bodies carry a material-event marker for exact replay and a
stable incident marker for issue recovery. Updates, reminders, and resolutions
search the incident marker when their queued payload predates the committed
opening attempt record, then comment or close the recovered issue rather than
creating a duplicate. Do not clear provider markers or outbox rows to force a
retry.
GitHub issue notification delivery uses the managed automation token projected
as `LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN`; it does not fall back to active
local `gh` authentication. Verify the configured actor with a token-scoped
GitHub `/user` API read outside Launchplane, and never print or paste the token
itself into logs, issues, or records.

Use the manual Product Preview TLS workflow for a bounded change to an
Odoo-driver product profile's preview certificate policy. Run `mode=dry-run`
first with the target product, requested `none` or `letsencrypt` value, and an
operator reason. The workflow requires a target-product-scoped
`product_profile.preview_tls.apply` grant from operator-supplied authz config;
do not add product grants to checked-in workflow defaults. Review the artifact's
current/requested values, profile timestamp, and plan SHA-256.
Apply only by starting a second run with `mode=apply`, the same target/value/
reason, the reviewed plan SHA-256, and the exact confirmation text. Launchplane
re-reads the current DB-backed profile, rejects stale plans, and preserves every
unrelated profile field. Do not replace this workflow with whole-profile API,
direct DB, or direct provider mutation.

Use the manual Product Health Monitoring workflow to create or update one
stable-lane `public_http` check without replacing the product profile. Run
`mode=dry-run` with the exact product/context/instance, check name, desired
enabled state, runtime-identity requirement, and operator reason. The workflow
does not accept URLs, domains, provider targets, proxy details, or certificate
references; Launchplane preserves or derives the URL from the current DB-backed
lane. Enabling strict runtime identity requires an existing lane-owned HTTPS
host. Review the current/requested values, resolved URL, profile digest, and plan
SHA-256. Apply from a second run with identical target and desired values, the
reviewed digest, a unique idempotency key, and confirmation
`APPLY PRODUCT HEALTH MONITORING`. The service re-reads the complete profile and
atomically rejects stale or concurrent edits. Authority uses separate exact-
instance `product_profile.health_monitoring.plan` and `.apply` actions supplied
through the dedicated `product-health-monitoring` managed authz set. That set
owns only the generic wrapper's exact immutable reusable-worker grant; real
product, context, instance, check, and endpoint values remain explicit operator
input and DB-backed Launchplane records.

Use the manual Product Prelaunch Rebuild Policy workflow when an Odoo lane must
authorize a reviewed destructive rebuild before stable target replacement. Run
`mode=dry-run` first with the exact product/context/instance, enabled state,
issue-backed approval URL, typed data source, target-replacement confirmation
phrase, expected target proof, expected-domain JSON array, and operator reason.
The workflow has no checked-in product, lane, target, domain, volume, or
notification defaults. Review the current/requested policies, data authority,
allowed rebuild sources, monitoring intent, complete-profile digest, and plan
SHA-256. Apply from a second run with identical desired policy fields, the
reviewed digest, a unique idempotency key, and confirmation
`APPLY PRODUCT PRELAUNCH REBUILD POLICY`.

Authority uses separate exact-instance
`product_profile.prelaunch_rebuild.plan` and `.apply` actions tied to the exact
immutable reusable worker. The service fails closed unless the lane remains
`prelaunch`, its existing `odoo_data_policy` permits the requested source, and
`empty` rebuilds are explicitly `resettable`. This workflow changes no data
policy, health intent, route, provider target, runtime setting, secret, or
volume identity. After apply, rerun Odoo Target Replacement Plan; add target-
replacement apply authority only after that plan returns `ready`. Disable the
policy through the same dry-run/apply workflow after recovery if no further
prelaunch rebuild should remain authorized.

Render an explicit emergency bootstrap policy for service startup with:

```bash
uv run launchplane service render-authz-policy --policy-file ./bootstrap-policy.toml
uv run launchplane service render-authz-policy \
  --policy-file ./bootstrap-policy.toml \
  --format b64
```

Do not import or replace the active DB-backed authz policy directly. Once the
service can start, policy changes use managed rule-set reconciliation and its
reviewed plan/CAS boundary.

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
- For repo-scoped runner lifecycle, use the stable
  `.github/workflows/runner-lane-registration.yml` workflow path and select
  `register` or `retire`. Start with `mutate=false`, use an operation-shaped
  audit key under `runner-lane-registration/<date>/...` or
  `runner-lane-retirement/<date>/...`, and inspect the uploaded lifecycle JSON.
  Registration mutation requires `register runner lane`; retirement mutation
  requires `retire <owner/name> <lane>`. The workflow writes service-backed
  lifecycle audit evidence through the existing exact OIDC-authorized path,
  serializes host mutation with runner-host hygiene, and uploads the local JSON
  artifact for operator review.
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
confirmation text, a previous immutable `@sha256` image reference from the
configured Launchplane image repository, and an operator reason. The reason
must be a single printable line from 8 to 500 characters and contain
non-whitespace text. Configure the `launchplane-break-glass` GitHub environment
with required reviewers and protected-branch deployment rules before enabling
this path: its approval gate completes before the job receives the emergency
Dokploy credentials, the workflow accepts manual dispatches only from the
repository default branch, and the job validates the request before the
credentials are scoped to the rollback step.
Keep the workflow configured with break-glass
`LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST` and
`LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN` repository secrets for that manual
emergency path. When direct Dokploy break-glass rollback runs, the workflow
writes a redacted `launchplane-break-glass-rollback` artifact and summary so the
provider mutation remains reviewable after the emergency.

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
`LAUNCHPLANE_DATABASE_URL`, `LAUNCHPLANE_SECRET_KEYS_JSON` (or the
migration-only legacy `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`),
Launchplane-managed Dokploy secret bindings, or a Dokploy SSH key for a private
`git@github.com:...` compose source.

The intended live service contract is now bootstrap-only target env plus
DB-backed Launchplane records:

- keep bootstrap/process inputs such as `LAUNCHPLANE_DATABASE_URL`,
  `LAUNCHPLANE_SECRET_KEYS_JSON`, migration-only
  `LAUNCHPLANE_MASTER_ENCRYPTION_KEY`, and policy selectors on the service target
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

For an invalid tracked Dokploy target domain authority, use the `Dokploy Target
Setup` workflow with `operation=repair-domain-authority`. Supply the exact
tracked target type and target ID, a comma-separated tuple of bare DNS hosts,
and the JSON projection of the current provider-target record as the compare
expectation. The service reads provider evidence only; it does not create,
update, or delete Dokploy domains. Run dry-run first. Apply is allowed only
after the dry-run shows the intended domain-only projection and requires the
normal setup confirmation, operator reason, and idempotency key. The repair
preserves all target metadata except `domains`, `updated_at`, and
`source_label`. The same atomic write advances only `updated_at` and
`source_label` on the canonical provider-target projection so downstream
projection checks remain consistent.

Two deployment prerequisites remain Dokploy-side operational contracts rather
than Launchplane CLI validations:

- Dokploy must already have a working saved registry credential that can pull
  Launchplane's GHCR image.
- The Postgres service referenced by `LAUNCHPLANE_DATABASE_URL` must already be
  deployed and reachable on the Dokploy network before Launchplane is redeployed.

The Launchplane service entrypoint runs
`uv run python -m control_plane.storage.schema_migration` with the container's
`LAUNCHPLANE_DATABASE_URL` before starting HTTP service. The helper serializes
startup migration through a PostgreSQL advisory lock and advances only to the
image's explicit migration target. This keeps hosted service startup fail-closed
on schema drift while preserving expand/contract rollout and rollback boundaries.
The self-deploy workflow should not run shared database migrations from the
GitHub runner. Its deployed-runtime smoke requires the observed database
revision to appear in the image's reported compatible-revision set before the
deployment is accepted; it records both the observed revision and the image's
forward migration target as rollout evidence.
The service route keeps OIDC authz and idempotency at the HTTP boundary, then
delegates provider target env mutation and deployment triggering to the
Launchplane self-deploy workflow module.
If a shared database already has Launchplane tables but no `alembic_version`
table from the pre-migration `create_all` era, the helper stamps the newest
revision that matches the detected historical table set before applying later
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
  captures DB and filestore backup evidence,
  `POST /v1/drivers/odoo/prod-backup-verification` reads an exact passing Odoo
  backup-gate record and runs read-only integrity checks against the captured
  backup, and `POST /v1/drivers/odoo/prod-promotion`
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
  workflow bridge. The UI reads product-owned promotion status, calls the
  product/environment direct dry-run route, then dispatches the matching
  product/environment workflow route. Launchplane derives artifact and source
  identity from generated testing runtime evidence, requires current
  testing/production evidence and the selected bump to match the accepted
  direct dry-run, resolves the repository/workflow from the DB-backed product
  profile, and resolves the managed `GITHUB_TOKEN` from runtime records. The
  product workflow receives reviewed `artifact_id` and `source_git_ref` inputs
  and still owns release/tag creation and product-specific safeguards.
- Promotion status requires digest-pinned testing and production artifacts,
  immutable source commit IDs, fresh matching runtime identity/health evidence,
  and an explicit current production provider-target record. Historical deploy
  targets do not satisfy this gate.
- Workflow dispatch is provider-free in the HTTP request. The outbox worker
  captures the pre-dispatch GitHub run set and persists its provider marker
  before sending. A marked delivery that cannot yet observe its run remains in
  reconciliation and is never sent again.

## Core Rules

### Conventional generic-web onboarding

Use the manual `Product Onboarding` workflow for a new generic-web product.
Start with `mode=dry_run` when operator review of the exact plan is useful, or
use `mode=apply` for one dispatch that pauses at the protected
`launchplane-authz-admin` environment before any writes. Provide product,
repository, image repository, runtime port, health path, preview base URL, and a
reason/issue reference; optional names derive from the product key. The preview
base URL must be a root URL. Its wildcard DNS and ingress namespace must already
route to the selected Dokploy server because onboarding owns Launchplane and
Dokploy records, not public ingress provisioning.

The workflow resolves immutable GitHub identity, plans one non-production
Dokploy application, plans Launchplane records including the context-scoped
`LAUNCHPLANE_PREVIEW_BASE_URL`, and plans the complete
`operator.generic-web-preview` managed authz set. The apply job reuses the
reviewed plan artifact and digests. Do not copy target ids, manifests, authz
JSON, or database credentials into the conventional workflow.

Planning authority is read-only and distinct: the plan job uses
`dokploy_target.plan`, `generic_web_onboarding.plan`, and
`generic_web_preview_authz.plan`. Only the protected apply worker receives
`dokploy_target.setup`, `product_onboarding.apply`, and
`authz_policy_grant.write`. Both protected workers are called by immutable
full-SHA reusable-workflow refs so policy-admin grants never depend on a mutable
branch identity.

Use `Generic Web Preview Authorization` for reviewed `onboard`, `expand`,
`contract`, and `retire` operations. `onboard` bootstraps the managed preview
rules for an existing product that has no current `operator.generic-web-preview`
rules; it rejects a different existing managed rule set. The workflow derives
the same conventional caller paths and immutable repository identity, submits no
hand-authored policy JSON, and applies only through the protected managed
reconcile contract.

For products previously owned by the retired per-product generic-preview
secret, run `expand` at the new Launchplane SHA, repin the product callers, then
run `contract` at that same SHA. This reviewed overlap removes legacy managed
rule identities without an authorization gap. Use `retire` when offboarding the
product entirely.

If provider creation succeeds and a later step fails, rerun the exact same
workflow inputs, including the reason. Stable idempotency keys replay the provider result and prevent duplicate
creation. If records succeed but authz fails, leave the records in place and
rerun; the product remains unauthorized until the existing managed authz
reconcile succeeds. Use `Product Onboarding Manifest (Advanced)` only when the
typed generic-web contract does not fit, and use `Dokploy Target Setup` for
adoption, production, compose, or repair operations.

To change the preview base URL later, rerun `Product Onboarding` with the new
root URL. Onboarding updates its owned key while preserving unrelated values in
the same preview-context runtime-environment record.

Products onboarded before this input existed must rerun `Product Onboarding`
once to record the preview base URL. Until then, preview readiness and refresh
return a typed blocked result rather than guessing a domain.

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
- Docker-provider application deploys without inline pull credentials verify
  that the target's saved registry host matches the artifact host, then ask
  Dokploy to authenticate that provider-held credential on every distinct
  deployment and build server before mutating the image. Registry mismatch,
  lookup failure, or login failure blocks deployment without exposing the saved
  password.
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
  `--search`, and redacts likely secret values from returned log lines. Compose
  runtime reads may also pass `--service <exact-compose-service>` to select one
  exact service container. Launchplane fails closed when that service is absent
  or ambiguous; application and deployment log reads reject service selection.
- `GET /v1/contexts/{context}/instances/{instance}/logs?lines=200` exposes the
  same tracked-target log reader through a native FastAPI service route using
  action `target_logs.read`. The default `source=runtime` reads current
  application or compose logs. `source=deployment` reads the latest tracked
  target deployment log with the same line bound and redaction contract; it
  requires `since=all` and does not accept search text. Launchplane verifies the
  selected deployment is bound to the requested tracked target before reading
  its detached log id. Provider failures return a bounded redacted operation
  label/detail instead of raw credentials or unrestricted provider output.
- The manual Tracked Target Logs workflow calls that service route with GitHub
  OIDC and uploads the redacted JSON result, so operators can inspect runtime or
  deployment failures without local Dokploy credentials. Tenant contexts need a
  matching service-backed `target_logs.read` GitHub Actions grant; do not broaden
  this workflow to read all contexts by default. The workflow uploads the
  redacted response artifact even
  when the provider request fails, then fails the job after preserving that
  diagnostic evidence.

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
  valid `LAUNCHPLANE_SECRET_KEYS_JSON` or the migration-only legacy key. Runtime
  and secret scopes default from
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
  when secret writes are requested without valid Launchplane secret-key
  configuration in the service runtime or when no active runtime key-safety
  policy allows the requested binding. Apply commits through a product
  authority bundle, so runtime-environment records, managed-secret versions,
  current secret pointers, bindings, audit events, and idempotency completion
  publish together or roll back together. When apply changes
  runtime-environment keys for a tracked Dokploy target, the response includes a
  required `live_target_runtime_apply` `next_actions` item. Treat
  product-config apply as a record mutation only until that next action has
  been dry-run and applied through `/v1/live-target-runtime/apply`; redeploying
  the same app image does not sync the live Dokploy target environment.
- `POST /v1/secrets/reencrypt` is the normal shared/production root-rotation
  path. Run `mode: "dry-run"` with `secret.reencrypt.dry-run`, then submit
  `mode: "apply"` with the returned plan digest, a reason, an
  `Idempotency-Key`, and `secret.reencrypt.apply`. Launchplane atomically writes
  the new versions, current-version pointers, audit events, and idempotency
  completion evidence in one storage transaction. A failure before commit leaves
  current pointers and replay evidence unchanged; a committed apply is replayable
  by the same idempotency key. Direct `launchplane secrets reencrypt --apply`
  remains bootstrap/recovery-only and requires explicit direct-DB
  acknowledgement.
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
  the exact schedule deployment emits nonce-, record-, and database-bound
  completion evidence with non-empty artifact sizes and SHA-256 values. A
  provider `done` status without that bounded marker fails the gate. Directory
  preparation accepts only the dedicated `/volumes/data/backups/launchplane`
  tree and assigns only its exact backup-root/database/record path to the
  script-runner identity, so stale root-owned directories cannot make `pg_dump`
  fail after the provider has reported success or broaden ownership changes to
  unrelated mounted data.
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
  databases. Preview refresh, stable replacement, post-deploy update, and stable
  bootstrap also preserve the recorded addon path while adding the managed
  `/opt/launchplane/addons` and `/opt/enterprise` roots when missing. This
  prevents a stale explicit `ODOO_ADDONS_PATH` record from overriding the
  compose default and making a required module dependency unavailable during
  fresh database initialization or maintenance. Managed data-workflow schedules
  also pass their resolved `ODOO_FILESTORE_PATH` explicitly so a target that
  relies on the compose default still satisfies the workflow's typed runtime
  configuration.
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
image tag, and product source repository. The DB-backed product profile remains
authoritative for the tenant checkout; an optional caller repository value is
only an equality assertion and cannot redirect the build. Launchplane passes the
runtime payload to `odoo-devkit` as a one-shot runtime payload for the publish
subprocess, validates the returned artifact belongs to the requested context,
and writes the artifact manifest back to Launchplane records. Schema-v2 evidence
must also identify the product-profile repository as the tenant uv-lock source.
Do not point a local devkit checkout directly at the live Launchplane database
or recreate runtime env files to publish artifacts.

Launchplane accepts historical schema-v1 artifact manifests and strict
schema-v2 manifests. V2 records carry typed uv-lock, per-platform Python package,
and exact-source external compatibility evidence. Launchplane validates the
evidence shape, exact refs, sanitized source identities, and canonical package
digest before storage, but it does not resolve or install tenant dependencies.
Dependency resolution, environment inspection, and evidence generation remain
with the artifact producer and generic image tooling.

The rollout is reader-first. During the coordinated producer migration, v1
publish requests remain accepted for compatibility but never satisfy a v2
provenance requirement. A v2 envelope, publish request, and returned manifest
must all declare version 2; local publish also binds the returned manifest to the
requested image repository and complete requested platform set. After all
producers migrate, service ingress can require v2 without rewriting historical
v1 records.

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
before considering any provider mutation. Product identity is always explicit;
the CLI does not select a tenant default:

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

The reusable target-replacement plan and apply workers first read
`GET /v1/products/{product}/environments/{environment}` and derive the exact
context plus current artifact from Launchplane records. They then call the
action-specific operational-readiness route with their own OIDC identity and
select the typed `readiness` member for the bounded evidence artifact and exact
gate check. Plan payload generation and apply operation creation occur only
after an exact `ready` result. Apply uses a
complete explicit `artifact_id`/`source_git_ref` pair when supplied; otherwise it
pins both values from the current environment record before enqueue. The plan
workflow accepts the same pair so a newly published immutable recovery artifact
can be reviewed before apply. Plan and apply both read the selected manifest,
require its source commit and image repository to match, and reject artifacts
whose `odoo_install_modules` omit Launchplane's required safety modules. Apply
repeats those checks before creating a durable operation. Both
workers also carry the environment's current artifact as a separate expected
snapshot; the plan route, apply enqueue path, and operation worker fail closed
if that lane artifact changes. Candidate artifacts must use the product
profile's recorded image repository. The dispatch wrappers are the operator
entrypoints and pin those reviewed reusable workers to a full commit SHA. When
worker behavior changes, land and validate the reusable contracts first, then
advance the wrapper pin in a separate reviewed change.

Target-replacement plan and apply readiness intentionally permits a degraded
current deployment or failed public observation because replacement is the
repair path for those states. Provider-target authority, route binding, runtime
environment, managed secrets, and the exact persisted artifact remain required.
The plan and apply paths independently fence the current inventory artifact,
and apply still requires passing post-deploy verification before inventory or
release evidence advances.

The operator UI can inspect that same readiness contract from the environment
`Actions` route without dispatching a workflow or invoking a descriptor route.
It derives exact lane and artifact selectors from the product-environment read
model, displays browser identity separately from immutable workflow identity,
and treats remediation routes as non-executable metadata unless an explicit
typed browser operation exists. Repository workflow metadata must keep the
testing deploy worker plus the target-replacement dispatch and reusable workers
discoverable so operators can follow reviewed workflow ownership instead of
falling back to an arbitrary checkout or local live-target command.

The plan reads the product profile, Launchplane Dokploy target/id records,
current inventory, live Dokploy target payload, domains, exact live
`ODOO_DATA_VOLUME`, `ODOO_LOG_VOLUME`, and `ODOO_DB_VOLUME` values, latest
deployment, and expected runtime identity. For `data_source_mode=existing`, the
plan also resolves desired volume authority from DB-backed runtime records and
blocks when any desired value differs from the live target. It does not create,
delete, deploy, change routes, or reinterpret a volume change as existing data.

When production recovery starts from retained physical volumes and no valid
logical backup exists, use the dedicated retained-volume backup import actions;
do not add import flags to routine backup or restore. First run
`Odoo Prod Retained Volume Backup Import Plan` with the exact product, production
context and instance, current artifact, active database/data/log volumes, source
database/data volumes, source database name, destination database name, expected
database owner, fresh staging-clone volume, source compose project, and a new
backup-record id. Every value is an operator request field checked against
DB-backed current authority or read-only provider evidence. The plan mounts the
source volumes read-only, verifies their compose project and `odoo_db` /
`odoo_data` roles, proves PostgreSQL major version 17, captures the exact
`PG_VERSION`, `pg_control` SHA-256 and checkpoint metadata, requires the source
cluster state to be exactly `shut down`, counts and sizes the source filestore,
proves the staging and destination paths absent, checks active data-volume free
space, and binds all evidence into a SHA-256 plan fingerprint. PostgreSQL
control tools are resolved from the image's versioned bindir rather than
assuming an overridden shell entrypoint preserves the image's tool `PATH`.
The fingerprint binds the conservative required-capacity calculation but not
the volatile observed free-byte count. Apply remeasures live free space and
blocks before backup-record or provider-import effects when it falls below the
reviewed requirement, while harmless filesystem churn does not invalidate an
otherwise identical plan.

A failed target replacement can update the live target's runtime identity before
post-deploy verification fails while production inventory correctly remains on
the last successful deployment. Retained-volume import accepts that repair state
only when every identity field except `deployment_record_id` and `deployed_at`
still matches inventory, `deployed_at` is empty, and the live deployment id
resolves to an exact failed deployment record for the same target, artifact, and
source. The accepted live identity is included in the reviewed plan fingerprint,
so another replacement attempt invalidates apply. This exception neither marks
the failed deployment successful nor advances production inventory.

The active database container may be stopped or restarting during this repair.
Plan and apply therefore resolve exactly one database container across all
states and use only `docker inspect` evidence from it; they never start or
execute inside that container or mount its corrupt database volume. The
script-runner remains running-only because it emits the bounded result and
writes the imported logical backup into the active data volume.

If the provider inspection schedule emits a failure instead of its result, the
service reads only that exact schedule deployment and accepts one nonce- and
backup-record-bound failure marker. The marker is honored whether Dokploy marks
the deployment failed or records it as done, because provider terminal status
does not reliably preserve the script exit status. The operation checkpoint
records only the request nonce, backup-record id, inspection deployment id, and
a bounded failure stage/code pair; raw provider logs and command output are not
copied into the plan, operation error, or API response. An unreadable exact log
is recorded as `result/provider_result_log_read_failed`; a missing, duplicate,
or invalid marker is recorded as `result/provider_result_invalid`. This
distinguishes a provider inspection failure from a GitHub environment approval
wait without weakening the fail-closed plan boundary.

Failures before a script result are also bounded. Target lookup, schedule
runtime resolution, upsert, baseline read, trigger, wait,
deployment-id extraction, result-log read, and result parsing each have a
distinct allowlisted code. Their checkpoint records the exact schedule and
deployment ids when those identities are available, but never persists the
underlying provider exception text.

Run `Odoo Prod Retained Volume Backup Import Apply` only with the exact reviewed
plan operation and fingerprint, a stable idempotency key, and confirmation phrase
`import-retained-volumes-as-production-backup`. The service queues a dedicated
durable operation and rechecks the recorded authorization after claim and again
immediately before the first provider effect. Once that effect is authorized,
the same operation keeps its authorization fence through later effects rather
than stopping mid-mutation on a policy revision. Apply re-runs the plan and fails
closed on authority or provider drift before mutation begins.
It never starts PostgreSQL on, or writes to, either retained source volume. It
copies the source database volume read-only into the fresh staging volume while
preserving ownership, verifies the copied `pg_control` fingerprint, and starts
PostgreSQL 17 only from that clone in an isolated named container and network.
It proves the requested source database exists by connecting to that exact
validated database name instead of interpolating the name into SQL. It then
writes a custom-format dump and source-filestore archive into the active data
volume's standard Launchplane backup directory. Artifact names and the schema-v1
manifest use the destination database identity; the archived filestore has that
identity as its top-level directory. Launchplane verifies sizes and SHA-256
values and accepts only nonce-, request-, and exact schedule-deployment-bound
completion evidence.

The clone container and network are removed on exit, while the staging clone
volume remains labeled for explicit recovery inspection. A passing import writes
a distinct retained-volume-import backup-gate source. Standard backup
verification and guarded restore accept that source, but routine production
promotion accepts only its ordinary backup-before-promote source. This action
does not update the live target, runtime environment, deployment, inventory, or
release tuple and performs no cutover. Inspect or cancel it through the deployed
service operation endpoint; never rerun its provider schedule directly or use a
local checkout as a live-target fallback.

Use the `Odoo Prod Backup Verification` workflow to exercise the verification
route through GitHub Actions OIDC. The workflow accepts an exact backup-gate
record id, stores a bounded workflow artifact, and returns the id of a separate
durable verification record. Verification recomputes the
expected paths from DB-backed runtime records, checks manifest identities,
paths, sizes, and SHA-256 values, runs `pg_restore --list` plus a full archive
render to `/dev/null`, validates safe tar members under the expected database
directory, and confirms enough free space on the Odoo data volume to stage the
uncompressed filestore. Each provider result is bound to a fresh request nonce,
the exact backup record id, and database name before Launchplane accepts it. The
route returns hashes, counts, sizes, per-check statuses, and a bounded failure
code only. If an unexpected verifier exception occurs, the active bounded check
is marked `fail`; manifest processing additionally reports a bounded path,
read, validation, artifact-path, or artifact-metadata subphase code. Operators
can distinguish the manifest, hash, database archive, filestore archive, and
staging-space phases without exposing provider paths or exception text.
Filesystem metadata failures in the manifest phase map to the existing bounded
manifest missing, metadata, size, read, or decode codes; artifact metadata
failures map to `artifact_missing`. Raw I/O errors and private paths are not
returned. Verification does not restore a database, extract a filestore, stop
containers, or mutate served Odoo data.

Production restore is a separate destructive authority. First run
`Odoo Prod Backup Restore Plan` with the exact product, production context,
passing backup record, passing durable verification record, current artifact,
current corrupt database volume, fresh database volume, and unchanged data and
log volumes. The planner reads all runtime authority from Launchplane records,
requires the live target to match the old database/data/log snapshot, requires
the fresh database volume name to be the DB-backed desired value and absent from
the provider, and binds the verification hashes, counts, sizes, paths, target,
domains, and runtime identity into the reviewed SHA-256 plan fingerprint.

Run `Odoo Prod Backup Restore Apply` only with that exact fingerprint, a stable
idempotency key, and confirmation phrase
`restore-verified-production-backup`. The service creates a dedicated durable
restore operation; routine existing-data target replacement cannot exercise
this authority. The reusable workflow posts the exact reviewed apply request
without independently rebuilding the plan. The service first resolves an
existing operation by idempotency key so an eligible terminal verification
failure can enter its bounded replay path after cutover. For a new operation,
the service still rebuilds the current plan, requires `ready`, and requires the
exact reviewed fingerprint before persistence. The workflow derives the
canonical bounded operation-status route from the accepted path-safe operation
id instead of requiring a redundant service-supplied poll URL. The worker
rechecks authorization after claim and immediately before the first provider
effect, then records before/after checkpoints for fresh database restore,
filestore staging, web quiesce, filestore activation, runtime-environment
update, deploy, post-deploy work, and verification. Once any provider effect
starts, an expired lease moves to `reconciliation_required`, keeps the shared
lane fenced, and requires exact provider inspection rather than automatic
retry.

The database phase creates the exact fresh volume and restores the complete
archive with `pg_restore --exit-on-error`; it never runs `pg_resetwal` and never
attaches or mutates the old corrupt database volume. Filestore staging validates
every tar member again, rejects absolute/traversal/link/special entries, checks
the reviewed member count and expanded size, and extracts into a sibling staging
directory on the unchanged data volume. After the web service is stopped, the
old filestore is moved to the reviewed quarantine path and the staged tree is
activated. The data and log volume names cannot change. The old database volume
and quarantined filestore remain available for explicit later cleanup.

Apply runs the Odoo post-deploy driver with deploy-phase semantics so
DB-backed website bootstrap and ordinary production post-deploy settings are
applied before canonical verification. It passes only after deploy and
post-deploy complete and Launchplane verifies the health endpoint, canonical
page, logo route, and exact runtime identity. Deployment, inventory,
release-tuple, phase checkpoint, authorization, provider result, and final
verification evidence remain durable in Launchplane. A failed or interrupted
operation must be inspected from its operation record before any new restore is
planned; do not rerun a provider schedule or use target replacement as a
recovery shortcut. Missing, duplicate, malformed, mismatched, or unbounded
provider phase results fail closed with the exact restore phase and an
allowlisted `provider_result_*` or `provider_evidence_unbounded` code; raw
provider log text is not persisted in the operation error.

An exact idempotent replay of the same restore apply request may requeue one
terminal failed operation only when the stored result proves database restore,
filestore staging, web quiesce, filestore activation, deployment, and
post-deploy all passed, the ordered durable checkpoints and phase evidence prove
those same effects plus target-env cutover, and the failure was limited to final
health, canonical, logo, or runtime identity verification. The requeued
operation keeps prior checkpoints and evidence, preserves the same operation id,
and is claimed as attempt 2. Attempt 2 verifies current lane, target, artifact,
and deployment-record authority; verifies the live target still points at the reviewed new
database volume plus unchanged data and log volumes, verifies the live runtime
identity matches the existing failed deployment record, reauthorizes before any
post-deploy provider effect, reruns only non-destructive deploy-phase
post-deploy, reruns final health/canonical/logo/runtime-identity checks, then
reconciles that same deployment record to pass and writes inventory and release
tuple evidence. It never reruns database restore, filestore staging,
filestore activation, target volume/env update, or main deploy. Any missing or
mismatched stored proof, active stable-lane operation, changed live volume
authority, missing failed deployment identity, or second failed replay remains
fail-closed and requires operator reconciliation rather than another automatic
provider mutation. Any operation carrying prior result evidence is barred from
the full restore path, even if a later stale-lease or malformed-record condition
changes its attempt number. Replay target-read, parser, post-deploy, readiness,
and runtime-identity failures persist only an allowlisted
`live_target_*`, `post_deploy_*`, `readiness_*`, or `runtime_identity_*` replay
code; raw provider response bodies and live env values are not persisted.
If an attempt-2 replay lease expires, recovery moves the operation to
`reconciliation_required`, clears the stale prior result envelope required by
the reconciliation-state contract, and retains the reviewed plan, deployment
record id, and ordered checkpoints for operator diagnosis.

Legacy backup-gate manifests created before manifest schema and hash fields were
introduced remain verifiable: Launchplane validates their recorded identity,
paths, and sizes, computes current SHA-256 values from the artifacts, and binds
those values into the verification result. When a manifest does include schema,
manifest-path, or hash fields, those fields must match exactly.

After a target-replacement apply deploys and the Odoo health, canonical, and
logo probes pass, lanes whose Odoo data policy requires runtime identity perform
a separate bounded JSON health readback. This second probe may use the full lane
health timeout so a stale old container can drain before the new deployment
identity appears. A missing, malformed, mismatched, or unreachable identity
marks the deployment failed and leaves current inventory and release tuples
unchanged. A successful record carries `runtime_identity_status=match` plus the
bounded observed identity copied into environment inventory.

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
operation leaves `pending` or `running`, then uploads the final operation payload
as the workflow artifact. `reconciliation_required` therefore stops polling and
surfaces as a failed, operator-actionable workflow result instead of waiting for
the poll timeout. `Idempotency-Key` is required: a repeated request with the same
key from the same caller identity returns the existing operation, while a
different key for the same product/context/instance is rejected while a
`pending`, `running`, or `reconciliation_required` operation is active. Storage
owns that active-lane reservation so the worker starts only after the lane is
claimed; abandoned filesystem reservations recover after a bounded settle
window if the owner or owner record never appears. The first apply surface is
testing-only and keeps
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

Before the worker's first provider mutation it reauthorizes the stored caller,
exact target, and managed rule against the current active policy. Revoked or
narrowed authority and legacy operations without provenance fail terminally
without provider mutation. Operators may cancel pending replacement work with
`POST /v1/drivers/odoo/target-replacement/operations/{operation_id}/cancel` and
a non-empty `reason`. Reconciliation-required work additionally requires a
structured attestation with a provider inspection timestamp between the fence
and cancellation request, the observed provider state, an evidence reference,
and explicit `safe_to_release=true`; running work returns
`409 operation_not_pending`.

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
operation leaves `pending` or `running`, then uploads the final operation payload
as
the workflow artifact. `Idempotency-Key` is required: a repeated request with the
same key returns the existing operation, while a different key for the same
product/context/instance is rejected while a `pending`, `running`, or
`reconciliation_required` operation is active. The operation record stores the
request, status, phase,
deployment-record linkage when known, final bootstrap result, and any terminal
error message so operators can inspect progress after the original HTTP request
has ended.
The worker reauthorizes the stored managed rule and exact lane after claim and
immediately before the destructive Dokploy schedule. Operators may cancel only
pending work, or reconciliation-required work after inspecting the exact provider
state, through
`POST /v1/drivers/odoo/stable-bootstrap/operations/{operation_id}/cancel`; the
request records a reason and caller identity. Reconciliation-required
cancellation also persists the provider-inspection timestamp, observed state,
evidence reference, and explicit safe-release attestation; stale or missing
inspection evidence cannot release the lane. Running work fails the cancellation
request with `409` rather than claiming no external effect occurred. Unsafe lease
expiry never frees the shared stable lane automatically: it clears the stale
lease, records `operation_reconciliation_required`, and keeps all bootstrap,
target-replacement, restore, and retained-volume-import claims blocked until the
operator resolves that evidence.

Pending VeriReel backup-gate work uses the equivalent endpoint
`POST /v1/drivers/verireel/prod-backup-gate/operations/{operation_id}/cancel`.
Cancellation writes terminal operation evidence and failed backup-gate evidence
atomically so later promotion checks continue to fail closed.

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
it runs on the operator-configured `LAUNCHPLANE_RUNNER_LABEL` self-hosted lane
instead of depending on hosted-runner capacity.
The service derives the participating products from product profiles where
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

### Preview Feedback Remediation

Use `POST /v1/previews/pr-feedback/remediation` only when a historical preview
workflow identity cannot be replayed under current authorization. Submit one
product/repository/PR per request. Run `dry-run` first with a stable
`Idempotency-Key`, inspect the observed marker ownership and planned action,
then submit `apply` with the same key and the request contract's exact
confirmation phrase. Stop if the product profile, PR URL, token actor, marker
owner, or observation differs. An absent comment is reconciled as
`already_absent` with `mutated=false`; it is not rewritten as a deletion.

The route uses the context-scoped preview `GITHUB_TOKEN`, accepts only local
operator/admin identities with `preview_pr_feedback_remediation.plan` or
`.apply`, and never grants this mutation to preview workflow OIDC identities.
Attach the remediation and companion feedback record ids to the governing issue.

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

## Product Retirement

Use the protected **Product Retirement** workflow; never delete a Dokploy
application directly. Run `mode=plan` with the exact product, profile-owned
instance, tracked target SHA-256, stable operator idempotency key, reason, and
issue. Review the persisted plan record ID and `plan_sha256` in the redacted
artifact and job summary. Run `mode=apply` with the same intent and idempotency
key, the reviewed record ID/digest, and the exact confirmation shown by the
workflow input contract.

An apply that returns reconciliation-required must be retried with the same
idempotency key after the provider can be observed. A `retiring` profile is an
intentional fail-closed state: deploys, monitoring, previews, onboarding,
discovery, and work-graph automation must leave it untouched while the durable
operation reconciles. Completion verifies provider absence, removes mutable
runtime/target authority, preserves secret and deletion evidence, disables
preview configuration, and marks the profile `retired`. Do not delete the
profile.

Authorization rollout uses the `product-retirement` selector in **Manage
Launchplane Authorization**, backed by managed set
`operator.product-retirement` and secret
`LAUNCHPLANE_AUTHZ_PRODUCT_RETIREMENT_MANAGED_SET_JSON`. The dispatch wrapper
is pinned to an immutable reusable worker. Authorization must bind both the
caller `workflow_ref` and the exact immutable `job_workflow_ref`:

```text
workflow_ref=cbusillo/launchplane/.github/workflows/product-retirement.yml@refs/heads/main
job_workflow_ref=cbusillo/launchplane/.github/workflows/reusable-product-retirement.yml@c922d5f1a0bf3ab17a829196042a96ba89d7b693
```

## Detached Application Retirement

Phase one provides **Reusable Detached Application Retirement** as a
`workflow_call`-only worker. Phase two adds the thin
**Detached Application Retirement** `workflow_dispatch` wrapper, pinned to the
exact merged worker SHA below. The wrapper only defines the manual inputs,
forwards them unchanged, and grants `contents: read` plus `id-token: write`; it
does not define a runner, environment, steps, or concurrency. Do not configure
the live authz managed-set secret, deploy this branch, or attempt a provider
mutation as part of this code phase. The worker uses the protected
`launchplane-authz-admin` environment, OIDC, target-digest concurrency, exact
input validation, and redacted evidence.

The protected identity pair to configure only after this wrapper is merged and
deployed is:

```text
workflow_ref=cbusillo/launchplane/.github/workflows/detached-application-retirement.yml@refs/heads/main
job_workflow_ref=cbusillo/launchplane/.github/workflows/reusable-detached-application-retirement.yml@11d53d2840a6f1898785d7c8f1553c202caa3fbf
```

The future operator sequence is plan then apply. Inputs are exact Dokploy
project/environment/application names, the candidate target SHA-256, a sorted
non-empty JSON array of every other accessible application target SHA-256 whose
provider payload has the same exact project name, including duplicate physical
project IDs, a stable idempotency key, reason, and issue. Discovery is bounded
to ten search pages of at most 100 applications each and fails closed rather
than truncating. Apply additionally requires the
persisted plan record/digest and the exact target-bound confirmation. Never pass
or recover a raw target ID through workflow inputs, artifacts, summaries, issue
comments, or local CLI fallbacks.

Planning stops on ambiguous or malformed `/api/project.all` evidence, a
non-idle candidate, any domain, any recognized deployment history, digest/set
drift, or any Launchplane authority reference. A deployment-source block means
either the candidate provider target ID is present or the deployment record
lacks consistent application-typed target evidence and still names the
candidate. Provider-target records use the same target-ID binding. Do not
rewrite append-only deployment history or adopted target authority to bypass
either gate.
Apply repeats all proofs under a durable provider-operation lease before checkpointing the sole
`application.delete`. Reconciliation and already-absent retries reuse the same
apply idempotency key. Completion requires candidate absence, unchanged
protected fingerprints, and zero authority writes.

Managed authz routing is reserved through the
`detached-application-retirement` selector, managed-set ID
`operator.detached-application-retirement`, and secret name
`LAUNCHPLANE_AUTHZ_DETACHED_APPLICATION_RETIREMENT_MANAGED_SET_JSON`. Neither
phase creates or populates that secret or reconciles a live rule. The remaining
sequence is: merge and deploy this wrapper, configure the exact caller and
worker authz pair through the managed authz path, then run the reviewed plan and
apply sequence. Never substitute a mutable ref, a checked-in target value, or a
local CLI live-target fallback.
