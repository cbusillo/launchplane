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
bindings, activate a backup policy, or change the legacy promotion gates. The
Odoo/generic-web promotion integration and each product's activation/proof are
separate rollout steps. Product repositories continue passing no provider
topology.

Deploy the worker and API together, and confirm the worker runs the new revision
before granting or using shared capture. An old worker cannot read schema-v3
operations and must never receive them. Do not run the legacy and shared backup
flows for the same guest during migration; the shared source lock does not
control legacy host commands.
