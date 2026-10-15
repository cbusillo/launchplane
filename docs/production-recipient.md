---
title: Fresh production recipients
---

A fresh-state generic-web production move must keep the current serving binding
and recipient recoverable. Preparing another Compose target does not authorize
bootstrapping it, changing the production binding, repointing consumers or
retiring either recipient. This source work implements the preparation boundary;
the cutover/recovery continuation is still being completed for #3699.

`POST /v1/products/{product}/production-recipient` prepares a private undeployed
recipient for a product that explicitly has no public website. Its `instance`
is always `prod`; the context remains the product's existing production context.
The existing `dokploy_target.setup` service authority is required for apply.
Terminal identities retain the existing production-setup refusal. Pure planning,
readback and receipt reads use the existing product read authority. No new grant,
credential, workflow or identity fallback is introduced.

The request names one immutable product build/source commit, the explicitly
reviewed destination server, existing lane setting keys for database and config
volumes, independent port settings and the two Compose services. The service
resolves the old target, provider binding, profile, inventory, managed setting
and secret-version metadata, monitoring endpoints and backup-policy revisions.
The saved plan binds all those inputs. Changed authority refuses preparation;
the original snapshots remain in custody for recovery.

The product's immutable Compose source must enforce each service's CPU and memory
limits and the database buffer-pool limit. Actual provider conversion must agree
with that source, image and managed production environment. The new recipient has
no domains, public routing labels, external networks, host mounts or automatic
deploy. It cannot borrow testing settings or export managed secret values.

The fixed host program creates only the two new service-namespaced local volumes.
It requalifies their labels, backing identities and emptiness, inspects every
container including stopped containers for mount and port references, and checks
port availability. Existing storage is never adopted or deleted. A partial
provider effect retains the original key and custody. A matching complete
configuration can be adopted by readback after a lost reply; an uncertain create
is fenced and never repeated with another key.

Use `scripts/prepare-production-recipient.py` with the installed
`launchplane-write-action.py` transport. Keep its request and evidence outside
the checkout. Run `dry-run` with a fresh evidence path, then `apply` with
`--reviewed-dry-run --idempotency-key ORIGINAL`. Use `receipt` for the original
completed receipt and `readback` for fresh provider/resource qualification.
Evidence files are private, exclusive-created and preserved across retries.
The command never accepts a secret or an alternate credential source.

Before any later bootstrap or cutover, separately qualify destination capture and
isolated recovery, the accepted production artifact/release, actual enforced
resource limits and writer/API-budget coordination. A backup-capture pass alone
is not an isolated restore proof. The current production binding, monitor and
backup authority must move together with exact revision checks; rollback must
reverse only those changes and stop only the replacement writer, preserving
intervening unrelated work. It must never restore a whole shared guest.

Old-Compose and exclusive-volume retirement requires at least 24 hours after a
recorded successful cutover, healthy continued sync and consumers, and fresh
provider/lane/mount/owner references. A testing reference protects the resource.
Shared backups and testing custody are retained. Product retirement and detached
application retirement are not substitutes for this operation. Neither a
successful preparation nor a source PR starts the observation window.
