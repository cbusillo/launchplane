---
title: Event-driven merge train passes
---

Launchplane's existing merge-train worker wakes on source-control events and
continues a controller mutation pass until an external wait or terminal result.
The active DB policy remains the switch: only scheduler-enabled targets run,
and their scheduler mutate and runner-mode settings still govern each action.

The existing signed `POST /v1/github/app-webhook` receiver wakes the worker
through PostgreSQL LISTEN/NOTIFY when an enabled, inventoried repository adds
its policy enqueue label, changes a PR head, opens or reopens a PR, completes a
check run, check suite or workflow, or reports a terminal commit status. Check
events include commits on `launchplane/train/**` refs and need no associated
PR number. The event body is only a wake hint: the worker re-reads the live
queue and all gates, using the DB policy and its own credential. Duplicate
hints add no authority; notifications missed during disconnect or restart are
recovered by the existing five-minute sweep. A notification failure does not
block product reconciliation. Existing poll/backoff admission still applies.

A controller mutation pass continues through candidate planning, construction,
and already-passed checks to landing planning and execution. Observing pending
checks stops the pass; branch updates and stack collapse start CI and also stop
it. Refusals, reconciliation and landing outcomes end the pass. Each action
re-reads policy and admission and acquires the normal controller lease;
disabling or disarming a target takes effect before the next action. A pass
allows at most 16 actions per target, then yields to the other trains. Dry-run
and level1 passes still run once. The HTTP controller and GitHub runner remain
one-action interfaces; this progression belongs to the in-service worker.

The App must already deliver these events to the receiver with its configured
managed webhook secret. This source change adds no App subscription, grant or
credential; enabling missing subscriptions is a separate administrator action.
Deployment and live delivery are separate from the local notification rehearsal.

See [merge-train policy](merge-train-policy.md) for admission, leases, policy
records and the existing service-worker and GitHub sweep entrypoints.
