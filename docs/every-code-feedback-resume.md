# Feedback continuation contract

Issue [#2328](https://github.com/cbusillo/launchplane/issues/2328) owns the approved
feedback continuation plan. The Owner selected implementation with a supported
operator recovery path required before production enablement. The
[#2058](https://github.com/cbusillo/launchplane/issues/2058) production authority
freeze and separate rollout approvals remain in effect.

## Implemented foundation

The contract models, exact authorization predicates, canonical GitHub comparison
helper, launch protocol and SQL evidence storage are an independently testable
foundation. They are not connected to production webhook ingestion, work-request
transitions, worker polling or HTTP execution routes. Existing feedback records
and the legacy pending queue keep their current shape and ownership.

An immutable acceptance binds a verified GitHub feedback revision to the exact
request and repository, immutable author ID and active-policy provenance.
Eligibility ends at the earlier of first verified receipt plus 24 hours and
provider revision time plus 24 hours. Delivery retries cannot renew it. Display
login and repository name do not participate in the revision digest. Canonical
provider timestamps use `updated_at` for comments and `submitted_at` for reviews;
an edited review with the same timestamp and a different digest is ambiguous.

The canonical comparison helper requires independently read repository, PR and
feedback objects. It rejects mismatched identities, authors, body, revision,
repository ownership, PR binding and closed PRs. Its inputs are not themselves
authentication: the future ingestion adapter must verify webhook transport and
obtain canonical objects through the managed GitHub integration before calling
it. A webhook-provided copy is never a canonical API read.

The two defined actions are exclusively scoped to one immutable GitHub repository:

- `every_code_feedback_resume.request` selects exactly one managed human rule
  containing the immutable GitHub ID and no login, role, organization or team
  selector. The webhook resolver scans the policy structurally; it does not
  fabricate a browser role or inherit local planning-file authority.
- `every_code_feedback_resume.execute` requires a real authenticated terminal
  agent, its exact subject and token label, and one exact managed rule. Empty,
  wildcard, superset and ambiguous selectors do not qualify. Neither the legacy
  worker token nor ordinary status/rerun permission supplies this capability.

These action names and predicates grant no permission. The current shared
terminal principal does not authenticate a particular physical host. A retained
host assertion and an execution fence must never be described as machine
authentication.

## Evidence and launch semantics

An immutable terminal intent records the expected terminal lifecycle, fence,
host and unchanged acceptance expiry. The foundation writer checks acceptance
linkage; it does not verify those expectations against a locked work-request
snapshot. Transactional minting must supply that verification. Operation evidence identifies a fresh lifecycle,
execution fence, lease owner and launch nonce. SQL persistence checks the linked
records and rejects changed replays. It does not perform the positive lifecycle
transition in this foundation slice.

Intent construction rejects an issuance time at or after eligibility ends.
Operation persistence checks that its recorded commit time precedes expiry and
its fence advances beyond the intent's expected fence. These recorded values
do not substitute for a future transaction's database time and request lock.
The inert gate-release helper does not load or recheck expiry; the future
transactional service must enforce expiry and current policy at release.

The inert launch protocol distinguishes process registration, gate release,
wrapper startup and exact handoff acceptance. A process binding includes a
process-start marker so PID reuse cannot match an older execution. Registration
of a different binding conflicts. Closing before release prevents release;
closing after release requires cancellation of the exact registered execution.
Process existence, `execve`, a successful tmux command and wrapper startup are
insufficient to mark feedback applied.

Only a separate exact session handoff receipt can establish acceptance of the
initial handoff. The default adapter is unavailable and performs no launch or
cancellation. Missing/mismatched process evidence and unknown delivery or
cancellation remain visible and blocked; they never authorize an automatic
relaunch. Proven absence is evidence for a future fenced recovery decision,
not a permission to start a process.

## Remaining implementation and production acceptance

The foundation does not complete feedback resume. Remaining service work includes
canonical managed API ingestion, revision disposition/ordering, explicit closure
observation, transactional intent minting and resume, monotonic fences and a
separate crash-recovery budget, versioned callbacks, gate registration/release,
receipt authentication, cancellation and evidence-bound operator recovery.

Legacy feedback has no verified authority; it cannot be converted by inferring
IDs from names. New resume-owned rows must be excluded from the legacy pending
API server-side before ingestion is activated. Missing legacy closure evidence
is unknown, not proof that a PR remains open. Stale recovery must carry its retry
budget forward even when it rotates a lifecycle ID; only a deliberate new
continuation resets that budget.

Production acceptance requires the complete service/worker protocol, a supported
audited operator recovery path, and an actual session-side exact-handoff receipt
capability. Unknown requests must be resolvable through reviewed service-owned
evidence and authority; no direct DB edit or force-launch is a supported escape.
Service contracts must deploy before compatible workers, with exact DB grants
and live enablement separately approved last. Codex Lab release and runtime
installation remain with their owning workstream.

Codex Lab already exposes typed `turn/start` and `userMessage` events through its
app-server protocol. The current interactive `codex-lab PROMPT` launcher does not
export those acknowledgements to Launchplane. The missing integration is a
supported bridge tying the initial user message to operation, launch binding,
client message ID, canonical prompt digest, thread ID and turn ID while preserving
the interactive TUI. A TUI callback or a Launchplane-owned app-server integration
could provide it. A noninteractive `exec --json` substitution or terminal-output
parsing would not establish the current interactive contract.

SQLite tests prove mapping and local transaction behavior. Only the real
PostgreSQL integration gate proves PostgreSQL migration, constraints and locking.
Neither pure protocol tests nor stored receipt-shaped fixtures prove a live
agent accepted feedback.

Protocol decisions are advisory values, not authenticated state transitions.
In particular, a `no_action` decision never authorizes persisting its suggested
phase. Receipt digests are attested evidence references until the service adds
authenticated receipt ingestion. A sender must retain the original receipt ID
for redelivery; generating a replacement ID conflicts with immutable receipt
evidence for the same operation and receipt kind.
