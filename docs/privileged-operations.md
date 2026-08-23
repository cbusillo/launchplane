---
title: Human-Governed Privileged Operations
---

# Human-Governed Privileged Operations

Launchplane owns a typed privileged-operation boundary for sensitive control-
plane work that cannot safely be delegated through static administrator
credentials. The boundary is separate from Owner Acceptance, Agent Write
Intents, workflow authorization, and ordinary operator mutations.

## Phase 1 Boundary

Phase 1 is planning and evidence only. It contains no approval, execution, or
provider-effect path.

- Descriptor IDs, versions, safety classes, request schemas, evidence schemas,
  and planners are compiled into the service registry.
- Registry validation runs at import/startup and prevents service startup when
  a descriptor or action-safety classification is invalid.
- The first descriptor is `managed-secret-reencryption` version 1.
- Its planner calls the existing managed-secret re-encryption computation with
  `apply=False` only.
- The Engineering UI lists human evidence but exposes no plan-create, cancel,
  approval, or execution control.

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

The Phase 1 actions are:

| Action | Safety | Surface |
| --- | --- | --- |
| `privileged_secret_operation.plan` | `secret_backed` | GitHub-human plan creation |
| `privileged_secret_operation.read` | `secret_backed` | GitHub-human plan reads |
| `privileged_secret_operation.cancel` | `secret_backed` | GitHub-human cancellation |
| `privileged_operation_summary.read` | `read` | Counts-only agent projection |

Code landing adds no policy rule or grant. The routes fail closed until a later,
separately approved DB-native managed-rule activation. GitHub secrets,
workflows, borrowed identities, and local-admin bearer credentials are not
bootstrap paths.

## Records And Lifecycle

`PrivilegedOperationRecord` is the current planning projection and
`PrivilegedOperationEventRecord` is the append-only lifecycle ledger. The
allowed Phase 1 states are:

```text
planned ──► expired
    └─────► cancelled
```

Terminal records cannot reopen. Plan creation is replay-safe by GitHub human
ID plus caller-supplied source event ID. A replay with changed input, evidence,
expiry, or actor data fails closed. PostgreSQL transitions lock the operation
row and atomically append the event and update the current projection.

Reads reconcile overdue `planned` records to `expired` with a system-authored
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

## HTTP And UI

Human routes:

- `POST /v1/privileged-operations/plans`
- `GET /v1/privileged-operations/plans`
- `GET /v1/privileged-operations/plans/{operation_id}`
- `POST /v1/privileged-operations/plans/{operation_id}/cancel`

Agent route:

- `GET /v1/agent/privileged-operations/plans/{operation_id}`

The read-only UI is at `/ui/engineering/privileged-operations`. It displays the
bounded human projection and explicitly states that Phase 1 has no approval or
execution path.

## Later Approval And Execution

Approval and execution belong to the separate Phase 2 contract. They must reuse
the GitHub-human dependency and exact managed-rule evaluator established here.
No Phase 1 record authorizes an effect, and no agent can approve or execute an
operation.
