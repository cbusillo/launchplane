---
title: Owner-Control Channel Contract
---

# Owner-Control Channel Contract

Launchplane publishes a public, versioned owner-control serialization for a
future trusted host confirmation channel. It lets a host render the exact
server-authored review and return a challenge response without exposing owner
authority to model tools, browser automation, shells, Code Bridge, MCP, or
agent-controlled IPC.

## Canonical Contract

`control_plane.contracts.canonical_json` defines the shared UTF-8 JSON bytes:
object keys sorted by ascending Unicode code point, compact `,` and `:` separators, JSON
ASCII escaping, signed-64-bit integer-only numbers, string-only object keys,
rejection of floating-point and non-finite numbers, no trailing newline, and
lowercase SHA-256 digests. Timestamps use whole-second canonical UTC form. The
checked artifact includes
canonicalization edge vectors for primitive values, escapes, Unicode text, and
Unicode key ordering. The
existing privileged-operation digest helper delegates to that same function, so
its existing digest bytes remain unchanged.

`control_plane.contracts.owner_control` defines strict schema-version-1
Pydantic contracts for:

- `ServerReviewPayload`, the server-authored, display-ready review content;
- `ApprovalRequest`, which binds operation and descriptor identity, request,
  plan, evidence, pre-state, policy, immutable GitHub owner ID, review, nonce,
  issuance time, and expiry; and
- `ChallengeResponse`, which embeds and digests the exact request, records an
  approved decision, channel binding digest, and bounded confirmation time;
- `ChannelBindingRecord`, which binds one canonical channel-session interval,
  immutable owner ID, and raw Ed25519 public key; and
- `OwnerControlConfirmationEnvelope`, which carries the exact challenge,
  binding record, and an Ed25519 proof over the domain-separated signature
  payload.

Version, digest casing, canonical UTC timestamps and ordering, nonce form, and
duplicate review fields fail closed. Single-field constraints are present in
the exported JSON Schemas; cross-field constraints are also represented by
negative conformance vectors.

## Binding and Signature Proof

`channel_binding_sha256` is the lowercase SHA-256 digest of the exact canonical
JSON bytes of the `ChannelBindingRecord` payload. The payload is not wrapped,
prefixed, or newline-terminated. The record uses schema version `1`, a
canonical `channel_session_id`, an `owner_github_id` in the signed 64-bit
positive range, `signature_algorithm: "ed25519"`, an Ed25519 public key as raw
32-byte unpadded base64url, and canonical whole-second UTC
`session_issued_at`/`session_expires_at` values with expiry later than issuance.

`OwnerControlSignaturePayload` is the exact schema-version-1 object
`{"schema_version":1,"domain":"launchplane-owner-control-confirmation-v1","challenge_response":<exact ChallengeResponse>}`.
The signature is Ed25519 over its canonical UTF-8 JSON bytes. Signatures are
raw 64-byte values encoded as unpadded base64url. The envelope repeats the
algorithm, binds the response digest to the exact binding record, requires the
owner ID to match, and requires request issuance, request expiry, and
confirmation time to remain inside the channel-session interval. Verification
fails closed for malformed encodings, wrong keys, tampered payloads, and
cross-session substitution.

The retained `golden_vectors` are legacy v1 byte-compatibility fixtures; their
`channel_binding_sha256` values remain explicit synthetic placeholders and do
not have a `ChannelBindingRecord` preimage. Signed-channel consumers must use
`confirmation_golden_vectors`, whose binding digests follow the definition
above. The artifact's signature declaration marks this compatibility boundary
explicitly.

Signature verification proves only that the private key corresponding to the
public key inside the envelope signed the exact challenge response. The
shadow-verifier storage slice compares the exact canonical binding and approval
request bytes plus their server-stored digests against the enrolled
channel-session and issued challenge records. A self-asserted binding or
otherwise valid self-signed envelope cannot create enrolled or issued state and
is never authorization evidence by itself.

## Service-Only Challenge Issuance

The unrouted PostgreSQL storage API accepts only a channel-session ID, planned
operation ID, and bounded requested TTL. It locks the enrolled session, exact
planned operation, active policy read, and active challenge guard in that order.
The service derives every approval-request field, nonce, whole-second timestamps,
and deterministic review payload from those locked records; callers cannot
author request, evidence, policy, owner, review, or provenance fields.

Issuance requires exactly one active schema-v2 policy, a live enrolled session,
an unexpired `planned` operation, and one immutable GitHub-ID managed rule that
allows the enrolled owner under the descriptor's existing approval action.
Blocked managed-policy plans and unsupported evidence fail closed. Challenge
expiry is the earliest of requested TTL, operation expiry, and session expiry.
The review discloses only typed status, bounded counts, digests, and timestamps;
it never discloses secret/key IDs, desired policy bodies, raw logins or subjects,
token labels, planner errors, or free-text request reasons.

At most one `issued` challenge can bind an operation. An exact repeat returns
that existing challenge only while it remains unexpired, its expiry does not
exceed the newly requested bound, and the session binding plus all current
derived provenance still match. When locked issuance observes
`expires_at <=` the database clock, it atomically appends one deterministic
challenge-lifecycle event, changes the old row from `issued` to `expired`,
flushes that transition to release the partial one-active-operation guard, and
inserts the newly derived challenge in the same transaction. The lifecycle
event contains no envelope and consumes no verification attempt. At the exact
expiry boundary, issuance and verification serialize on the enrolled-session
and challenge locks: verification may consume first under its inclusive
confirmation-time contract, or issuance may expire first; the loser observes
the committed terminal state. Verification updates only
the challenge's mutable state/attempt evidence and does not change immutable
challenge provenance, privileged-operation state, its event ledger, browser
approval, or execution authorization. The verifier remains `shadow`, inert, and
returns `authorizes_execution: false`.

## Conformance Artifact

`contracts/owner-control-contract.json` is deterministic and public-safe. It
contains the JSON schemas, canonicalization declaration, and byte/digest golden
vectors for every descriptor currently registered in
`control_plane.privileged_operation_registry`.

The artifact container is schema version `4`; the embedded approval, response,
binding, signature-payload, envelope, and shadow-verifier record models remain
schema version `1`. Version `2` added the signed-channel declarations and
vectors. Version `3` adds deterministic server-state verification and reactive
challenge-lifecycle vectors while preserving every version-2 section at its
pinned canonical SHA-256. Version `4` adds separate enrollment-provenance
schemas, declarations, exhaustive caller-claim vectors, and negative storage
vectors. Its compatibility declaration pins every version-3 top-level section;
the existing wire, verification-state, and challenge-lifecycle sections remain
byte-identical. Consumers must reject unknown container versions.
The preserved `signature_declaration.contract_schema_version` remains `2`
because it identifies the unchanged signed-channel declaration; the top-level
schema and compatibility block are authoritative for the version-4 container.

Descriptor vectors derive all synthetic identity values from the descriptor ID,
so registering another descriptor does not churn existing vectors. Negative
vectors use one explicitly named public descriptor rather than registry order
and cover version, digest, timestamp, nonce, issuance/expiry ordering, exact
request binding, confirmation-time rejection, review-key uniqueness, and review
text normalization. Timestamp vectors cover both lexical UTC form and calendar
validity.

`verification_state_vectors` carry synthetic channel-session state, issued
challenge state, one confirmation envelope, a whole-second observation time,
and the exact inert shadow evaluation. Their rejection-reason set must equal
the complete `OwnerControlShadowVerificationReason` literal, and at least one
vector must verify successfully. `challenge_lifecycle_vectors` separately
replay the exact-boundary `issued -> expired` transition, preserving attempt
count and emitting no envelope digest or verification sequence. Tests rebuild
the strict models from the JSON payloads and replay both sections through the
real verifier and lifecycle functions, so adding or changing an outcome cannot
silently leave the shared artifact behind.

`provenance_vectors` exhaust every currently supported combination of principal
separation, key custody, and gesture-source claims. Every combination derives
only `self_asserted`, has `server_observed_corroboration: "none"`, remains
`inert`, and sets `authorizes_execution: false`. Negative provenance vectors
cover claim drift, unsupported fields and versions, attempts to raise trust
without corroboration, missing stored provenance, and rejection of every
published synthetic conformance public key at runtime enrollment.

Regenerate and verify the checked artifact with:

```bash
uv run launchplane service export-owner-control-contract --output contracts/owner-control-contract.json
uv run --extra dev python -m unittest tests.test_owner_control_contract
```

The vectors contain only synthetic identifiers, digests, nonces, timestamps,
and generic review strings. They carry no tenant, product, repository, domain,
operator, policy authority, session credential, or live endpoint data.

## Shadow-Verifier Storage

`control_plane.contracts.owner_control_shadow_verifier` defines server-state
records separate from the published wire contract. PostgreSQL persists enrolled
channel sessions, immutable one-to-one enrollment provenance, issued single-use
challenges, and append-only verification events. Enrollment accepts an exact
`OwnerControlHostPrincipalClaim` and writes the session plus provenance in one
transaction. The record repeats the exact canonical channel-binding bytes and
digest, stores the exact canonical claim bytes and digest, and shares the
session's database enrollment timestamp. Re-enrollment is idempotent only for
the exact binding and claim; any claim drift fails closed. A separate
append-only challenge-lifecycle ledger records reactive
non-verification transitions such as `issued -> expired` before re-issuance;
it carries exact challenge/session/operation identifiers, request and binding
digests, timestamp bounds, and inert non-authorizing markers without an
envelope digest or attempt sequence. Enrollment and revocation are DB-backed;
challenge issuance, expiry,
verification audit timestamps, and successful-consumption timestamps use the
database clock. Verification locks the enrolled session and issued challenge in
one transaction, so one exact valid challenge can verify only once. Each issued
challenge permits at most eight audited verification attempts; the eighth
non-terminal rejection closes the challenge as rejected, and later attempts
cannot append more events.

The closed claim values describe only what the caller says about principal
separation, software or hardware-backed key custody, and local gesture sourcing.
Launchplane implements no independent corroboration source in this slice, so
even `hardware_backed` or `separate_os_principal` cannot raise trust above
`self_asserted`. Published artifact keys are fixtures only and are explicitly
rejected by storage enrollment. Challenge issuance also refuses a session whose
provenance row is absent, and shadow verification refuses to consume an already
issued challenge if the bound session's provenance is missing.

Challenge issuance remains an unrouted service-internal storage API. It accepts
only the enrolled channel-session ID, planned operation ID, and bounded TTL,
then derives the complete `ApprovalRequest` from the locked operation, active
policy, enrolled owner, server-generated nonce, and database-clock bounds. The
remaining transport and trusted-host corroboration boundary requires separate
review before any route exists. Unknown challenge nonces create no durable
state.

Every stored event and returned result has `verifier_mode: "shadow"` and
`authorizes_execution: false`. The slice adds no HTTP route, authorization
action or grant, browser workflow, privileged-operation approval/execution
coupling, worker, outbox, filesystem store, signing key, or channel-host
implementation. Existing browser approval behavior remains unchanged until a
separately reviewed runtime adoption change proves the trusted owner-control
path.

`ChallengeResponse` represents successful confirmation only. Challenge expiry,
replay, mismatch, and attempt-budget rejection are shadow audit outcomes rather
than alternate signed response decisions. The checked artifact uses one
explicitly synthetic deterministic Ed25519 key only to make public conformance
vectors reproducible; it is not runtime authority, a credential, or a deployed
key.
