---
title: Restart One Lane Service
---

The service restart action resets one existing Odoo or generic-web Compose service on the lane's
current artifact. It calls Dokploy's [container restart API](https://docs.dokploy.com/docs/api/docker),
never pull, build, deploy, config, env, database repair, or volume replacement.
The [direction stop boundaries](../DIRECTION.md#stop-boundaries) still govern
live-site writes. Implementing this recovery action grants no production action
or hotfix authority.

## Supported lane and identity

`POST /v1/drivers/odoo/service-restart` accepts product, context, instance, service,
reason and mode (`dry-run`, `apply` or read-only `reconcile`). An authenticated admin session or
scoped local admin credential (`LocalOperatorIdentity` or `LocalAdminIdentity`) may call it with existing
`live_target_runtime.plan` and `live_target_runtime.apply` authority with the
explicit product, context and instance; no access rule is installed.

The service-authenticated Director `local_operator` can also restart an eligible
testing service under standing direction without a write grant. Immediately
before the restart checkpoint, under the release lane reservation, it checks
[disposable-lane protections](disposable-lanes.md). Starting its own stopped
container preserves the image, configuration and volumes, so no fresh collection
or positive data-custody/isolation proof is required. Staff-testing holds,
protected use, target aliases and recorded production sharing still block. A
running service uses that same recovery rule when the existing lane monitors
verify it is down; a currently unhealthy Odoo web also qualifies repeated HTTP404
observations from the managed Docker route. Otherwise a running service keeps
the full current disposable qualification. These checks
apply to the Director's testing restart even with an explicit grant; other
callers and production keep their existing policy checks. Resolve holds through
the testing-hold action and protective observations through
`/v1/disposable-lanes/observations` before apply. Reset, rebuild, retirement and
target changes keep their own data and isolation gates. Completed replay and
original-request reconciliation remain observational when that eligibility
changes. Terminal-agent credentials cannot restart a service.

The active Odoo product lane must have agreeing provider/Compose records and a
passing deployment named by its environment inventory, with an agreeing runtime
identity and immutable artifact manifest. Newer attempts may be skipped only when
each is a completed failed deploy with a recognized pre-provider-write `deploy_blocked.*`
code, no attempted post-deploy work and no uncertain provider outcome. Attempts
started or finished since the current deployment began are checked regardless
of their position in history. Newer unfinished attempts, successes, unclassified
failures and uncertain effects refuse recovery and name the blocking deployment record. The same selection is checked during
apply and original-request reconciliation. Production also requires recorded acceptance of that exact artifact
and source commit. The same requirements apply to generic-web Compose lanes.
Historical rows may omit a start or finish time: their latest available timestamp
places them relative to the current deployment's required start time. Missing
both timestamps or a malformed recorded timestamp refuses recovery.
Other drivers and application targets are refused. Odoo uses
the lane reservation respected by release enqueues, workers and synchronous
releases. Generic-web holds its existing product serializer exclusively through
the final inspection, restart and verification, excluding deploy, promotion,
and rollback. Application-target retirement is outside this Compose-only action.
Both drivers use the existing endpoint;
the historical `odoo` route segment does not restrict it to Odoo products.
Generic-web waits for its product serializer, then rechecks the reviewed plan;
Odoo refuses when its lane reservation is held.
A nested release step refuses a lost serializer transaction, including an
automatic rollback. Preserve the original receipt for supported reconciliation;
do not assume a rollback ran or reacquire authority to overwrite a potentially
newer deployment.

Container selection requires inspected project and service labels and exactly
one non-one-off container. Name matches alone never authorize a restart. The
container must report the recorded deployment identity and immutable artifact
image, a valid timezone-aware start time and a container health check. Missing or ambiguous identity refuses
without a restart. `web` additionally verifies the lane's HTTP runtime identity
after restart and requires that endpoint before the restart; other named services verify their own container health and identity.

Odoo testing additionally supports the stopped `database` dependency. It must be
the sole non-one-off database container in the recorded Compose project, created
no later than the inventory deployment finished. A sole `web` sibling in that
project must match the inventory's application identity and immutable image,
whether running or stopped. The database needs a valid start time and container
health check; its own image ID, configuration and mounts are bound in the plan.
Inspection and verification accept that same database after it starts;
fresh dispatch requires it still stopped. Production and generic-web database
dependencies are refused.

For a stopped Odoo testing lane, inspect, apply and read back `database`, `web`, and `script-runner` in that order.
Use a separate review file and idempotency key per service. Web's HTTP identity
verification proves the recovered application on its recorded artifact.
A web restart alone does not start its Compose dependencies. This action never
changes database credentials, configuration, health checks or volumes.

Apply requires `reviewed_plan_sha256` from inspection and an `Idempotency-Key`.
Launchplane acquires the release lane reservation, rechecks the reviewed identity
and release ownership, durably checkpoints the exact container restart, and sends
one POST. It verifies a later start time, unchanged container/image/configuration/
runtime identity, and health. A failed verification remains a failed receipt.
Verification retries unavailable inventory and transient provider reads within
its bounded window; identity changes fail verification and never cause another
restart. Policy-authorized callers use the current policy, including live revocations.
Planner refusals retain their fixed, credential-free reason under
`restart_refused`; unsettled original requests retain reconciliation responses.

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
An absent or other-account receipt releases only the browser draft; a fresh
request refused by an existing restart does not create a recovery handle.

Activity's `restart_recovery` contains the original redacted request and key.
The bounded helper also accepts `resume --evidence-file <private-recovery.json>`
with that object and the original coordinates/reason. This sends `reconcile`,
which refuses a missing original receipt and can never dispatch a restart.

An unknown provider outcome holds that exact container against another restart
under any key. A replacement container has a distinct fence; an old POST names
the immutable original ID and cannot affect it. Resuming the original request performs read-only reconciliation: unchanged
identity, a healthy later start after its effect checkpoint, and HTTP runtime
identity for web can settle the receipt without another POST. A definite,
nonretryable provider 4xx settles as a failed receipt and releases the fence;
timeouts, retryable replies and genuinely uncertain effects stay held. A request that
stopped before any effect checkpoint can settle as undispatched. Changed or
unverifiable evidence remains unknown; inspect its activity and provider evidence
rather than deleting its reservation or inventing a new key.

If a later verified deployment replaced the original container, reconciliation
can settle the old receipt as **unknown** and release its fence. It requires
the same target/app/server, a different current deployment record and a different
sole inspected service container. A changed deployment record alone is
insufficient. The original outcome remains unknown in activity; this observes an
existing deployment and dispatches no restart, deployment or new code.

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
