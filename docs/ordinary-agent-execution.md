---
title: Ordinary Agent Execution Contracts
---

# Ordinary Agent Execution Contracts

Launchplane has a separate ordinary-agent credential, enrollment and session
boundary. It does not add ordinary credentials to the legacy service identity
union. Dedicated client routes propose connections and sessions; signed-browser
routes review, approve, cancel and revoke their persisted domain operations.
The service worker recovers approved enrollment and expires private delivery
capsules. This source integration is not a deployed installation or a merge
permission: ordinary-worker runtime qualification and the separately reviewed
activation package remain prerequisites to live execution.

The older `proposed_ordinary_agent_v1` evidence models and their pure eligibility
result remain inert fixture contracts. They do not authenticate a caller or
perform an effect. The generic authorization evaluator still ignores ordinary
rules; the dedicated gateway and joined lifecycle checks enforce them. Existing
human, workflow and operator identities retain their established behavior. The
new terminal enrollment proposer checks one current managed capability directly
under schema v2/v3 rather than coercing v3 through a v2-only generic helper.

Schema-v3 policy writes are not activated. The authoritative store rejects a v3
seed even when no active record exists, rejects v3 replacement records, and
rejects replacement, deletion, downgrade, or retirement when the observed
active record is v3. Managed reconciliation and candidate-preview paths can
construct read-only schema-v3 plans, including an explicit v2-to-v3 migration,
while preserving unrelated managed sets. Recovery and generated planners retain
the active schema in their desired set. The common apply path and storage still
reject schema-v3 persistence. This fence remains until a later activation slice
enforces verified schema/protocol compatibility and a rollback boundary.

The typed activation source slice persists a separate long-lived administration
record without enabling delivery. A human selects a server-resolved reviewed
policy package and current repository inventory through Engineering Ops. Setup
accepts either an explicit schema-v2-to-v3 proposal or, after a future migration,
an ordinary schema-v3-to-v3 reconcile. It recomputes the stored proposal and full
candidate digests, derives one ordinary-agent rule and target, and records the
installed schema and compiled handler/version support. The browser never asks the
operator to search for or type policy, inventory, activation, rule, digest,
issue, or pull-request IDs, and it derives the bounded audit reason from the
selected intent.
The server-authored review names the project and branch, delivery expiry and
remaining duration, checks-only starting behavior, and stop outcome. The UI
labels the shorter plan deadline separately as `Approve by` and keeps digests
and event provenance under collapsed technical details. Setup and stop choices
include server-recorded preparation or installation times so same-project rows
remain distinguishable without exposing rule IDs. Setup sends an absolute expiry
offered by the server clock, and the planner enforces a maximum of 30 days.

The first setup state is always desired `guarded` and effective
`qualification_only`. That record has `authorizes_execution = false`; it creates
no principal, credential, session, grant, provider request, worker registration,
or policy write. The schema-v3 write fence above remains unchanged. A later
readiness implementation must still join current policy authority, qualification,
protection and merge identity, protocol/schema compatibility, and cleanup
evidence before deriving effective guarded state.

Activation revocation is a new reviewed `revoke_activation` operation rather
than the generic pre-execution approval revoke. It writes terminal desired and
effective `revoked` states by exact revision/digest CAS. A later setup receives a
new activation ID and binds the exact predecessor. Expired guarded replacement
supersedes the old record and appends both changes atomically; revoked predecessors
never revive. Append-only operation-bound install/revoke events provide recovery
evidence even after later revocation or supersession changes the current
projection. Known custody cleanup and history remain maintainable, while this
slice still has no guarded execution consumer.

Ordinary rules participate only in structural policy normalization, managed-set
replacement and reporting. Their managed set and rule IDs remain semantic
because eligibility binds those exact IDs; changing either produces a missing
bound rule for existing leases. They remain absent from generic caller identity
and effective-access probe unions and never count toward human administrator
reachability or quorum.

Existing v1 and v2 policies omit the empty `ordinary_agents` field from
serialization and canonical hashing, preserving their prior payloads, digests,
and record IDs. Phase-one deployments therefore continue to persist only the
same v1/v2 records that previous service images can parse. A future persisted v3
record will be unreadable to those images, so they cannot be rollback targets
after v3 activation.

The earlier dormant enrollment request/review API remains a compatibility
surface. The new compiled client requests use the persisted enrollment domain
instead. Clients may propose, read their own operation, and cancel their own
issued session; no client route can approve or apply enrollment. Signed
administrator controls approve, and the service-owned worker applies enrollment.
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

These storage records do not grant execution. The enrollment recovery worker
constructs and applies the internal envelope only from a persisted, approved
enrollment and rechecks its exact policy snapshots. Clients cannot dispatch that
envelope directly. Production policy schema-v3 writes remain fenced pending the
separate activation package.

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

## Merge-train semantic effect seam

The existing privileged merge-train controller routes its provider mutations
through a closed, provider-neutral semantic effect protocol. The current legacy
executor maps those commands to the same GitHub client operations and preserves
the existing controller route, credentials, admission checks, checkpoints,
errors, and provider behavior. Landing commands carry the newly issued admission
record ID; it is provenance, not a replacement for guarded admission. The
head-refresh command has a legacy adapter, but the separate Level 1 worker
continues using its existing interface. It is not a controller run-once phase.

This seam is an internal refactoring boundary. It adds no ordinary-agent
executor, effect permit, budget reservation, job or scope record, route, policy
action, credential custody, or execution authority. Those integrations require
the authoritative lifecycle and custody contracts before they can be designed
against stable identities and state transitions.

The shared controller core accepts an explicit provider client and constructs no
credential or transport. The legacy entry point still constructs its established
token-backed client before invoking that core. Ordinary integration must provide
its scoped client and joined bound-record adapters; passing an ordinary bearer
to the privileged controller route is not an integration path.

The ordinary controller adapter filters planning reads to the exact job binding
and target. Acquisitions and checkpoints use joined storage operations; after a
progress successor is stored, a checkpoint rereads its authoritative record ID.
Ordinary wrapper builders include the binding before calculating their IDs.
Release uses a history-only fence operation, including terminal cleanup after a
worker crash, so cancellation does not require reacquiring execution authority.
This adapter alone does not register an executor or activate the worker stage.

The scoped candidate client consumes durable snapshot and check callbacks. It
prepares a job/revision-specific ref, then returns one completed merge step with
full structural progress at a time. Restart uses that persisted progress rather
than resetting the ref. It has no ambient provider transport; provider evidence
and mutations must come through the scoped callbacks and executor. Candidate
step coverage alone is not proof of landing, stack execution, or worker activation.

The ordinary job status API returns a typed, non-secret view to the current
ordinary identity for its own work or to a signed, current managed administrator.
The engineering UI accepts the exact principal/request link and shows waiting,
partial completion, cancellation and uncertain results without offering a second
execution action. Recorded effect counts are historical actions, not a forecast
or percentage of the remaining work. These read routes do not activate dispatch.

Merge admission can be built as an inert proposal before persistence. Proposal
construction performs the existing evidence and history reads; it does not write
an admission or dispatch. The legacy path immediately persists through its
existing controller fence. The ordinary path must instead atomically finalize
that proposal with its exact effect and dispatch checkpoint; this factoring alone
does not implement or activate that transaction.

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


## Session and finite-job storage integration

The session lifecycle calculations consume current principal, credential and
policy records. They do not authenticate a caller or authorize provider writes
on their own. The storage transaction must verify the issuer proof and the
receiver-bound approved delegation, resolve operation replay before calculating
issuance, and serialize session, lease and finite-request records. Reconnecting
to the same approved operation returns the stored session, including terminal
state; it does not renew its lifetime. Separate approved operations can coexist.

One finite request is one job. Historical schema-v1 requests remain byte- and
identity-compatible guarded-delivery records. Schema v2 is a discriminated JSON
payload: `guarded_delivery` retains the exact base, PR/head, stack-edit and
refresh scope, while `qualification` has no base, PR or stack-edit fields.
Version-2 intent and scope identities use a fixed v2 domain plus the schema
version and purpose, so the two purposes cannot share an identity. Qualification
requires a `preflight` lease with a positive finite action allowance, charges
zero PRs against that lease, and guarded delivery requires `guarded_merge`.
Existing merge, snapshot, landing and effect paths refuse qualification. The
separate qualification advancer is a dormant, direct-call-only source slice:
it has no ingress, dispatcher registration, worker assembly, activation record,
provider setup resolver, or production loop wiring.

The dormant advancer reserves a schema-v2 controller-free read attempt and one
read-only `merge_train_snapshot` custody lease. Its only provider observation is
one `GET /repos/{repository}/collaborators?permission=admin&per_page=100&page=1`.
It retains no collaborator list: exact ID plus casefolded login and literal
admin permission produces an attestation; a changed login, conflicting ID,
short-page absence, and full-page inconclusive result remain distinct terminal
outcomes. Malformed or incomplete responses alone use the finite retry path.
The first logical qualification attempt consumes one `preflight` action across
all binding revisions; retries never recharge it. Current authority is required
to reserve, mint, and read, while recording an already-issued response or cleanup
uncertainty remains possible after expiry or revocation. A positive response is
not complete or readiness-valid until its custody attempt has closed.

Admission charges a guarded request's PR count once against the lease; effect
reservations spend the action allowance. Existing-job checks do not spend
admission capacity again. A bounded guarded refresh preserves the original PR
and stack-edit scope and consumes the original refresh allowance with a
binding-revision compare-and-swap. Cancellation does not refund capacity.

The internal dispatcher can continue an already admitted job only under its
original explicit finite continuation grant. The interactive session and lease
remain expired; this path cannot create new requests or renew either record.
Current policy, current credential version, revocation, the original budget
window and the finite deadline still apply. Cancellation of an unknown provider
effect retains its durable reconciliation fence and execution-record links.
The joined storage implementation persists these records, validates current
issuer provenance and serializes admission budget updates. It does not register
an ordinary HTTP route or dispatch provider effects; the separate semantic-effect
gateway must reauthorize within its own reservation transaction.

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

These source primitives and transaction proofs now have dedicated client and
browser adapters. Installed private client support, ordinary effect/admission
integration, and exact-scope live qualification remain separate prerequisites. Owner acceptance remains tied
to a PR preview and never requires reading code.


## Authenticated session approval boundary

Initial enrollment can include optional `session_attenuation`. Its whole stable
intent, including the finite session bounds, must first receive authenticated
administrator approval. The domain operation row records the canonical approved
intent; enrollment validates that exact row, digest and immutable administrator
identity before issuing the session alongside the credential. Adding or changing
attenuation after approval fails. Absence preserves the previous issuer intent.

An already enrolled client proposes a fresh bounded session using its ordinary
credential. The service stores the exact proposal and private credential proof
provenance. The browser approves that stored operation using its signed human
session and CSRF token; it never receives the ordinary bearer. The application
adapter verifies signature, CSRF and existing claims currency. Storage then locks
and rereads the human session, rechecks the current exact administrator policy,
and verifies the current ordinary principal/credential and original proposal.
Session issuance and domain approval commit together. Reuse of an operation with
a different intent fails deterministically; historical replay returns the stored
session, including cancellation, without renewal. No credential redelivery or
policy write is required for another approved session.

The domain session operation is the sole approval authority. Future generic
operation views must project it rather than maintain a second independent
approval. Initial issuer preparation must consume its exact approved intent and
respect the original absolute session deadline; it must not reset deadlines from
worker execution time. Dedicated client and browser routes below invoke these adapters. They do not
activate schema-v3 policy writes or ordinary provider effects.

The session lock order appends domain operation, session, lease and finite job
after the issuer's policy/principal/credential/delivery locks. Browser approval
locks the human session first; existing human logout/CSRF mutations lock only that
row. Rotation, principal revocation and unclaimed credential expiry cancel all
matching sessions and pending jobs in the same transaction. Dispatched unknown
effects keep their reconciliation state and execution references.


The initial proposed intent contains planned credential identity/lifetimes only,
with no generated credential hashes or assumed administrator. The actual browser
approver supplies verified identity; the domain transaction derives the exact
current managed administrator binding. The private approved-intent read gives the
worker a canonical issuer-intent digest. Only after approval does the issuer
generate a credential; constructing the apply envelope checks its identity and
lifetime against that plan. Standalone enrollment without a session follows the
same stored approval protocol and creates no dummy lease. Activated adapters use
`apply_approved_ordinary_agent_enrollment`, whose typed locator is verified against
the persisted domain approval inside the issuer transaction. The original issuer
foundation primitive remains an internal unregistered compatibility boundary.

Public operation views contain original requested scope, finite bounds, credential
ID/version and diagnostic status only. They exclude proof/receiver/approval hashes
and private payloads. `approved` and `applied` describe historical records, not
permission to dispatch; expired, revoked and current-policy-blocked status remain
distinct. Request replay likewise returns history without new writes or authority.


Administrator lifecycle controls use the same signed human session, CSRF, and
current managed administrator checks as approval. Cancelling a pending request
also cancels an approved enrollment that has not been applied: its durable
operation tombstone fences recovery and final issuer apply. An applied operation
cannot be labelled cancelled. Revoking one session cancels its leases and finite
jobs; disconnecting a principal revokes all of its credentials and sessions through
the atomic lifecycle writer. Neither control depends on a still-present ordinary
agent rule or a valid ordinary bearer. Unknown execution outcomes retain their
reconciliation fence. Disconnect audit bindings are derived from the actual
human request; authority is rechecked against the locked human and current policy
inside the mutation transaction, never conferred by an audit hash.

The public operation view distinguishes the authenticated terminal requester from
the recipient credential, and displays the reviewed credential/delivery deadlines
even when no session was requested. An authenticated terminal may reconnect to
its own initial proposal; ordinary clients and human administrators retain their
separate read paths. Private bounded worker discovery returns only approved,
unapplied, uncancelled, unexpired operation references. Discovery does not
replace authoritative read and final apply checks.

Recovery discovery accepts a keyset cursor over principal and operation IDs, so
an operation that repeatedly fails current authority checks cannot starve later
approved requests. The worker advances the cursor and wraps on an empty page;
a process restart may replay discovery without renewing any authority. Disconnect
operation IDs are derived by the store from the event and exact principal
prestate, separate from enrollment operations. Only enroll/rotate audits count
as an applied initial proposal.

A retry after successful disconnect first revalidates the current human and
administrator authority, then returns the already-revoked principal status. It
creates no new principal revision, revoke audit, or operation ID from the changed
prestate. The public result is current revoked state, not a reusable authority
receipt.

## Private client HTTP delivery

An initial proposal's client `operation_id` is a retry key scoped to its principal.
The service derives a globally scoped enrollment ID from that pair and returns
it for review, status and private delivery. Clients retain the original request
and retry it unchanged if the response was lost: exact authenticated replay
returns the committed operation before provider inspection or credential
preparation, including after apply or expiry. A changed request using the same
key is rejected. No client must compute a hash or search for an operation.

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
tracing, ingress rate limits, installed private client support and qualification
of the deployed cleanup/worker image remain activation prerequisites.
Run schema adoption through the session migration before starting the new worker
image. Its startup probe requires the enrollment, delivery, session and inventory
relations; a worker started against older schema refuses all privileged polls
until schema adoption completes.
The route creates no principal, session, policy, or grant on its own.

An exact operation review link can include `operation_id` on the existing
Engineering Ops privileged-operation page. The page reads that operation's
review directly and preserves server authorization checks; it does not require
searching the operation list. An ordinary request adds its opaque `principal_id`
and opens the domain-backed agent review directly. It shows authenticated
requester, exact repository, current policy ceiling, requested session limits and
absolute deadlines without exposing code, digests, or credentials. Missing bound
policy means no engineering execution enabled; it is not inferred read-only access.

The enrollment preparation helper resolves an explicitly supplied nonsecret App
ID and managed-secret binding selector against current LP records. It selects
one exact ordinary-agent policy rule and the latest repository inventory, then
performs the supported read-only App/installation inspection. Missing selectors,
ambiguous records, and retired inventory do not fall back to another binding.
Rotation carries the current principal and custody predecessor into the reviewed
intent. Preparation creates no authority or credential; final apply still checks
these bindings in its transaction. Installation tooling must obtain the selectors
from verified setup facts rather than asking the administrator to type them.


## Client proposal and recovery protocol

`POST /v1/agent/ordinary-agent-enrollments` accepts the compiled
`ordinary-agent-enrollment` request from an authenticated terminal client with
one current managed `ordinary_agent_enrollment.propose` capability. Operator or
administrator credentials do not substitute for that client identity. The
request contains setup selectors and absolute lifetimes, never administrator,
approval or provider-proof fields. It returns only its own public operation view
and a relative exact-review URL. The same requester can reconnect through
`GET /v1/agent/ordinary-agent-enrollments/{principal_id}/{operation_id}`.

An existing ordinary credential uses
`POST /v1/agent/ordinary-agent-session-proposals` for a bounded session and the
corresponding operation GET for status. Its cancellation endpoint cancels only
its issued session. These routes never run legacy credential resolution. Signed
browser decisions use `/v1/ordinary-agent-operations/{principal_id}/{operation_id}`
with approval/cancel suffixes; session revoke and whole-principal disconnect have
distinct routes and controls. Current immutable administrator authority, signed
cookie, origin/fetch metadata and CSRF are required for every browser mutation.
The browser receives public views, never private issuer or session write sets.

The existing privileged-operation worker scans authoritative approved initial
operations, checks their original deadlines and exact current policy before
preparing issuer material, and calls domain-backed joined apply. Its cursor
advances on failures and wraps on an empty page; one blocked page cannot pin
later requests. Per-operation failures report counts only and do not alter the
generic worker's failure threshold or heartbeat. Existing reviewed delivery
cleanup keeps its independent failure/backoff behavior. There is no public
execute route, second approval store, new scheduler, or post-commit-only wake-up.

The shared private helper/CLI/Lab installation remains separate: it must generate
and retain receiver proof privately, save the claimed credential atomically
before reporting readiness, reuse finite approved sessions, and handle bounded
status retries without GitHub polling. No installed-consumer or live usability
claim follows from these source and controlled browser/HTTP checks alone.

## Durable ordinary execution records

The private ordinary storage boundary separates a finite job claim from authority
to call a provider. Claims coordinate workers; controller acquisition, progress
successors and each effect checkpoint independently join current policy,
credential, session, lease and finite request. An already reserved last action
remains usable while its original authority remains current; reserving another
action requires remaining capacity. Terminal effects retain their action ordinal.

A semantic effect owns append-only dispatch attempts and response or reconciliation
history. A repeated checkpoint cannot authorize another provider call. Unknown
responses remain visible after cancellation. Reconciliation uses current preflight
authority and bounded observations; it cannot revive the old session. Only exact
stored head-refresh evidence may update the request binding, without resetting
its deadline or spending a second action.

Normalized provider snapshots and candidate-check observations have durable read
attempts. Source snapshots with pending/unknown checks or unknown mergeability
retain their successful response but wait for bounded re-observation; only decided
source evidence can be replayed into planning. Source observations have a separate
nine-observation budget, preserving the three-attempt provider-failure budget.
Candidate pending and unknown results share their existing nine-observation cap.
Both use the finite candidate-check backoff within the original request deadline.
Missing required-check configuration terminates the read instead of polling forever.
Token cleanup uncertainty fences subsequent work while retaining the immutable
response and its readiness decision; confirmed cleanup restores that decision and
its retry time, never promoting pending or exhausted evidence to ready. Candidate
observations bind protection to the accepted source snapshot. Provider quota waits
are shared monotonic deadlines for their actual quota identity, independent of job expiry.

Ordinary controller and candidate/landing/collapse records carry an explicit job
binding. Generic writers cannot adopt them. Joined progress successors preserve
history; terminal history cleanup retires only that job's database lineage and
retains unresolved provider fences. Candidate refs are job-and-binding scoped and
are retained when the provider offers no conditional delete.

Landing preparation reserves one action and custody attempt before bounded
provider observation. Checks identify the tested candidate commit; the individual
source PR has separate head and diff evidence. Finalization rechecks current
authority, controller ownership and remaining time, then commits the admission,
effect, first dispatch attempt and consumed preparation in one transaction.
Failure rolls back those records together. A repeated finalization returns history
and never authorizes another provider call, including after the original lease
expires. Provider calls happen outside the transaction.

The landing evidence envelope includes every recorded candidate entry, including
merged or skipped predecessors, while its queue snapshot contains only the
remaining entries. Preparation rejects more than four entries before charging
an action or reserving custody. File-change and contributing-identity readers
share the existing GitHub parsing rules; a head SHA alone is not sufficient to
reuse mutable PR diff or authorship evidence.

The provider transport supports a minimum remaining-time requirement for each
read. Landing acquisition must reserve 61 seconds before each file/commit page
and 46 seconds before the final batched identity confirmation, within the
original 75-second work window. Finalization and dispatch retain their separate
30-second minimum. These checks use the earlier work or token deadline and never
renew it. Failed requests and reported GraphQL costs, including partial responses
and queries exceeding the per-query limit, remain in quota accounting. The
complete custody orchestration and fleet budget proof remain integration work.

The prepared landing reader batches initial identities and final confirmation,
reads the combined candidate's checks against both classic and evaluated branch
rules, and obtains each entry's files and contributing identities through the
same deadline-bound transport. Missing required checks remain unavailable. Its
queue snapshot reports source-head checks as unknown because only combined
candidate checks were queried. Terminal predecessors retain immutable commit
evidence even when their source branches have been deleted. Confirmation proves
identity stability; it does not renew the original protection observation.

The finalized landing dispatcher supports merge commits and consumes only a
newly created finalization. It sends one PR merge request through that same
transport and requires the current base ref, result tree and ordered parents to
match the approved step. Storage independently checks the result tree against
the consumed preparation. Definite provider rejection and a pre-dispatch
deadline remain distinct from a lost response or unproven merge result. Replay
does not dispatch, and the generic ordinary effect executor cannot reacquire
custody to land. The inactive worker composes these joined operations; supported
installation and runtime qualification remain required before it is enabled.

These internal records and tests do not activate ordinary execution or establish
installed-client or production usability. Runtime wiring must retain the same
joined boundaries.

A reconciliation read that proves a different merge tree or ordered parents is
retained as a terminal conflict with its specific reason. It never counts as
successful landing and is not repeatedly retried as an unrecorded observation.
Preparation custody IDs use the supported token helper's idempotency identity,
so issuance stamps and cleanup closes the original reservation. Enrollment
builds its declared profiles and permissions from the shared provider contract;
it still rejects provider permissions outside that contract.

The internal effect-history reader returns the latest dispatch and immutable
outcome/reconciliation evidence from one SQL snapshot without a controller write
lock. It grants no provider access and remains available after session
cancellation. The fresh-landing orchestrator refuses reservation replay before
minting, retains the original issuance deadline, and persists the landing and
progress before token revocation. A completed landing with uncertain cleanup is
recovered through that history; it is never sent again to recover a return value.
Quota waits retain their own reason. If preparation cleanup also fails, both
errors remain visible rather than reporting a clean deferral.


Internal effect history includes both dispatch outcomes and completions that
required no dispatch. For an already-present stack label, the scoped executor
records its typed provider observation before creating any dispatch child.
Candidate references retained because conditional deletion is unavailable also
have a durable completion. Recovery reads these records without minting another
token or sending the semantic operation again.


Ordinary installation-token minting authenticates the managed App key with the
mandatory JWT-authenticated repository installation lookup. Its required positive
`app_id` must match the locally configured JWT issuer before minting; a failed
lookup never falls back to a cached installation or another identity. The extra
`GET /app` is omitted only for ordinary minting. Enrollment inspection and legacy
token paths retain their own identity checks.

Within each landing observation, unresolved queue-author roles share one bounded
`collaborators?permission=admin&per_page=100&page=1` read. An exact numeric user ID
and case-insensitive login match with boolean `permissions.admin == true` supplies
positive admin evidence. Missing users and malformed individual items remain
unknown; no second page, per-user fallback or cross-observation role cache is used.
The actual ordinary installation/profile must qualify this endpoint during the
existing activation package; source tests do not establish deployed capability.

The [six-repository qualification](../tests/test_ordinary_agent_six_repository_qualification.py)
drives two scan clients through the real inactive worker and shared store. Each
repository has four provider-side PRs and one finite request naming exactly two;
the unbound PRs receive no provider reads or mutations. The immediate-success
NORMAL run measures 294 raw provider calls and 48 reported GraphQL points, with
maximum query cost 1. Its seven custody leases per repository include installation
lookup, token mint and confirmed revocation. Five provider mutations and one
retained-delete completion consume six actions per request, with two PRs used and
no refresh. All custody is closed on completion.

The test asserts the 300-call/120-point ceilings rather than requiring equality
to that measured total. Its ledger records failed requests as well as successful
calls, including App and installation-token traffic; recovery journeys have
separate ledgers. Retained candidate-ref cleanup has no provider request or token
lease. A deterministic clock measures full processed polls under controlled
provider latency. This proves the composed source workload, not a hard wall-clock
SLA under arbitrary database or network delays.

Separate fault journeys exercise a quota wait while another installation completes,
permission denial without a false quota wait, restart after a lost progress
checkpoint, and read-only observation after a lost merge response without resend.
They also cover expiry before dispatch, session revocation preserving unresolved
history, and genuine containment followed by the next landing and later cleanup.

NORMAL uses non-strict required checks and truthful ancestry after the first
landing. It changes no live protection and does not prove the real strict
multiple-PR journey, which can require head refresh and readmission. Live
installation access, runtime activation and engineering/owner-preview delivery
remain separate qualification gates.

Concrete controller snapshot and candidate-check readers query exact repository,
base and immutable head identities, provider protection, and each relevant
commit's own checks. A single observed base policy is evaluated against each
source head without repeating its rules request. Initial queue checks and base
freshness remain separate: passing CI on a stale strict branch requests a refresh,
while unavailable source checks remain unknown and cannot start planning.
Candidate checks bind to the candidate SHA and never reuse source-head results.
Landing admission may evaluate them later within the existing 45-second evidence
window; it retains their original observation timestamp and digest. Expired
evidence yields a deadline wait rather than being restamped as fresh.
These readers supply the existing custody acquisition adapters, including bounded
pending-observation recovery, in the inactive worker assembly. Source qualification
does not register that callable with the runtime scheduler.

Restart recovery interprets the latest durable dispatch and its immutable outcome
before selecting more work. Completed or reconciled proof is replayed without a
provider call; a missing response or accepted asynchronous action requires
observation, and a known provider rejection or conflict is terminal. Proven local
non-dispatch or an exact no-effect observation may use the existing finite retry
budget; uncertain and accepted-asynchronous outcomes cannot. A fresh classification
is only a routing decision and still requires the joined authority/custody checks.
Candidate no-op recovery requires exact containment of the requested head at the
unchanged rolling parent. Retained candidate refs and already-present labels keep
their distinct completions without fabricating a dispatch.

Synchronous label/close completion history is a command-bound acknowledgement,
not a new assertion about current provider state. Commit-changing effects retain
exact commit proof. Uncertain label/close results require typed read observations;
recovery does not invent missing responses. Missing or inconsistent completion
proof produces a terminal recovery decision. Child/effect binding mismatches
remain explicit reader failures.

The reconciliation reader uses only GET requests through a custody-scoped read-only
transport and an installation token attenuated to contents and pull-request reads.
It reads the latest dispatch history before minting and appends one
observation before revoking the token. Completed history replays without another
mint. Recovery uses a currently authorized scoped read credential, including after the
original session/request expires. New mutations retain their original expiry gates.
Each read remains bounded by token expiry, per-attempt work allowance, the existing
observation/backoff limits and custody-mint caps.
A proof read failure after a mutation also retains uncertainty: its HTTP status
is not a rejection of the preceding write. Existing controller error handling
keeps this provider failure eligible for recovery; it cannot resend the mutation.
A failed reconciliation read appends a typed incomplete-read observation and retains uncertainty;
a later attempt may observe but cannot resend. It consumes the existing three-read
observation budget and backoff, so failed reads cannot create an unbounded mint loop.
If a read token is confirmed closed but its observation append was lost, the store
records an incomplete read before permitting another attempt. Unknown token cleanup
continues to fence new issuance.
Ref/commit/containment evidence uses the captured immutable SHA, comment scans stop
at three pages, and a full label page cannot prove a missing label. Installed
runtime qualification remains separate from the controlled source workload.
Landing reconciliation also captures the target base ref's exact commit and tree.
It proves containment against that immutable commit before completing the effect;
a merged PR alone, or a base that does not contain the merge, remains unproven.
These reads use the existing observation, custody, deadline and quota limits.


A yielded ordinary job keeps a private controller checkpoint on its existing claim
row. The store captures only an active tip with the exact current job binding,
then clears shared step and reconciliation data. Another job may use the target
only when every foreign tip has a matching released checkpoint and its provider
effects, preparations and custody are settled. Expiry does not grant new mutation
authority; resumed work still passes the current chain and lease checks.

Resume restores that job's saved progress under a fresh controller lease. Completion
uses its own checkpoint even if another job now owns the controller, and retires
only its own progress. Invalid or old-revision checkpoints remain blocking evidence.
Generic request refresh refuses parked or active progress until it is retired.
Actionable readiness uses the current controller's exact binding; parked progress
remains visible in history. These storage and projection changes do not assemble
or activate the ordinary worker, renew a finite admission, or resolve changed-base
landing evidence.

Ordinary snapshot, mutation, landing, and reconciliation transports observe
provider quota evidence. Credential discovery and mint requests use the App
quota identity; token revocation uses the actual installation identity. Successful
HTTP response headers are observed before JSON validation, including GraphQL
rate-limit errors with null data. Observation does not add requests or double
count costs, gate in-flight proof or revocation, or change the provider outcome.
Best-effort persistence cannot replace that outcome. A quota-shaped mutation
failure remains outcome-unknown even without a usable future reset deadline.
Primary and secondary constraints are both retained when one response carries
both; secondary waits share the secondary resource key across request types.
Valid provider deadlines are never shortened. The existing quota guards report the latest applicable deadline
through `retry_not_before`, including all known App and installation keys. The
worker must persist that deadline as its next due time; adding the exception
field alone does not implement scheduling. Installation identity must be known
before custody reservation to avoid charging an attempt for an existing wait.

The ordinary controller client has an optional one-entry landing callback. Each
call persists the exact successor before returning, retains the proven rolling
base across completed entries, and rejects inconsistent recovery state and
unassembled or unsupported stack-collapse paths. The merge callback owns joined
admission and durable dispatch intent. Before a normal landing step, the client
uses the existing admission-phase checkpoint to bind the selected PR to the
controller. The legacy provider checkpoint hook does not establish ordinary-agent
authority.
Already-contained entries require the separate joined no-op callback described
below. Retry admission uses the bounded fresh-attempt path; runtime activation
remains a separate operation.

New provider-inspected enrollment carries the discovered installation ID into
the target-specific custody record. Absent IDs are omitted when serializing old
records, preserving their existing digests. A known installation quota wait is
checked before custody reservation charges an attempt. Discovery must still
match that expected ID before token minting; a mismatch closes the unissued
attempt and requires fresh supported enrollment or rotation. Expected identity
is separate from actual token evidence and never proves a token was issued.
The new worker must require an inspected installation binding before activation;
legacy decoding alone does not qualify an older principal for that worker.

The shared controller returns `landing_progress` for ordinary `mode=land`
results: `partial`, `cleanup_pending`, then `complete` after semantic cleanup.
Blocked admission or recovery results use their explicit blocked reason instead.
A terminal active landing remains selectable until cleanup; job completion then
yields its controller and supersedes its own progress through the joined store.
This result contract does not itself assemble the production worker.

Creating an ordinary landing plan supersedes its candidate as the active
coordination record. The active landing may still use that exact historical
candidate as evidence: the binding, candidate ref, policy and immutable digests
must all match. A bounded exact lookup rejects ambiguous dependencies. The
candidate is never revived as active work, and generic controller behavior still
requires an active candidate. Current controller and admission gates continue to
decide whether the landing may advance.

Completed ordinary landing history can restore a missing landing outcome or
progress checkpoint without provider work. Creating a missing exact landing
outcome requires a stored base-ref proof or a merged-PR observation with an exact
base-containment witness. If the observed base is ahead of the merge, the outcome
retains its actual SHA and tree separately from the merge commit. Legacy PR-only
completion proof remains insufficient and does not trigger another provider write.
Recovery checks the exact admission
scope before interpreting missing outcomes and reuses an existing matching
terminal outcome. It resolves the latest attempt in a bounded linear preparation
history, so a successful second dispatch replays against its own admission rather
than the first attempt's admission. The current joined progress checkpoint still
owns authority to persist the successor.

The ordinary semantic router reserves each invoked command by its kind and its
stable position in the finite request. It dispatches through the existing bounded
executor or replays validated completion without provider work. Unknown effects
and head-refresh rebinding return typed recovery requirements for a separate job
poll; the router never reconciles and resends in the same call. A later job poll
completes an exact proved head refresh by updating the finite request binding,
completing the effect, and yielding the empty controller in one transaction. It
spends one existing refresh allowance while preserving action and dispatch charges,
scope, and deadlines. Active progress or a parked checkpoint blocks this rebind;
failures release the controller acquired by this attempt before returning. The
next claim reads the new binding without another delegation approval. Landing
remains in the joined landing path, and stack methods are excluded from this router.

A landing dispatch may retry only after an explicit pre-request deadline outcome,
with no reconciliation history and conclusively closed custody. Unknown effects,
provider rejections and reconciliation-derived no-effect classifications never
permit a landing resend. The next poll reserves one successor preparation and
rechecks current authority, the exact plan/head/rolling base, fresh provider
evidence, and a fresh admission before dispatch. A gracefully terminal preparation
with a provider wait, deadline or evidence denial can follow the same bounded path;
crashed reserved/observed attempts remain blocked. Every successor consumes an
attempt from the existing cap while retaining the original action charge.
Gracefully terminal preparations between consumed attempts retain the lineage's
original effect; preparation ordinals and dispatch ordinals count independently.
Exhausting the preparation cap blocks further attempts even when dispatch budget
remains.

Consumed retries reuse one immutable semantic effect and command digest. Each
child has a distinct preparation, custody attempt and admission binding; the
command's embedded admission remains the first dispatch's compatibility value.
The pre-send outcome atomically projects a truthful `dispatch_not_attempted`
landing outcome for that attempt, allowing the latest-admission gate to consider
a fresh proposal. The joined finalizer advances the dispatch and custody counters,
consumes the successor, and restores the effect to dispatching together. Generic
dispatch/custody entry points cannot authorize landing. Replay never sends, and
neither this retry path nor successful readmission activates the worker.

If the initial provider snapshot finds changed source heads or a changed base,
it records the complete ordered tuple as immutable readmission evidence before
reading rules or repository roles. After custody cleanup and the core's confirmed
empty-controller yield, a joined finalizer may consume that evidence and rebind
the request. This path requires no effects, landing preparations, execution links,
active progress, or parked checkpoint anywhere in the request history. It preserves
the original scope, budgets, and deadlines and spends one existing refresh allowance.
A later acquisition must observe the source again; only the same controller fence
may replay an unconsumed observation. A changed, closed, or merged PR observation
is evidence only: the fresh-binding normal admission and exact-containment checks
still decide whether the next step may proceed.

The internal job recovery snapshot checks the exact live job claim and reads the
current binding, reserved or uncertain effect history, latest open or terminal landing preparation,
custody uncertainty, and applicable read/provider deadlines together. PostgreSQL
uses a fresh read-only repeatable-read transaction; SQLite rehearsal uses an
explicit read transaction. The read neither acquires a controller nor renews
mutation authority, so recovery remains possible when the original request has
expired or been cancelled. Every subsequent write still checks its own current
authority and claim. Historical counts cover the request; actionable history and
read deadlines belong to its current binding.

The inactive ordinary job advancer uses that snapshot before acquiring a
controller. It can perform one uncertain-effect observation and then yield,
without continuing to mutation in the same poll. Mutation assembly requires the
inspected installation and current policy and repository identities. A normal
controller result, or an expected error mapped to a wait, is returned only after
the acquired lease has a confirmed joined yield. Failed yield remains an error
for claim recovery. Completion requires a fresh recovery snapshot with no
unresolved effect or custody uncertainty. This callable is not registered with
the service worker and does not activate delivery.

For a proven `no_op_already_contained` candidate step, the ordinary landing
executor uses one charged preparation and custody lease to read fresh evidence.
It retains the actual PR lifecycle, including a closed or merged PR whose only
queue ineligibility is that lifecycle. Current Owner, engineering, technical,
policy and controller gates still apply. The executor arms a one-use progress
route, then invokes the existing controller checkpoint. The route builds the
admission after that checkpoint renews its lease and commits the admission,
zero-effect outcome, exact skipped successor, predecessor supersession and
preparation consumption together. It creates no dispatch child or landing effect
and sends no merge request. A later poll loads the committed successor; recovery
does not create a second progress record to recover a lost return value.
An expired provider window closes the preparation with its deadline reason and
returns a bounded wait after confirmed controller yield. Fresh authority or
evidence changes use the existing blocked-admission path; a failed yield,
missing historical proof, conflicting successor or persistence failure remains
an error. None of these outcomes reuses a consumed admission or authorizes a
new provider attempt.
