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
descriptor, route, worker dispatch, provider call, session path, or effect path.
Internal authentication issuance and verification primitives exist; no public
ordinary authentication gateway is registered.

The authoritative lifecycle store is present behind that unreachable boundary.
Its internal apply envelope separates an agent-to-Launchplane authentication
credential candidate from Launchplane-held provider App custody. The first
contains a service-derived authentication digest and no bearer value. The second
contains only provider-inspected App, exact target, permission, inventory, and
managed-secret record/version metadata; it contains no private key or minted
token. Enroll and rotate now require an internally generated authentication
bundle: opaque random credential material, its verifier, and a receiver-bound
encrypted capsule. Metadata-only candidates cannot enroll through the store.
The stored receipt remains redacted; private delivery is a separate DB record.

The internal custody-enrollment builder resolves the exact managed-secret binding
and current version, verifies the App and repository installation through
read-only provider requests, and derives the closed permission profiles and
inspection digest. This work happens before the storage transaction; it neither
mints an installation token nor issues an agent authentication credential.

One PostgreSQL transaction reserves the descriptor-specific inner idempotency
tuple, locks the active authorization policy, serializes the principal even when
it is absent, and then locks the exact inventory, secret, credential, and custody
evidence. It rechecks the exact immutable human administrator rule and, for
enroll/rotate, the schema-v3 ordinary rule and every CAS input. It writes the
principal, authentication credential, custody reference, audit, store-built
receipt, and private delivery capsule together. No callback, provider request, secret decryption, or token mint
runs under those locks. Revoke requires current immutable administrator authority
and the principal CAS but deliberately does not require an ordinary rule,
inventory, secret, or readable custody. A missing, invalid, or misbound
authentication credential does not block principal revocation. Unusable credential rows remain untouched; the receipt and
audit omit credential record evidence while retaining the principal's last known
credential reference. The store also requires the code-owned App integration,
private-key binding, full installation permission ceiling, and enrollment
capability set, even when a caller constructs an internal candidate directly.

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

## Provider credential custody foundation

The internal custody foundation can resolve one exact, current DB managed-secret
binding and mint a repository-scoped GitHub App installation token for one closed
effect profile. The provider token remains in service memory and is revoked when
the internal lease exits. Durable issue-attempt records contain redacted scope,
permission, App, installation and expiry evidence only; they never contain the
private key or installation-token value.

Enrollment can separately bind the exact current repository-inventory identity
and inspect the configured App installation, immutable owner account and closed
permission ceiling without minting a token. The inventory record supplies the
repository ID and name; the App-JWT provider read proves the installation at that
repository path and the same numeric owner. The resulting non-secret evidence
feeds the later atomic enrollment candidate builder, and provider reads complete
before any enrollment write transaction begins. At effect time, the downscoped
token response must still return the exact repository ID and name before the token
can be used, so stale name reuse cannot direct an effect to another repository.

A partial unique fence covers `(principal_id, repository_id)` while an attempt is
minting, issued or unresolved. Repository name, base branch, request identity,
credential version, secret version and App ID are evidence rather than fence-key
components, so rename, branch changes and rotation cannot bypass an outstanding
repository-scoped token hazard. A provider-authored, validated token expiry can
bound cleanup after that expiry plus clock skew. A lost mint response or crashed
mint has no inferred expiry and remains fenced until a later supported provider
reconciliation supplies affirmative evidence; no timeout or local administrative
clear releases it.

This foundation adds no route, worker, grant, policy write or live App binding.
It does not activate enrollment or execution. The fence is principal-scoped, so
it does not serialize two distinct principals operating on one repository;
guarded execution must supply its own cross-principal effect fence.


## Internal authentication and private delivery

The service generates an opaque random credential with a bounded canonical
locator and at least 32 random bytes. A domain-separated digest is stored, and
verification joins the current principal, credential version, and durable issuer
provenance at DB time. Both records must be active and consistent. A credential
is identity only: the same mechanism serves read-only and guarded-executor
principals, while future admission still checks current policy, session, lease,
and effect scope. There is no positive authentication cache. Legacy marker-only
rows have no issuer provenance and cannot authenticate; authorized rotation or
principal revocation remains available. The reserved ordinary prefix is rejected
before all legacy terminal/operator/administrator token comparisons.

Issuance encrypts through the existing managed-secret key ring before taking DB
locks. Enrollment idempotency commits the reviewed intent, including receiver
binding and finite lifetimes, but excludes generated token and ciphertext
randomness. Concurrent attempts may prepare different material; only one commits,
and retries return its original immutable receipt plus current delivery status.
Ciphertext, claim proof and bearer never enter public receipts or audit events.
A database compromise together with access to the service key root can expose
retained capsules; temporary encryption is not a claim of immunity to that threat.

A separately generated receiver secret has at least 32 random bytes; only its
independently domain-separated hash binds the request. An invalid proof cannot
decrypt, mark attempted, consume or erase a capsule. Claim first checks current
policy and credential state, decrypts outside locks, then reacquires the same
policy/principal/credential/delivery lock order and rechecks the exact snapshot.
It durably marks possible delivery before emitting plaintext. Retries recover
the same credential until the reviewed delivery expiry, at most 15 minutes after
DB issuance. An unrelated authorization-policy revision does not break delivery
when the same managed principal rule and target remain present.

No client acknowledgement or manual code/paste/search step is required. Expiry
removes ciphertext. A never-attempted capsule also revokes its matching current
authenticator, preserving the principal and newer versions; an attempted or
ambiguous response preserves authentication until its own expiry or revocation.
Replay reports terminal expired/revoked/superseded delivery rather than promising
unavailable material or reminting. Recovery after the delivery window uses the
existing authorized rotation operation. Rotate and revoke erase outstanding
capsules even when ordinary-agent policy denies work. A response already emitted
can race cancellation, but its credential fails subsequent current-state checks.
Retained capsules participate in key-usage and rotation-plan accounting, blocking
retirement of their encryption key until cleanup. Replayed secret-root rotations
retain their historical operation evidence while reporting current retirement
safety. Private persistence failures expose a safe error category, SQLSTATE and
trace ID, suppressing SQL parameters and PostgreSQL failing-row detail.

These are internal source primitives and transaction proofs, not an activated
client connection flow. Authenticated proposal/approval descriptors, client
installation, ordinary HTTP admission, session/effect integration, and exact-scope
live qualification remain separate prerequisites. Owner acceptance remains tied
to a PR preview and never requires reading code.

## Private client HTTP delivery

`POST /v1/agent/ordinary-agent-enrollments/{operation_id}/claim` accepts the
receiver capability in the Authorization header using the Bearer scheme. This
route does not run legacy terminal, operator, human-cookie, or Actions identity
resolution. It accepts no JSON body or query input. A missing, malformed, or
unavailable delivery uses a generic denial; errors never echo the submitted
capability or a keyring/persistence exception. A successful response contains
one private `credential` value and uses `Cache-Control: no-store`.

Only a private client adapter may consume that response. The exported agent
contract marks its sole supported surface as `private_agent_client` and requires
private response custody evidence. It is not an LLM-visible generic tool result,
operator UI response, or public agent context. The client must save the credential
privately before reporting redacted readiness, reject authorization redirects,
and reuse the same operation/receiver proof for delivery retries. Request logging,
tracing, ingress rate limits, cleanup scheduling, installed private client support,
and authenticated enrollment proposal/approval remain activation prerequisites.
The route creates no principal, session, policy, or grant on its own.

An exact operation review link can include `operation_id` on the existing
Engineering Ops privileged-operation page. The page reads that operation's
review directly and preserves server authorization checks; it does not require
searching the operation list. Ordinary delegation presentation and its
server-authoritative approval adapter are a separate part of client integration.

The enrollment preparation helper resolves an explicitly supplied nonsecret App
ID and managed-secret binding selector against current LP records. It selects
one exact ordinary-agent policy rule and the latest repository inventory, then
performs the supported read-only App/installation inspection. Missing selectors,
ambiguous records, and retired inventory do not fall back to another binding.
Rotation carries the current principal and custody predecessor into the reviewed
intent. Preparation creates no authority or credential; final apply still checks
these bindings in its transaction. Installation tooling must obtain the selectors
from verified setup facts rather than asking the administrator to type them.
