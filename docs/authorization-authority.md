---
title: Authorization Authority
---

# Authorization Authority

Launchplane's active PostgreSQL authorization-policy record is the live
application authority. GitHub identities, workflows, environments, repository
secrets, OIDC tokens, and checked-in files may authenticate callers or transport
reviewed requests, but they do not grant Launchplane permission by themselves.

Stored-policy startup retains the context-scoped meaning of
`live_target_runtime.plan` and `live_target_runtime.apply`. The service-restart
route uses those actions with an instance target; schema-v2 context-only rules
do not authorize that request. Instance rules still require explicit selectors.
Registering a new driver route must not retroactively change existing actions
into exclusively instance-scoped actions.

If an active stored policy fails model or digest validation at startup, the
service keeps an empty authorization runtime and logs a bounded validation
error. It neither seeds nor falls back to bootstrap grants, and does not rewrite
the stored record. `/v1/health` remains a liveness check; other `/v1/` requests
return `503 authz_policy_unavailable`. The existing PostgreSQL request refresh
continues validating the active record and can recover only after it reads a
valid policy. This keeps the process available for diagnosis without granting
access to an invalid policy or silently starting with bootstrap authority.

## Who Can Do What

- People sign in with GitHub and hold the `admin` or `read_only` role. Clients
  hold the narrow `owner` role for their own site's decisions.
- The [admin](#the-admin) may do anything.
- Machine writes hold DB policy grants except for proven projection sync. The
  Director's authenticated agents use the standing permission below; other agents
  retain product-scoped policy rules.
- Granting access is a stop boundary: an agent asks the Director before creating
  credentials, granting access, or changing who can merge (see
  [DIRECTION.md](../DIRECTION.md#stop-boundaries)). Do not create, edit,
  retarget, or dispatch a workflow, secret, or local helper to make a denied
  operation succeed. Fixing the refusing tool's rule through a reviewed source
  change, as [Denial Handling](#denial-handling) requires, is not such a
  workaround.

The [feedback continuation foundation](every-code-feedback-resume.md), which is
not continued and remains unwired in code, defines
`every_code_feedback_resume.request` and
`every_code_feedback_resume.execute` as separate exclusively instance-scoped
actions. Structural immutable-ID human matching and exact authenticated worker
matching produce policy provenance only; they create no grant, live route or
execution authority. The webhook-specific human predicate does not synthesize a
browser identity or expand browser role permissions. New live grants and worker
enablement remain a Director decision and need their rollout approvals.

`owner-control` channel-session, issued-challenge, and shadow-verification event
records are inert verification evidence, not authorization policy or grants.
They define no HTTP action, route, managed set, workflow, secret, or production
access path; every result persists `authority_state = 'inert'` and
`authorizes_execution = false`. Adding these records does not make a
self-asserted key, binding, or challenge authoritative.
Service-only challenge derivation may fail closed unless the enrolled admin's immutable
GitHub ID has exactly one ID-only managed rule for the descriptor's
existing approval action. That read does not define a new action, grant access,
or authorize approval or execution; rules that also depend on mutable login,
organization, team, or role selectors are intentionally insufficient without a
live authenticated human identity.

Administrator-enrollment records are likewise inert evidence only. They may
record a Director-created, 30-minute opaque challenge and a later server-derived
candidate GitHub identity that proved control, but they do not create an
admin, grant a policy action, change a managed set, or make any route
or workflow available. Every record is fixed to no authority. A future bridge
may compile a final enrolled record into a Director-gated DB-native policy change
only after separate design, review, apply, and read-back work, and installing it
is a Director decision. Landing enrollment storage does not approve or
implement that bridge.

Repository inventory follows the same authority boundary. Its
`repository_inventory.read` and `repository_inventory.write` actions use the
Launchplane service scope (`product=launchplane`, `context=launchplane`), but
defining those actions grants neither one. Active tracked inventory records are
repository-record evidence for the redacted repository-scope read model;
retired records contribute no active inventory membership. When authorization
rules are the only remaining active source, the read model reports stale
authorization membership rather than complete coverage.

## Standing Agent Read, Plan And Proven Projection Permission

The [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md#stop-boundaries)
owns who may read and plan. The service authenticates the existing Director
`local_operator` and `terminal_agent` bootstrap tokens and marks their resolved
identities internally. A policy principal or diagnostic request cannot supply
that marker. Diagnostics bind an exact subject, token label and transport to
the configured identity; another principal never inherits its authority.

[`action_effects.py`](../control_plane/action_effects.py) owns source declarations
of `read`, `plan`, `projection_sync` and `write`. These form three effect tiers:
read/inert plan, proven projection sync, and other writes. The Director's
authenticated agents inherit every declared read, inert plan and proven
projection sync directly, without a DB `agent.read` rule, a product
catalog or a policy approval. New products need no read-grant update. Unknown
actions fail closed; policy-schema/target validation and response redaction stay
in force. Pure dry runs can change only their own evidence. Queuing execution,
taking leases, creating operative lifecycle plans, revealing credentials and
copying secret values retain write or explicit source permissions. The two
secret-copy source checks explicitly disable read-role inheritance. Existing
terminal transport restrictions remain; `local_operator` supplies the full
planning surface, including mixed plan/apply routes.

Health-monitoring record apply also inherits the authenticated Director-agent
standing permission for the product's declared lanes, including testing and
production. It stays classified as
`write`, with an explicit standing declaration in the same action catalog. It
changes Launchplane's monitoring records, not the live site, credentials, access
or merge authority. Only this monitoring route admits the Director's terminal
transport to its existing browser/bearer mutation path; other terminal mutation
restrictions remain. Exact product/lane/check and private endpoint validation,
reviewed plan digest, idempotency, concurrent-profile checks and readback still
govern apply. Other machine identities retain their existing scoped policy.

The existing testing-retry route also admits the authenticated Director agent
through the existing `local_operator` bearer transport under the standing
permission. Terminal-agent credentials retain their read-only transport boundary.
This is a testing-only reconciler wakeup, not
inherited authority for `odoo_target_replacement_apply.execute`, whose mixed
production/testing scope keeps its policy checks. Other callers still require
that action on the exact testing lane. Mutation authentication and browser CSRF,
reviewed digests, original-key replay, generation and custody fences, staff
holds, active/unknown-operation refusal and the worker's safety checks remain
in force. See [testing retry](testing-deploy-retry.md).

The [same-artifact service restart](lane-service-restart.md) likewise admits the
authenticated Director `local_operator` on testing without inheriting the mixed
`live_target_runtime.apply` action. Fresh restarts require the existing current
disposable-lane qualification under the release reservation immediately before
the effect checkpoint, even with a policy grant. Staff-use, only-copy, resource
isolation and target-alias protections remain. Completed replay and the original
request's read-only reconciliation do not dispatch again and need no renewed
disposable qualification. Production, scoped callers and terminal transport
retain their existing authorization boundaries.

Generic-web deploy recovery dry runs authorize the instance-scoped
`generic_web_deploy_recovery.plan` action, with the existing execute grant as a
compatibility alternative. The authenticated Director agent inherits this inert
plan permission. Both the current lane and the stored original lane must pass
authorization; exact request, reference, reservation and target checks remain.
Apply still requires `generic_web_deploy.execute`, including production recovery.
An unknown provider outcome still holds the original fence; planning never
settles, retries or releases it.

Projection sync requires executable proof of zero provider writes and no access
or policy change. Every declared projection action must run its invariant proof
in `tests/test_projection_sync.py`; an action without that proof stays `write`.
Currently only `ingress_route.audit` qualifies. It uses the existing ingress
endpoint with `instance` and `mode=apply`, verifies lane-owned domains, an exact
existing host, an exclusive product context and an active DB-backed edge endpoint,
and completes only for an unchanged provider route. The audit reuses the existing
exact-instance no-op guard: creation, update and enable/disable are forced off
for every exact-instance apply. It records pending evidence before the provider
recheck. Drift refuses the request and leaves that pending audit; route-binding
reconciliation refuses unresolved latest evidence until a newer terminal audit
confirms the route. Shared-host comparison preserves sibling domains, while records and
responses contain only the requested lane's domains and redact provider config
for inherited standing permission. Domain-authority repair and route-binding
creation or reconciliation remain writes; an audit never performs them.

Other agents retain exact subject/token/product-scoped DB `agent.read` rules
and their existing context and instance matching. Client agents must be scoped
to their Client's products; source delivery installs no Client credential or
grant. Generic identities never become Director agents from a type name or
caller-supplied claim. Product-level metadata uses the `launchplane` context;
separate context and instance rules support both scopes. Requests authorize
the returned product and collections filter each returned record. Revocation
continues to apply to these scoped rules and to all explicit write permissions.

Expected-configuration dry runs also preserve existing explicit grants: a
`local_operator` granted `product_profile.expected_config.apply` may inspect the inert
plan for a live product without changing its profile. The corresponding apply
still refuses with `live_product_requires_operator`; browser CSRF and read-only
identity restrictions remain in force.

Product-config likewise lets the authenticated Director agent plan undeclared
non-secret settings on a Live product, including testing, production and context
scope. The plan saves only review evidence. An owned testing lane is non-live
even when its product is Live: existing product-config write authority may record
its plain settings and provider-key retirements, or adopt plain settings from
that lane's provider environment, after a matching dry run. Adoption reads stay
inside the service and return only key dispositions. Credential-shaped keys
remain refused; a request containing one cannot apply or create secret records.
These operations record configuration without applying it to a provider. Production and
context-wide undeclared settings still require admin authority and return
`live_product_requires_operator` for the `local_operator` credential, even after
a successful matching plan. Exact lane ownership, instance-only secret writes,
credential safety, and scoped-agent grants remain enforced.
An instance-scoped provider-key adoption dry run can read that lane's provider
environment inside the service, including production. It returns key dispositions
without values, preserves credential refusals, and records no adopted settings.

For scoped agents, product reads authorize the returned product. Profile and product overview lists filter role access
per returned product; contextual secret and runtime-setting collections omit
global or other-product records unless their scopes are also granted; saved Client reviews resolve the repository's product.
Dokploy dry runs use the recorded product owning the context, and ingress plans validate the
product lane's domains and remove provider configuration fields that can contain
credentials. Service metadata and global policy history require their own
Launchplane product scope. That scope never stands for another product.
Declarations distinguish service-owned metadata from aggregates containing
product records; aggregate reads require each recorded product scope, including
historical product projections without a current profile. Caller-supplied intent
evaluations, including denied requests, do not establish product scopes. Snapshot authorization
checks the actual exported profiles and inventory inside the same read-only
transaction. The Delivery selector GET uses the declared metadata read;
its configuration and credential retirement keep their admin gates.

Non-agent policy-diagnostic identity gates remain. For scoped agents,
repository-scope lookups require both the Launchplane metadata scope and
coverage of recorded products; they include Root-owned repository inventory,
classification, work-request and policy membership metadata. Global effective-access,
candidate-policy diagnostics and policy-retirement evidence require the recorded
product scopes they inspect. Service-only metadata at the root context requires
the Launchplane product scope; supplying another product cannot borrow it.
Denial explanations additionally require the recorded denial's product and context.
Workflow retirement probes also require the products in matching policy rules,
including products not yet recorded; unbounded selectors require explicit authority.
Snapshot inventory for Launchplane itself uses its own scope; ambiguous
product context ownership fails closed.

Installation activates the Director's declaration-based reads; no managed-policy
proposal or grant step follows it. Verify the installed revision with the same
agent's real dry run, a refused write and a foreign-product scoped-agent probe.
Existing DB grants are preserved for image rollback; an older image may still
refuse a Director read without a matching rule. Report that as a tool defect.

## Disposable-lane engineering

The authenticated Director-agent marker also identifies ordinary disposable-lane
work under the overall direction. `POST /v1/live-target-runtime/apply` retains
the existing context-scoped policy check for existing callers, including
production and Client scopes; an instance rule does not acquire new rights.
The separate Director path validates an exact testing or uniquely owned preview
lane. Director testing/preview applies need current safety even with a broad
context grant; a grant cannot establish that a lane is unused or data is disposable.
`deploy=false` may stage an independently reviewed separation without starting
existing shared-storage containers. Staging eligibility never authorizes deploy.
Preview-inclusive product retirement accepts that identity only for never-live
products and testing; Client and other machine callers keep their existing rules.

These paths consume [recorded disposable-lane facts](disposable-lanes.md). They
are generic source behavior, not a new policy grant, administrator identity,
credential, promotion right or Client-access expansion. Existing runtime key
safety, transport isolation, reviewed retirement, provider fences and backup
protection still apply. Paths requiring positive recorded facts return
`disposable_lane_safety_unknown` when those facts are missing. Existing Odoo
preview refresh instead uses the direct resource inspection described below.
The assertion API cannot produce positive facts while the authoritative
collector is unavailable. Fixing that evidence producer is engineering;
never ask for a grant to bypass it. Bounded empty testing replacement staging
also follows this boundary; it creates only new placement and a new empty
compose, leaving retained target/storage and historical effects untouched.

### First testing target creation

The authenticated Director agent may stage a first testing target without a
policy grant through the existing Dokploy target setup dry-run/apply endpoint.
This path creates only a new project, a new environment and an empty raw compose
for an active product's exclusively owned context. It accepts no existing
project, environment or target IDs, replacement reference, repository source,
domain route or published port. The testing lane and all three target authority
records must be absent. Production ownership on the same product does not block
this new-resource staging and acquires no new permission.

The service checks eligibility before each provider effect and provider read,
and retains atomic ownership/profile comparison when committing authority.
Completed same-key replay does not allocate again. Source completion, lane
registration, credential delivery, deployment and health qualification remain
separate supported operations; staging an empty target proves none of them.
An existing lane needs its own supported qualification/rebuild path, including
current usage, data and isolation evidence. The compose shape cannot reset or
replace it.

The same path also accepts `create-application`, the conventional testing-lane
shape: a new project, a new environment and an empty application on a named
server, with no domain route. It may replace the lane's current testing target
only with positive evidence that nobody used the lane: the setup route created
that target empty (not adopted, not source-completed), no deploy passed there
and no artifact was ever current. The target must carry an `isolation_staging`
testing hold, which stops event deploys; a `staff_testing` hold refuses. Setup
holds the target's event-deploy execution lock through its provider effects and
authority commit, so a running deploy refuses it and a queued one waits. The
lane's policies, including that hold, move to the new target; lift the hold
through the testing-hold path when the lane should deploy. The request names
the exact target it replaces in `expected_current_provider_target`; a
different, partial or missing target refuses. The replaced provider resource is
left in place for a supported retirement. Image delivery and runtime settings
then follow the ordinary event-driven testing deploy.

The optional `state_volume_path` uses that same first-testing application
boundary. It accepts only a new service-named volume on the new application,
never a selected existing volume, mount, host path or production target. A saved
service plan binds the caller, target intent, placement and current lane
authority. Durable original-key execution retains partial effects and blocks
duplicate creates; readback is observational. See the
[storage request and recovery contract](dokploy-service-deployments.md#testing-application-state-volume).

Other agent identities retain their scoped policy rules. No grant or credential
is installed, and general setup, production, backup and promotion authority do
not change.

### Testing-copy connection isolation

The Odoo post-deploy endpoint classifies validated `connection_isolation`
requests separately from general maintenance. Its instance-scoped plan and
read actions inherit the Director's standing observation permission; a scoped
`agent.read` rule alone does not execute its provider schedule. Isolation apply remains
a write. Only the authenticated Director identity can use the source-owned
testing repair without a policy grant, after operation-specific qualification.
The fixed repair preserves application records without asserting that a copy is
unused or reproducible. Production, staff holds, recorded protected use or
only-copy data, target aliases, shared resources and unverified actual testing
resources refuse. No new policy record or credential is installed. General post-deploy
authority does not authorize isolation apply. See the
[repair procedure](operations.md#repair-copied-carrier-and-customer-sms-connections)
for reviewed plan binding, stable idempotency, independent readback and current
evidence-producer limitations.

### Reviewed never-live classification and preview execution

The existing Director-agent identity can record a reviewed `unknown` to
`prelaunch` classification through the production-use route for an active
product whose recorded stable lanes are testing only. A live classification,
production lane, historical context, production deployment or promotion history
refuses this path. The full reviewed profile digest, atomic profile comparison,
idempotency and audit receipt remain. Release-acceptance settings cannot change
through this path. The request's reason records the reviewed product facts; it
cannot assert resource isolation or remove a usage hold. The metadata correction
satisfies retirement's never-live prerequisite; retirement still performs its
resource, ownership and protected-use checks. It creates no general provider,
backup, release or promotion authority. The profile persists
`production_use_authority=director_agent`: this bounded attestation cannot
supply the existing prelaunch production-bootstrap exemption, even if a
production lane is later added. Retirement with this provenance still requires
testing-only ownership. Missing history alone is not evidence that a product
never went live; the Director's reviewed fact is the input, and contradictory
records refuse it. An already explicitly authorized prelaunch classification
keeps its provenance. The existing `product_profile.write` review/apply path can
explicitly reconfirm a classification for production bootstrap; this source path
cannot do so. Prelaunch and held products still cannot start an
acceptance-triggered release.

Existing full-profile writes cannot change classification or its provenance;
they point to that reviewed endpoint. Changing only release-acceptance settings
preserves provenance. Explicit re-confirmation of an agent attestation returns
the before/after bootstrap-exemption effect in the reviewed plan, and the
Client-settings panel displays it before Apply. Ordinary bounded agent
classification keeps its existing response shape and supplies no exemption.

Odoo preview apply uses the same source-owned Director identity boundary as
[testing-copy isolation](#testing-copy-connection-isolation). It remains a write,
including preview destruction. It requires the caller's exact service-issued
plan and an existing uniquely owned preview. Stable lanes cannot be substituted
for a preview. The frozen plan must name the discovered compose and cannot alias
a recorded stable target. Completed same-key replay and read-only outcome
reconciliation remain available after qualification expires or lifecycle changes.
Scoped callers retain their explicit policy rules and inherit no Director authority.
Source-read permission for an import credential remains separate from execution.

Existing **Odoo preview refresh** follows overall direction's disposable-preview
rule rather than requiring positive `unused` or `reproducible` observations that
no automatic preview collector produces. This does not publish either fact.
Scheduled closed-preview cleanup uses the existing disposable observation API:
the authenticated Director agent records its current facts for the exact owned
preview, with evidence and binding checks. These observations qualify cleanup
only; they cannot admit runtime sync or another preview mutation. Unknown facts hold cleanup. Testing
observations retain their separate service-collector requirement. Same-key
observation and lifecycle finalization remain available after evidence expires.
An active, unique service
preview is required. A staff hold or recorded `in_use`, `only_copy` or shared
resource report refuses refresh even after that report expires or its binding
changes. Resolve the real exception first through the existing target-policy or
`/v1/disposable-lanes/observations` clear path; expiry never clears it. Positive reports confer no admission on this path and are not required.
Destroy and stable testing retain their existing qualification requirements.

Before changing an existing preview compose and again before deployment, the
service runs a fixed read-only Docker inspector on that compose's actual server
through its manual-only, disabled data schedule. Its spec contains only target,
project and resource identifiers. It requires all three current/proposed volume
names to agree, actual local volumes without external backing options, distinct
Docker-reported backing directories, and complete container mount inventory (including
stopped containers). Foreign consumers, writable parent mounts, unknown backing,
host networking or published ports refuse. Read-only administrative parent mounts
are allowed; exact/child consumers are protected. Existing data is inspected,
never emptied or deleted. The qualified Director refresh generates a compose with no host ports or host
SSH mounts. Existing scoped/source-reconciler SSH behavior is unchanged. Managed integration-key filtering and exact managed-source authority
remain in force; no template source allowance is inherited.

The schedule receipt binds the target/project/volumes, is consumed immediately,
and is invalidated by a changed provider target readback. It is not a reusable
positive usage/custody assertion. A new apply repeats collection automatically;
no manual DB qualification or grant is required. Missing/failed/stale/conflicting
inspection refuses before new application effects. The same operation/key observes
an uncertain effect without redispatch; preserve it and use normal reconciliation.
A refused or expired issued plan requires fresh plan/apply inputs.

The inspector reads Docker's daemon metadata, not filesystem paths in the schedule's
mount namespace. Only default local volumes without backing options qualify;
image-declared anonymous volumes are inspected and checked for foreign consumers
too. Docker's anonymous-volume marker must identify their provenance, including
when Compose reuses them during recreation; a hash-like name alone is insufficient.
Foreign volumes' backing device paths and bind sources are inspected for aliases,
including parent directories and chains through unmounted local volumes. Missing
intermediate metadata refuses qualification. Bulk, filtered
container reads collect no environment values or arbitrary labels. Unrelated
vanished containers and stale disjoint bind sources are not usage.
There is no inode or arbitrary host-symlink attestation. Managed resource identities
and Docker's reported backing/mount relationships are the inspection authority.

PostgreSQL excludes managed configuration, first global/instance settings, aliases,
secret metadata, bootstrap overrides, key policy, safety-evidence and preview-record
writers for each bounded provider dispatch. It acquires after the executor, with a
five-second lock wait. Deploy/job polling and smoke checks hold no authority fence.
Guarded schedule triggers have a short request deadline and then use the existing
exact-job observation without retriggering. Profile/configuration equality and
protective reports are checked inside each dispatch guard and before lifecycle
publication; a late refusal retains uncertain outcomes. Preview generation progress
and housekeeping timestamps are left to existing supersession/finalization logic;
preview identity/state, configuration and real protective reports stay bound. Direct external host
administration is outside this transaction.

Inspection commands time out and the fixed script exits within its own deadline.
If the base inspection job remains running, collection uses a separately named,
disabled inspection schedule, reused while that stale job persists. It preserves
the old job for diagnosis.
It neither cancels that job nor changes an application. An inspection-only refusal
releases the pre-effect reservation. It never permits a second application dispatch to settle uncertainty.
SQLite remains a rehearsal backend and cannot enable this live qualification.
No production, backup, promotion or Client-access boundary changes.

## Current Transitional Model

The current service has a DB-backed managed-rule reconciliation endpoint with
compare-and-swap, idempotency, reviewed-plan digests, and redacted evidence.
Managed sets are maintained through the DB-native Access policy workbench and
Director-approved privileged-operation proposals. The GitHub-secret reconcile
wrapper and reusable worker are deleted; their removal does not revoke stored
rules or delete repository secrets. The bootstrap installer is deleted with
them. Use [agent proposal access](#preparing-agent-proposal-access) for its
DB-native successor; installing that access still requires Director approval.

Reconciliation planning accepts schema-v2 and schema-v3 desired managed sets.
It resolves explicit v1-to-v2, v2-to-v3, and same-schema transitions,
preserves unrelated managed sets across all six policy collections, and reports
ordinary-agent rules as structural policy content. Planning is read-only.
Raw schema-v3 seeds, unbound replacements, deletion and downgrade remain fenced.
The separately mediated A5 transition described below requires exact reviewed
operation and activation evidence under the storage locks; a planned candidate
or compiled handler alone cannot enable a rule. Exact removal and unrelated
schema-v3 maintenance preserve the documented post-stop boundary.

An empty schema-v2 desired fragment can also contract one exact managed set
against an active schema-v3 policy when `schema_migration` and
`unmanaged_adoption` are both `reject` and no administrator-quorum change is
requested. The candidate keeps the active schema, quorum and every unrelated
rule, including ordinary-agent rules. This is a set removal, not a policy
downgrade. Nonempty incompatible fragments still return HTTP 409
`authz_policy_schema_conflict` with the deterministic schema incompatibility;
refreshing and retrying the same request does not resolve it.

The approved `operator.manager-preview-approval` contraction and independent
empty-set readback are recorded on #2006. Its dedicated
`manager-preview-approval-retirement` wrapper selection is removed after that
operation; it is no longer a dispatch path.

A PR that retires a caller links the matching grant-removal proposal in the
same change, prepared with `privileged-policy-propose` through the
[Launchplane helper](privileged-operations.md#agent-policy-proposals); if that
path is unavailable, record the exact blocker on the owning issue for the
Director while continuing independently authorized source cleanup.

## The Admin

One kind of rule already carries the power to change every grant: a
`github_humans` rule named by immutable GitHub id with the `admin` role and
`authz_policy_grant.write` on `launchplane`/`launchplane`, with no login,
organization, team, or instance selector. A signed-in person matched by such a
rule is the admin, and the runtime allows them every action.
Enumerating actions for that person added ceremony without adding protection,
because they could already approve any change to their own grants.

This is the same predicate that gates every policy write, so there is one
definition of administrator. It does not widen anything else:

- a rule that merely carries the `admin` role (for example the product-evidence
  read set, which lists one read action) stays exactly as narrow as it is
  written;
- `read_only` people, Clients, and every machine identity (workflows,
  agents, tokens) are still limited to their enumerated grants;
- the schema-version and instance-scope checks still apply to the administrator;
- approval and worker reauthorization of privileged operations still require the
  exact managed rule and never consult this shortcut.

## Denial Handling

Treat `authorization_denied` as an authority result, not a credential-selection
hint. Record the denied action, scope, and trace ID, then:

- **A refused Director-agent read, plan or inert dry run** is a tool defect.
  Diagnose authentication, the source effect and the installed revision, then
  fix the tool under the overall direction. Do not prepare a grant or ask for
  read-role approval. Continue independent work while installation is pending.
- **A refused Director-agent write that hits no stop boundary** is also a tool
  defect, fixed at the blocked work's priority, not a new operation or a
  question (see [DIRECTION.md](../DIRECTION.md#stop-boundaries)). Fix the tool
  so that kind of refusal cannot happen again, and never ask the Director for a
  grant to get around it. A refusal that rightly protects production, a backup,
  a live site, Client data or merge safety is met, never removed.
- **A refused grant or access change, or a write that hits a stop boundary**,
  is a stop. Ask the Director once for that specific change and continue with
  work that does not depend on it.

Do not work around a denial by borrowing another identity, adding a workflow or
secret, running a direct database command, or calling the provider. Manual route
probing, wildcard grants, temporary CI authority, and copied policy payloads are
not diagnostic substitutes.

## Target Model

The delegated-session target in
[issue #2240](https://github.com/cbusillo/launchplane/issues/2240) was closed as
not planned; the ordinary-agent delegated-delivery design it served is retired
(see [DIRECTION.md](../DIRECTION.md#retired)). Client acceptance remains
product-decision evidence consumed by an independently
authorized delivery request and never grants merge, deploy, configuration,
secret, or policy-administration power.

The currently implemented action-by-action policy evaluation, diagnostic
routes, activation bridges, and recovery mechanisms recorded in this document
remain the runtime contract. The administration surface described immediately below
is itself the target model. This target text grants no capability.

The DB-native administration surface must support authenticated administrators
through Launchplane's API and UI:

- inspect active policy, managed sets, principals, and effective access;
- explain denials without requiring policy-write permission;
- propose, dry-run, review, apply, revoke, and roll back exact changes;
- preserve the applying administrator and at least one reachable strict
  immutable-ID GitHub-human administrator without claiming total-lockout
  recovery;
- carry an explicit administrator quorum in schema-v2 policy state; records
  without the field retain legacy quorum `2`, while quorum `1` is an explicit
  reviewed policy change and is reported as solo administration;
- prevent final-admin lockout and reject known readiness blockers;
- retain immutable identity, least-privilege scope, revision, digest,
  idempotency, and audit evidence;
- export and restore policy without making GitHub the durable desired-state
  store.

Quorum-one recovery is a separate browser-human bridge. It requires a fresh
GitHub session created within five minutes, exact current strict immutable-ID
administrator authority from the active DB policy, a reviewed dry-run digest,
the exact warning acknowledgement, a future apply idempotency key, and a
candidate with exactly one strict human administrator and no safety or
operational-readiness blockers. The service returns a random one-time secret
only at issuance, stores only its SHA-256 digest, and records an immutable
issued event followed by exactly one immutable consumed, revoked, or expired
event. The apply route binds the confirmation to the active policy record,
candidate, plan, session digest, GitHub ID, idempotency key, and secret inside
the same serialized transaction as policy CAS and idempotency completion.

Issue `#2277` recovery uses that inert evidence model through separate hidden,
browser-human-only, code-defined candidate routes. They accept a closed
candidate ID rather than a policy body, recompile the candidate against the
single active record for dry-run, confirmation issuance, and apply, and reject
the apply when that fresh plan no longer matches the observed generation/digest.
Every candidate whose resulting quorum is one requires a fresh five-minute
session, the recovery-specific exact acknowledgement, a future candidate-scoped
idempotency key, and an exact-digest confirmation consumed atomically with CAS.
The only recovery sequence is: remove an exact active activation set that has no
consumed confirmation bound to its active digest; create and confirm a fresh
activation; then retire overlapping temporary bootstrap actions. The service
never backfills or forges evidence for an already-active historical revision.
Run those policy writes as one uninterrupted recovery window: any intervening
authorization-policy revision invalidates the active-digest confirmation
backing and must stop the cutover for a new reviewed recovery change.

The first DB-native read-only slice keeps those capabilities separate:

- `authz_policy_effective_access.read` is restricted to an authenticated
  GitHub administrator or local administrator and evaluates exactly one supplied
  principal, action, product, context, explicit context-or-instance target scope,
  and optional exact instance against the single active DB policy record;
- `authz_denial_explanation.read` is independently grantable to a human support
  reader and returns one redacted denial record by trace ID;
- effective-access responses expose only the supplied scope, decision, bounded
  reason code, and active policy record identity; they never expose matching
  rules, selector values, managed-set topology, tokens, or other principals;
- denial records contain no principal identifier or token label, expire after
  30 days, and return the same not-found response when absent or expired.

The next read-only administration slice adds `authz_policy_health.read` for an
authenticated GitHub administrator or local administrator. It reads the exact
active DB policy record and returns only immutable policy provenance, bounded
health reason codes, managed-set rule counts, and admin rule
counts. Managed summaries may identify a managed set but never expose managed
rule IDs, rule hashes, selectors, actions, repositories, workflows, logins,
GitHub IDs, subjects, token labels, or raw policy payloads. "Reachable
administrator" means a rule satisfies the existing policy-administration safety
predicate; it does not prove that an external credential or identity provider
is currently available.

The health read checks the caller against both the current runtime policy and
the freshly loaded active DB record, rejects non-administrator principal types,
and fails closed when active policy state is missing or ambiguous. It performs
no policy, provider, runtime, bootstrap, secret, or deployment mutation. Landing
the action and route does not grant production access to them.

The bounded policy-administration read slice adds the independently grantable
`authz_policy_administration.read` action and two backend-only routes:
`GET /v1/authz-policies/administration` and `GET
/v1/authz-policies/revisions`. Both routes are restricted to authenticated
GitHub administrators or local administrators, require the action in both the
current runtime policy and the freshly loaded single active DB policy record,
and fail closed for missing, ambiguous, or non-database policy authority. The
action is defined but deliberately ungranted; landing these routes gives no real
caller access.

The administration response contains only policy-record provenance, principal
rule counts, and the existing bounded health, managed-set, and reachable-
administrator summaries. The revision response is newest-first, returns at most
50 records, and reports truncation from one additional bounded read. Revision
audit data is reduced to presence, a canonical SHA-256 digest, normalized
operation and mode enums, and allowlisted nonnegative numeric counts. Neither
route returns raw policy or audit payloads, principal identifiers, selectors,
managed rule IDs or hashes, reasons, key IDs, tokens, or free text.

Browser administrators must provide strict same-origin `Sec-Fetch-*` metadata
and CSRF proof. Same-origin browser GET requests normally omit `Origin`, so this
sensitive-read validator accepts zero or one `Origin` value, validates it
against the configured public origin when present, and always rejects duplicate
or cross-origin values. It still requires `Sec-Fetch-Site: same-origin`, a
`cors` or `same-origin` mode, `Sec-Fetch-Dest: empty`, and one valid CSRF token.
The stricter mutation validator continues to require exactly one matching
`Origin` for POST requests. Sensitive-read validation neither renews the session
nor rotates the CSRF token. Successes and every error class are
`Cache-Control: no-store`,
and the routes do not write denial, session, policy, idempotency, audit, outbox,
provider, runtime, deployment, secret, or other persistent state. This slice
adds no proposal, export, rollback, mutation, workflow, UI, or authorization
grant.

The activation preflight is a separate, read-only self-check at
`GET /v1/authz-diagnostics/activation-preflight/self`. It accepts only the
signed Launchplane browser-session cookie and rejects every Authorization
header. The route accepts no body or query parameters, uses the immutable
GitHub ID and current claims already stored in the session, ignores the
persisted session role, re-derives the role from the single active DB policy,
and evaluates the fixed global
`authz_policy_grant.write`/`launchplane`/`launchplane` request through the
ordinary evaluator. Missing, invalid, expired, or claims-stale sessions return
`401`; missing or ambiguous active policy state fails closed.

The response contains only the allowed/denied decision, an hour-bounded UTC
evaluation time, an opaque keyed purpose-separated policy-generation digest, and a
trace ID. It never returns policy identity, policy rules, session or human
identity, selectors, memberships, managed IDs, reason codes, permission lists,
or action inventory. The route and all of its errors are
`Cache-Control: no-store`; it performs no session renewal, CSRF rotation,
denial, audit, idempotency, outbox, policy, operation, provider, runtime, or
secret write. No additional diagnostic grant or credential is required.

Issue `#2277` adds one narrow browser-human activation bridge for the compiled
privileged-policy operation managed set. The bridge has separate dry-run and
apply POST routes, accepts only a bounded reason, the reviewed dry-run digest on
apply, and an `Idempotency-Key`, and derives the immutable GitHub ID exclusively
from the authenticated Launchplane session. Both routes require strict
same-origin fetch metadata and a single-use CSRF token. Bearer, workflow,
terminal-agent, `local_operators`, and local-admin identities fail before policy
evaluation.

The bridge does not trust the session role or mutable login, organization, or
team selectors. It reloads the single active DB policy and requires an exact
immutable-ID human administrator rule with explicit `admin` role, literal
`authz_policy_grant.write`, and exact `launchplane` product/context scope. Its
code-compiled managed set contains one GitHub-human rule for that same immutable
ID and exactly `authz_policy_operation.propose`,
`authz_policy_operation.read`, `authz_policy_operation.approve`,
`authz_policy_operation.revoke`, and `authz_policy_operation.cancel`. It cannot
create workflow, terminal-agent, `local_operators`, local-admin, wildcard,
provider, deployment, or unrelated authority.

The companion recovery routes remain hidden from public OpenAPI like the
activation bridge. They are not a policy editor: the closed candidates can only
reset an unconfirmed exact activation, create a fresh exact activation, or
retire the temporary bootstrap set after that fresh activation has consumed
confirmation backing. Bootstrap retirement removes its terminal-agent proposal
rule and removes all overlapping `authz_policy_operation.*` actions while
preserving any non-overlapping action on the closed human bridge rule. It fails
closed on any other bootstrap shape and verifies that each of the five actions
then has exactly one match from `operator.privileged-policy-operation`.

An immutable-ID administration denial remains ordinary redacted authorization
evidence: the service records its trace, fixed action and scope, reason category,
and active-policy provenance. The route response and every error remain
`Cache-Control: no-store`; no principal selector or raw policy is persisted in
the denial record.

Dry-run binds the observed active record ID, revision, policy digest, candidate
revision and digest, desired-set digest, exact action set, applying-admin
continuity, effective administrator quorum, and strict human administrator evidence. Apply
repeats that
compiled request with the reviewed digest, non-empty reason, immutable-ID-scoped
idempotency, active-policy compare-and-swap, and exact record/revision/digest and
policy read-back. The written record uses the distinct
`service:authz-policy-operation-activation` source. Once the exact managed set
is active, both activation routes return the terminal
`authz_policy_operation_activation_retired` result; an occupied but non-exact
set fails as a conflict. This state-derived retirement is not total-lockout
recovery, a recurring break-glass path, or admin-configured authority. The
routes remain hidden from the general OpenAPI surface so the bridge can be
deleted after production activation evidence is preserved.

The next read-only administration slice adds
`authz_policy_candidate_preview.read` for an authenticated GitHub administrator
or local administrator. It accepts one complete schema-v2 candidate policy and
at most 25 explicit effective-access probes, validates every managed set through
the existing reconciliation contract, and compares the candidate with the exact
single active DB policy record. The response binds to the active record ID,
revision, and digest and contains only submitted and canonical evaluated-
candidate digests, a normalization flag, bounded health and
administrator counts, count/category-only structural changes, operational-
readiness reason categories, and old/new probe decisions from the ordinary
effective-access evaluator.

The preview permission includes the bounded active-policy health and reachable-
administrator summaries returned in the comparison; callers do not also need
`authz_policy_health.read`. Both permissions remain restricted to authenticated
administrators, and neither implies policy-write authority.

Candidate preview responses never return raw policy, active or candidate rule
IDs, rule hashes, selectors, repositories, workflows, actions, principal
identifiers, token labels, secrets, or managed-set topology. Probe evaluation
disables request-local denial recording so caller-supplied identities cannot
contaminate support-readable denial evidence. Browser administrators use
same-origin and CSRF verification without session renewal or token rotation;
the preview performs no policy, session, denial-evidence, idempotency, outbox,
provider, runtime, secret, durable-operation, or other persistence write. The
preview does not produce an apply digest, does not prove future applying-
administrator continuity, and does not authorize any production grant.

The bounded repository-scope slice adds `authz_repository_scope.read` as a
separate human-reader permission. `POST
/v1/authz-diagnostics/repository-scope/read` accepts at most 100 exact
caller-known repository candidates so an authorization audit can reconcile its
GitHub/planning evidence with DB-backed Launchplane scope without granting the
broad active-policy or work-graph reads. The permission is available only to
authenticated GitHub humans, `local_operators`, and local administrators;
GitHub Actions and terminal-agent identities remain ineligible even when a rule
mentions the action.

The route derives current membership from active product profiles, current
repository role/classification records, nonterminal Every Code work-request
records, and exact GitHub Actions repository membership in the single active DB
authorization policy. It does not evaluate or return actions, principals,
selectors, rule identities, workflow identities, or policy payloads. Every
repository identity is redacted. Candidate results are referenced only by input
position and return a purpose-separated opaque handle plus source-membership
categories when matched. Handles remain stable within one opaque handle
generation; canonical managed-secret key-ring rotation changes both the handles
and the non-secret opaque generation marker so evidence cannot silently compare
across generations. Legacy passphrase-only managed-secret configuration is not
accepted for this public identifier derivation and fails closed.

This permission is a bounded repository-existence oracle and must not be granted
broadly. Unmatched DB entries appear only as counts, never as handles or names.
Each source query retains at most 1,000 records; any truncation is an explicit
partial-coverage gap rather than silent omission.
Any unmatched DB entry, submitted candidate missing from DB scope, conflicting
identity evidence, case variant that would not match the exact authorization
evaluator, malformed timestamp, missing immutable identity evidence, or
expired active work-request lease, or stale-only authorization membership produces
`coverage.state=partial` with bounded count/reason gaps. Partial coverage is a
fail-closed audit result. Storage
or active-policy absence still fails hard; multiple active policies fail as
ambiguous. Landing this route grants no production access and authorizes no
policy, workflow, secret, provider, runtime, deployment, or durable-operation
change.

For host-local audit recovery when the admin does not hold the HTTP action,
`launchplane authz-policies repository-scope-evidence` accepts the same bounded
exact-candidate request JSON and reads the same redacted response directly from
the configured PostgreSQL record store. This command derives evidence from the
admin's DB credentials; it does not evaluate the admin against the active
policy and is not proof that the admin is policy-authorized. It requires
exactly one active policy record, fails closed on missing or ambiguous active
state, and performs no policy, secret, workflow, provider, runtime, deployment,
session, denial, idempotency, outbox, or durable-operation write.

Landing these read contracts does not authorize their production grants.
Production policy changes are a Director decision made through the supported
DB-native administration route; total-lockout recovery remains explicitly
deferred.

The ordinary-agent delegated-delivery pilot is retired (see
[DIRECTION.md](../DIRECTION.md#retired)); issue
[#2437](https://github.com/cbusillo/launchplane/issues/2437) deletes its code.
Do not stage or install its `operator.ordinary-agent-delivery-administration`
managed set.

After parity and administration gates pass, protected desired-set secrets and
routine authorization workflows must be retired. GitHub may remain an identity
provider and transport for already-authorized workloads. No total-lockout
bootstrap or break-glass path is active.

Issue `#2243` removes the unactivated hardware-recovery API, UI, service, CLI,
and public contracts. Alembic revision `f2239a0b1c2d` remains immutable deployed
history, and its inert tables remain available only for safe rolling deployment
and rollback compatibility. The outbox worker retains the historical recovery
alert kind only to drain any already-persisted row; neither compatibility path
can create authorization authority.

## Bootstrap And Break-Glass

Bootstrap exists only to establish the first reachable DB-backed administrator
and service roots. It must stop acting as ordinary runtime authority after
cutover. The current service disables bootstrap-email role elevation only after
the active policy contains an immutable-ID-bound human administrator with exact
Launchplane policy-administration scope; explicit DB denial then wins on login
and session revalidation. Legacy human rules retain their existing runtime
matching semantics until a reviewed migration replaces wildcard or implicit
selectors, while all newly reconciled managed human rules require explicit
roles, explicit principals, exact selectors, and immutable IDs for sensitive
access. Changed applies must also retain the applying administrator, at least
one strict human administrator, and enough distinct immutable GitHub IDs to
satisfy the effective quorum. Continuity recognizes only an immutable-ID-bound GitHub
ID-only human rule with the explicit `admin` role, literal
`authz_policy_grant.write` action, and exact `launchplane` product/context
selectors; roles-empty rules, mutable login, organization, team, or instance
selectors, action-empty or wildcard actions, wildcard selectors, workflow,
terminal, `local_operators`, and local-admin rules cannot satisfy the strict human
administrator predicate. Any future break-glass design
must be separately approved by the Director before implementation, use an
independent credential and approval boundary, bind the expected active policy
digest, make the smallest recoverable change, append audit evidence, and
require normalization through the ordinary
DB-native path.

Immutable-image rollback is service-code recovery; it is not authorization-data
recovery. Do not claim that rolling back the Launchplane image repairs an active
DB policy.

## Privileged-Operation Canary Actions

The governed privileged-operation surface accepts schema-v2 and schema-v3
managed rules for the existing caller identity types and requires exactly one
match with both managed IDs. Durable authorizations captured under schema v2
may continue under schema v3 only while that exact caller, action, target, and
managed rule still match. Stored schema-v3 durable captures fail closed against
schema v2. Legacy unmanaged action-empty rules cannot inherit any action. Code
deployment introduces no policy rule or grant, and schema-v3 policy writes and
activation remain separately fenced.

Merge-train policy imports use a dedicated privileged-operation action family:
`merge_train_policy_operation.propose`, `.read`, `.cancel`, `.approve`, and
`.revoke`, plus the read-only terminal-agent projection action
`privileged_merge_train_policy_operation_summary.read`. Existing
`authz_policy_operation.*`, workflow, `local_operators`, local-admin, or raw
`merge_train.policy_import` grants do not authorize this lifecycle. Activation
of those exact actions remains a DB-native managed-authz privileged operation
with fresh admin authentication, review, CAS, idempotency, and read-back.

The browser-human identity dependency is separate from the existing browser
mutation dependency that permits bearer identities to pass through. Bearer,
workflow, terminal-agent, `local_operators`, and local-admin identities are
rejected before human-route policy evaluation. The agent summary route is a
separate action and projection and never authorizes approval or execution; keep
`privileged_operation_summary.read` ungranted during the canary.

Before activation, inspect active action-empty rules, record policy-schema
evidence, prove the expected-image worker container is running, and retain one
successful DB-backed worker poll. Use staged DB-native activation: first grant
only `privileged_secret_operation.plan`,
`privileged_secret_operation.read`, and
`privileged_secret_operation.cancel`; after exact plan review, add only
`privileged_secret_operation.approve` and
`privileged_secret_operation.revoke`. Approval can be claimed immediately, so
revocation is only possible before worker claim. Keep approval authority active
until the worker's terminal reauthorization, then revoke every canary rule and
read the active policy back after terminal verification or any post-activation
worker stop.

The ordinary-agent delegated-delivery design is retired (see
[DIRECTION.md](../DIRECTION.md#retired)), and
[#2437](https://github.com/cbusillo/launchplane/issues/2437) deletes its code.
The ordinary-agent activation text below describes that code until then; do not
activate or extend it.

The retired activation executor and enrollment recovery have been removed.
Historical activation contracts and schema-3 parsing remain until the
domain/storage layers are deleted under #2437. Bounded legacy ciphertext expiry
maintenance remains necessary for current managed-secret key retirement.

The source-owned activation administration descriptor is
`ordinary-agent-delivery-activation`. Its human lifecycle uses the distinct
`ordinary_agent_delivery_activation.plan`, `.read`, `.cancel`, `.approve`, and
`.revoke` actions. Each action must appear literally on the one matching managed
rule; the legacy empty-action compatibility behavior used by other descriptors
does not authorize this family. The descriptor has no terminal-agent proposal or
summary capability.

Setup resolves a stored managed-policy proposal and current repository inventory
on the server. A schema-v2 source policy requires the explicit
`migrate_v2_to_v3` proposal mode. A schema-v3 source policy uses an ordinary
schema-v3 reconcile with migration mode `reject`; renewal does not repeat a
completed migration. The source operation may be planned, approved, executing,
or executed, and its request, evidence, plan, managed-set, and full candidate
policy digests are recomputed. It must identify exactly one ordinary-agent rule
and target. Other reviewed rule families remain governed by that policy
operation rather than forcing an artificial package split.

An approved setup writes only an inert activation record with desired state
`guarded` and effective state `qualification_only`. A separate approved
`revoke_activation` request performs exact activation CAS and makes both states
terminal `revoked`. Replacing an expired guarded intent atomically supersedes the
old projection, appends its event, and installs a new activation ID; a revoked
predecessor stays unchanged. Recovery follows the append-only event bound to the
setup or revoke operation, so later legitimate transitions do not erase the
original result. Scope history and current-row uniqueness use the immutable
repository ID, branch, managed set, and managed rule; the repository name remains
display context and a rename cannot create a second current intent. The Launchplane
UI offers server-resolved targets and server-clock expiry choices from one hour
through 30 days without requiring typed record IDs, digests, issue IDs, or
free-form reasons. The planner rejects any setup expiry beyond 30 days.

This descriptor makes no provider call, installs no authorization grant,
registers no ordinary worker, and derives no guarded readiness. The schema-v3
write transition is separately mediated: a complete reviewed policy candidate
may add or change exactly one ordinary rule only when it matches a current,
unrevoked typed activation and its executed setup provenance. Exact ordinary-rule
removal and unrelated schema-v3 maintenance remain possible after activation
revocation or expiry; raw schema-v3 seed and unbound replacement stay fenced.
Runtime capability evidence records installed schema and compiled
parser/storage/write-handler support. `policy_v3_write_supported` reports the
presence of this mediated typed handler and is not itself authority or a raw
write bypass. Qualification and guarded-worker support remain separate.

Activation remains a separately Director-approved DB-native administration event;
it is not authorized by landing code.

### Preparing Agent Delivery Administration

The Access policy workbench can prepare the closed
`ordinary-agent-delivery-administration` candidate with an add or remove intent.
Preparation derives the immutable GitHub ID from the signed-in browser session
and the schema from the single active policy record. It requires existing
managed `authz_policy_operation.propose` authority and strict immutable-ID
administrator authority; it cannot repair missing policy-administration access.
The browser supplies no principal, policy fragment, managed IDs, scope, actions,
quorum change, or routine reason.

The candidate owns a separate managed set for one pilot administrator. Its rule
contains exactly the five explicit Agent delivery activation lifecycle actions,
the authenticated immutable ID, the admin role, and global Launchplane scope.
An occupied set with a different identity or shape refuses preparation. Adding
access also refuses conflicting explicit activation authority; overlapping
authority does not prevent removing this isolated set. Legacy empty-action rules remain
unchanged and do not count as explicit activation authority. The foundational
policy-operation set and all unrelated policy content remain untouched.

Both intents create ordinary `managed-authz-policy-set` plans through the
existing planner. Preparation may persist a planned operation and event; it
does not write policy, approve a plan, change schema or quorum, enroll an agent,
start a worker, or call a provider. Existing approval, current-policy checks,
worker execution, CAS, and read-back still govern any policy change. Already
satisfied intents create no new plan. Removal proposes an empty desired fragment
for only this set and refuses while an unstopped current activation could lose
its stop controls. Revoking an operation approval before worker claim and
removing an already-installed administration rule are different operations.
If an activation starts after removal was prepared, the policy write refuses
under the shared authorization lock. Stop that activation before preparing a
fresh removal; re-planning alone does not clear this condition.

The five lifecycle actions are prepared together because an approved activation
setup still writes only a qualification-only intent. They do not authorize the
separate ordinary-agent policy, provider, or worker changes. The pilot is
retired, so do not install this set; only its removal remains supported. This
composer is a constrained input path for an existing
admin, not a replacement bootstrap or total-lockout recovery.

### Preparing Administrator Product Evidence Access

The Access policy workbench can prepare the closed
`administrator-product-evidence-read` candidate. It uses the same existing
managed proposal authority and fresh immutable-ID administrator checks as the
delivery-administration candidate. The server derives the requesting human and
current policy schema; the browser supplies only the candidate, add/remove
intent, and an idempotent source event.

The isolated `operator.product-evidence-read` managed set contains only
`product_environment.read` for that human with the `admin` role. Its product
selector intentionally covers all current and future projects. Two rules cover
the existing read API's scope forms: the project-level context rule remains
pinned to the Launchplane context, while the environment-level rule uses the
explicit all-instances selector with an empty context selector so it can read
the stored context of each environment. Neither rule adds another action or
principal.
Other agents, other humans, writes, separate secret reads, provider operations,
Client acceptance, merge/deploy, and delivery activation receive no authority
from this candidate.

Preparation creates only a standard `managed-authz-policy-set` plan for review.
The active schema (2 or 3), administrator quorum, and unrelated managed sets
are preserved. An occupied set with another identity or shape refuses
preparation. Already satisfied add/remove intents create no operation. Reusing
one source event for a different candidate refuses with a conflict; replay
must match the requested candidate and the current human across every rule.

An exact pre-correction environment rule pinned to the Launchplane context is
recognized only for correction, removal, and truthful historical review: add
prepares the corrected same-set update, remove prepares the empty same-set
fragment, and historical review states that the old request was limited to the
Launchplane context. It never upgrades a persisted request or approval.

Review names the requesting administrator, both read scopes, all current and
future projects, and the standing duration. The installed access would remain
until a separately governed removal; the plan's **Approve by** deadline does
not expire it. Concrete administrator confirmation, current-policy checks,
worker execution, CAS, and read-back still govern installation. Installing it is a
grant, so it is a Director decision.
Source delivery alone adds no access.

The review keeps the access scope, duration, and approval blockers visible.
Rule counts and policy digests remain available under **Technical details**.

Removal proposes an empty fragment for only this managed set. Independently
granted read access can remain. This capability is not a stop control, so its
removal does not depend on stopping an activation. The separate Agent delivery
administration set retains its own stop-before-removal requirements. Neither
intent reads denied product data through an alternate route; normal product
reads use their existing authorization after any approved installation.

### Preparing Agent Proposal Access

The Access policy workbench's **Prepare agent proposal access** card compiles
one rule in the isolated `operator.agent-policy-proposer` managed set for the
service-configured `local_operator` identity. The browser supplies only the candidate
`agent-policy-proposer`, add/remove intent and stable source event. It cannot
choose a principal, descriptor, action or policy fragment.

The single rule names only `authz_policy_operation.propose` and
`merge_train_policy_operation.propose`, on product/context `launchplane`, with
no instance selector. It grants proposals for the two existing policy descriptors
only. Plans are inert until the signed-in Director approves them; neither the
rule nor the proposal route grants approval, policy writes, import apply,
credentials, provider operations or ordinary-agent delivery. Existing standing
read grants remain separate. See [agent proposals](privileged-operations.md#agent-policy-proposals).

Preparation rejects a missing or globbed configured identity, an occupied set
with another identity/shape, and overlapping proposal authority. Exact installed
add/remove intents are already satisfied. Replays bind the configured principal
and exact candidate; a configuration change cannot rebind a saved plan. Removal
proposes an empty fragment for every rule in only this set, including unexpected
rules. The exact diff remains subject to Director approval. Unrelated access
and existing pending plans remain unchanged.
The standard managed-policy review, human confirmation, current-policy checks,
CAS and service worker govern installation. Installing or removing the rule is
an access change, so the Director approves it separately; code adds no grant.

### Preparing Agent Product Setup

The Access policy workbench can prepare the closed `agent-product-setup`
candidate (launchplane#2766). It lets the Director's agent set up named
products' testing lanes and production backup policy. The agent's helpers dry-run
each write first; product config and backup authority also bind apply to the
reviewed digest, while Dokploy target setup relies on its confirmation, reason
and idempotency key.
It replaces the earlier "operate" card, which #2750 removed.

For each selected product the isolated `operator.agent-product-setup` managed
set holds exactly three `local_operators` rules, all for one subject and token
label and the product's one lane context:

| Rule id                        | Product     | Lane      | Actions                                       |
| ------------------------------ | ----------- | --------- | --------------------------------------------- |
| `<product>.testing-config`     | the product | `testing` | `product_config.plan`, `product_config.apply` |
| `<product>.prod-backup-policy` | the product | `prod`    | `production_backup_authority.write`           |
| `<product>.testing-target`     | the product | `testing` | `dokploy_target.lane_setup`                   |

The set deliberately leaves out:

- `product_profile.write`. It also lets the holder override release review;
  the [overall direction](https://github.com/cbusillo/direction/blob/main/DIRECTION.md#stop-boundaries)
  owns release authority. The Director sets the
  Client, image repository, `production_use`, and whether the Client's acceptance
  starts the release (`release_on_acceptance`) in the Client panel under
  `product_profile.write`. Each control dry-runs and reads back the profile; the
  panel keeps the reviewed request frozen for Apply. Image Apply checks the
  starting repository; production-use and release Apply verify the full reviewed
  plan digest.
- `product_onboarding.apply`. It is checked on product `launchplane` with no
  product or lane scope, so it would reach every product. Adding the lane record
  stays with Launchplane's own onboarding workflow.
- `dokploy_target.setup`. It is checked on product and context `launchplane`,
  so it reaches every product's Dokploy targets. `dokploy_target.lane_setup` is
  the lane-scoped alternative (see the Dokploy target setup section in
  `service-boundary.md`): it is checked on the product that owns the lane's
  context, creates only that lane's compose in a new provider project and
  environment, and can't adopt, re-point, replace or prune a target.

Secondary Odoo addon settings, integration allowances, and testing-hold writes
carry the named product's exact `(product, context, instance)` lane into storage
for every supported caller. Storage rechecks lane ownership under the same
lock and transaction as product-profile writes. Losing or duplicating the exact
lane returns `product_profile_conflict` (409), leaving the lane record unchanged.
`local_operator` callers also retain the exclusive-context requirement below:
that check runs first and returns `local_operator_lane_scope_required` (403)
when the context stops belonging to the named product alone. Privileged callers
can still write a uniquely owned instance in a
context shared by products using different instances. Dry-runs and unchanged
applies perform no lane record write and retain their existing behavior.

Product config applies from a `local_operators` caller, which is how this set
is used, also refuse (`local_operator_lane_scope_required`) a context that is
not the named product's alone, and a context- or global-scoped secret written
through an instance request: those would change what another product or
another lane, such as production, resolves. Secondary lane writes also carry
this exclusive-context requirement into the commit-time check described above.
Dry-runs and unchanged applies do not write and retain their existing behavior.
Backup authority proposals from an agent or `local_operators` caller are inert;
they use existing exact-lane backup planning authority. The proposer cannot
approve or directly apply, even when an older rule lists backup write authority.
A signed-in administrator decides in the production backup card. The service
refuses target revisions outside the submitted policy or shared with another
active product policy; shared targets can be referenced without revision. The
planner and the locked write both enforce that boundary. See the
[proposal contract](production-backup-provider.md#review-a-proposed-backup-policy).
A `local_operators`
caller may set an ordinary protective testing hold. Lifting an ordinary staff
hold remains an administrator action because it requests testing reconciliation,
which can deploy. The product's testing overview exposes **Review lift** and
**Lift testing hold** only to the service-verified signed-in administrator.
The existing testing-hold endpoints accept that browser session, with origin,
fetch-metadata and single-use CSRF protection on the review and apply requests.
The card displays the stored reason, recorder, time and an HTTPS issue link
recorded in the reason. Missing historical metadata is shown as not recorded.
The administrator
reviews the current hold, supplies an audit reason and confirms that testing
can deploy before submitting its digest-bound lift. The existing authenticated
idempotency receipt retains the original hold, lift reason, actor scope, time
and trace. A lost response retains the original reviewed request and key for
retry, including after reloading the tab. If that request cannot settle, an
administrator can explicitly start a separate review of the current hold;
the earlier key is shown as unverified in this tab across reloads. Closing the
tab or clearing its storage discards this local history, not the service's audit
receipt. This does not claim the earlier request failed or succeeded.
Isolation staging interlocks retain
their separate activation flow.
The Director's agent can set a testing hold but not lift one.
The authenticated Director agent has the separate bounded
[disposable rehearsal continuation](operations.md#odoo-instance-override-contracts):
the service must have marked its original hold as a qualified rehearsal, and
the original actor, current safety evidence, bindings and unresolved-effect
checks must still pass. This source permission installs no policy grant and
does not apply to actual staff use, only-copy data, shared production resources,
staging/activation holds or unknown outcomes.

What it can reach on a live product: the testing lane's settings and secrets,
the testing lane's new compose target, and an inert proposal for production's backup policy. The
Director approves that policy in the signed-in card; the worker applies only its
service-pinned dry-run digest. It cannot deploy, promote, roll back, run a
backup, change the Client or release review, or touch another product. Live
products are therefore listed and accepted.

Preparation uses the same managed `authz_policy_operation.propose` authority and
fresh immutable-ID administrator checks as the other candidates. The browser
supplies only the candidate, the add/remove intent, an idempotent source event,
and, for add, the product identifiers chosen with checkboxes. The server
validates every selected id again: each must name an existing product profile
whose lanes share exactly one lowercase context, other than `launchplane`
(compared case-insensitively), that no other product's lanes or historical
contexts use; an empty selection is refused; the list is deduplicated and
sorted. Products and contexts
are never hard-coded. The selection replaces the products the set already
covers.

The principal is never supplied by the browser. The server uses the service's
configured `local_operator` identity (`LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT` and
`LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL`, active only when the `local_operator`
token is configured). That is the only identity the service authenticates as a
`local_operator`, so it is the identity the Director's agent uses. Add refuses
(`authorization_candidate_principal_unavailable`) when no identity is configured
or either value contains a glob character. The plan binds the reviewed subject
and token label; a later configuration change does not rebind it.

Adding again with the same products is already satisfied. A set held by another
identity, in another principal collection, missing a rule, or with any other
shape is a conflict that preparation does not adopt or repair. Replay
recognizes only the exact shape, the configured identity, the same normalized
product list, and contexts that the product records still confirm as each
product's own. The review uses the agent wording only when every rule binds the
service's configured `local_operator` identity and the product records confirm
every rule's context; any other same-shape proposal gets the generic
managed-policy review.

Removal proposes an empty fragment for only this set and does not need the
configured identity. Review names the selected products, what the rules reach,
and that the access stands until a separately governed removal. Preparation
creates a plan for review and changes nothing until approval, current-policy
checks, worker execution, CAS and read-back install it. Installing it is a
grant, so it is a Director decision.

### Inspecting Pilot Preparation Inputs

The dedicated ordinary-agent preparation routes and browser callers are removed.
Historical preparation context remains readable; new generic proposals cannot
supply it. Remaining domain/storage retirement is tracked by
[launchplane#2437](https://github.com/cbusillo/launchplane/issues/2437).


### Preparing The First Ordinary Client Policy

The first-client preparation route is removed. Its persisted context remains
part of historical policy records.

## Existing-identity policy version compatibility

Authorization policy readers support schemas 2 and 3 for existing human,
workflow, and terminal-agent identities. `owner-control` challenge derivation,
authorization diagnostics, and feedback actor/worker resolution preserve the
same immutable identity, scope, role, current policy, expiry, and revocation
checks across these versions. This does not register an ordinary-agent identity
or enable ordinary-agent execution.

These compatibility paths do not widen authorization policy writers or the
candidate-policy preview boundary. Their existing v2-only behavior and the
mechanical v3 persistence fence remain in place until the separately reviewed
activation work enables a complete supported path. Once v3 policy or evidence is
persisted, rollback must use an image that can read it; a source compatibility
change alone is neither activation nor proof of the deployed policy version.

### Pending Client review withdrawal

`product_review.withdraw` is a write under the Director agent's existing
standing permission. It creates no DB policy grant, credential, Client decision,
release acceptance or merge authority. Only an authenticated Director agent or
admin can apply withdrawal; read-only identities, ordinary scoped agents and workflow
identities cannot. Dry-run and readback use `product_profile.read`; saved operation
keys remain bound to the original caller. The [withdrawal contract](owner-acceptance.md#withdrawing-a-pending-request)
requires a recorded Director decision, reason and exact reviewed head and
refuses after a current-head Client decision.
