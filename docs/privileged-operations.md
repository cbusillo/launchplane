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
approval and a supervised service-internal worker; it adds no HTTP execute
route, execute action, static execution credential, or agent execution path.

- Descriptor IDs, versions, safety classes, request schemas, evidence schemas,
  and planners are compiled into the service registry.
- Registry validation runs at import/startup and prevents service startup when
  a descriptor or action-safety classification is invalid.
- `managed-secret-reencryption` version 1 calls the existing managed-secret
  re-encryption computation with `apply=False` only.
- `managed-authz-policy-set` version 1 calls the existing managed-rule-set
  reconciliation planner with `mode="dry_run"` only. Its request contains one
  managed set, the exact desired schema-v2 policy fragment, reason, and optional
  related issue. Planning never writes the active authorization policy.
- The Engineering UI lists redacted human evidence and may offer browser-human
  approve/revoke controls. Policy proposals include an explicit exact-policy
  review disclosure. The UI never offers an execute control.

The registry is not an arbitrary route, command, SQL, or payload proxy. New
descriptors require code, schemas, tests, documentation, and review.

## Owner-Control Contract Seam

`contracts/owner-control-contract.json` publishes canonical owner-control
request and response bytes plus generic golden vectors for every registered
descriptor. It is a cross-host conformance artifact only: it does not issue a
challenge, consume an owner confirmation, alter browser approval, add a route,
or authorize execution. See `docs/owner-control-channel.md`.

The DB-backed shadow verifier remains independent from this lifecycle. No
privileged-operation route, service, worker, transition, approval, or execution
path imports it. Its unrouted storage API can retain test or future
service-authored challenge state and return only `authorizes_execution: false`.
Its service-only issuance derives a challenge from an already-locked planned
record and active policy without mutating that record or its event ledger;
browser approval remains the only active approval transport.

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

| Action                                     | Safety          | Surface                                         |
| ------------------------------------------ | --------------- | ----------------------------------------------- |
| `privileged_secret_operation.plan`         | `secret_backed` | GitHub-human plan creation                      |
| `privileged_secret_operation.read`         | `secret_backed` | GitHub-human plan reads                         |
| `privileged_secret_operation.cancel`       | `secret_backed` | GitHub-human cancellation                       |
| `privileged_secret_operation.approve`      | `secret_backed` | GitHub-human browser approval                   |
| `privileged_secret_operation.revoke`       | `secret_backed` | GitHub-human browser revocation                 |
| `privileged_operation_summary.read`        | `read`          | Counts-only agent projection                    |
| `authz_policy_operation.propose`           | `policy_admin`  | Inert GitHub-human or terminal-agent proposal   |
| `authz_policy_operation.read`              | `policy_admin`  | GitHub-human policy-plan reads                  |
| `authz_policy_operation.cancel`            | `policy_admin`  | GitHub-human policy-plan cancellation           |
| `authz_policy_operation.approve`           | `policy_admin`  | GitHub-human browser approval                   |
| `authz_policy_operation.revoke`            | `policy_admin`  | GitHub-human browser revocation                 |
| `privileged_policy_operation_summary.read` | `read`          | Proposal-owner agent projection                 |

Approval requires exactly one managed GitHub-human rule that is pinned to
non-empty immutable `github_ids`. Login, organization, team, or role selectors
may additionally narrow approval at approval time; execution reauthorization
uses only the immutable GitHub ID and exact rule scope. Code landing adds no
policy rule or grant. The routes fail closed until a later, separately approved
DB-native managed-rule activation. GitHub secrets,
workflows, borrowed identities, and local-admin bearer credentials are not
bootstrap paths.

Policy-operation approval additionally requires that the signed-in approver is
already authorized for `authz_policy_grant.write` by an active schema-v2 rule
with the same immutable GitHub ID. Execution checks that immutable pre-existing
administrator authority again, then requires the candidate policy to retain the
applying administrator and at least one distinct reachable policy
administrator. An approval-only rule cannot bootstrap its holder into policy
administration.

The one-time issue `#2277` activation bridge exists only to make this ordinary
policy-operation lifecycle reachable for an already-authorized immutable-ID
GitHub-human policy administrator. Its compiled managed set grants that one
human ID exactly the five `authz_policy_operation.*` propose, read, approve,
revoke, and cancel actions. It grants no agent summary, workflow, terminal,
operator, local-admin, provider, deployment, wildcard, or policy-write action.
The caller supplies no rule, selector, principal, action, policy body, or
managed ID.

The bridge dry-run exposes bounded active-policy, candidate, exact-action, and
continuity evidence. Apply requires the reviewed digest, reason,
immutable-ID-scoped idempotency, policy CAS, and exact read-back. If the active
policy already contains the exact compiled set, the bridge is permanently
retired by DB state and rejects new dry-runs or applies; a same-key replay can
still return the original completed response. A conflicting use of the managed
set fails closed. This is a temporary transport for existing DB authority, not
an alternate policy editor, total-lockout recovery, or break-glass credential.

The dry-run planner does not know which human will approve, so its evidence
cannot include the identity-dependent applying-administrator and independent-
administrator checks. Those checks run at approval and execution and may still
reject an otherwise blocker-free plan. Worker safety uses immutable GitHub-ID
semantics and treats GitHub-human administrator rules without a distinct
immutable ID as possibly covering the approver rather than as independent.

Canary activation is staged and DB-native: first activate exactly
`privileged_secret_operation.plan`, `privileged_secret_operation.read`, and
`privileged_secret_operation.cancel`. After exact plan review, activate exactly
`privileged_secret_operation.approve` and
`privileged_secret_operation.revoke`. Keep
`privileged_operation_summary.read` ungranted. Before activation, prove the
worker container is running the expected image and retain telemetry for one
successful DB-backed poll. After terminal verification, or any
post-activation worker stop, revoke every canary rule and read the active policy
back. Approval rules must remain active through the worker's terminal
reauthorization.

Use the protected `Dokploy Target Inspect` workflow for that pre-activation
proof. Its optional runtime-evidence mode requires an exact compose service,
expected immutable image, and optionally an allow-listed structured event for
diagnostics. It returns only bounded image/state identity, successful-poll
heartbeat status, and diagnostic structured-event counts. For the worker,
request service `launchplane-privileged-operation-workers` and event
`privileged_operation_worker_poll_succeeded`, supply the exact immutable
deployment reference as `expected_image`, and proceed only when
`runtime_evidence.proof_ready` is true. Do not retain or expose raw runtime logs,
container configuration, or environment values as canary evidence. The
canonical poll proof is a DB-backed current heartbeat written only after a
successful poll transaction. The proof reader requires a fresh heartbeat whose
internal worker identity matches the selected container. Dokploy first proves
that its observed hostname is a Docker-assigned prefix of the selected
container ID, then hashes it for comparison; operator-chosen hostnames fail
closed. The heartbeat image must also match both provider observation and
`expected_image`. Missing, stale, future-dated, identity-mismatched, or
image-mismatched heartbeats fail closed. Heartbeat persistence failure is a
worker polling error, so the worker never emits successful-poll telemetry for
evidence it could not persist. Raw hostnames and identity digests never leave
the service.

Allow-listed worker events remain diagnostic-only localization markers and do
not make the response proof-ready. Failed log reads and recognized provider log
errors are reported only through bounded availability and classification fields;
they do not block a valid DB-backed heartbeat proof and never include a runtime
log line. Provider failures in target lookup, service selection, container
configuration, container identity, or image identity remain hard failures.
When an event is supplied, the protected reader passes only the code-owned
allow-listed name to Dokploy's fixed-string search and requires an exact JSON
event field match when logs are available.
The shell-level `privileged_operation_worker_entrypoint_started` and
`privileged_operation_worker_entrypoint_probe_succeeded` markers localize the
boundary before the continuous Python worker starts and remain diagnostic-only.

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
- managed-policy previous/candidate revisions and policy digests, bounded rule
  change counts, blocker codes, and exact desired policy input for authorized
  human review.

It never persists managed-secret IDs, secret-version IDs, ciphertext,
plaintext, raw terminal-agent subjects/token labels, or raw planner error
strings. A terminal-agent requester is persisted only as a domain-separated
SHA-256 principal fingerprint. Agent projections are narrower: they contain
counts, status, descriptor/version, and timestamps only; they exclude request
data, desired policy bodies, key IDs, and human identity. A terminal agent may
read only the policy proposal created by its own fingerprint.

## HTTP, UI, And Worker

Human routes:

- `POST /v1/privileged-operations/plans`
- `GET /v1/privileged-operations/plans`
- `GET /v1/privileged-operations/plans/{operation_id}`
- `POST /v1/privileged-operations/plans/{operation_id}/approve`
- `POST /v1/privileged-operations/plans/{operation_id}/revoke`
- `POST /v1/privileged-operations/plans/{operation_id}/cancel`

Agent route:

- `POST /v1/agent/privileged-operations/plans`
- `GET /v1/agent/privileged-operations/plans/{operation_id}`

The UI is at `/ui/engineering/privileged-operations`. It exposes bounded
browser-human approve/revoke controls and clearly states that the service worker
executes approved work internally; it has no execute control.

Approval may make an operation immediately claimable by the worker. A browser
human can revoke only before that claim; after claim, the worker owns terminal
execution and performs its required fresh authorization check.

Human and agent proposal request bodies are capped at 256 KiB before parsing or
persistence.

`launchplane service privileged-operation-workers run` is the service-internal worker loop.
It claims approved records, re-plans with `apply=False`, rejects plan/pre-state
drift, reads the fresh active policy, reauthorizes only the immutable approver
GitHub ID against the exact approval rule, and invokes the descriptor's typed
executor. Managed-policy execution also proves the approver's pre-existing
immutable-ID policy-admin authority, constructs the exact reviewed apply
envelope, and uses the existing atomic authorization-policy CAS plus
idempotency completion before reading the active record back.
The operation-record ID derives the deterministic token passed to the existing
managed-secret reservation/single-flight path. All managed-secret re-encryption
operations share one global provider-target fence even when their operation IDs
differ. If a worker lease is lost after an ambiguous effect, reconciliation
replays the typed executor with the same deterministic token so it can adopt a
completed rotation or fail closed with reconciliation still required. Results
and failures write only redacted counts, digests, bounded failure codes, and
reconciliation state.

Managed-policy recovery does not recompute against the already-applied policy.
It verifies the completed inner CAS reservation and exact active-policy
revision/SHA read-back, then adopts the outer privileged-operation result. A
stale or ambiguous read-back remains reconciliation-required.

## Legacy Re-encryption Route

`POST /v1/secrets/reencrypt` is retained only as an explicit migration boundary:
`mode="dry-run"` refuses with `privileged_operation_planning_required`, and
`mode="apply"` refuses with `privileged_operation_approval_required`. The
legacy `secret.reencrypt.dry-run` and `secret.reencrypt.apply` actions cannot
approve or execute an operation.

## Transitional Managed-Policy Route

`POST /v1/authz-policies/managed-rule-sets/reconcile` remains temporarily
available for existing protected workflows, but accepts only GitHub Actions
OIDC workload transport. Browser sessions, terminal agents, local operators,
local administrators, and other bearer identities fail closed. Signed-in
humans review and approve `managed-authz-policy-set` records; they cannot call a
public policy execute route.

## Completion Holds

Keep #2204 open until actual migration, rollback, policy read-back, and soak
evidence are complete. Keep #2177 open until its handoff criteria are complete.

**Preserved history:** Phase 1 planning-only descriptions remain historical
context only; the supervised Phase 2 worker flow above is current guidance.
