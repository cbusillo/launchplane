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
that existing challenge only when the session binding and all current derived
provenance still match; otherwise issuance conflicts. Verification updates only
the challenge's mutable state/attempt evidence and does not change immutable
challenge provenance, privileged-operation state, its event ledger, browser
approval, or execution authorization. The verifier remains `shadow`, inert, and
returns `authorizes_execution: false`.

## Conformance Artifact

`contracts/owner-control-contract.json` is deterministic and public-safe. It
contains the JSON schemas, canonicalization declaration, and byte/digest golden
vectors for every descriptor currently registered in
`control_plane.privileged_operation_registry`.

The artifact container is schema version `2`; the embedded approval, response,
binding, signature-payload, and envelope models remain schema version `1`.
Version `2` adds the signed-channel declarations and vectors while preserving
the legacy version-1 canonicalization, approval, response, and negative-vector
bytes.

Descriptor vectors derive all synthetic identity values from the descriptor ID,
so registering another descriptor does not churn existing vectors. Negative
vectors use one explicitly named public descriptor rather than registry order
and cover version, digest, timestamp, nonce, issuance/expiry ordering, exact
request binding, confirmation-time rejection, review-key uniqueness, and review
text normalization. Timestamp vectors cover both lexical UTC form and calendar
validity.

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
channel sessions, issued single-use challenges, and append-only verification
events. Enrollment and revocation are DB-backed; challenge issuance, expiry,
verification audit timestamps, and successful-consumption timestamps use the
database clock. Verification locks the enrolled session and issued challenge in
one transaction, so one exact valid challenge can verify only once. Each issued
challenge permits at most eight audited verification attempts; the eighth
non-terminal rejection closes the challenge as rejected, and later attempts
cannot append more events.

Challenge issuance accepts an exact `ApprovalRequest` only through an unrouted
service-internal storage API. This slice does not claim that arbitrary supplied
request fields are trusted or derive a challenge from a live privileged
operation; the database service replaces the nonce and timestamp bounds with a
server-generated nonce and database-clock values. The remaining
operation-binding and transport boundary requires a separate review before any
route exists. Unknown challenge nonces create no durable state.

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
