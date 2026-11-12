---
title: Event-driven merge train passes
---

Launchplane's existing merge-train worker wakes on source-control events and
continues a controller mutation pass until an external wait or terminal result.
The active DB policy remains the switch: only scheduler-enabled targets run,
and their scheduler mutate and runner-mode settings still govern each action.

The existing signed `POST /v1/github/app-webhook` receiver wakes the worker
through PostgreSQL LISTEN/NOTIFY when an enabled, inventoried repository adds
or removes its policy enqueue label, changes a PR head, or opens, reopens or
closes a PR into a train's base, or when a check run, check suite or workflow
completes, or a commit status turns terminal, on a head that train may wait on:
a PR into its base, one of its own `launchplane/train/**` candidate refs, or
its base branch. Check payloads carry no labels, so every PR into the base
still wakes its train. Completions on other branches or bases wake nothing;
a head GitHub cannot associate with a PR, such as a fork PR, is left to the
sweep. A commit status names no PR, so a status on any non-candidate branch
still wakes the repository's trains.

Each hint names the trains it concerns by controller key, and the worker runs
only those trains, folding hints that queued during a pass into the next one.
Every target still runs on the five-minute sweep, which keeps its own clock:
event passes between sweeps never postpone it, and a due sweep drops queued
hints because it rereads every train after they arrived. A hint that names
no train, a listener failure and a timeout all run the full sweep.

The event body is only a wake hint: the worker re-reads the live queue and all
gates, using the DB policy and its own credential. Duplicate hints add no
authority; notifications missed during disconnect or restart are recovered by
the sweep. A notification failure does not block product reconciliation.
Existing poll/backoff admission still applies.

A controller mutation pass continues through candidate planning, construction,
and already-passed checks to landing planning and execution. Observing pending
checks stops the pass; branch updates and stack collapse start CI and also stop
it. A finished landing continues to the next queued batch, which builds against
the new base; an unfinished landing, refusals and reconciliation end the pass.
Each action re-reads policy and admission and acquires the normal controller
lease; disabling or disarming a target takes effect before the next action. A
pass allows at most 16 actions per target, then yields to the other trains.
Dry-run and level1 passes still run once. The HTTP controller and GitHub runner remain
one-action interfaces; this progression belongs to the in-service worker.

When another eligible root is available, new-candidate selection defers unsupported
stacks using the existing stack-discovery checks. The root's feedback names
the unfinished child or ambiguous topology; unrelated work can form a candidate
in the same pass. No hold label or permanent failure is written. Every fresh
snapshot rechecks the stack, so it can combine again when its children are ready.
The root's own check or merge conflict is reported first. Existing candidates keep
their membership checks. Ready handoffs, Client decisions and holds still apply to each PR, and complete
stacks continue through the existing combination and shared-CI path.

The dry-run result's `stack_waits` reports children's own readiness and parent
wait reasons even when no root is eligible. Children stay outside the root
queue and flat candidate admission. Unsupported stacks report their reason on
the selected root before a collapse plan exists. Each scheduled snapshot
rechecks waits; ordinary parent waits reuse the existing deduplicated waiting
feedback. The monitor still measures work the train owes through eligible roots.

The GitHub Runner defers a pass when the controller returns HTTP 409 with
`merge_train_controller_lease_held`: another worker owns that train's lease.
It records the deferral in the job summary, sends no PR feedback, and leaves
the service scheduler or next manual pass to reobserve. Other 409 responses
still fail the job.

The App must already deliver these events to the receiver with its configured
managed webhook secret. This source change adds no App subscription, grant or
credential; enabling missing subscriptions is a separate administrator action.
Deployment and live delivery are separate from the local notification rehearsal.

## Wake observations

After signature and JSON validation, the receiver captures the delivery header,
its request trace and receipt time, normalized event/action, and bounded optional
head and event identifiers. These are correlation fields from a signature-verified
request; the delivery/event headers are not themselves covered by the payload
signature. Redelivery may reuse a delivery ID, so the request trace distinguishes
observations. The fields never replace fresh merge evidence.

Each notification carries one advisory controller key and its bounded receipt
in a single versioned envelope. PostgreSQL exposes the complete notification,
so separate client reads cannot detach the receipt from its routing hint.
Invalid or oversized optional metadata falls back to the original raw key.
The new listener also accepts raw keys from older publishers. An older worker
cannot select an envelope's target during a mixed rollout; its independent timed
sweep remains available. Qualify event latency only after both receiver and
worker run the new source. No independently pinned old-worker mode or
compatibility service is introduced. A failed notification relies on the sweep.

The listener fully drains queued hints while retaining the newest bounded set of
receipts. `receipts_truncated` reports lost attribution, not an exact count of
unique deliveries. Missing, malformed or legacy metadata remains unattributed;
an envelope's routing key remains a hint, independent of its optional receipt.
Unreadable envelopes with no routing hint fall back to a full sweep. A due sweep
still consumes queued hints and rereads all targets, retaining observed receipts.

Every loop pass emits `merge_train_pass` projections on stderr, each bound to
its loop-pass start (`started_at`). The first contains the actual wake reason, hint observation
time, any whole-pass error (`wake.error_code`), and counts of results, explicit target keys and
receipts. A null target count denotes a full sweep. Subsequent records
contain one explicit target key, one target-keyed receipt, or one result binding
at a time. `wake.targets` and receipt/result `routing_key` retain the opaque
controller identity through redaction. Result bindings keep repository, base,
status, reason, controller
action, trace and record identifiers. Full controller/runtime bodies and the
duplicate stdout fleet array are omitted. The command's final stopped status
remains on stdout; `run-once` retains its existing result response.

The counts let a reader check whether all projections were retained. A receipt
is emitted once even when a target continues through several actions. Empty or
failed passes still emit their wake and counts, and unmatched hints remain
visible without inventing an action. Each projection stays independent of fleet
size and receipt count, so valid aggregate output no longer overwhelms the
supported reader's per-message bound. The reader's limits, redaction and
quarantine remain unchanged. These are correlation observations, never quota,
admission or landing proof.

Startup, event hints, timed sweeps, empty hints and listener fallback remain
distinguishable. A timer sweep can also observe receipts, and a later pass does
not reuse them as a new cause. The loop-pass start is not every action's start;
use each controller's times and trace for its bounded continuations.

A target disabled or skipped during shutdown can have a receipt with no result
binding. That establishes no action or particular skip reason. Receipt retention
remains bounded independently of result counts. Read the structured stderr
projections together. A receipt alone establishes neither an action nor a landing.
Join the actual trace and record identifiers through existing admission/landing
reads; immutable landing outcomes bind their `source` to `service:merge-landing-outcome:{trace_id}`.
No additional proof store, grant or reader credential is introduced. Retain raw
timestamps and clock uncertainty when measuring latency; local rehearsals and
nearby timestamps do not prove a natural event-to-landing chain or saved idle time.

See [merge-train policy](merge-train-policy.md) for admission, leases, policy
records and the service-worker sweeps and manual GitHub entrypoint.

## Stall monitoring

The merge-train worker starts an independent monitor thread. Every five minutes
it reads each enabled mutating controller target's live queue, candidate records and controller lease.
Observation clocks live in `launchplane_merge_train_stalls`, independently of
controller leases and heartbeat updates; restarting the worker preserves them.

An unchanged controller phase with work for at least thirty minutes, or an
eligible change with no progress for that interval, raises an incident. A new
queue arrival does not reset an older change's wait. Progress by a candidate
resets its participating changes' waits. Idle observations clear the controller
clock; a held lease alone is not work. New work starts a fresh thirty-minute
clock even when the completed controller state is unchanged. An expired held
lease with queued, blocked or candidate work has no live lease heartbeat and
is reported after a second consecutive observation; the monitor does not
inspect a process on another host. A failed candidate whose exact-head checks
have passed on rerun is similarly confirmed on the next pass if recovery has
not yet cleared it. This gives normal lease renewal/recovery a pass to settle.
In-flight phases and heartbeat writes do not reset the completed-state clock.
Recorded Client release holds pause queue/controller timers; lease and recovered
failure signals still run. Long-running CI can cross the thirty-minute threshold:
the incident reports delay for the Supervisor to assess, not proof of dead CI.

The same monitor tracks open heads carrying the train's blocked label, including
candidate-failure blocks and admin holds. After thirty minutes it lists each PR
and head in the existing incident,
even with no eligible queue or active candidate. Unrelated train progress does
not reset that head's clock. Removing the block or closing the PR clears the
observation; a changed head starts its own clock. Release holds reset this timer
too. The incident also names a persistent check-retry wait, unavailable retry,
conflicting checks or admin check hold from the controller's existing last result.
This observation changes no block or recovery authority.

Stall observations and notification transitions stay in Launchplane's existing
repository-scoped stall record. The monitor never creates, edits, closes or
reopens product-repository issues. It never merges, resets a lease, reruns
checks, grants access or changes readiness labels. The repository-scoped
database lock serializes observations and delivery transitions.
Disabling or disarming a target suspends monitoring and preserves an unresolved
incident; it does not claim progress. Re-enabling it resumes fresh observation.
Legacy issue identity fields remain readable for stored-record compatibility;
they no longer control delivery. Stall state is the `stall` field in the existing
`GET /v1/work-graph/merge-train/controller/status` response.

Discord stays with the existing Launchplane sender. Put
`LAUNCHPLANE_MERGE_TRAIN_STALL_ALERT_REPOSITORY` and
`LAUNCHPLANE_MERGE_TRAIN_STALL_ALERT_REF` in Launchplane's shared runtime settings
for its `launchplane` context, through the existing runtime-environment record
admin path. They select the repository and ref containing
`merge-train-stall-alert.yml`. They are notification routing, independent of
train policy and candidate digests; changing them never retires train work.
The selected ref must contain the current alert workflow. Its optional ignored
`issue_number` input accepts dispatches from the previous service image during
rollout or rollback; the current monitor never sends it.

The workflow uses the existing `DISCORD_LAUNCHPLANE_WEBHOOK`; no webhook is copied
to service records and no new credential or grant is created. Existing delivery
App purpose `workflow_dispatch` dispatches the alert. A missing route or refused dispatch remains recorded in
`delivery_error`, with notification pending for a later pass. Discord reports
opening, changes in stall reasons, and resolution with the repository identity and a pointer to Launchplane, never provider payloads,
token values or error messages. Confirmed dispatch proves dispatch, not Discord
receipt; workflow failure stays visible in Actions. An uncertain dispatch can
repeat a notification with the same transition identity.

Configure that route during source activation before claiming Discord alerts
are operational. Schema migration and worker rollout are required; source tests
do not prove a live alert or delivery.
