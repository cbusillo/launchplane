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
never authorize a retry. A repeated explicit apply after a verified terminal
failure automatically makes another attempt under the original key, including
after a deterministic verifier refusal. Each such attempt retains another full
archive and consumes additional space; readback/receipt mode does not request an
apply. Inspect the failure before another apply. The archives are retained at
`/var/lib/launchplane/recipient-recovery/<recovery-binding>/attempt-*/capture.tar`,
with per-attempt receipts and an atomic `outcome.json` pointer. Failed or uncertain attempts retain
their archive and any scratch state whose cleanup could not be verified. No
automatic archive deletion or shared-backup retention change is introduced.
After the move's recovery obligations end, the maintaining session reviews that exact
archive directory for retirement. This scoped capture does not replace Gate C's
independent destination backup qualification on shiny-infra-ops#528.

### Recovery after publication

The same recovery route and `--recovery` terminal consumer support
`"purpose": "published"` with an explicit `"generation": "post-window-1"` in the
planning request. The default purpose remains `bootstrap`. Use a new generation
for a genuinely new observation; changing the generation does not establish
freshness. Dry-run, reviewed apply, readback and original-key receipt retain the
existing original preparer identity and setup authority. This capability does
not approve a live capture or grant an agent production setup access.

Published recovery requires the exact currently owned publication and its
authority, recipient, artifact and storage. It leaves the cold bootstrap proof
unchanged. Before any provider schedule is written, the current execution lease
and an atomic authority comparison admit exactly one original operation for the
reviewed plan. Its immutable operation binding uses the existing execution metadata.
Another key cannot capture or adopt its witness, even if proof storage failed.
A new reviewed generation is the supported way to request another observation.

Active proof commits require the current provider lease. Original-key terminal
adoption compares the existing reservation's identity and writes only under the
atomic current-authority check; it does not claim a renewed execution lease or
start another capture. A custody collision merges unrelated append-only entries
or accepts the identical original proof. Infrastructure errors preserve
uncertainty. Only positively changed authority or publication can complete a
terminal witness without qualification.

The proof records publication and authority digests, capture and logical-data
digests, exact host container/storage identity, capture start, completion and
measured duration. The immutable `recovery_fresh_until` timestamp is 15 minutes
after actual completion. Current dry-run/readback eligibility requires a fresh
proof. An original completed receipt remains historical and is never refreshed
by replay. The later #3711 consumer must also require proof after its observation
window. This producer performs no retirement and clears no backup, health,
ownership or uncertain-effect gate. Host/service clock skew fails closed; a
failed timestamp check does not grant qualification.

The verifier inventories every running and stopped container. Only the exact
pinned, running application and database containers may reference the allocated
MariaDB/config volumes. A service-owned session acquires `FLUSH TABLES WITH READ
LOCK` and remains idle while a second connection runs `mariadb-dump
--skip-lock-tables`. This preserves mixed transactional and nontransactional
tables. Writes wait during the global read lock, potentially for the native
attempt deadline. Reads remain allowed after acquisition; acquisition and
engine-specific queues can affect latency. See the
[MariaDB FLUSH contract](https://mariadb.com/docs/server/reference/sql-statements/administrative-sql-statements/flush-commands/flush).
Native client and server statement deadlines constrain attempts on a best-effort
basis; a killed client alone proves neither server completion nor a strict lock
duration. Only a positively completed capture of at most 120 seconds qualifies.
Any live capture must account for this write pause; source availability alone is
not evidence of acceptable live impact.

The protocol conservatively requires exclusive use of the existing root utility
credential. Preexisting root sessions refuse before capture; later sessions may
keep quiescence unproved. No unrelated session is killed and no credential or
grant is added. Credentials remain inside the database container. Native source
launches are fenced as one original attempt, and source closure requires
positive cancellation and server quiescence rather than elapsed-time inference.
The native coordinator acquires its nonce lock and announces exact `READY`
before any SQL. Only its live host parent, still holding the attempt lock, can
send the matching `GO` over a private pipe after receiving that announcement.
No permission is prebuffered. EOF, malformed input or a five-second input timeout
refuses before SQL. A late queued exec after restart cannot receive permission
from a dead parent, even when `/tmp` cancellation state was lost. Docker exec
processes end with the container's PID1 and are not restarted with it; see the
[Docker exec lifecycle contract](https://docs.docker.com/reference/cli/docker/container/exec/).
This uses no database-volume metadata, image configuration or ownership repair.

One monotonic host journal and lock own that attempt. A missing journal can be
permanently tombstoned under the same lock, fencing a queued original worker.
After native source closure and verification of the stable SQL/config artifact,
the host durably records `source_closed` before any scratch-create dispatch.
All subsequent commands affect only its independent scratch. Create intent,
private name/path and exact isolation specification precede create; exact
observed container identity/isolation is durable before start or import.
Generation-specific provider schedule identities prevent delayed old triggers
from receiving a newer script.

Scratch has no application, network, published port, source mount, replication,
event execution, binary logging or inherited service credential. Restore uses
explicit autocommit control. Integrity checks, useful nonempty business data and
an exact same-image canonical SQL roundtrip are required. Config content,
permissions and ownership remain stable across capture. System/auth databases
are independently initialized. Unsupported or nondeterministic roundtrips
refuse. The 2 GiB source/dump ceiling is a safety bound, not a promise of recovery
throughput; time and free-space bounds may refuse a smaller database.

After source closure, a lost restore or cleanup reply may terminalize with
`recovered=false` and an explicit `retained_scratch` disposition. It identifies
the originally observed isolated container ID, private name and path as
`retained_or_cleanup_unconfirmed`; it claims neither presence nor absence.
This releases the shared production target without resuming, reusing or cleaning
that uncertain attempt. A new generation uses disjoint names/paths and repeats
space checks. A lost create reply can also settle as `stopped_create_unconfirmed`, with an
unknown ID, when the exact source-closed create-only journal is intact. Docker
create produces a stopped object with restart disabled. Every start is a
separate ID-only call after isolation is durably observed. Thus any late create
cannot run code, and the terminal tombstone prevents a later start. This makes
no claim that an object exists or is absent. Corrupt create intent, unproved
source closure, or a start phase without durable isolation keeps the target held.

After verified scratch cleanup, success durably records a value-only receipt
before removing its own SQL/archive copies. It does not create a second backup
service. Known pre-scratch failures remove only their own partial files. Unknown/retained
attempts keep their private data and explicit custody; no automatic cleanup
endpoint, retention service or maintenance promise is introduced. Original-key
inspection never starts a new capture. Cold bootstrap keeps its existing archive
and safe same-key retry contract.

After changing either verifier, the implementing agent runs the focused host and
adapter tests plus the real storage gates before handoff. PostgreSQL uses the
repository's normal integration gate. The explicit disposable MariaDB proof is:

```bash
LAUNCHPLANE_TEST_MARIADB_IMAGE=<reviewed-immutable-mariadb-image> \
LAUNCHPLANE_TEST_MARIADB_DIRECTORY=<task-local-docker-accessible-directory> \
uv run --extra dev python -m unittest tests.test_production_recipient_mariadb
```

Run `tests.test_production_recipient_published_host.RealPublishedMariaDBTests`
with the same explicit local-image variables to prove populated InnoDB and
nontransactional Aria recovery while their original databases remain running.

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

## Accepted-release cutover and exact inverse

`POST /v1/products/{product}/production-recipient/cutover` reviews and arms an
exact preparation. It does not deploy or publish. Supply the preparation digest,
every existing private monitor's replacement URL, a private readiness URL,
every active backup policy's replacement source target, the existing managed
application API writer binding, and a completed service-owned destination backup
operation. Use `--cutover` with the terminal consumer, separate private request
and evidence files, dry-run, then reviewed apply under a stable key. The saved
intent binds all current authority, consumer bindings and managed versions. Before
execution, a fresh reviewed intent can replace an expired destination capture;
every earlier intent stays in custody history. Once execution begins its admitted
intent stays fixed, and recovery does not expire the already-admitted destination
capture while resolving that admitted release, including its drill's resume of
the same already-bootstrapped recipient. The fresh source-backup gate and live
identity, readiness, resource and upstream checks still apply.
Source backup and destination capture must be current; the isolated recipient
recovery proof is also required. Capture alone does not qualify recovery.

The bounded custody result includes `publication_sha256` after publication.
`--inverse` uses that digest, the preparation digest and context to review and apply
`POST /v1/products/{product}/production-recipient/inverse` through existing production
rollback authority. It works independently of whether this release has a drill.
Apply requires its saved exact review and original inverse key; terminal agents
cannot apply. It never grants authority or starts a new release. An admin
inverse ends this generation's automatic forward continuation; a later move needs
a freshly reviewed generation and the Client accepted-release path.

Only the existing Client accepted-release worker starts bootstrap. It checks the
exact accepted artifact/commit, live release grant and production backup gate
before effects and at publication. This also supports the first-release rollback
drill: restore the retained recipient and its original data, capture again, then
resume the replacement without redeploying or initializing either database.
Arming an intent or merging this source does not authorize a live cutover.

The worker temporarily excludes effects by other service operations sharing the
API writer budget, including already-reserved operations at their next provider
effect checkpoint. It verifies all recorded testing peers are parked, pauses
only the old application writer, starts the prepared replacement and verifies
health, bootstrap readiness and exact runtime identity. Actual Docker metadata
must agree with both immutable images, database command memory bounds, enforced
CPU/RAM, independent named volumes and reviewed listeners. It then publishes
the target and provider records, inventory/deployment evidence, private monitor
endpoints and append-only backup source revisions in one PostgreSQL transaction.
The commit repeats custody, authority, release and execution-lease checks.

A shared testing writer can be coordinated through
`POST /v1/products/{product}/production-recipient/testing-writer`, using
`--writer-control`. This uses existing testing deploy authority, requires positive
disposable-lane evidence and names the exact armed preparation and testing secret
binding. Review separate pause/resume requests and retain each original key. Each pause
receipt returns `pause_operation_id`; pass that exact ID on its resume request.
A later pause uses a new operation key and witness, so cycles remain repeatable
without reopening an earlier released pause. Testing authorization uses the
product profile's actual testing context, including when it differs from prod.
Only the recorded application container is stopped or restored; the database and
unrelated containers remain untouched. A resume restores the witnessed original
running state. A stopped writer stays stopped. Host terminal witnesses support
receipt/readback after a lost reply. Bounded custody lists the original writer
operation and binding references, including carried pauses after supersession.
Historical receipts read the retained host witness even if the provider target
was removed; they prove the original completion only. Cutover separately checks
that every current and retained testing peer is parked. Missing completion
evidence remains unknown.

Rollback stops only the exact replacement application writer and conditionally
inverts task-owned authority rows, then resumes the retained writer only after
its binding is restored. Extra monitors, other lanes and unrelated settings
survive. Any changed owned row refuses the whole inverse; no shared guest is
restored and no data volume is deleted. Backup history receives a new inverse
revision rather than being rewritten. The release worker and reviewed recovery
path retain both recipients when a deploy or writer outcome remains uncertain.
`--release-recovery` reviews and resumes an existing Client release step with its
original decision, kind and attempt. Planning returns the existing original
idempotency key; apply takes it from the saved review rather than minting a key. It cannot invent a
step or bypass acceptance, backup gates, leases or terminal provider evidence.

## Retained original-key continuation

After an unknown create or authority drift, authorized product readers can read
`custody` without borrowing the original preparer's identity. Continuation writes
retain the original preparer, caller scope and preparation key. This read intentionally reports the
retained record even when fresh qualification fails; it grants no bootstrap
eligibility. Use `--continuation` to review that custody digest, the original key,
a replacement preparation request and a reason. Apply requires the exact saved
review. Every observed or uncertain project, environment, target, volume, port,
reservation and peer-writer reference stays in prior-attempt history. The
replacement receives a distinct inert namespace and a derived execution key
under the same public original preparation key. It needs fresh preparation,
upstream checks, recovery, destination capture and cutover review.

Expired worker leases alone never prove absent host effects. New preparations
wrap every fixed host job in a per-generation execution lock. Supersession first
writes a permanent tombstone under that lock; an active job blocks it and a queued
late job refuses the tombstone. An unknown create before any possible host job
can be retained and superseded without claiming provider absence. Legacy
unwitnessed host effects remain fenced until positive terminal evidence exists.
An active or uncertain cutover cannot use preparation supersession as recovery.
A settled inverse or a refusal before writer dispatch can be reviewed for a new
generation, retaining its original execution records and fencing every recorded
recipient or peer host before supersession. Unknown testing-writer operations require a positive retained terminal receipt
before adoption, and that receipt stays in continuation history. Missing writer completion evidence
never proves that a queued pause cannot still run. The original release recovery
keeps that fence until positive terminal evidence exists.

## Fencing an unwitnessed writer dispatch

A writer pause can be dispatched and then lost before its host job writes the
original witness, for example on a schedule trigger timeout. Its effect stays
unknown: the queued job may still run. `--writer-fence` (route
`POST /v1/products/{product}/production-recipient/writer-fence`) reviews one such
operation by its original operation ID. It uses existing production setup
authority, as continuation does. It admits only an uncertain testing-writer
operation, the original pause of the retained writer during cutover, or the
replacement pause of an inverse, and only after the original worker lease has
expired.

Apply runs the fixed writer program on the operation's own recorded host, and
refuses if the provider has since moved the target to another server. It runs
under the same writer lock the original job takes. An active job holds that lock
and blocks the fence. If the original binding persisted `before.json`, the fence
refuses: a witnessed writer may have stopped, and is reconciled from its witness
instead. Otherwise it exclusively creates `before.json` as a permanent fence
record for that binding only. Every dispatched writer program, including a job
queued before this fence existed, refuses a witness that does not match its
container before touching one, so any late job for that binding refuses. Other
bindings and later operations are unaffected. Launchplane then records the
no-effect disposition in custody, checking that custody and the original
reservation did not change during the fence. The disposition is the flow's own
"no writer was touched" state: a testing operation settles as not completed, a
cutover attempt returns to `preflight_refused`, and an inverse is aborted with
the replacement still published.

The fence never completes the reservation itself. Retry the original operation
key through its own route (`--writer-control`, `--release-recovery`, or the
inverse) to adopt the recorded no-effect result. A fenced pause cannot be
resumed; start a new pause instead. A cutover settled this way can continue
through generation continuation. Readback verifies the retained host tombstone.
Elapsed time, a missing container or a missing target never substitute for the
fence.

Host custody lives under `/var/lib/launchplane/recipient-execution/<namespace>/`
and `/var/lib/launchplane/recipient-writers/<binding>/`. Execution tombstones and
writer witnesses and writer fences are fsynced and retained. A permanent writer-release marker
rejects delayed pauses after restoration. A completed resume never starts a
container again. An interrupted start can retry only while its original pause
still owns the writer; an older resume cannot undo a later pause. Preserve these directories and recovery
archives through host maintenance and rollback; removing them would remove the
late-worker fence. There is no automatic host-custody or old-volume cleanup.
Retirement remains a separate scoped operation after the successful-cutover
observation period above, with fresh reference checks.

A writer dispatch with no terminal host witness stays unknown, including a
pre-witness host refusal after schedule dispatch. Its reserved target cannot be
reused or abandoned by guessing that the queued job will never run. The narrower
per-operation host-fence recovery is tracked in [#3811](https://github.com/cbusillo/launchplane/issues/3811).
