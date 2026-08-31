---
title: Authorization Authority
---

# Authorization Authority

Launchplane's active PostgreSQL authorization-policy record is the live
application authority. GitHub identities, workflows, environments, repository
secrets, OIDC tokens, and checked-in files may authenticate callers or transport
reviewed requests, but they do not grant Launchplane permission by themselves.

## Active Freeze

Issue `#2058` freezes production authorization changes while the authentication
and authorization audit, DB-native administration design, migration plan, and
independent review remain incomplete.

Until that work closes:

- do not add new routine grants or managed sets through GitHub secrets or
  workflows;
- do not create, edit, retarget, or dispatch a workflow merely to make an
  `authorization_denied` operation succeed;
- route authorization gaps to `#2058` and add a native `blocked-by` relationship
  from the affected work;
- permit only explicitly reviewed maintenance of an already-authorized
  transitional path, or a documented bootstrap/break-glass recovery operation;
- do not apply a production policy change without the separate approval required
  by the owning issue and operator boundary.

This freeze does not prohibit code, tests, documentation, threat-model work, or
dry-run-only validation that cannot mutate live policy.

Owner-control channel-session, issued-challenge, and shadow-verification event
records are inert verification evidence, not authorization policy or grants.
They define no HTTP action, route, managed set, workflow, secret, or production
access path; every result persists `authority_state = 'inert'` and
`authorizes_execution = false`. Adding these records does not relax issue
`#2058` or make a self-asserted key, binding, or challenge authoritative.
Service-only challenge derivation may fail closed unless the enrolled immutable
GitHub owner ID has exactly one ID-only managed rule for the descriptor's
existing approval action. That read does not define a new action, grant access,
or authorize approval or execution; rules that also depend on mutable login,
organization, team, or role selectors are intentionally insufficient without a
live authenticated human identity.

Administrator-enrollment records are likewise inert evidence only. They may
record an owner-created, 30-minute opaque challenge and a later server-derived
candidate GitHub identity that proved control, but they do not create a policy
administrator, grant a policy action, change a managed set, or make any route
or workflow available. Every record is fixed to no authority. A future bridge
may compile a final enrolled record into an owner-gated DB-native policy change
only after separate design, review, apply, and read-back work under the active
authorization-administration boundary. Landing enrollment storage does not
approve or implement that bridge and does not relax `#2058`.

Repository inventory follows the same authority boundary. Its
`repository_inventory.read` and `repository_inventory.write` actions use the
Launchplane service scope (`product=launchplane`, `context=launchplane`), but
defining those actions grants neither one. Active tracked inventory records are
repository-record evidence for the redacted repository-scope read model;
retired records contribute no active inventory membership. When authorization
rules are the only remaining active source, the read model reports stale
authorization membership rather than complete coverage.

## Current Transitional Model

The current service has a DB-backed managed-rule reconciliation endpoint with
compare-and-swap, idempotency, reviewed-plan digests, and redacted evidence. A
protected GitHub workflow currently reads desired managed sets from repository
secrets and transports them to that endpoint through GitHub Actions OIDC.

That workflow is transitional compatibility infrastructure. The database remains
the live decision authority, but GitHub-hosted desired sets still make GitHub
part of the effective administration chain. Do not interpret the workflow's
existence, its protected environment, or its listing in repository metadata as
approval to use GitHub for routine permission administration.

## Denial Handling

Treat `authorization_denied` as an authority result, not a credential-selection
hint.

1. Record the denied action, scope, trace ID, and current work item.
2. Determine whether Launchplane already has a sanctioned native capability for
   that record type.
3. If the capability exists but the caller lacks scope, block the work on the
   authorization architecture and operator decision; do not borrow a workflow
   identity.
4. If no native capability exists, treat it as an architecture gap and route it
   to `#2058`/`#2061`; do not close the gap with a new workflow, secret, local
   helper, direct database command, or provider call.

Manual route probing, wildcard grants, temporary CI authority, and copied policy
payloads are not diagnostic substitutes.

## Target Model

The DB-native administration surface must support authenticated administrators
through Launchplane's API and UI:

- inspect active policy, managed sets, principals, and effective access;
- explain denials without requiring policy-write permission;
- propose, dry-run, review, apply, revoke, and roll back exact changes;
- preserve the applying administrator and at least one distinct active policy
  administrator without claiming total-lockout recovery;
- prevent final-admin lockout and reject known readiness blockers;
- retain immutable identity, least-privilege scope, revision, digest,
  idempotency, and audit evidence;
- export and restore policy without making GitHub the durable desired-state
  store.

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
health reason codes, managed-set rule counts, and policy-administrator rule
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
grant and does not weaken the issue `#2058` freeze.

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
terminal-agent, local-operator, and local-admin identities fail before policy
evaluation.

The bridge does not trust the session role or mutable login, organization, or
team selectors. It reloads the single active DB policy and requires an exact
immutable-ID human administrator rule with explicit `admin` role, literal
`authz_policy_grant.write`, and exact `launchplane` product/context scope. Its
code-compiled managed set contains one GitHub-human rule for that same immutable
ID and exactly `authz_policy_operation.propose`,
`authz_policy_operation.read`, `authz_policy_operation.approve`,
`authz_policy_operation.revoke`, and `authz_policy_operation.cancel`. It cannot
create workflow, terminal-agent, local-operator, local-admin, wildcard,
provider, deployment, or unrelated authority.

An immutable-ID administration denial remains ordinary redacted authorization
evidence: the service records its trace, fixed action and scope, reason category,
and active-policy provenance. The route response and every error remain
`Cache-Control: no-store`; no principal selector or raw policy is persisted in
the denial record.

Dry-run binds the observed active record ID, revision, policy digest, candidate
revision and digest, desired-set digest, exact action set, applying-admin
continuity, and distinct reachable-administrator evidence. Apply repeats that
compiled request with the reviewed digest, non-empty reason, immutable-ID-scoped
idempotency, active-policy compare-and-swap, and exact record/revision/digest and
policy read-back. The written record uses the distinct
`service:authz-policy-operation-activation` source. Once the exact managed set
is active, both activation routes return the terminal
`authz_policy_operation_activation_retired` result; an occupied but non-exact
set fails as a conflict. This state-derived retirement is not total-lockout
recovery, a recurring break-glass path, or operator-configured authority. The
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
authenticated GitHub humans, local operators, and local administrators;
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
fail-closed audit result and cannot satisfy #2177 or final #2062 review. Storage
or active-policy absence still fails hard; multiple active policies fail as
ambiguous. Landing this route grants no production access and authorizes no
policy, workflow, secret, provider, runtime, deployment, or durable-operation
change.

For host-local audit recovery when the operator does not hold the HTTP action,
`launchplane authz-policies repository-scope-evidence` accepts the same bounded
exact-candidate request JSON and reads the same redacted response directly from
the configured PostgreSQL record store. This command derives evidence from the
operator's DB credentials; it does not evaluate the operator against the active
policy and is not proof that the operator is policy-authorized. It requires
exactly one active policy record, fails closed on missing or ambiguous active
state, and performs no policy, secret, workflow, provider, runtime, deployment,
session, denial, idempotency, outbox, or durable-operation write.

Landing these read contracts does not authorize their production grants and
does not relax the active freeze. Production policy changes still require the
separate reviewed administration gate owned by `#2058`/`#2061`; total-lockout
recovery remains explicitly deferred.

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
access. Changed applies must also retain a policy administrator independent from
the applying identity. Continuity recognizes only an immutable-ID-bound GitHub
ID-only human rule with the explicit `admin` role, literal
`authz_policy_grant.write` action, and exact `launchplane` product/context
selectors; roles-empty rules, mutable login, organization, team, or instance
selectors, action-empty or wildcard actions, wildcard selectors, workflow,
terminal, operator, and local-admin rules cannot satisfy that
independent-administrator predicate. Any future break-glass design
must be separately approved by the owner before implementation, use an
independent credential and approval boundary, bind the expected active policy
digest, make the smallest recoverable change, append audit evidence, and
require normalization through the ordinary
DB-native path.

Immutable-image rollback is service-code recovery; it is not authorization-data
recovery. Do not claim that rolling back the Launchplane image repairs an active
DB policy.

## Privileged-Operation Canary Actions

The governed privileged-operation surface uses schema-v2 managed rules and
requires exactly one match with both managed IDs. Legacy unmanaged action-empty
rules cannot inherit any action. Code deployment introduces no policy rule or
grant.

The browser-human identity dependency is separate from the existing browser
mutation dependency that permits bearer identities to pass through. Bearer,
workflow, terminal-agent, local-operator, and local-admin identities are
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

Activation remains a separately owner-approved DB-native administration event;
it is not authorized by landing code. Keep #2204 open until actual migration,
rollback, read-back, and soak evidence exists, and keep #2177 open until its
handoff criteria are complete.

**Preserved history:** Phase 1 introduced planning-only actions without grants.
That history does not describe the deployed Phase 2 worker flow.
