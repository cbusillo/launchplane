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

The GitHub Runner defers a pass when the controller returns HTTP 409 with
`merge_train_controller_lease_held`: another worker owns that train's lease.
It records the deferral in the job summary, sends no PR feedback, and leaves
the next scheduled pass to reobserve. Other 409 responses still fail the job.

The App must already deliver these events to the receiver with its configured
managed webhook secret. This source change adds no App subscription, grant or
credential; enabling missing subscriptions is a separate administrator action.
Deployment and live delivery are separate from the local notification rehearsal.

See [merge-train policy](merge-train-policy.md) for admission, leases, policy
records and the existing service-worker and GitHub sweep entrypoints.

## Stall monitoring

The merge-train worker starts an independent monitor thread. Every five minutes
it reads each enabled mutating controller target's live queue, candidate records and controller lease.
Observation clocks live in `launchplane_merge_train_stalls`, independently of
controller leases and heartbeat updates; restarting the worker preserves them.

An unchanged controller phase with work for at least thirty minutes, or an
eligible change with no progress for that interval, raises an incident. A new
queue arrival does not reset an older change's wait. Progress by a candidate
resets its participating changes' waits. An expired held lease has no live lease
heartbeat and is reported after a second consecutive observation; the monitor does not
inspect a process on another host. A failed candidate whose exact-head checks
have passed on rerun is similarly confirmed on the next pass if recovery has
not yet cleared it. This gives normal lease renewal/recovery a pass to settle.
In-flight phases and heartbeat writes do not reset the completed-state clock.
Recorded Client release holds pause queue/controller timers; lease and recovered
failure signals still run. Long-running CI can cross the thirty-minute threshold:
the incident reports delay for the Supervisor to assess, not proof of dead CI.

The existing delivery App creates or updates one bot-owned `Merge train stalled`
issue in each affected repository. It stays open while any monitored base has a
stall, closes when complete fresh evidence shows none, and reopens for a later
incident. Missing evidence cannot resolve it. Create reconciliation and a
repository-scoped database lock prevent routine duplicate issues. This is the
Supervisor's durable attention record; the monitor never merges, resets a lease,
reruns checks, grants access or changes readiness labels.
Disabling or disarming a target suspends monitoring and preserves an unresolved
incident; it does not claim progress. Re-enabling it resumes fresh observation.
If an issue loses its bot marker or ownership, delivery records the conflict
instead of overwriting another actor's issue. Missing/deleted issue identities
are reconciled on the next pass before a replacement is created.

Discord stays with the existing Launchplane sender. Put
`LAUNCHPLANE_MERGE_TRAIN_STALL_ALERT_REPOSITORY` and
`LAUNCHPLANE_MERGE_TRAIN_STALL_ALERT_REF` in Launchplane's shared runtime settings
for its `launchplane` context, through the existing runtime-environment record
admin path. They select the repository and ref containing
`merge-train-stall-alert.yml`. They are notification routing, independent of
train policy and candidate digests; changing them never retires train work.

The workflow uses the existing `DISCORD_LAUNCHPLANE_WEBHOOK`; no webhook is copied
to service records and no new credential or grant is created. Existing delivery
App purposes `release_record` and `workflow_dispatch` deliver the issue and
dispatch respectively. A missing route or refused dispatch remains recorded in
`delivery_error`, with notification pending for a later pass. Discord reports
opening, changes in stall reasons, and resolution with the issue link, never provider payloads,
token values or error messages. Confirmed dispatch proves dispatch, not Discord
receipt; workflow failure stays visible in Actions. An uncertain dispatch can
repeat a notification with the same transition identity.

Configure that route during source activation before claiming Discord alerts
are operational. Schema migration and worker rollout are required; source tests
do not prove a live alert or delivery.
