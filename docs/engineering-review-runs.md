---
title: Engineering Review Runs
---

Engineering review runs are a **shadow-only, non-authoritative evidence path**.
They do not satisfy merge, readiness, promotion, deployment, or product Owner
policy. Every record is fixed to `rollout_mode=shadow`, `authoritative=false`,
and `enforcement_effect=none`; no gate consumer reads an approved result as
admission evidence.

## Server authority

Policy administrators write revisioned DB-backed authority selecting the
repository, contiguous model slots, controlled worker runtime and host, absolute
Every Code executable, expected binary SHA-256, and lease. Compare-and-swap
writes retain retired history. Repository and runtime identities never come from
checked-in defaults.

Run creation accepts only `work_request_id`. Launchplane loads the stored
`EveryCodeWorkRequestRecord`, requires its completed linked PR, resolves the
exact current GitHub head and tree through authenticated server evidence, loads
the one active authority, and creates deterministic pending assignments. Until
issue #2000 provides server-derived routine classification, creation always selects
the first two contiguous, model-family-diverse slots.

## Worker lifecycle

The controlled worker lists and claims only pending assignments matching its
exact host and runtime. Claims, starts, failures, completion, and expiry use
row locks, compare-and-swap fencing, and database time. Credential handoff
decrypts the managed-secret envelope before claim mutation; retrying the same
claim returns the same scoped credential without exposing its hash or envelope.

Before launch, the worker verifies the configured absolute executable SHA-256
and requires the existing Every Code PR worktree to be at the exact GitHub head.
It invokes the executable with an explicit model and read-only review mode. The
reviewer environment excludes broad worker and GitHub tokens and receives only
the run-scoped completion URL and credential.

Reviewer completion accepts only credential, decision, bounded findings, and
summary. Repository, target, review tier, reviewer identity, model identity,
model family, and evidence digest are server-owned. Identical completion replay
is idempotent; conflicting replay and expired leases fail atomically.

Credential hashes, ciphertext, key identifiers, worker host names, and resolved
executable paths remain internal persistence fields and are excluded from
ordinary review-run reads and OpenAPI response schemas.
