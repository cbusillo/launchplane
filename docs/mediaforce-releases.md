---
title: Mediaforce macOS release adapter
---

Launchplane owns the source-side macOS release adapter in
`control_plane/mediaforce_release.py`. Mediaforce owns application behavior and
work admission/process custody. [Release review](release-review.md) owns
acceptance; an agent's request never supplies acceptance or promotion authority.

## Source contract

`request_mediaforce_release` records a product and a full source commit through
the existing DB-backed mutation reservations. A trusted record-backed resolver
must prove that the commit belongs to the product's recorded default branch,
verify its immutable artifact and checksum, and pin a passing prior deployment,
its artifact/checksum and the database/host target. Inputs remain in the original
intent receipt. Reusing a key for a different commit conflicts; a replay retains
the original plan.

The system calls `run_mediaforce_release` with that recorded request. The worker
never accepts deployment commands, host paths, artifact identities or acceptance
from an agent. It rechecks the resolver's exact plan, active product profile,
production context, latest published Client acceptance, checklist digest and
candidate/prior versions. Held releases and overrides cannot start it. Standing
Director acceptance must already be recorded by Launchplane and still name the
admin named by the active policy; this adapter does not invent standing decisions.
The profile driver is `mediaforce-macos`. Rollback drills and temporary hotfixes
are not supported by this adapter and cannot silently become ordinary releases.

The durable provider runner serializes every request targeting one recorded
physical login-item runtime, including requests from different products. It
renews the lease and checkpoints each host mutation. The host independently
enforces the operation's fence token; losing the Launchplane lease cannot permit
a stale worker to replace a runtime or resume admissions.

The host boundary must:

1. Fence all new admissions without terminating active work.
2. Report fresh quiescence under that fence: the five Mediaforce database work
   domains, full/prefix scan jobs, pending publishing, publishing locks, remote
   worker custody and quiesced producers. Queued folder children count as work.
   Receipts may be at most 30 seconds old, allowing up to 5 seconds of forward
   clock skew between the host and worker; larger future timestamps remain unknown.
   Stale nonterminal scan rows require Mediaforce's supported reconciliation;
   Launchplane never repairs them from a count or guesses that a dead process
   means the row is safe. The read-only `/api/release/work` payload alone is
   insufficient even when every count is zero.
3. Capture and verify a consistent database backup bound to the same operation,
   database, fence and pinned prior runtime. Retain the backup's record and digest.
4. Recheck work and acceptance before replacing the immutable runtime, then
   verify its source/artifact checksum, health and post-deploy checks.
5. On a known failed post-check, replace the pinned prior runtime and qualify it
   before resuming admissions. This is compensation for the admitted effect and
   still runs when forward releases have since been held. Recovery never restores
   database data; source resolution must prove backward-compatible database use.
6. Store the terminal result and fence disposition durably, with an exact
   completion receipt. A lost finish response can adopt that receipt without
   replacing twice. A healthy process alone never proves the release completed.

Unknown evidence, an uncertain replacement/recovery, lease loss or failed recovery
keeps the target reserved for reconciliation, with admissions held. No automatic
retry or fence expiry may reopen it. A known pre-replacement refusal resumes
admissions and records `blocked`; after resolving the gate, submit a new request
key. A recovered release records `failed_recovered`, never `passed`.

## Activation and qualification boundary

This is a source adapter with deterministic host substitutes, not an installed
release path. The functions are not registered HTTP endpoints or a CLI; their
route strings namespace durable reservations only. No profile, credentials,
worker, login item, running database or runtime is modified by this source change.

Activation requires a record-backed resolver, a system-only macOS host transport
implementing the fence/custody/backup/completion contract, authenticated request
ingress and worker scheduling through Launchplane's supported service boundary.
Those bindings must be qualified before calling this a working release flow.
Keep [#3184](https://github.com/cbusillo/launchplane/issues/3184) open for that
integration and the observable release proof; source tests are not live proof.
