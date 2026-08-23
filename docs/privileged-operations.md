---
title: Human-Governed Privileged Operations
---

# Human-Governed Privileged Operations

Launchplane owns a typed privileged-operation boundary for sensitive control-
plane work that cannot safely be delegated through static administrator
credentials. The boundary is separate from Owner Acceptance, Agent Write
Intents, workflow authorization, and ordinary operator mutations.

## Approval And Execution Boundary

Planning remains typed and dry-run-only. Phase 2 adds a separate finite human
approval and a service-internal worker; it adds no HTTP execute route, execute
action, static execution credential, or agent execution path.

- Descriptor IDs, versions, safety classes, request schemas, evidence schemas,
  and planners are compiled into the service registry.
- Registry validation runs at import/startup and prevents service startup when
  a descriptor or action-safety classification is invalid.
- The first descriptor is `managed-secret-reencryption` version 1.
- Its planner calls the existing managed-secret re-encryption computation with
  `apply=False` only.
- The Engineering UI lists redacted human evidence and may offer browser-human
  approve/revoke controls. It never offers an execute control.

The registry is not an arbitrary route, command, SQL, or payload proxy. New
descriptors require code, schemas, tests, documentation, and review.

## Authorization

Privileged-operation routes do not use bare `LaunchplaneAuthzPolicy.allows`.
They require active schema-v2 policy and exactly one matching managed rule with
both `managed_set_id` and `managed_rule_id`. Unmanaged rules, including rules
with an empty action list, cannot authorize the routes.

The human routes use a named GitHub-human browser dependency that:

1. authenticates the Launchplane GitHub session;
2. enforces same-origin/fetch-metadata and single-use CSRF checks for writes;
3. rejects bearer, GitHub Actions, terminal-agent, local-operator, and local-
   admin identities before policy evaluation.

The planning and approval actions are:

| Action | Safety | Surface |
| --- | --- | --- |
| `privileged_secret_operation.plan` | `secret_backed` | GitHub-human plan creation |
| `privileged_secret_operation.read` | `secret_backed` | GitHub-human plan reads |
| `privileged_secret_operation.cancel` | `secret_backed` | GitHub-human cancellation |
| `privileged_secret_operation.approve` | `secret_backed` | GitHub-human browser approval |
| `privileged_secret_operation.revoke` | `secret_backed` | GitHub-human browser revocation |
| `privileged_operation_summary.read` | `read` | Counts-only agent projection |

Approval requires exactly one managed GitHub-human rule that is pinned to
non-empty immutable `github_ids`. Login, organization, team, or role selectors
may additionally narrow approval at approval time; execution reauthorization
uses only the immutable GitHub ID and exact rule scope. Code landing adds no
policy rule or grant. The routes fail closed until a later, separately approved
DB-native managed-rule activation. GitHub secrets,
workflows, borrowed identities, and local-admin bearer credentials are not
bootstrap paths.

## Records And Lifecycle

`PrivilegedOperationRecord` is the current operation projection and
`PrivilegedOperationEventRecord` is the append-only lifecycle ledger. The
states are:

```text
planned ──► approved ──► executing ──► executed
  │            │              └─────► execution_failed
  │            └─────► revoked
  └─────► expired
```

Terminal records cannot reopen. Approval binds descriptor/version, normalized
request and evidence digests, plan and pre-state digests, the exact active
policy record/revision/SHA/source, managed rule IDs, immutable approver ID,
expiry, reason, and rollback class. Every transition is replay-safe by source
event ID; PostgreSQL locks the operation row and atomically appends the event
and updates the current projection.

Reads reconcile overdue `planned` and `approved` records to `expired` with a system-authored
terminal event before returning them. This is bounded lifecycle maintenance,
not caller-authorized execution: it cannot touch managed secrets or create an
approval, and concurrent reconciliation is replay-safe.

Filesystem storage exists for local/test/rehearsal parity. Shared runtime truth
is PostgreSQL-backed.

## Evidence And Redaction

The persisted human evidence may include:

- plan and evidence digests;
- configured, candidate, unchanged, and unreadable counts;
- active and retirement key IDs needed for human root-rotation review;
- legacy-compatibility state;
- bounded request reason, source, actor, and lifecycle timestamps.

It never persists managed-secret IDs, secret-version IDs, ciphertext,
plaintext, or raw planner error strings. The agent projection is narrower: it
contains counts, status, descriptor/version, timestamps, and compatibility
state only; it excludes key IDs, request data, and human identity.

## HTTP, UI, And Worker

Human routes:

- `POST /v1/privileged-operations/plans`
- `GET /v1/privileged-operations/plans`
- `GET /v1/privileged-operations/plans/{operation_id}`
- `POST /v1/privileged-operations/plans/{operation_id}/approve`
- `POST /v1/privileged-operations/plans/{operation_id}/revoke`
- `POST /v1/privileged-operations/plans/{operation_id}/cancel`

Agent route:

- `GET /v1/agent/privileged-operations/plans/{operation_id}`

The UI is at `/ui/engineering/privileged-operations`. It exposes bounded
browser-human approve/revoke controls and clearly states that the service worker
executes approved work internally; it has no execute control.

`launchplane service privileged-operation-workers run` is the service-internal worker loop.
It claims approved records, re-plans with `apply=False`, rejects plan/pre-state
drift, reads the fresh active policy, reauthorizes only the immutable approver
GitHub ID against the exact managed rule, constructs approver-bound durable
authorization provenance, then invokes the typed executor with `apply=True`.
The operation-record ID derives the deterministic token passed to the existing
managed-secret reservation/single-flight path. All managed-secret re-encryption
operations share one global provider-target fence even when their operation IDs
differ. If a worker lease is lost after an ambiguous effect, reconciliation
replays the typed executor with the same deterministic token so it can adopt a
completed rotation or fail closed with reconciliation still required. Results
and failures write only redacted counts, digests, bounded failure codes, and
reconciliation state.

## Legacy Re-encryption Route

`POST /v1/secrets/reencrypt` is retained only as an explicit migration boundary:
`mode="apply"` always refuses, and `mode="dry-run"` directs callers to the
privileged-operation planner. It cannot approve or execute an operation.
