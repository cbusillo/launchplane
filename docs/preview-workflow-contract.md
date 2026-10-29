---
title: Preview Workflow Compatibility Contract
---

Status: transitional compatibility only. [DIRECTION.md](../DIRECTION.md) retires
product repositories calling Launchplane, pinned reusable Launchplane workflows,
workflow-identity grants, and caller-grant rules. New products use
[event-driven deploys](event-driven-deploys.md) and the build handoff in
[artifact provenance](artifact-provenance.md).

## Existing Compatibility Boundary

Launchplane still contains old reusable preview workflows and caller-policy
readers. Their presence supports products not yet migrated; it is not an
onboarding recipe or permission to add, widen, or refresh a caller grant. Keep
existing callers unchanged until event-driven reconciliation is activated and
verified for that product, then retire the caller through the supported cleanup
and readback path. Do not delete a compatibility workflow merely because this
document no longer teaches product repositories to call it. Active backup,
promotion, rollback, runtime-health and cleanup gates remain until Launchplane
owns equivalent behavior and tests. The [onboarding cleanup runbook](operations.md#conventional-generic-web-onboarding)
owns the retained caller-rule retirement path.

The service remains responsible for preview records, provider mutations, runtime
settings and secrets, cleanup, feedback, and reconciliation. Product repositories
build immutable artifacts only. The canonical event and provenance documents own
the new-product contract rather than duplicating it here.

## Retained Preview Safety

Generic-web lifecycle cleanup uses the repository-name anchor from the product
profile for stored-preview lookup and terminal evidence; the product key need
not equal the repository name. Historical callers without a profile retain
their product-shaped anchor, with no fallback to unrelated records.
Provider application lookup validates the complete project/environment/application
inventory before proving absence or reusing a match. Missing or malformed nested
relations fail cleanup without deleting provider state or marking a preview
destroyed; a fully enumerated empty inventory remains valid absence.

After native generic-web provider teardown succeeds, the destroy route records
the matching preview as `destroyed` and clears its active and serving generation
links before returning success. A destroy for a preview that was never recorded
is a successful no-op with `destroy_outcome=no_preview_recorded` when no provider
resource evidence exists. Reusable feedback maps that outcome to `cleared`.
Provider teardown failure leaves durable preview state unchanged, and managed
failure comments claim that a preview may remain only when the feedback request
contains preview URL or image evidence or Launchplane finds an active preview
record with a canonical URL; otherwise the failure wording remains neutral about
resource existence. Driver-transport extensions retain ownership of their own
destroy transition. Because provider and record mutations cannot share a
transaction, retrying destroy after a record-write failure is the supported
convergence path; an already-absent provider application is treated as a
successful teardown so that retry can repair the record.

Preview refresh readiness should allow the serving runtime identity to converge
after a provider deploy trigger. A health response that still reports the prior
deployment/artifact/source identity for the same product, context, preview slug,
and environment kind remains a polling signal until the route timeout; a
different product, context, preview slug, or environment kind remains a hard
mismatch.

New preview settings accept only `data_transport_mode="none"` (ordinary preview
provisioning with the declared environment copying) or `"driver"` (executed
driver-owned preparation). Clone, bootstrap, migration/seed modes and generic
migration/seed command settings were never executed and are no longer inputs.
Stored profiles retain that unsupported intent in read-only
`preview.historical_transport`; readiness reports `unsupported_saved_intent`
and refresh blocks before provider mutation. To resume, replace the preview
settings through the product-profile API with supported configuration, omitting
the historical field. Storage reads preserve the original intent and commands;
they do not authorize or execute them. Ordinary readiness reports only the
prerequisites it checks. Driver preparation is proved by successful driver
execution evidence rather than a passing generic transport-policy check.
Profile API consumers must use a version compatible with this service contract;
older clients that forbid unknown fields cannot consume archived unsupported
intent. Ordinary profiles omit the empty historical field. Update strict profile
API clients alongside the service when deploying this contract change.

When the stored product profile declares `preview.data_transport_mode="driver"`,
the generic-web lifecycle route delegates refresh and destroy execution to the
registered shared capability. The `postgres` capability provides an isolated
database and role, profile-declared generated secrets and application lifecycle
commands for any generic-web product with a PostgreSQL testing template. Its
configuration and the supported legacy transition are described in
[driver development](driver-development.md). Missing driver
extensions fail closed instead of falling back to generic environment copying.
Driver-owned application commands must invoke executables already present in the
immutable image instead of relying on package-runner download or discovery
behavior. Dokploy schedule failures remain failed refreshes. Launchplane attaches
bounded redacted deployment output when the provider exposes it, and retries only
typed transient failures that occur before schedule execution; deterministic
remote exits and ambiguous post-trigger failures are not executed again.
After a driver-owned refresh passes its provisioning and health boundary, the
generic response records successful readiness and smoke evidence and persists the
same exact runtime identity injected into the serving application. Later product
verification may advance that generation from verifying to ready, but it must not
replace or fabricate the serving artifact or source revision.
Before driver-owned provisioning starts, Launchplane resolves the canonical
preview record ID and next generation ID from the DB-backed preview history.
DB-backed execution serializes refreshes by canonical preview ID, then injects
both values into the expected runtime identity, requires the live health endpoint
to echo them, and persists that observed identity on the same explicit generation.
Product-local preview slugs are not substitutes for Launchplane's canonical
preview and generation identities.

PostgreSQL refresh and destroy use the same product-operation serialization as
plain previews, rechecking the profile after acquiring it. A profile write cannot
change resource declarations during a product operation; retained previews keep
those declarations fixed between operations. This also applies to plain previews:
clean them up before changing resource names, source or template. Inert refresh
configuration failures preserve serving evidence. Destroy never creates a record
for an unrecorded preview; an already destroyed record is an idempotent success.
PostgreSQL destroy also accepts complete historical declarations for active or
retiring products, including disabled previews, while refresh still requires an
active canonical profile. Cleanup preserves the stored profile and retirement
hash fence; retired products cannot execute cleanup.
PostgreSQL environments use the same explicit template copy list, managed preview
configuration and credential-safety checks as plain previews, with additional
copied query-value checks limited to PostgreSQL. Generated database/application
declarations take precedence over managed values and retired entries; removing a
generator declaration is the supported way to change that ownership. Template connection identity and
credential query overrides fail before provider effects.

Because product callers pin reusable workflows while the service deploys
independently, add any future destroy outcome to the `reusable-preview-feedback-status.yml` allowlist
and roll caller pins forward before the deployed service starts emitting it.

## Route Handoff

This section preserves the Odoo preview data-source and recovery operations
contract that existing service code and operational documents still use. It does
not define a product-repository caller or grant.

Odoo preview builds and lifecycle reconciliation follow
[event-driven deploys](event-driven-deploys.md). The product publishes an
immutable artifact and Launchplane verifies its source-control provenance.
Launchplane's reconciler plans and applies previews without a product workflow
calling the service. Existing generic-web feedback wrappers remain for
products that have not completed their staged migration.

Launchplane owns isolated Odoo preview planning and smoke checks. The event reconciler supplies the verified manifest and PR facts, obtains a ready plan,
and applies it inside the service. Runtime bindings, provider targets and
per-preview data names come from Launchplane records. Existing preview discovery
must prove the target and hostname before cleanup. The reconciler publishes
readiness only after the preview's runtime identity and Odoo checks pass.
A passing
result persists the active preview, ready serving generation, immutable artifact
identity, and verified runtime identity. The
service requires an exact lowercase 40-character source commit and lowercase
64-character `sha256` image digest, injects its runtime identity into the preview
environment, and requires `/launchplane/health` to echo the matching identity;
event reconciliation cannot disable that verification.
Refresh explicitly sets `PLATFORM_INSTANCE=preview` in the runtime environment,
overriding any inherited instance name on both new and existing previews. The
devkit treats this as a public, non-production runtime: startup password and
`dbfilter` checks apply, outgoing mail is blocked, and restored production
credentials are cleared.
Refresh merges the artifact manifest's declared Odoo modules
with Launchplane-required modules into both `ODOO_INSTALL_MODULES` and the
explicit maintenance-only `ODOO_UPDATE_MODULES` input. It also merges the
managed Launchplane and Enterprise addon roots into `ODOO_ADDONS_PATH` so a
stale explicit runtime-environment value cannot override the compose default
and make required module dependencies unavailable. Refresh then deploys the
compose and runs the managed Odoo data-workflow schedule before any
smoke check can pass. An Odoo preview with `data_transport_mode="driver"` and
an upstream source initially runs the existing devkit restore, replacing a partial
unrecorded database before OpenUpgrade. Later refreshes preserve that copy through
maintenance only when a successful generation's completed original apply receipt
proves a requested restore completed on the same compose. The requested mode is
attached by the control plane, independently of addon output. Unknown history on an existing preview
refuses a reset. Retained-copy refreshes inspect current storage and never
bootstrap a missing database. No new database marker is needed.

The restore requires complete upstream settings, a non-root reader, and an explicit
dedicated SSH directory. Its source mount is root-only and read-only. The workflow
checks actual runner/web bind mounts before staging an identity: the runner must
have only the canonical source from Launchplane's resolved settings bound read-only at `/root/.ssh`, and web has no
host bind. Missing, changed or unreadable mounts stop before credential access.
The workflow must run without root, effective/permitted/ambient capabilities, root-directory
access or privilege escalation. The existing root broker stages the temporary
identity; it and upstream environment variables are removed after filestore
transfer, before addon code. The restricted source remains behind that root
boundary until a later maintenance refresh omits the mount. Qualification, cleanup
and restore programs run in isolated Python mode so PR files in the working
directory cannot execute through standard-library imports before that boundary.
These checks do not establish trust in an image's interpreter or installed
packages; [artifact provenance](artifact-provenance.md) remains a separate prerequisite.

The path forces bootstrap off and sanitization on, disables copied crons and
serves without cron threads. The existing SQL connection-isolation library runs
and verifies its committed changes at the devkit's pre-registry credential boundary,
before OpenUpgrade or addon code. The unsanitized recovery dump is removed before
that code runs and cleaned up on failure. No integration credentials are retained
from the source. Guarded schedule entry/failure cleanup removes retained capture
and temporary identity files; cleanup failure keeps web stopped.
Previews with `data_transport_mode="none"` use post-deploy maintenance without
host SSH mounts, even if their template carries upstream settings. A new source-free preview has
no database yet, and its web container
cannot create one, because it waits for the integration read-back and that
needs the database. So the preview schedule first asks the preview's own
Postgres whether the database exists. If it doesn't, the schedule runs the
devkit bootstrap instead of maintenance, which creates and installs it. The
bootstrap drops any existing database, so it runs only when that check says
there is none. When the check gets no answer, the schedule stops before it
touches web. Stable lanes never run this check. The schedule must prove that exactly one current web
container and script-runner container use the same artifact image, that an
explicit module list was configured, and that the install/update workflow
completed. Launchplane passes the resolved filestore path explicitly to the
workflow even when the live target relies on the compose default. Missing,
false, or unavailable schedule-log evidence fails the refresh. A terminal
provider deployment alone remains an unknown recovery outcome because it does
not prove that database-backed views were upgraded.

When the preview template instance has a deploy-enabled website-bootstrap
record, refresh also renders a preview-scoped copy of that website intent,
replaces its canonical URL with the exact preview URL, and passes it through the
same devkit application/readback path used by stable lanes. The copy excludes
stable config parameters, addon settings, and secret bindings, and never writes
the PR-specific canonical URL back to the stable record. The refresh fails when
the required website-bootstrap application and canonical readback markers are
missing or false.
Like resolved runtime-environment values and managed secrets, the bootstrap
contents come from current DB authority during apply; plan provenance binds the
preview route, target, and artifact facts rather than freezing mutable runtime
configuration values for the plan lifetime.

Ready Odoo apply-inputs responses also include the normalized `plan_request`
and `plan_provenance`: a service-derived plan id, canonical SHA-256 fingerprint,
issuance time, and 30-minute expiry. Launchplane persists that response under the
calling identity and requires the returned plan id as the `preview-apply`
`Idempotency-Key`; workflows must not derive a replacement apply key. Apply
loads the persisted result, requires the caller's dry-run plan and artifact to
match exactly, rejects missing, mismatched, or expired provenance, and rebuilds
the plan from current Launchplane records and current provider discovery before
the first provider effect. Any changed profile, runtime routing, template target,
environment id, discovered preview target, or set of blanked integration
credentials makes the plan stale and requires new apply inputs. Ready refresh
plans list in `omitted_integration_credential_keys` the template-lane
integration credentials the preview receives as empty values because no
runtime key-safety rule allows them on previews (key names only). Completed exact retries replay the stored apply response only
while its stored lifecycle evidence remains the current preview holder. A changed
product profile, missing legacy lifecycle evidence, newer serving generation, or
newer destroy returns a conflict and does not publish ready/destroyed feedback.
Uncertain operations reconcile against the originally issued plan instead of
replanning into another effect. Blocked apply-inputs responses have no apply
provenance and are not persisted as issued plans, so a later retry can
re-evaluate recovered dependencies.

Odoo destroy observation treats a missing provider lookup as inconclusive until
two complete, bounded compose inventories agree that neither the original ID
nor a replacement with its name exists, and the exact compose's domain list is
empty (or its parent is reported deleted). The deleted-parent case still
requires both inventories and inspection of every remaining compose's domains.
Conflicting, malformed, duplicate or changing inventory and remaining
domains (including the preview host attached to another compose) retain an unknown
outcome. This observation never dispatches a provider
write. It still requires the reservation's retained original issued intent;
legacy holds without it remain fenced and cannot be reconstructed from a new
plan, current preview history or a missing tracked target. Executor exclusion,
expired grace and original data-schedule quiescence remain separate recovery
requirements.

An interrupted clean-snapshot restore is replaced through those same service-issued
destroy and refresh operations. Its host retry does not drop the database or clear
private overlay writes. The reconciler observes the retained original and inspects
its exact physical volume bindings and pinned snapshot. Newer issued snapshots do
not replace that pin. Discovery makes one host check only when work is needed;
an already-serving preview does not inspect failed history. Only a native receipt that records
the start of this never-served restore returns the distinct disposal result.
Historical incomplete receipts without that proof remain held with a distinct
custody diagnostic: resolve use and needed-copy ownership, then use supported
preservation or qualified preview destroy. The native start
is recorded only after verifying the empty database and private upper, with web
held behind its gate; its writes can be reproduced from the bound snapshot/build.

Those two dispositions are completed inspections, carried in a closed completion
frame through the native transport and converted to the existing typed refusals.
They do not report successful preparation or authorize a restore. Generic failed
frames, incomplete streams and unknown dispositions remain unavailable evidence.
The missing-start disposition keeps the original reservation fenced.

For that qualified case, the reconciler issues the ordinary destroy plan/apply and
rechecks native custody under the execution lock. Current usage, needed-copy,
staff-testing and shared-resource protections are checked at every provider effect.
The existing expired lease, original schedule quiescence, target ownership and
resource inspection remain mandatory. An uncertain destroy is observed on its
original key until provider absence and the destroyed lifecycle are verified.
Only then does ordinary reconciliation rebuild an open PR on new storage.
An unattempted disposal refusal releases its selection so the next tick can plan
from current evidence; it does not retain an uncertain provider effect.
If the same verified build fails both before and after that reconstruction,
reconciliation leaves it failed until a new build or CI run attempt. The bound
issued times and successful service destroy receipt establish this limit; there
is no separate retry counter. Closed-PR cleanup remains available.
There is no new grant, caller assertion or provider-direct cleanup path.

Preparation pins the source before first restore and records that restore has
not started. Restore records its start before dispatching database work. Only
an unstarted allocation may continue preparation; an interrupted started restore
requires disposal. Historical incomplete receipts without start evidence remain
held. Completed receipts and their physical database witness continue to preserve
preview edits after a lost service completion reply.

Held-reservation classification distinguishes the original plan's
`pull_request_number` from `target_pull_request_number`, attributed only by an
exact recorded reconciliation-key and provider-key match against the current
profile's naming policy. The latter stops fresh planning for that target when
the original intent is unavailable; unrelated previews remain independent. It
does not recover the original action or plan, permit provider observation, or
settle the reservation. Original availability flags remain false.

## Client Review Handoff

When a ready preview belongs to a repository with an authoritative Client
requirement, the Launchplane-owned PR feedback comment is the canonical handoff.
It includes the public preview URL, immutable image and current revision, the
exact Launchplane Client-workbench deep link, the PR changes link, a concise test
plan, Accept and Request changes instructions, a staleness warning, and the next
Launchplane step. GitHub reviews and comments do not record Client acceptance.

If Client authority cannot be resolved or the workbench route is unavailable,
the comment fails closed: it tells reviewers not to merge and exposes no Client
action instructions. Repositories classified as not requiring Client acceptance
receive the ordinary ready-preview comment without an interactive Client handoff.

The reconciled pilot keeps the decision itself on a trusted Launchplane Client
surface, visually separate from untrusted preview content. Client acceptance is
bound to the complete accepted delivery evidence and is consumed by a separately
authorized Launchplane delivery job; it never authorizes merge, deploy, source,
configuration, secrets, or access administration. Changed or incomplete binding
evidence requires fresh review.

## Client Review Request

[Client review](owner-acceptance.md#current-client-review) owns review intent
from test notes, label overrides, engineering-only checks, invitations and
decision delivery. Its [carry rules](owner-acceptance.md#carried-acceptance) own
acceptance across unchanged-feature base updates.

Feedback comments use a repository-scoped Launchplane Delivery App token;
the [Client-review check](advisory-governance-checks.md#client-review-check)
uses the separate Advisory Checks App. Check delivery is best-effort and never
loses a saved decision or fails preview feedback. A later preview reconcile or
decision retries publication.

## Manager Preview Approval

Launchplane no longer posts the `manager-preview-approval` status or its managed
`/preview` command comment on pull requests. Preview refresh, verification,
destroy, Odoo preview apply, authorization-policy changes, and the signed GitHub
webhook leave the pull request alone; Client review is shown by
`launchplane/owner-review` (see Client Review Request). When Launchplane next
writes a ready preview comment, a leftover `manager-preview-approval` status on
the current head is closed once as `success` with
`Retired. Owner review is recorded in Launchplane.`, and a leftover
`launchplane/owner-acceptance` check run created by Launchplane's advisory app
is updated once to `neutral` with the title "Retired". Neither signal is created where it does not already exist.

Manager preview approval is retired. Its evaluator, webhook commands, reconcile
route, and store methods are deleted; existing manager events stay in the
database unread. Current approval uses product review and the release checklist.

The `tenant-admission` status is a separate Launchplane projection of the current
repository classification and candidate identity. It no longer evaluates manager,
waiver, or maintenance admission records. The controller separately requires
current technical checks before merging. Preview refresh, verification, destroy,
and cleanup do not wait for this status, and projection delivery failure never
rolls back a completed lifecycle mutation.

Generic-web routes write lifecycle records as part of refresh, destroy, and
verification. Odoo preview apply finalizes the serving preview and generation
evidence after the provider result is `pass` but before the durable provider
reservation releases its target fence. The completed provider response stores
those lifecycle record identities, so exact replay never synthesizes new
evidence under changed profile authority. The service uses the issued plan as the stable generation
identity and returns a non-passing conflict when a delayed refresh or destroy no
longer owns the preview.
