---
title: Shared Production Backup Provider
---

The shared provider captures a Proxmox guest snapshot and an independent PBS
backup from an exact production backup policy. Host, guest, storage, snapshot
prefix and retention come from revisioned Launchplane target/policy records.
Requests cannot override topology or skip either operation.

## Service contract

`POST /v1/production-backup-gates` accepts product, context, instance, promotion
action, backup record ID and timeout. It requires
`production_backup_gate.execute` for that exact scope, PostgreSQL, a managed
authorization rule with durable provenance, and `Idempotency-Key`. The service
resolves current typed authority and stores the exact binding in a durable
operation. The same caller/key and request reuses that operation even after
authority changes; changed requests conflict. New captures resolve current
authority. Backup record IDs cannot be reused for another capture, including
after an operation finishes.

`GET /v1/production-backup-gates/operations/{operation_id}` takes the same
product/context/instance as query parameters and requires
`production_backup_authority.read`. It returns status and bounded evidence,
without host coordinates, SSH material or raw provider output. Passing evidence
includes policy revision/digest, target record IDs/digests, snapshot identity,
PBS archive identity (including guest kind/ID), and timestamps. Host and storage
coordinates are not exposed.
Partial captures fail and retain the evidence already collected, with a bounded
error code. The worker persists capture intent and verified progress as it goes;
lease-expiry recovery retains that evidence and reports an unknown effect rather
than replaying a possibly completed host command.

`POST /v1/production-backup-gates/operations/{operation_id}/cancel` takes the
same scope query and a cancellation reason. It requires the capture action for
that scope and only cancels pending operations. The claim/cancel race is resolved
atomically; running captures cannot be cancelled through this route.

The shared operation uses schema version 3 of the existing backup-operation
record. It reuses the database queue, claims, heartbeats, authorization checks,
and atomic operation/evidence completion. Historical versions 1 and 2 retain
the legacy request contract. The existing `launchplane-verireel-workers`
service consumes both forms; its name, table and compatibility entrypoints stay
unchanged during rollout. No new worker service is needed.

Before each provider mutation, the worker rechecks authorization, lease loss,
and the exact policy/target binding. Missing, stale, retired or changed authority
blocks execution. An expired operation that entered the provider phase is not
automatically retried because its effect may be unknown.

A database advisory lock covers capture and retention for the configured host,
guest kind and guest ID, across backup record IDs and worker replicas. A second
capture fails with `backup_source_busy` before host effects. After the first
operation is reconciled, a new capture uses a new backup record ID. The worker
checks the lock connection before effects and before completion; losing it fails
closed. Policies for one physical guest must use the same canonical host endpoint;
the lock does not resolve different IP/name aliases into physical identity.

## Host and credential prerequisites

The exact production instance needs managed runtime secret bindings for
`PRODUCTION_BACKUP_SSH_PRIVATE_KEY` and `PRODUCTION_BACKUP_SSH_KNOWN_HOSTS` under
the runtime key-safety policy. The worker ignores workstation keys and ambient
SSH configuration. The Linux worker keeps key material in anonymous memory-backed
files with mode 0600, exposes them to SSH through its own procfs descriptors, and
closes them on completion. It never writes the material to disk. A runtime
without Linux memfd support fails closed. Strict host-key checking stays enabled.

An operator must install the reviewed `scripts/proxmox-prod-gate-filter.sh` on
the bound host and pin its forced-command environment to one guest, storage and
snapshot prefix. `PROD_GATE_GUEST_KIND` selects `lxc` or `qemu`; the existing
`PROD_GATE_ALLOWED_CTID` variable carries the exact guest ID for either kind.
Existing snapshot-style settings remain supported.

Every filter installation must explicitly set `PROD_GATE_ALLOW_RESTORE`:
capture keys use `false`. If the setting is missing, all commands are refused,
so an incomplete upgrade fails during its initial probe.
The shared provider requires that capability in the boundary read and refuses a
key that also permits rollback or start. A separately approved legacy restore
key may set `PROD_GATE_ALLOW_RESTORE=true`; installing a new filter must preserve
that explicit setting when legacy restore access is intended. The shared capture
key must remain separate from such a restore key.

The worker first compares `launchplane-backup-boundary` with the Launchplane
binding, then requires the exact storage to report `pbs` and `active`. A storage
rename on only one side fails before capture. Older installed filters without
these reads fail closed and need a separately approved host update.

Snapshot prefixes are checked before SSH against the
[Proxmox snapshot-name format](https://github.com/proxmox/pve-common/blob/master/src/PVE/JSONSchema.pm):
a leading letter, letters/digits/underscore/hyphen thereafter, and enough room
for the timestamp/hash suffix within 40 characters. An unusable prefix makes the
authority read model invalid. Boundary and storage metadata use stdout only;
vzdump's archive identity may come from its stderr log.

The filter permits storage status for its bound destination and backup listing
for its bound guest. Other storage, guest, content type, shell commands and
multiline commands remain denied. Storage commands follow the
[Proxmox CLI contract](https://github.com/proxmox/pve-docs/blob/master/generated/pvesm.1-synopsis.adoc).

## Evidence and rollout

Capture creates and reads back the named snapshot, then requires successful
`vzdump` completion and the exact reported archive in the bound storage's backup
listing. This proves capture and presence; it is not a full restore test or a
PBS datastore-wide verification job. Driver-specific logical backup verification
and recovery exercises remain separate evidence.

Snapshot pruning runs only after both captures pass. It removes only names
matching this provider's configured prefix and timestamp/hash format, preserves
the just-created snapshot, and retains at least one snapshot even when retention
is zero. Existing snapshots outside that format are kept. Retention failure is
recorded separately with a bounded error code and does not invalidate a verified
capture when the worker can commit its result; authority or lease loss stops
further deletion. A worker crash or expired lease still fails closed, including
during retention. Saved verified-capture evidence remains available for operator
reconciliation; recovery does not automatically authorize a promotion.

Deploying this code does not install host filters, grant access, create secret
bindings, or activate a backup policy. Each product's activation and live proof
remain separate rollout steps. Product repositories pass no provider topology.

## Promotion enforcement

The Launchplane reusable Odoo and generic-web promotion workflows first enforce
the current release-review requirement, then enqueue the shared capture and poll the same
idempotent request until it completes. The service checks release approval again
before deployment. A prelaunch product whose review explicitly says `required=false`
follows the service's existing exemption; absent review metadata does not grant it.
Failure or cancellation stops the workflow before promotion. Generic-web reads
the production context from the current product profile. Odoo's thin workflow
then sends `run.infrastructure_backup_record_id`; Launchplane resolves the
accepted testing artifact, checks the infrastructure evidence, captures the
logical database/filestore backup, and promotes. The direct Odoo promotion route
requires both its logical `backup_record_id` and
`infrastructure_backup_record_id`.

Both Odoo routes use the policy action `odoo_prod_promotion_run.execute`;
generic-web uses `generic_web_prod_promotion.execute`. These are server-owned
policy selectors, independent of the action authorizing each HTTP entrypoint.
The generic-web `backup_required` field only accepts `true`, and its
`backup_record_id` must identify the completed shared capture. Supplying a
legacy backup record or a caller-authored evidence map cannot satisfy this gate.

Every live entrypoint requires a successful worker operation and matching
backup record for the exact product, context and instance. It compares the
saved policy and both target revisions/digests with current authority, requires
both verified capture identities, and applies each policy's evidence-age limit.
Set the snapshot freshness budget to cover the independent PBS capture, queue
delays and any driver-specific logical backup before deployment starts. Each
age is measured from that operation's own completion time; completing PBS does
not reset the snapshot clock. Size the policy from observed backup durations.
Missing, failed, partial, stale, future-dated, mismatched or superseded evidence
refuses deployment. Promotion records retain the infrastructure backup and
operation IDs plus exact policy/target evidence. Odoo keeps its logical evidence
in the same record and prefixes the additional infrastructure evidence keys.

Deployment holds the same canonical guest lock as capture and retention. Current
authority, evidence age and capture supersession are checked under that lock
before the first provider effect. A durable pending promotion reserves the capture
under that lock; another promotion cannot reuse it, including after a crash or
failed attempt. Promotion IDs include a unique component so same-second attempts
cannot overwrite this evidence. Subsequent checkpoints verify the lock;
the admitted evidence remains bound for the rest of that promotion, including
Odoo module updates. An elapsed freshness limit or a later policy edit must not
interrupt a promotion after its image has changed. Existing execution-authorization
and operation-lease checks remain independent. Lock loss before the first effect
refuses deployment. If the lock connection is lost after effects start, the
admitted deployment finishes and retains its actual deployment, health, inventory
and release-tuple results. A warning and promotion evidence record
`source_lock_status=lost_after_effect` with the observation time; this is not a
claim that lock protection remained intact. A new promotion still needs a fresh
admission check.
A subsequent verified capture can have pruned an earlier snapshot, so earlier
evidence cannot authorize a new promotion. A capture refused before verification,
including a source-lock refusal during promotion, cannot prune the selected
snapshot and does not invalidate the in-flight promotion. Other host/name aliases
and legacy commands remain outside this lock, as described above.

Serialize the whole capture/logical-backup/promotion sequence for products that
share a guest. The lock covers each capture and deployment; it does not queue
the intervening Odoo logical backup. An overlapping capture can supersede the
first product's evidence even when retention has not yet removed its snapshot.
That conservative refusal requires a new capture after the other promotion
finishes; do not run competing retry loops for tenants on one guest.

An exception after admission marks the promotion failed and retains its backup
reservation. Evidence distinguishes `provider_effects_status=not_started` from
`unknown_after_failure`; a failed attempt after effects does not prove the
provider rolled back. A process crash or unavailable database can still leave
the durable pending record for reconciliation. Neither state permits capture
reuse.

A generic-web dry run creates no backup. Without a supplied capture it reports
backup status `pending` with `required=true`; a successful dry run never provides
production backup proof. Live calls cannot use dry-run evidence to bypass the
gate.

Roll out the new reusable workflow revision and explicitly grant its caller
`production_backup_gate.execute` for the exact production scope. Release-review
reads use an existing promotion grant covering both testing and prod in the
lane's context, or `product_profile.read` for the product in the Launchplane
context. Generic-web's workflow also uses that product-profile read capability
to resolve its lane. No grants are
created by deployment. Older workflow pins or missing policies/evidence fail
closed; enforcement applies to every Odoo and generic-web testing-to-production
promotion, including products awaiting their own activation.

Deploy the worker and API together, and confirm the worker runs the new revision
before granting or using shared capture. An old worker cannot read schema-v3
operations and must never receive them. Do not run the legacy and shared backup
flows for the same guest during migration; the shared source lock does not
control legacy host commands.
