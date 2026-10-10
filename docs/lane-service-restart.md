---
title: Restart One Lane Service
---

The Odoo service restart action resets one existing Compose service on the lane's
current artifact. It calls Dokploy's [container restart API](https://docs.dokploy.com/docs/api/docker),
never pull, build, deploy, config, env, database repair, or volume replacement.
The [direction stop boundaries](../DIRECTION.md#stop-boundaries) still govern
live-site writes. Implementing this recovery action grants no production action
or hotfix authority.

## Supported lane and identity

`POST /v1/drivers/odoo/service-restart` accepts product, context, instance, service,
reason and mode (`dry-run`, `apply` or read-only `reconcile`). Only an authenticated admin session or
scoped local admin credential (`LocalOperatorIdentity` or `LocalAdminIdentity`) may call it. It uses existing
`live_target_runtime.plan` and `live_target_runtime.apply` authority with the
explicit product, context and instance; no access rule is installed.

The active Odoo product lane must have agreeing provider/Compose records and a
settled current deployment with a runtime identity and immutable artifact
manifest. Production also requires recorded acceptance of that exact artifact
and source commit. Other drivers and application targets are refused: the
supported Compose path uses the Odoo lane reservation already respected by
release enqueues, workers and synchronous releases.

Container selection requires inspected project and service labels and exactly
one non-one-off container. Name matches alone never authorize a restart. The
container must report the recorded deployment identity and immutable artifact
image, a valid timezone-aware start time and a container health check. Missing or ambiguous identity refuses
without a restart. `web` additionally verifies the lane's HTTP runtime identity
after restart and requires that endpoint before the restart; other named services verify their own container health and identity.

Apply requires `reviewed_plan_sha256` from inspection and an `Idempotency-Key`.
Launchplane acquires the release lane reservation, rechecks the reviewed identity
and release ownership, durably checkpoints the exact container restart, and sends
one POST. It verifies a later start time, unchanged container/image/configuration/
runtime identity, and health. A failed verification remains a failed receipt.

## Product Ops

Open a product's environment **Actions** page. For a server-advertised service
restart action, enter the service and reason, choose **Inspect restart**, review
the current artifact/container, confirm the interruption, then choose **Restart
web (same version)** (or the named service). Editing a reviewed field invalidates
the inspection. Fixture mode disables these controls.

Product activity records the actor, reason, artifact, before/after container and
start time, and pass/fail/unknown result. The durable reservation owns this
evidence; there is no second restart-record table or deployment-history rewrite.

## Bounded terminal helper

The repository helper reuses the installed Launchplane skill's private admin
transport and configuration. It never accepts provider credentials or host
commands. For example, with fake product coordinates:

```bash
uv run python scripts/restart-lane-service.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product example-site --context example-site --instance testing --service web \
  --reason "Recover an unhealthy web worker." --evidence-file <private-review.json>

uv run python scripts/restart-lane-service.py apply \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product example-site --context example-site --instance testing --service web \
  --reason "Recover an unhealthy web worker." --evidence-file <private-review.json> \
  --reviewed-dry-run --idempotency-key <one-stable-request-key>
```

The review file is created exclusively with mode 0600. Keep the same review file,
payload and key on retries. Repeated keys replay the original completed result.
A lost HTTP response retains the browser request across reloads. A definite
pre-effect refusal releases a new browser draft for another inspection; errors on
an existing request retain its handle, including an expired session or a lane
temporarily held by a release. After closing a tab, **Recover restart from
activity** restores the original request. Resume with the original actor;
the handle grants no authority to a reader or another admin.

Activity's `restart_recovery` contains the original redacted request and key.
The bounded helper also accepts `resume --evidence-file <private-recovery.json>`
with that object and the original coordinates/reason. This sends `reconcile`,
which refuses a missing original receipt and can never dispatch a restart.

An unknown provider outcome holds the target against another restart under any
key. Resuming the original request performs read-only reconciliation: unchanged
identity, a healthy later start after its effect checkpoint, and HTTP runtime
identity for web can settle the receipt without another POST. A definite,
nonretryable provider 4xx settles as a failed receipt and releases the fence;
timeouts, retryable replies and genuinely uncertain effects stay held. A request that
stopped before any effect checkpoint can settle as undispatched. Changed or
unverifiable evidence remains unknown; inspect its activity and provider evidence
rather than deleting its reservation or inventing a new key.

## Isolated proof

```bash
LP_REHEARSAL_OPERATOR_HELPER=<installed-launchplane-skill>/scripts/launchplane-write-action.py \
  uv run --extra dev python scripts/qualify-lane-service-restart.py
```

This explicit fixture requires the local Docker Desktop test engine and already
cached Python/PostgreSQL images. It creates its own web and database containers,
local service and a Dokploy HTTP protocol shim. The actual helper/service path
restarts web, verifies HTTP identity/health and activity, replays without a second
restart, and proves the database start time unchanged. It removes only its own
containers and anonymous volumes. It proves the container restart behavior and
HTTP contract; it does not contact a deployed Dokploy or production lane.
