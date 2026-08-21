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
- preserve the applying administrator and an independently recoverable
  administrator;
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

Landing these read contracts does not authorize their production grants and
does not relax the active freeze. Production policy changes still require the
separate reviewed administration and recovery gates owned by `#2058`/`#2061`.

After parity and recovery gates pass, protected desired-set secrets and routine
authorization workflows must be retired. GitHub may remain an identity provider
and transport for already-authorized workloads, plus a narrowly bounded
bootstrap/break-glass path that cannot replace arbitrary policy.

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
the applying identity. Break-glass must use a separate credential and approval
boundary, bind
the expected active policy digest, make the smallest recoverable change, append
audit evidence, and require normalization through the ordinary DB-native path.

Immutable-image rollback is service-code recovery; it is not authorization-data
recovery. Do not claim that rolling back the Launchplane image repairs an active
DB policy.
