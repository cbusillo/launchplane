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
  approved decision, channel binding digest, and bounded confirmation time.

Version, digest casing, canonical UTC timestamps and ordering, nonce form, and
duplicate review fields fail closed. Single-field constraints are present in
the exported JSON Schemas; cross-field constraints are also represented by
negative conformance vectors. Single-use nonce tracking, challenge issuance,
channel/session binding verification, and expiry enforcement at request time
require later storage and service work; this contract slice adds none of them.

## Conformance Artifact

`contracts/owner-control-contract.json` is deterministic and public-safe. It
contains the JSON schemas, canonicalization declaration, and byte/digest golden
vectors for every descriptor currently registered in
`control_plane.privileged_operation_registry`.

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

## Deferred Runtime Work

This is a behavior-neutral contract seam. It adds no HTTP route, storage record,
migration, frontend, worker action, authorization grant, source-kind change,
policy change, credential, channel implementation, or live runtime mutation.
Existing browser approval behavior remains unchanged until a separately reviewed
runtime adoption change proves the trusted owner-control path.

`ChallengeResponse` represents successful confirmation only. Owner rejection,
challenge expiry, and replay rejection are future audit events rather than
alternate signed response decisions. `channel_binding_sha256` is reserved for
the digest of the later channel-protocol binding record; its preimage and proof
mechanism remain deliberately undefined until that separately reviewed runtime
protocol exists. The current vectors use a synthetic placeholder digest and do
not claim to prove a live channel.
