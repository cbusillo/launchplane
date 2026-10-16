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

Both the application and database images must be immutable before planning can
pass. During leased preparation, a bounded MariaDB client from the database
image connects with TLS verification from the recipient's own private default
network. The conservative existing source-grant classifier must observe only
read privileges, with no roles, grant option, writer privilege or unknown syntax.
The private network has exact project/custody labels and IPv6 disabled; neither
an arbitrary existing network nor a caller's connection assertion is adopted.
No application starts. Managed values remain inside the service/provider path;
terminal receipts contain neither endpoint values, credentials nor grant text.
Private probe inputs reach Python through stdin, avoiding Python's process
arguments. This is still a private service/provider credential delivery path:
Dokploy stores the script in its schedule record and materializes it on the
destination host. Its shell transport can carry the encoded script in process
arguments, and its script file permissions depend on the host's umask. The
provider and destination host administrators are within that custody boundary;
this mechanism does not promise secrecy from other host users during delivery.
The manual-only script is scrubbed after success or failure under its execution
lease, even if authority changes during the probe. Unverified cleanup is reported
as `recipient_probe_cleanup_unverified` and never qualifies preparation. Scrub
requires provider readback of the inert script and disabled schedule. Dokploy's
[schedule update implementation](https://github.com/Dokploy/dokploy/blob/53464e9096a0976e8e4a8f3a1908bc1054e45a37/packages/server/src/services/schedule.ts#L170)
awaits rewriting the destination script as well as updating its record. This is
source contract evidence, not verification of an installed provider version.
On unverified cleanup, preserve the original operation and private provider
evidence for the reviewed continuation; do not repeat the probe blindly.
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
The receipt and terminal plan distinguish an empty captured database from an
initialized one through `captured_database_empty`; initializing an empty restored
copy is never presented as recovery of populated business data. Integrity success
requires both a successful checker exit and no `--silent` diagnostics, because
MariaDB can report corrupt tables while exiting zero. Unsupported storage-engine
checks also refuse: startup alone cannot qualify data whose integrity the
verifier cannot check. The verifier
also requires a populated, readable privilege catalog, so an interrupted partial
initialization is not mistaken for recovered data. The verifier ignores image
option files and supplies its own data directory. File and archive hashes
are streamed. Capture requires space for archive and restored trees, their
metadata, and 2 GiB of database/verifier overhead before writing either copy.

The service receipt binds capture digest, immutable application/database images,
recipient, destination, storage allocation, preparation and authority revisions.
CAS custody and the shared target reservation exclude competing recoveries. A
lost reply after committed completion replays the original receipt. A lost host
reply before service commit can be adopted under the same key from its retained
terminal host witness after the archive digest is verified. A per-binding file
lock excludes an active or late host writer. Only a terminal failure with verified
verifier cleanup permits another attempt under the original key; every attempt
keeps its own capture and receipt. Absence, lock contention or uncertain effects
never authorize a retry. The archives are retained at
`/var/lib/launchplane/recipient-recovery/<recovery-binding>/attempt-*/capture.tar`,
with per-attempt receipts and an atomic `outcome.json` pointer. Failed or uncertain attempts retain
their archive and any scratch state whose cleanup could not be verified. No
automatic archive deletion or shared-backup retention change is introduced.
After the move's recovery obligations end, the maintaining session reviews that exact
archive directory for retirement. This scoped capture does not replace Gate C's
independent destination backup qualification on shiny-infra-ops#528.

After changing either verifier, the implementing agent runs the focused host and
adapter tests plus the real storage gates before handoff. PostgreSQL uses the
repository's normal integration gate. The explicit disposable MariaDB proof is:

```bash
LAUNCHPLANE_TEST_MARIADB_IMAGE=<reviewed-immutable-mariadb-image> \
LAUNCHPLANE_TEST_MARIADB_DIRECTORY=<task-local-docker-accessible-directory> \
uv run --extra dev python -m unittest tests.test_production_recipient_mariadb
```

This gate proves empty restoration, populated restoration with a recovered-row
query, rejection of a real corrupt Aria table, and partial-initialization refusal.
It needs a local Docker engine
and no live product data or credential. Record the exact image and result on the
PR. Ordinary unittest/CI runs explicitly skip it without that fixture input;
their success alone is not the real MariaDB proof.

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
