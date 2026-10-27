---
title: Clean testing snapshots for previews
---

Q788 separates the source used to refresh testing from the source used to
create a PR preview. Testing reads the product's qualified real source.
Previews read a frozen, sanitized snapshot captured immediately after a
successful refresh, before testing serves staff.

The source contract is tracked in [#4756](https://github.com/cbusillo/launchplane/issues/4756).
The preview API binds its selected snapshot into the existing issued plan.
CM import-completion integration remains a prerequisite on [#4753](https://github.com/cbusillo/launchplane/issues/4753).
This page does not establish live activation or qualify any product's data source.

## Refresh and publication

The existing Odoo testing drill owns scheduled upstream refreshes, and the
native testing rebuild owns on-demand upstream refreshes. Both use the
existing restore, sanitizer and independent integration readback. Holds,
staff use, needed-copy custody and uncertain original effects retain their
existing admission checks.

`preview.clean_testing_snapshot` opts a product into clean snapshots. It can
be enabled before storage qualification to hold new preview copies. Snapshot
capture and use require the testing lane's reviewed `odoo_testing_drill_storage` budget.
With this mode enabled, a missing budget also refuses testing rebuild/capture
planning before source probes or planned-generation receipts. Qualify the budget
before rebuilding testing; opting in does not authorize a budgetless restore.
Scheduled refresh remains separately enabled by `odoo_testing_drill_enabled`.
Snapshot products are staggered inside the scheduler's 5:00–6:00 AM UTC nightly window.
A missed slot can catch up when the scheduler resumes. The same
existing worker executes the operation.

Capture occurs after sanitization and readback, while testing web is stopped.
A failed capture keeps that new generation held rather than opening it to
staff before its clean checkpoint exists. Previews keep their previous
qualified source. Existing guarded recovery owns retry and storage admission.
The host saves a bounded PostgreSQL dump and an independent filestore copy
outside every application volume. It verifies their hashes and atomically
renames the completed directory. It uses the existing clone-or-copy behavior
for the filestore.

## Storage

An SSH refresh uses the saved host SSH directory and identity from the bound
runtime environment when its source mapping contains only source coordinates.
An explicit source transport selection takes precedence. These paths remain
host-only metadata; no identity file or SSH directory enters an application container.

An SSH refresh stages the source dump and filestore on disk in
`launchplane-restore-staging` beside the testing data volume's `_data`
directory, never in `/tmp`, which may be a RAM disk. The container cannot see
that directory. Because it is on the volume's own filesystem, the filestore
moves into the volume by rename, with the runner paused, and is never copied
twice. The next restore removes staging that a killed run left behind;
retiring the volume removes it too. Every child of a restore inherits its
namespace lock, so staging is never removed while a child still runs.

The space check charges the SSH filestore once, as staging, and the runner the
dump. Snapshot filestores retain their full capacity budget even
when a real `cp --reflink=always` probe proves block sharing. Cloning saves
physical pool space, but dataset quotas can still charge each reference;
the available filesystem bytes must cover the whole admitted budget and reserve.
Each new capture rechecks its destination capacity, including when a restore resumes.
A proved clone runs `cp --reflink=always` and fails rather than silently
switching to a full copy. Database restores are also charged in full. Demands on
datasets sharing a ZFS pool are summed, using the smallest available-space reading in that pool;
other filesystems remain grouped by device. Dataset quotas can therefore make
the shared-pool check more conservative.

The newest qualified immutable database-backed capture is the preview source,
separate from the current testing generation. Worker completion records it
atomically; there is no second mutable pointer. Repeated completion preserves
the original capture receipt. A snapshot-domain publication refusal records
`clean_snapshot_published=false` on the terminal operation and preserves the
previous source; database failures still roll back the transaction. Failed restore,
sanitization, capture or health checks keep the previous receipt. A testing
hold does not remove that receipt or turn preview selection into a read of
the staff-used database. Partial captures cannot become preview sources.

## Preview copy

A new preview selects one completed same-product snapshot. An existing preview
with a qualified completed deployment keeps its data and needs no snapshot
lookup, including before the product has published its first snapshot. There
is no production or live-testing fallback. The preview and snapshot must be on
the same execution server; cross-host transport is not implied.

The copy receives no source SSH mount or upstream connection. Private-port
preview renders also omit the host-gateway alias; runtime inspection rejects
host aliases before copying. Its schedule
uses the immutable image bound to the preview request, without requiring
a separate image setting in the runtime environment. Snapshot schedules use
the existing restore execution budget, independently of the shorter deployment
wait. An explicit schedule execution timeout still takes precedence. A new preview
uses a Docker local overlay volume with the frozen snapshot filestore as its
read-only lower. Its private upper and work directories live beside the preview
data volume's `_data`, outside application mounts. Docker mounts the overlay at
the preview database's filestore directory in both application containers.
The volume uses `userxattr`; there is no full-copy fallback. Preparation preserves
populated databases and filestores unless the existing exact-volume receipt and
SQL witness prove an owned attempt that never opened to a user. An incomplete
legacy attempt whose database holds its exact restore witness resumes on its
existing filestore, preserving attachments written during that attempt. Other
owned incomplete legacy attempts can restart with an overlay without copying
the filestore again. A declared overlay that is missing refuses deployment when
the database or writable filestore could contain retained preview data.
An empty target can resume allocation after its compose was persisted, including
an interruption before the first receipt. Existing-target qualification permits
the pending overlay to be absent before compose declares it; the data, database
and log volumes must still exist. An owned incomplete allocation can
reconstruct its overlay before the database has its completed restore witness.
An owned reset restarts the runner even if cleanup fails, with web still held.
Later deploys retain an existing
overlay and its original source, even when the newest snapshot changes.

Admission charges the full database allowance, the bounded dump's actual bytes
and existing reserve against real filesystem availability. The testing snapshot's
filestore staging allowance is not charged again. The shared lower adds no
per-preview filestore charge. Upper writes consume the unchanged host quota.
The configured memory floor also applies to a new snapshot restore on an
existing provider target. Restore avoids recursive ownership changes that
would copy every lower file into the upper. The lower must already match the
workflow UID and GID. Ordinary maintenance also skips recursive ownership and
mode changes on an overlay, so the next deploy does not copy its lower up.

The inert preparation plan checks source receipts, storage ownership and disk
headroom before creating application storage. Launchplane persists the planned
volume declarations and environment on the target before allocation. An
interrupted allocation retains that target and its original operation for
recovery, rather than deleting the target that owns the partial storage.

The restore stops the preview writer and restores the frozen dump into a missing
database, then uses the existing credential, sanitizer, module-update and
connection-readback boundary before starting web. Existing preview databases
and Client edits are preserved. A failed initial restore carries an exact volume-generation-bound ownership
receipt and a preview-specific SQL witness. An owned attempt that never opened
to a user can restart from its verified source after a partial restore or failed
module build. Ready metadata is fsynced before either gate or web is released.
Ready copies, foreign witnesses, old receipts and ambiguous reads preserve their
data. Later builds keep Client edits. The marker inherited from testing cannot
prove a preview restore. Retry evidence records both the requested snapshot and
the original snapshot retained by the attempt.

Preparation and restore use the same nonblocking exclusive lock for their
Compose project. Each host pass retains it through its own work; the restore
holds it through runtime inspection, sanitization, module work and cleanup.
Every host subprocess inherits its descriptor, so a retry
refuses while an earlier run or its surviving children still hold it. Different
preview targets can restore concurrently. The persistent target lock is separate
from the shared snapshot read lock, which is released after copying.
Runtime inspection also requires the runner's Docker `ExecIDs` to be empty;
an in-container restore can outlive its host CLI client. Active or unavailable
exec custody refuses before any preview data or web state changes. An owned
runner left paused by an interrupted copy is unpaused under the target lock;
the idle check briefly retries to let a short health probe finish. This checks
active exec roots; it does not establish custody of arbitrary in-container
descendants after their exec root dies.

PostgreSQL 17 and separate preview database volumes still require a full
database restore. Snapshot capture retains its existing clone-or-copy behavior;
only new preview filestores use overlays. This does not provide database copy-on-write.

## Idle previews

Service-issued previews without upstream connections put their public route on
a small router from the same immutable image. It receives the preview generation
identity, with no application environment file, data mount or source credential.
The existing network layout stays the same. A private Unix socket lets Launchplane
read activity and coordinate sleep through its existing provider transport.
There is no public control endpoint or outbound wake callback.

After fifteen minutes without traffic and with no open connections or active
runner execution, Launchplane freezes forwarding and stops web, the runner and
PostgreSQL. It retains their containers, database, attachments and overlay source.
A visitor sees a page that checks again while Launchplane starts the preview.
A waiting non-GET request is forwarded once after wake, or receives a temporary
failure if startup exceeds its bounded wait. The router never replays it.

Sleep and wake use the existing preview executor and product locks plus the
host project lock. They are best-effort, idempotent operations without durable
provider reservations or retained intents. Each pass observes the current ready
generation, gateway receipt and service state. A partial stop finishes on a later
pass, after any failure backoff, before admitting a requested wake. This lets
the next wake measure headroom after the stop completes. An admitted partial
wake resumes from the gateway's persisted receipt, even after a router or
scheduler restart. A lost completion response needs only another observation.
A failed stop/start backs off that preview for five minutes in memory, allowing
other previews and ordinary refresh/destroy operations to proceed; no retry
intent or budget is stored. Busy host locks and failed observations retry on the
next pass. Manual stops, staff testing holds, changed generations, other
provider reservations and unavailable custody still block idle effects.
Existing previews gain the router on their next normal data-preserving refresh.

Wakes serialize on the host and check the configured memory headroom threshold
and disk reserve before starting stopped services. PostgreSQL starts first,
followed by runner and web. A wake keeps the existing executor and host startup
locks for one attempt, with one shared five-minute health deadline. Docker
reporting a stopped, restarting or unhealthy service fails the attempt at once.
The gateway opens only after all services pass health and the exact runtime
identity agrees. Insufficient capacity leaves the preview asleep; a later pass
checks capacity again. The gateway's wake receipt identifies recovery; any
attempt that still needs to start stopped services checks current memory and
disk again. If admission refuses a partial wake, its running services stop so
it releases their memory before the next pass. It does not reserve capacity
across an interruption. A preview that
never becomes healthy can be refreshed or destroyed through the ordinary
preview path. Data-preserving refreshes also credit their own running services
after matching the current target's identity; cold starts still meet the memory
floor.

The normal schema migration removes historical idle reservations and their
retained intents, leaving other operations intact. A database constraint refuses
late idle-reservation writes from an older overlapping service before effects.
Gateway receipt IDs keep the existing wire format for routers already deployed;
they are opaque correlation IDs and no longer refer to database reservations.
Refresh and destroy use their own ordinary recovery paths and have no idle
supersession special case.
Quiet polling does not take the preview executor lock or perform a host-wide
storage inspection. Full qualification runs under exclusion before effects.

## Product transition and custody

OPW's preview repair landed in PR4734. Its transition still needs the Director's
actual testing hold lift and the existing source/artifact/custody qualification.
After source deployment, opt the product in and run its first admitted
refresh. A Director agent may conditionally set only `preview.clean_testing_snapshot`
from false to true through the existing `POST /v1/product-profiles` route. Read the
current profile and strong `ETag`, preserve every other field, and submit that
exact replacement with `If-Match` and a stable `Idempotency-Key`; read back the
profile after acceptance or an uncertain response. This opt-in preserves the budget,
retention bindings, drill state, source settings, Client and release authority.
The agent rebuilding testing separately qualifies and records its four
[testing storage requirements](service-boundary.md#testing-storage-requirements)
through the same conditional profile route, preserving retention bindings and
every other field. These changes cannot be combined in one write. A missing
budget refuses snapshot use before reading a completed snapshot; opting in does
not admit headroom or start a refresh. Opt-out still requires full profile authority and custody review.
New preview copies refuse until a reviewed budget exists and an admitted refresh
publishes a clean snapshot. Existing preview data remains intact. Enable the existing daily
drill only with its qualified source, storage budget and custody checks. This changes no production key or access grant.

CM first needs CMSEED's qualified Fishbowl/RepairShopr fill and supported
sanitizer/import-completion execution. Capture must attach to that completion;
an ordinary code deploy must never snapshot staff-edited testing. CM's
production reader is the later source once CM runs its business on Odoo.
Existing Client-used previews remain protected.

Snapshot bytes remain in private host custody, independently of testing or
preview volume retirement. Cleanup keeps the published source and its predecessor,
unexpired issued preview plans, active or uncertain preview operations, and
incomplete host restore attempts. Completed overlay attempts keep the same pin
until their original backing volumes are retired, preserving the lower even
after an interrupted teardown removes the overlay volume. Every overlay volume also pins its lower
snapshot by its validated snapshot label, including completed or sleeping previews and volumes without running
containers. Preparation creates that pin under the shared snapshot lock before
deployment. Preview retirement removes the overlay and private writable backing
with the preview's other volumes. An orphaned overlay still pins its source and
can be retired by its project after the writable backing has disappeared.
Invalid source labels defer snapshot retirement. Only obsolete copies with matching immutable
service receipts may be removed. Host copying uses a shared read lock; retirement uses an exclusive lock. The
read lock is released after copying, before sanitizer and module work. Attempt
metadata is retired only after every original volume and physical backing is
proved gone or replaced. Unknown hard-crash partial directories remain in
custody; they are never swept by filename.
Unknown reader evidence preserves copies. Reviewed headroom is checked before
testing stops; insufficient capacity keeps the previous source usable.
New overlay previews require database and dump-staging headroom plus the
reviewed reserve. Snapshot capture retains its separate filestore headroom
gate. Neither cleanup path removes testing originals, backups or unknown files.

Obsolete snapshot retirement commits an atomic tombstone rename before deleting
bytes, so an interrupted delete can finish on the next pass. Cleanup errors and
malformed reader metadata defer retirement; capacity still controls refresh
admission. Unknown capture partials remain preserved. Cleanup runs with an
opted-in refresh; opting out or disabling drills requires a separate custody
review for retained bytes rather than an automatic purge.
