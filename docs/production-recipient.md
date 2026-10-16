---
title: Fresh production recipients
---

A fresh-state generic-web production move must keep the current serving binding
and recipient recoverable. Preparing another Compose target does not authorize
bootstrapping it, changing the production binding, repointing consumers or
retiring either recipient. This API implements the preparation boundary. It exposes no bootstrap, cutover,
rollback or retirement operation. It also provides purpose-bound upstream qualification
and an isolated cold-data recovery verifier. [#3699](https://github.com/cbusillo/launchplane/issues/3699)
tracks the complete source capability; preparation alone does not clear Gate A.

`POST /v1/products/{product}/production-recipient` prepares a private undeployed
recipient for a product that explicitly has no public website. Its `instance`
is always `prod`; the context remains the product's existing production context.
The existing `dokploy_target.setup` service authority is required for apply.
Terminal identities retain the existing production-setup refusal. Pure planning,
provider readback and receipt reads use the existing product read authority. Host
resource checks run only in leased apply/reconciliation. No new grant,
credential, workflow or identity fallback is introduced.
The preparer identity must perform its own dry-run and apply. Once custody
exists, planning and provider readback remain bound to that original actor and
scope, in addition to product read authority; a reader cannot transfer a saved
plan to a different setup identity.

The request names one immutable product build/source commit, the explicitly
reviewed destination server, existing lane setting keys for database and config
volumes, independent port settings and the two Compose services and their reviewed resource bounds. The service
resolves the old target, provider binding, profile, inventory, managed setting
and secret-version metadata, monitoring endpoints and backup-policy revisions.
The saved plan binds all those inputs. Changed authority refuses preparation;
the original snapshots remain in custody for recovery.

The product's immutable Compose source must enforce each service's reviewed CPU
and memory limits and any requested command memory limit. For the Sync move, the
request must bound each service to 1 CPU / 2 GiB and MariaDB's buffer pool to
512 MiB; these values are reviewed request data, not product-specific defaults. Actual provider conversion must agree
with that source, image and managed production environment. The new recipient has
no domains, public routing labels, external networks, host mounts or automatic
deploy. It cannot borrow testing settings or export managed secret values. Local database
hosts and URLs must name this Compose's services or loopback. Managed values alone
do not authorize an external writable endpoint. Existing external read-only
integrations require explicit `upstreams` entries. Each entry declares only
`read_only_source` purpose, the application service, its five environment keys
(`host`, `port`, `user`, `database`, `password`), and existing managed binding and
version IDs. Every rendered field must equal the lane's managed setting; the
password key must select that exact production binding. The binding must already
be `shared_safe` with recorded read-only-source evidence. These classifications
are prerequisites, never proof of permissions. Local database credentials, borrowed
testing bindings, undeclared external hosts, and external database URLs refuse.

During leased preparation, a bounded MariaDB client from the immutable database
image connects with TLS verification from the recipient's own private default
network. The conservative existing source-grant classifier must observe only
read privileges, with no roles, grant option, writer privilege or unknown syntax.
The private network has exact project/custody labels and IPv6 disabled; neither
an arbitrary existing network nor a caller's connection assertion is adopted.
No application starts. Managed values remain inside the service/provider path;
terminal receipts contain neither endpoint values, credentials nor grant text.
The source observation is bound to the preparation, destination and managed
versions. Readback retains its original observation time and does not renew it.
Later bootstrap must requalify the actual connections; a historical proof cannot
authorize starting a writer. Changed settings or managed versions invalidate
the reviewed authority snapshot.

For MariaDB command memory bounds, use the image entrypoint and direct command
options, optionally preceded by `mariadbd` or `mysqld`. The reviewed memory option
accepts either an equals value or a following value; other options use
`--name=value`. Custom database entrypoints, shell wrappers and defaults-file
overrides refuse qualification.

The fixed host program creates only the two new service-namespaced local volumes.
It requalifies their labels, backing identities and emptiness, inspects every
container including stopped containers for mount and port references, and checks
port availability. Existing storage is never adopted or deleted. A partial
provider effect retains the original key and custody. A matching complete
configuration can be adopted by original-key apply/reconciliation after a lost reply; an uncertain create
is fenced and never repeated with another key.

Before any provider effect, a rejected attempt may be replaced by a fresh reviewed
plan and a new key only when its reservation is gone and it has no provider IDs.
The compare-and-write retains the complete prior attempt in custody history.
Any possible provider effect keeps the original plan and key fenced. A real
authority change after provider effects invalidates qualification; the later
cutover/recovery continuation must reconcile it, rather than automatically
rebasing an existing recipient onto new authority.

Use `scripts/prepare-production-recipient.py` with the installed
`launchplane-write-action.py` transport. Keep its request and evidence outside
the checkout. Run `dry-run` with a fresh evidence path, then `apply` with
`--reviewed-dry-run --idempotency-key ORIGINAL`. Use `receipt` for the original
completed receipt and `readback` for fresh provider configuration verification.
Readback returns the original host-qualification timestamp; it does not run a
host command or renew resource qualification. Leased apply/reconciliation owns
that evidence, and later bootstrap must qualify resources again.
Evidence files are private, exclusive-created and preserved across retries.
The request file never accepts a secret value or an alternate credential source.
The installed transport retains its existing private caller configuration.

`POST /v1/products/{product}/production-recipient/recovery` takes the context,
exact `preparation_plan_sha256`, reason and mode. It uses the original preparer's
identity and existing setup authority for apply, with PostgreSQL leases and a
separate original recovery key. Planning/readback use product read authority;
terminal-agent identities do not inherit production setup authority. Run the
same terminal consumer with `--recovery`, a separate request/evidence file,
`dry-run`, then reviewed `apply`. `receipt` reads the completed original-key
result; `readback` rechecks provider and authority agreement. Neither read
reruns the host verifier or turns a historical observation into fresh proof.

The verifier supports empty or cold MariaDB storage and config storage only.
It rechecks exact local volume labels/backing identities, all running and stopped
container mounts, project references and ports; any consumer refuses capture.
It captures the two scoped trees into an exclusive archive, checks for concurrent
source changes, restores into separate scratch storage and compares file contents,
ownership and permissions. It then starts only the pinned database image with
networking disabled, no published ports, no application, no carried credentials,
replication disabled and the event scheduler off. An empty captured database is
initialized only in the restored copy. Startup and `mariadb-check --all-databases`
must pass before a service-owned recovery result is committed. A nonempty tree
that is not MariaDB, a capture-only result, a writable/outbound verifier, changed
authority or uncertain cleanup never qualifies. Source trees and the retained
production recipient are never restored, stopped or deleted.

The service receipt binds capture digest, immutable application/database images,
recipient, destination, storage allocation, preparation and authority revisions.
CAS custody and the shared target reservation exclude competing recoveries. A
lost reply after committed completion replays the original receipt. Unknown host
effects remain fenced under the original key; absence of a service receipt is
never permission to repeat capture or restore. The complete original archive is
retained at `/var/lib/launchplane/recipient-recovery/<recovery-binding>/capture.tar`,
with a value-free host receipt beside it. Failed or uncertain attempts retain
their archive and any scratch state whose cleanup could not be verified. No
automatic archive deletion or shared-backup retention change is introduced.
After the move's recovery obligations end, the maintaining session reviews that exact
archive directory for retirement. This scoped capture does not replace Gate C's
independent destination backup qualification on shiny-infra-ops#528.

Before any later bootstrap or cutover, separately qualify destination capture and
isolated recovery, the accepted production artifact/release, actual enforced
resource limits and writer/API-budget coordination. A backup-capture pass alone
is not an isolated restore proof. The current production binding, monitor and
backup authority must move together with exact revision checks; rollback must
reverse only those changes and stop only the replacement writer, preserving
intervening unrelated work. It must never restore a whole shared guest.

Old-Compose and exclusive-volume retirement requires at least 24 hours after a
recorded successful cutover, healthy continued sync and consumers, and fresh
provider, lane, mount and custody references. A testing reference protects the resource.
Shared backups and testing custody are retained. Product retirement and detached
application retirement are not substitutes for this operation. Neither a
successful preparation nor a source PR starts the observation window.
