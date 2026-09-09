---
title: Ordinary Agent Execution Contracts
---

# Ordinary Agent Execution Contracts

The ordinary-agent execution models describe proposed, inert execution evidence.
They are not part of the service authentication union, HTTP routes, or provider
executor. Eligibility computed from fixture records is not
authentication, merge admission, human acceptance, or permission to perform an
effect.

Authorization-policy schema v3 is currently a compatibility reader and version
discriminator only. It can deserialize an `ordinary_agents` rule collection,
report the observed schema version and rule count, and retain the rules during a
record round trip. The five existing human, workflow, and operator identity
types keep their schema-v2 evaluation behavior. The generic authorization
evaluator has no ordinary-agent identity branch and deliberately ignores the
ordinary-agent rules. Only the separate pure evaluator described below can
reason about those rules, and its result remains inert.

Schema-v3 policy writes are not activated. The authoritative store rejects a v3
seed even when no active record exists, rejects v3 replacement records, and
rejects replacement, deletion, downgrade, or retirement when the observed
active record is v3. Managed policy administration, recovery, and generated
preview planning also reject an observed v3 policy before constructing an
applicable v2 plan. This fence remains until a later activation slice enforces a
minimum compatible service image and a rollback boundary.

Existing v1 and v2 policies omit the empty `ordinary_agents` field from
serialization and canonical hashing, preserving their prior payloads, digests,
and record IDs. Phase-one deployments therefore continue to persist only the
same v1/v2 records that previous service images can parse. A future persisted v3
record will be unreadable to those images, so they cannot be rollback targets
after v3 activation.

The enrollment request and review contracts remain dormant in this phase.
They parse exact enroll, credential-rotation, and principal-revocation pre-state,
derive the minimum execution profile from the bound rule, and return only a
stable unavailable result. They have no registered privileged-operation
descriptor, route, worker dispatch, credential issuer/verifier, provider call,
session path, or effect path.

The authoritative lifecycle store is present behind that unreachable boundary.
Its internal apply envelope separates an agent-to-Launchplane authentication
credential candidate from Launchplane-held provider App custody. The first
contains a service-derived authentication digest and no bearer value. The second
contains only provider-inspected App, exact target, permission, inventory, and
managed-secret record/version metadata; it contains no private key or minted
token. The future authentication issuer must derive the digest from actual
credential material and arrange delivery separately. The stored receipt is
redacted and therefore does not define or constrain that delivery mechanism.

One PostgreSQL transaction reserves the descriptor-specific inner idempotency
tuple, locks the active authorization policy, serializes the principal even when
it is absent, and then locks the exact inventory, secret, credential, and custody
evidence. It rechecks the exact immutable human administrator rule and, for
enroll/rotate, the schema-v3 ordinary rule and every CAS input. It writes the
principal, authentication credential, custody reference, audit, and store-built
receipt together. No callback, provider request, secret decryption, or token mint
runs under those locks. Revoke requires current immutable administrator authority
and the principal CAS but deliberately does not require an ordinary rule,
inventory, secret, or readable custody. A missing authentication credential does
not block principal revocation: the receipt and audit explicitly omit credential
record evidence while retaining the principal's last known credential reference.

These storage records do not grant execution. Their only callable entry point is
the internal store method used by tests; production policy schema-v3 writes stay
fenced, and there is no descriptor or route that can construct or dispatch the
apply envelope.

Every proposed record requires the `proposed_ordinary_agent_v1` record kind,
`authority_state = "inert"`, and `authorizes_execution = false`. Missing markers,
unknown fields and unsupported versions fail validation. Frozen, strict models
and explicit serializers help preserve that distinction; the markers alone do
not make an incorrectly wired consumer safe.

The active DB authorization policy remains the only source of positive live
permission. A future production integration must obtain policy and identity
inputs from trusted service-owned records and prove conformance with the complete
production evaluator. It must not accept these proposed records from a caller as
an authorization decision or translate a legacy global controller grant into an
ordinary-agent grant.

## Scope and lifetime

A proposed managed rule names an immutable principal, stable managed-set/rule
IDs, exact repository ID and canonical name, exact base branch, and closed
supported ordinary actions. Repository names and branches are supplied inputs;
there is no checked-in inventory or wildcard fallback.

Credential evidence contains a version and digest reference, never a bearer
credential or provider secret. A session binds to that exact credential version
and principal. A lease further restricts the session to the selected policy rule,
repository/base, one selected action, lifetime and budgets. These types issue no credential,
session or lease. Rotation invalidates use of sessions bound to an older
credential version; a lease cannot enlarge the policy or session.

Validity intervals use whole UTC epoch seconds and are half-open:
`valid_from <= now < expires_at`. Session lifetime is contained in credential
lifetime; lease lifetime is contained in session lifetime. A scheduled revocation
is effective when `revoked_at <= now`, including equality. No local clock or live
service read is hidden inside pure evaluation.

Each lease selects one action so its per-action effective-decision fingerprint
is unambiguous. The read-only budget snapshot records a finite rate window,
action usage and PR usage; evaluation checks the requested cost against the
remaining limits but does not consume or reserve them.

A standing repository/base lease need not be reissued for each PR. Each request
still pins a finite set of PR numbers and exact head/base commits, with an
explicit set of permitted stack edits. Unlisted queue entries or stack edits
cannot join that request. Changed commits require fresh bounded admission; that
is distinct from asking for a new delegation. Genuine required human acceptance
must still be refreshed when its own exact-evidence binding becomes stale.

## Effective-decision fingerprints

A fingerprint identifies what the proposed evaluator actually evaluated. It is
not a cache key, a signature, or evidence of a live policy read. Evaluation must
run before comparison with a lease's fingerprint, even when a previous
fingerprint matches.

The canonical preimage contains a closed input-domain identifier, evaluator
semantics version, the bound rule and exact target, applicable principal
status/profile, the decision and its reason. Canonical JSON separates fields,
uses sorted keys and integer values, and is hashed with SHA-256. The fingerprint
has an `oae-fp-v1:` prefix and a lowercase digest.

An unrelated policy revision may leave the effective fingerprint unchanged while
the result carries the newly supplied policy provenance. `policy_digest` is
unverified in these proposed models; production integration must bind it to the
actual active record and evaluated rule content. A changed bound rule, ambiguous
managed identity, overriding read-only/revoked principal restriction, or unknown
semantics cannot preserve a positive decision. The proposed evaluator does not
invent a second deny-rule language. If production integration discovers another
input that can change the effective decision, the input-domain/semantics contract
must change and prior bindings must be reevaluated. Production conformance tests
must cover those inputs, including applicable deny/precedence rules.

## Eligibility and recovery

Schema validation precedes eligibility evaluation. Policy is evaluated for the
lease target and action, then the request is checked against that lease. The pure evaluator uses a
stable first-failure order for principal restrictions, exact target, identity
chain, lifetime/revocation, policy/rule/fingerprint, request scope and available
budget/recovery evidence. It reports a bounded reason, evaluated policy provenance and a canonical digest
of the exact request, the supplied evaluation time, and principal/session/lease
references. These are internal
proposed evidence records, not a public denial response. The caller must provide
a unique `result_record_id` for each distinct evaluation; reuse is permitted only
for a byte-identical replay, and conflicting same-ID results must never overwrite
history. A public projection must
redact policy provenance and collapse foreign/nonexistent target distinctions
through an authorized self-read path. It does not reserve budget, authenticate a caller, check CI, consume
Owner acceptance, or perform provider operations.

Recovery evidence distinguishes completed effects, partial completion, known
budget exhaustion and unresolved provider outcomes. Unknown outcomes retain their observed reservations or fences and cannot claim
success or authorize replay. An interruption while establishing protection may
leave only one kind of guard; the record must preserve that incomplete state
rather than inventing a guard or rejecting the recovery evidence. It grants no
new effect permission. Runtime reconciliation must retain every known guard and
restore the required protection before any new effect. In-flight records require
observed protection; a known budget stop cannot conceal outstanding effect
reservations. Completion requires success and no active protection, and may have
zero provider effects for a confirmed no-op. Budget exhaustion is a known stop,
not an unknown result. Provider credential expiry and
in-flight effects remain explicit because revocation cannot recall an already
sent GitHub request atomically.

Live execution will require serialized effect permits and fresh authorization at
every effect boundary. Revocation must prevent new permits while allowing honest
read-only reconciliation and protective fencing. Any new provider mutation during
repair requires its own current authority. The proposed storage interface and
authoritative lifecycle records do not yet prove session, effect-permit,
provider-custody, or distributed recovery properties.

## Human administration integration check

The current human administration path has a useful, narrower boundary:

- `ManagedAuthzPolicySetProposalInput` in
  [privileged_operation.py](../control_plane/contracts/privileged_operation.py)
  accepts a desired authorization policy and creates a managed-policy reconcile
  request. It does not describe enrollment or lease writes.
- `plan_managed_authz_policy_set` in
  [privileged_operation_registry.py](../control_plane/privileged_operation_registry.py)
  plans that policy reconciliation.
- The `managed-authz-policy-set` branch in
  [privileged_operation_service.py](../control_plane/privileged_operation_service.py)
  checks exact request/evidence variants and renders policy-specific review
  content. The renderer itself confers no authority.
- `_execute_managed_authz_policy_set` in
  [privileged_operation_worker.py](../control_plane/privileged_operation_worker.py)
  reuses the reviewed policy request and performs policy-record CAS with mutation
  reservation and read-back. The worker separately reauthorizes the immutable
  human approver against current policy before the effect.

That path currently covers policy reconciliation only. It does **not** cover
credential enrollment, session issuance/invalidation, or lease issuance/revocation.
Those effects need an explicit typed review and transactional lifecycle contract
with fresh human authorization, payload/digest binding, effect-specific CAS and
idempotency, read-back, and reconciliation. Reusing a descriptor's name or adding
post-policy-CAS side effects does not establish that boundary. Whether a versioned
extension can preserve it is a separate integration decision, not a proven
property of these inert models.

The proposed contracts therefore add no admin command union, descriptor,
approval route, policy bridge, or additional write under the existing policy
executor. See [privileged operations](privileged-operations.md) and
[authorization authority](authorization-authority.md) for the existing human and
production boundaries.

## Validation boundary

Tests use synthetic principals and targets. Canonical JSON round trips and
same-ID replay/conflict tests establish the inert evaluation contract. Separate
SQLite portability tests cover lifecycle read/write/replay/drift, while the
PostgreSQL integration gate applies the migration from empty schema and proves
concurrent first enrollment, reservation-first locking, and whole-transaction
rollback. These tests do not prove deployed authentication, token delivery,
provider credential custody, live budget reservations, or live merge readiness.

Required production integration proofs remain distinct from this source-only
contract: full-policy evaluator conformance, database transactions and revocation
races, credential custody, exact-request guarded execution, bounded read-only
projections, and exact-scope live qualification. Third-party self-read does not
imply private provider access or permission to push, enqueue or merge.
