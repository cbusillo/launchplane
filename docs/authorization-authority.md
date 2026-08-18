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
