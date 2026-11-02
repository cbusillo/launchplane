# Odoo two-slot releases

`workflows/odoo_cutover.py` executes both release classes from one immutable
artifact pair. `OdooCutoverPlan` recomputes the classification using the
[verified declaration contract](release-database-compatibility.md) and refuses
an incomplete module plan. Each slot binds its source/image/runtime identity
and selected database. The serving slot's deployment identity remains its own;
the prepared copy's release binding names the new cutover operation.

The product lane's persisted `odoo_cutover` record owns slot, route, database,
prepared endpoint and writer coordinates. `odoo_cutover_plan.py` combines that
record with current inventory and verified artifact/tuple authority; release
requests cannot supply provider coordinates. The existing promotion executor
uses this plan inside its Client-release and independent-backup guard.
`DokployOdooCutoverProviderFactory` binds installed host capabilities to the same
records. A configured lane without a bound provider refuses in-place deployment,
as does an unfinished recorded cutover even if inventory still names the old
artifact. The HTTP service and native worker accept the installed factory.
Source implementation does not activate a live lane. The service worker must
bind the `OdooSlotDriver` capabilities to the admitted lane's provider records,
managed environment, exclusive operation lease and observed writer inventory.
No capability grants access, copies secrets, or supplies checked-in site values.

## Provider preparation and authority

`slot_prepare.py` updates/deploys only the distinct private candidate Compose
project. `slot_compose.py` renders two app services on the explicitly supplied
external database network and filestore volume: no database service, public
port or Docker router. Both web slots have cron threads disabled. A web guard
waits for its exact slot/module-plan receipt marker before starting any registry.
Existing serving Compose configuration and routes must remain unchanged.

The installed candidate-preparation capability must preserve the serving slot's
consumer meaning for shared settings, including legacy credential transport.
Equal parsed values alone are insufficient when legacy and literal transports
differ. Preparing a shared-database candidate is not a fresh-database
initialization and must not re-encode its retained credentials. Installed-host
qualification must demonstrate this before lane activation.

Runtime identity omits unset slot, release, image-digest and database-UUID fields
so legacy container identities keep their reviewed service-restart digests.
Populated two-slot identity fields remain serialized and bound to verification.

`slot_maintenance.py` supplies the normalized candidate/production manifests,
resolved compatibility record and selected database to devkit's full
`--post-deploy-maintenance` entrypoint. It verifies the immutable runner image,
the baked declaration/actual graph, applied updates and module readback, then
verifies the selected physical database UUID through an independent connection
and seals the fresh committed boundary with this passing run before releasing
web. Replay consumes that bound receipt without another registry. The SQL-only
credential transaction in devkit remains the sole sanitizing/override policy;
no registry, shell or module hook is substituted for it. A started attempt with
no passing bound receipt stays uncertain rather than rerunning an upgrade.
The adapter uses the image's registry-free devkit resolver and an independent
SQL module inventory to seal the exact install/update work before the child.
The passing receipt must match that resolved work, including optional-addon and
dependency behavior, rather than merely containing a subset of static roots.
Secrets stay in the existing managed container environment, outside plans and
records. The legacy empty install pass is never called on this path.

`TraefikFileRoute` publishes one YAML route file with an atomic rename and
reads the actual router/service from Traefik's API, followed by the provider's
independent public identity probe. The watched directory must be mounted, not
the individual renamed file. The publisher rejects duplicate host routers,
foreign targets and missing applied evidence. An asynchronous apply can return
an uncertain result after a bounded applied-readback deadline; the persisted
controller reads authority before retrying. A file containing other routes or
services is refused rather than overwritten. Applied and public health evidence
records the observed endpoint and runtime, including a fresh switch-back probe.
See [Traefik's file-provider documentation](https://doc.traefik.io/traefik/v3.3/providers/file/).

## Compatible and database-changing paths

Private readiness executes the selected image's shipped readiness helper.
Synthetic cutover images inherit that helper from their immutable base without
installing a separate readiness implementation.

The local synthetic image builder accepts an explicit `target_platform` for a
published multi-platform base. Select an already-local immutable parent and an
available `linux/amd64` or `linux/arm64` child; inspection, graph/version probes,
wheel selection and builds use that same platform. A missing or unsupported
selection refuses without guessing the host architecture or another child.
Concrete single-platform images keep the existing default inspection path.
The rehearsal treats response header names case-insensitively while still
requiring HTML and exact served-mode/public-copy evidence.

A compatible release runs the full planned credential/settings/readback
sequence with no install or update, warms the candidate, and obtains fresh
private database/Host/render/asset/browser/identity evidence. The old slot keeps
serving through preparation. Then one route changes, public identity is read
back, the predecessor writer/cron process drains before its successor resumes,
and old connections drain. Keep the old slot and referenced assets retained.
Compatible slots must have identical managed settings, apart from their runtime
identity, so reapplying settings cannot replace the serving lane's credentials.
Switch-back, including recovery of an unfinished compatible release, requires
fresh old-slot readiness and the exact current route;
its intent is persisted so an uncertain switch-back can be reconciled.
Its deployment event has real times and republishes the retained artifact's
production tuple together with inventory. Default rollback uses the cutover's
own retained artifact, rather than inferring it from null-finished history rows.

A database-changing release first prepares and browser-checks the bound
[public bundle](prepared-public-serving.md). The pause begins before form
blocking or writer fencing. Prepared serving is published/read back, every
writer drains, and a coherent image/database/filestore recovery point is
captured after verified drain. The [online logical backup](odoo-online-backups.md)
and existing independent infrastructure protection are both required. Only
then may targeted maintenance, candidate startup/warming and private readiness
run. The candidate route is published/read back before exactly one running process per
writer target resumes. Prepared serving never falls back to the updating DB.
`ContainerSlotWriters` stops and observes the bound predecessor containers,
requires the database observer to report their jobs drained, starts the target
idempotently, then verifies one running target container per writer. Web traffic
authority is the applied route; cron and other scheduled processes use the
separate writer inventory. Neither web slot starts inherited image healthchecks
before its guarded startup.

The `OdooRecoveryTuple` records both backup IDs, image, selected database,
recovery timestamp and dump/filestore hashes. Image-only rollback of a
database-changing release refuses. Rollback target resolution and the product
path check share that same refusal: `rollback_requires_coherent_restore` is a
blocked image-rollback path, never a recorded usable target. Use the existing authorized coherent-tuple
restore workflow; source cutover work never authorizes restoring a live site's
data or overwriting later business writes.

## Records, retries and activation

Product re-onboarding preserves the exact matching lane's `odoo_cutover` record.
Stable target replacement reports `two_slot_cutover_required` for a configured
two-slot lane or a recorded serving slot, including when configuration has been
removed. It cannot recreate that lane's serving project. Use the accepted
two-slot release path; repairing missing lane authority requires restoring its
record before another release. Apply rechecks current lane authority after
planning and before any provider effect.

Stable logs and outgoing-email diagnostics select the slot in current serving
inventory from the lane's recorded Compose coordinates. They inspect that
slot's container/image/runtime identity and reject missing slot authority or an
unfinished cutover. Switch-back events prove the retained runtime identity
through their explicit deployment link. Mail diagnostics use the recorded
public route host and independently verify its current runtime identity before
authentication; private slots do not acquire public Dokploy domains. Selection
is checked again after the read, so a concurrent switch cannot return stale
diagnostics as current evidence.

`DeploymentCutoverStore` stores typed checkpoints, slot/route identities,
classification, maintenance/readiness/backup observations and the full
`PublicPauseRecord` in the existing DB-backed deployment payload. There is no
new parallel shared truth or schema table. File/SQLite stores are isolated
rehearsal paths. Provider exclusivity and the existing worker lease protect
effects; payload storage is not itself a distributed lock.
The native worker reacquires the original held operation under its lane lock,
service-drain admission and a renewed lease. Reconciliation preserves the
original phase and bounded checkpoint history. Forward replay uses the existing
configured attempt limit; authority refusal refunds its reserved attempt.
An original abort, inverse or terminal audit remains available after that limit.
Maintenance without a passing receipt may enter or observe only its original
atomic native attempt, which never starts a second upgrade child. Those original
outcome observations do not consume the forward retry budget; a definite
validation failure stays held. Once a passing receipt exists, startup replay uses
the forward budget. One production-write
checkpoint separates fresh admission from continuation. Dispatch guards read the
current lease, original decision, ownership or Director authority, production
identity and recovery pin without appending checkpoints. Production identity uses
the admitted artifacts, not website metadata that the release itself updates.
Testing freshness, release holds and release windows gate the start; they do not strand a release
after its first production write. These checks belong to the native adapter,
including its metadata saves; legacy in-place after-write behavior is unchanged.
An ordinary drill or rejection inverse still passes its fresh start gates before
its own first write. A completed forward release does not admit a fresh inverse.
Changed ownership, revoked authority and foreign production identity still refuse
native effects. A refused native inverse retains its
reconciliation hold without effects; the service never adopts another caller or
turns revocation into an automatic Director override.

Recovery reuses the stored candidate, classification, website snapshots and
original passing logical backup. Fresh requests still use current admission and
classification. The infrastructure backup guard evaluates capture freshness at
original admission while retaining current policy, retention and competing-use
checks. A finished cutover whose worker lost its terminal write verifies the
serving route and completes its original audit without another capture,
maintenance run, writer readiness or switch. A rolled-back deployment retains
its original cutover provenance for the same read-only completion path. Only
original-operation reconciliation opts into that terminal provenance lookup;
ordinary and later manual rollback lookup retain their existing behavior.
Terminal audit still requires the original installed host/lane authority to remain
valid. Missing or changed private resources remain a hold; this is not a route-only
reconciliation interface.

Unfinished work retains the lane hold. Reconciliation yields to pending release
queues, including rollback and replacement, and waits 30 seconds after the last
update, including after denial. That delay survives worker restarts and lets
other eligible lanes run. Promotion claim admission and execution share the same
original-record, lane, release and deployment identity predicate. A mismatched
stored cutover stays held without taking a valid lane's recovery turn.
Preparation and compatible settings-free inverse
interruptions use that same original-operation continuation. A Client rejection
retains the canonical last accepted release at its rejecting decision as the
inverse target, including when multiple temporary hotfixes were stacked. A later
acceptance cannot retarget the original inverse. Switch-back intent retains its
original start time. The Client view uses the same begun-native-operation boundary;
it does not report fresh testing, hold, window or service-drain admission blockers
for work that is already continuing. A real native revocation or recovery hold
remains visible.
The direct promotion route uses the original logical-backup identity for a
stable recovery key; queued releases retain their operation identity.

Persist intent before publication, maintenance, switch and resumption. Failed
preparation/readiness keeps old traffic or verified prepared traffic intact.
An uncertain switch reads current applied/public authority; an uncertain
resumption retains its fixed recovery target and idempotent writer receipt.
Missing evidence never invents a success, pause end, zero active connections
or writer count. The pause duration includes fence/drain, backup, maintenance,
startup, warming, readiness, route application and writer resumption. Zero 5xx
while forms pause is reduced service, which remains visible in the bundle and
record.

Activate through the exact Client-accepted service release with explicit slot,
volume/network, prepared-serving, proxy and runtime capabilities already
available; this is separate from source/component acceptance. Missing provider
access or activation prerequisites are reported, never bypassed with a local
live-target command. Real-release acceptance remains on #3268 and the parent
#3237. Delivery follows the repository's [delivery boundary](../AGENTS.md#delivery-boundary).

## Typed website settings

The release decision supplies both the candidate and retained production website
snapshots. They are persisted in the immutable cutover plan and bound into the
native maintenance payload digest; a retry cannot substitute mutable prepared
intent. Preparation of website intent remains inert for two-slot lanes.

`run_planned_slot_maintenance` applies the candidate snapshot with Launchplane's
native website consumer after the credential boundary and module work pass,
before releasing the web startup guard. The receipt includes the committed
settings digest and the independently read-back durable startup digest. Missing
or mismatched evidence blocks readiness, route switching and writer activation,
including recovery from persisted intermediate steps. The deployment bridge
records the verified applied baseline separately from pending preparation.

The installed host adapter must bind `configure_startup` and the driver's
`website_settings` capability. `dokploy.slot_startup.DokploySlotStartup` supplies
the concrete Compose startup operation using the existing host transport and
the same `ContainerSlotWriters`. Read-only preflight verifies both projects'
observed Compose files and env-file consumption. Under the writer fence it
preserves unrelated provider/file payload values, reads back both, recreates
only inventoried consumers with `up --no-start --no-deps`, and refreshes their
shared inventory before native image/settings verification. An incomplete or
uncertain recreation keeps the release held. Source delivery does not install
this capability or configure a real lane.

### Interrupted startup recovery

`DokploySlotStartup` requires an explicit host `authority_directory`: an existing,
canonical private runtime directory, persistent across API/worker replacements
and available through the same admitted host transport. It is recovery evidence,
not checked-in configuration or a second lane catalog. Before the first provider,
file or recreation effect, the adapter atomically publishes and fsyncs an immutable
record keyed by the complete admitted plan. It contains observed project,
working-directory, Compose-file and writer/service coordinates; no environment
values or Docker IDs. A conflicting, missing or malformed record never grants
recovery authority. Retain it while that cutover can require recovery.

Normal startup refreshes IDs from fresh Docker project/service label inventory.
A lost reply leaves the operation held. If interruption leaves zero or multiple
containers, the installed host uses `DokploySlotStartup.recover(plan, slot,
intent, verify_guard=..., active_jobs=...)`, under the original exclusive release
guard and already-published prepared-serving write pause. `verify_guard` must
freshly verify both that guard and the persisted pause; `active_jobs` observes the
selected physical database by logical slot/writer identity, even when no container
exists. Neither callback may infer drain from a missing or stale Docker ID.
Reconstruct the writer binding from the original admitted writer identities;
recovery reloads service coordinates from durable authority before reading IDs.
It does not repeat native maintenance, clear uncertain maintenance/backup markers,
close the pause, commit native settings, release a web guard or resume writers.

A recovery call performs one convergence pass, validating both projects, immutable
Compose images, env-file consumption and every matching container before effects.
It refuses foreign images/labels/files, one-offs, running/restarting/paused writers
and undrained jobs. Fully matched stopped duplicate remnants can be removed with
non-force container removal, retaining volumes. Zero inventory is recreated with
`up --no-start --no-deps --force-recreate --no-build --pull never`. Both durable
startup payloads are merged/read back, unique stopped inventory refreshes the
same shared writer object, and independent effective image/settings/indexing
verification precedes the receipt. Missing readback, guard loss or a lost reply
keeps the operation held; a later call begins with fresh observation. There is no
unbounded retry loop, deployment fallback or shell repair procedure.

Before activation, [the installed qualification](https://github.com/cbusillo/launchplane/issues/3268)
must prove the host package/transport, persistent authority directory and its
atomic publication permissions, exact Compose behavior under interruption,
logical-writer database observer, guard/pause callbacks and shared writer wiring.
Qualify these capabilities and representative forward/rollback recovery in
isolation before any exact Client-accepted gated real release; source tests do
not activate a lane.

The opt-in `tests.test_odoo_slot_startup_rehearsal` exercises the startup adapter
against real, stopped Docker Compose services. Select an already-local immutable
image through `LAUNCHPLANE_TEST_STARTUP_IMAGE`; run the
`qualityGate.test.odooStartupRehearsal` command in `.github/github.json`. It
creates disposable projects without networks, published ports or data mounts,
uses inert environment values, and removes its projects and files afterward.
It covers a lost reply after recreation, reconstruction from durable authority,
zero/multiple stopped container inventories, foreign one-off refusal and
effective forward/rollback startup payload readback.

The gate refuses an unset image instead of reporting skipped tests as proof.
The rehearsal requires a local Unix-socket Docker endpoint. If the current
context or `DOCKER_HOST` selects a remote engine, select an existing local
context with `DOCKER_CONTEXT` before running it, after unsetting `DOCKER_HOST`.
The rehearsal refuses competing `DOCKER_CONTEXT` and `DOCKER_HOST` settings
before any Docker command; use only one endpoint selector.

That rehearsal substitutes the Dokploy environment-record API and the guard
and database observers. Its containers run no Odoo registry or jobs. It does
not qualify the installed service package, remote transport, actual process
interruption, physical-database drain, native settings commits or public traffic.
Both slots use the same fixture image, so this rehearsal does not qualify
cross-image slot identity checks. The inline host programs run on the test
interpreter, not the remote host's Python. Those remain part of #3268's
installed and full-release qualification.

`configure_startup` merges the selected snapshot and its legacy indexing parameter
into both slots' durable managed startup payloads, preserves unrelated settings
and secrets, and returns `StartupSettingsReadback` only after readback. It must
recreate every stopped writer container in both slots from those payloads and
return the complete bound inventory. Restarting an existing container retains
its creation-time environment and cannot satisfy this contract. Launchplane
independently inspects that inventory's images, stopped state, effective typed
payload and legacy indexing parameter before native commit or guard release.
The maintenance/rollback helper must receive the same `ContainerSlotWriters`
object used by the driver's handoff; readback must match its current targets.
Recreation under new names must update that shared inventory while fenced.
The driver also binds `website_settings_preflight` to verify both forward and
rollback startup/native capabilities before any cutover record, provider-effect
checkpoint or candidate preparation. It must keep the slot
fenced on failure. `run_slot_website_settings` checks the immutable runner image,
merges the native execution payload without exporting secrets, and requires the
consumer's post-commit digest. Missing capability refuses the release before
candidate preparation; there is no in-place fallback. Source support does not
configure those installed host capabilities or activate a lane. A Client's
hotfix rejection describes the rejected release's current production snapshot;
switch-back uses the original cutover's retained snapshot instead.
The run admission, recovery comparison and provider factory all bind the same
decision snapshots. Rollback repeats read-only preflight before any publication
or writer fencing, since forward-time capability proof may no longer hold.

Typed-settings releases use prepared public serving and a measured writer pause
even for compatible artifacts, so candidate settings never appear on the old
public artifact during failed readiness. Compatible switch-back reuses that
verified prepared bundle and records a separate rollback pause. It applies the
retained production snapshot before old-slot
readiness/traffic/writer activation. If the release has no historical snapshot,
it disables analytics and indexing across websites while preserving native
consent/embedding controls. The startup configuration must agree even for legacy
images, so a restart cannot re-enable forward indexing. The rollback receipt and
intent survive an uncertain switch; replay reads authority and the saved
receipt, repeating the idempotent settings operation only when its receipt was
not persisted. An abort before maintenance leaves the verified baseline and
collection unchanged. Database-changing rollback still requires the
existing authorized coherent-tuple restore path.

Forward and rollback writer pauses use distinct persisted provider idempotency
keys. A cached forward fence/resume receipt cannot stand in for the rollback's
fresh drain or old-slot resumption. The maintenance attempt marker is persisted
before the applied baseline is marked pending, so an interrupted pending write
still takes the settings recovery path.
If failed forward maintenance/readiness is recovered by rollback, the original
forward pause closes at the verified rollback resumption and retains its full
measured interval and actual old-slot recovery binding. A started maintenance
whose settings outcome is unproved remains conservative: without a verified
retained snapshot, rollback disables collection even if the failure occurred
before a settings receipt was produced. Only a pre-maintenance abort proves
that the candidate's maintenance never began.

### Logical writers and physical services

Each configured logical writer remains bound in both slots. Several roles may
share one physical container within a slot, with the slot's immutable image.
A container cannot serve both slots or images. Fencing and handoff stop, observe
and start each physical container once while returning complete logical coverage.

Typed startup keeps all logical writer-to-Compose-service bindings in its durable
authority, recreates each service once while stopped, and refreshes every shared
binding to the newly observed container. Original-operation recovery also
inventories/removes duplicate remnants once per physical service. It retains
logical database drain checks even when a container is absent. Effective settings
are inspected once per physical container after full logical coverage validation.

The rendered candidate includes separate web, scheduler and idle maintenance-runner
services. Web and scheduler wait on the same exact native-maintenance run marker.
The scheduler uses odoo-devkit's `--scheduler --ready-file --ready-token` startup
mode: cron enabled, HTTP disabled, without repeating initialization or overrides.
Artifacts used for this topology must include that startup capability. Older
images without it cannot qualify scheduler startup. Installed host binding and
isolated qualification remain prerequisites to lane activation; this source
capability alone proves no live release.

### Installed Dokploy host

The service package registers `DokployCutoverHost` as the `dokploy` member of
`launchplane.cutover_hosts`. API and Odoo workers discover that same installed
package. Discovery constructs no provider client and performs no effects. This
host implements Odoo slots; generic-web host activation remains unavailable.

The lane's optional `host_runtime` record supplies the watched route file and
Traefik API, public origin, remote authority directory, existing shared data
volume and private network, backup locations, website/module/readiness coverage,
public-copy paths and exclusions. It also names the retained prepared container,
its dedicated public volume and immutable Launchplane image, and its dedicated serving lifecycle. These are persisted lane
coordinates, never Python defaults or request-selected commands. Existing lanes
without these capabilities remain unavailable for installed-host activation.

The worker uses Dokploy's existing registered-server terminal WebSocket. It
requires both Compose targets on that server, private without provider domains
or automatic deploys. Production execution uses the existing operation's lease,
authorization and backup checkpoint at each dispatch. A lost terminal completion
holds the original operation; it does not become a successful deployment result.
The worker image needs neither a Docker CLI nor a Docker socket.

Route publication remains synchronous and local. API, worker and registered
provider host must share the same Linux host and the exact watched-file mount.
A read-only boot identity, inode, device and content comparison proves that
placement before admission. The initial file must already name the serving slot,
and applied Traefik authority must agree with its single exact route. Missing
mounts or file permissions are activation prerequisites, not a reason to use a
provider-side route write. Changing live ingress access remains a Director stop
boundary.

The service Compose mounts a dedicated prepared-public volume into API and Odoo
workers at `/app/prepared-public`. The `LAUNCHPLANE_PREPARED_PUBLIC_ROOT` bootstrap
setting names that mount. It contains only already-public immutable bundles and
an atomic active-pause pointer, not runtime records or credentials. Staging
reads back every resource, its digest and original pause ID through the retained
server before publishing the route. Another process can serve the persisted
bundle after the worker exits; missing or corrupt bundles return unavailable.

`prepared-public.compose.yml` is the separate retained server definition. Supply
its explicit image, container, volume and private network from the lane record.
Deploy/adopt the component through Launchplane's existing
[target setup and service deployment paths](service-boundary.md)
under the applicable release authority. It has no env file, credential source,
public port, database connection or Docker socket. It reads the public volume
only, as `launchplane-browser`, with a read-only root filesystem and dropped
capabilities. The host inspects the exact running image, command, environment,
network and mount before admitting it. Keep this component outside API/worker
replacement; retain its original image throughout an active pause. Profile-based
artifact protection includes its image through its lane record. A package
update must not replace this retained server while it serves a pause.

Admission seals the original plan, provider placement, maintenance payload,
run directory and startup stamp before effects. Default-only model additions
preserve semantic hashes. A package that changes an original maintenance payload
keeps that cutover held instead of selecting another run directory. Historical
records load their recorded classification; new admissions still validate the
classifier. Immutable prepared bundles omit additive defaults, retaining their
original content and pause authority across worker replacement.

Drain evidence comes from a registry-free connection to the selected physical
database, with its UUID verified. It observes selected-database and system-DB
connections for the runtime user, and prepared transactions. Persisted process
address history survives container recreation; absent containers never imply
zero database work. Unknown sessions or missing evidence hold the lane. Every
logical role keeps its own observation even when cron-family roles share the
separate scheduler. Handoff additionally checks the effective Odoo command,
configuration, current database connection, stable restart identity, and absence
of scheduler HTTP listeners. Each background role additionally binds distinct,
enabled module-owned cron jobs in the selected database. Missing bindings,
shared job identities or unsupported queue/integration executors refuse before
effects. This host supports cron-backed roles, not HTTP-dependent queue runners.
Docker `Running` alone is insufficient.

Candidate writers are fenced before maintenance releases the native marker.
Private readiness starts only its web process and keeps its cron-family services
drained. Real native readiness and a headless browser verify the declared pages,
assets and exact runtime; previous retained asset bytes must still match the
original capture. The coherent backup uses the existing online SQL/attachment
capture and archive verifier, followed by a network-isolated database and
filestore restore on a dedicated disk volume. One batch verifies every restored
attachment against the restored database. The command deadline comes from the
lane's operation timeout, whose default and bounds match the backup gate.
Exact restore readback is persisted before the tuple is returned. An atomic
native capture claim distinguishes intent from capture. A kernel lock belongs to
the capture coordinator and is inherited by every native dump or verification
child. A live child keeps recovery held even after coordinator death; an unlocked
protocol-bearing claim proves that capture ended without a manifest. Legacy
claims without that liveness protocol remain unknown.

A passing native-maintenance receipt is checkpointed before startup recreation.
Startup recreation requires that receipt, the coherent backup, complete logical
drain evidence and the still-published original public pause. Reading an original
maintenance result after a lost response requires the verified backup and drained
pause, so it can recover the receipt before considering startup recreation. It uses the stored startup authority for zero/multiple stopped inventory,
refreshes the same logical writer object, and rechecks the operation's lease and
pause before effects. Both promotion and rollback continuation retain their
original operation and key, wait behind pending work, and obey the reconciliation
delay without charging another deployment attempt. Missing, ended, foreign or unproved evidence remains
held. A failing candidate health probe can be switched back after retained-slot
private readiness; positive foreign identity still refuses rollback.

The cutover transport compresses command envelopes within its existing wire
bound. Decoded commands and output share a bounded buffer; incomplete or trailing
compressed input refuses before execution. Native argv, stdin and sealed
maintenance payloads retain their original bytes and authority.

Failed commands report only an allowlisted category and numeric exit code;
timeouts and missing transport completion never prove that effects ended.
Private health distinguishes connection failures, HTTP status, invalid health
data and foreign runtime identity. When its existing retry deadline expires,
readiness reports its start time, elapsed time, attempt count and configured
budget, followed by a bounded read of the candidate web and probe runner's
running, OOM and exit state. Unavailable state stays unknown. Commands, stdin,
response bodies, provider errors and container logs are excluded. These
diagnostics do not change the original operation's reconciliation hold,
readiness deadline, writer fences or recovery authority.

The installed qualification uses isolated representative Odoo 20 data and the
shipped web/scheduler commands. It must prove real scheduled ticks without HTTP,
physical drain, coherent capture/restore, browser readiness, persisted prepared
serving and original-operation forward/rollback recovery. Infrastructure backup
provider simulation in that fixture is explicitly separate from native logical
backup proof. It neither proves a real infrastructure backup nor activates a
live lane. Parent #3268 still waits for its exact accepted real-release event.

### Source recovery and retention

Canonical maintenance seals a failed outcome in its existing exclusive attempt
marker only after the child exits unsuccessfully and its log is durable. Before
dispatch, the native plan is published atomically and synced with the exclusive
attempt file and their directory entries up to the data mount. An interrupted
plan write can retry while no attempt exists. A persistence interruption refuses the child; a
retained started attempt cannot dispatch another one. A full log or outcome
volume leaves the original attempt UNKNOWN even after space becomes available. The
outcome binds the immutable plan digest. Reading that outcome ends automatic
reclaiming without starting another child. An interrupted command, partial log,
legacy started marker or missing boundary witness remains UNKNOWN.

Original inverse recovery deliberately continues under its original guards,
with the persisted reconciliation delay, after the forward budget is exhausted.
Before activation, #4844 must inject a persistent inverse failure and its repair,
prove recovery can finish beyond that budget, and prove replay cannot start
another upgrade, choose a new target or admitted caller, or starve other lanes. This source
choice preserves cleanup when the forward attempt budget has been spent.

Before a pause, guarded candidate preparation acquires the pinned immutable restore
image and checks its native tools and PostgreSQL archive major version. Provider discovery
only binds and reads. Executor preflight inspects the exact web container's
immutable command, mounts and environment, evaluates that environment with its
artifact's startup code, and inventories installed and planned module manifests
through the same artifact's runner. Its effective database name and connection settings
must match that runner. Read-only old-slot readiness verifies the database UUID,
credential boundary, required modules and absence of pending module work before
any public pause. Native PostgreSQL and Docker addresses are normalized before
attributing database sessions to physical writers. Web warming requires zero cron threads and the supported
core `base,rpc,web` server-wide modules and the Launchplane runtime health module.
The inventory comes from Odoo's parsed effective configuration, including artifact
defaults, and every loaded module's manifest is checked. Installed `queue_job`, queue configuration or
module `post_load` hooks remain unsupported; distinct cron jobs alone cannot
prove that an HTTP queue or integration runner is fenced. Qualify a different
executor topology before admitting it rather than aliasing it to a cron role.

The restore probe reserves cleanup time inside its outer transport budget. Every
hash, stream, readiness observation and database command shares the earlier
monotonic work deadline. Capacity is checked on the actual anonymous Docker data
volume against the source database and attachment sizes before copying. The
original operation admits only one restore allocation, using its label and a
deterministic name. Atomic host receipts bind intent, dispatch, allocation and completion
to the original capture nonce, immutable image and archive hashes. A reclaimed
operation verifies an orphan's full container identity and isolated configuration,
removes that disposable container and reads back its absence. It then aborts
the cutover; it never starts another restore copy. The
container runs without network access, owns its anonymous volume and enforces
its own maximum lifetime. Docker removes it and the volume on exit, including
when the coordinator dies. Cleanup uses the returned container ID, never a
reusable name. A timeout or lost reply cannot attest definite termination.
Only the process that atomically creates restore intent may allocate. It must
also win an atomic dispatch slot immediately before allocation. Cancellation can
win that slot first and prove no dispatch occurred. If dispatch won but its reply
was lost, cancellation records abandonment with allocation still unknown. A late
copy is network-isolated, has no production mounts, and owns only a disposable
anonymous volume with a native KILL lifetime. It cannot affect restoration of the
old serving slot. Abandonment never claims termination or passing verification,
and a late success cannot override it. Later observation can remove an exact
owned late container. Legacy unknown allocations without the dispatch protocol
remain held. A synchronously rejected allocation becomes definite only after
native absence readback.

A native capture writes a failure witness only after its synchronous work ends
unsuccessfully and only if that process created the original nonce claim. A
rejected duplicate cannot write that witness. A restored-copy failure is definite
only after cleanup reads back absence of the exact owned container. Definite
pre-maintenance failures may enter `aborting` under the original operation,
lease and plan. The original pause and complete drain must still agree; readiness
of the old slot precedes the switch and writer handoff. Recovery may leave an
unhealthy prepared server only when the original active pointer, verified bundle,
and exact applied route prove ownership. The same authority permits forward and
rollback recovery with a verified maintenance receipt. Fresh publication still requires health.
A lost switch reply can resume from the exact old route. A resumed or completed
old-slot handoff never runs readiness that would stop its writers again.
An unpaused compatible abort verifies the
already-serving old slot without stopping its writers or switching traffic. The final `aborted` state
records failure with the old service restored, and is terminal for deployment,
worker and unfinished-operation readers. Before a native cutover row exists,
authorization refusal during the installed host's entry phase proves no product
effect began and finishes as a plain failure. Only that typed entry outcome and
an absent row permit the exception to the completion provenance hold; legacy
and uncertain-effect gates are unchanged. A live or unknown native capture,
foreign tuple or pause, maintenance dispatch without recoverable evidence, or
lost authority keeps the recovery hold. Pre-maintenance reconciliation always
chooses the inverse: cancel or abandon the isolated verification, restore the
old route and resume its writers. It cannot create a capture, maintenance run or
candidate deployment. It uses the same uncharged reconciliation lease as other
original-operation recovery, even after the fresh-attempt budget is exhausted.
If `aborted` was saved before the terminal worker write, the same path verifies
the old route and finishes the failure audit without creating another rollback.
Expired-lease recovery reads the complete checkpoint history and production-write
evidence, including death before the first dispatch of a reclaimed lease.
An interrupted compatible inverse remains claimable before its pause is first
saved, during fencing, and after writer resumption. Its prepared snapshot and
any persisted pause must still belong to the original plan. Reclaiming does not
prove drain or permit startup: those effect gates continue to require their
complete native evidence. Inverse reconciliation retains the original operation,
attempt and checkpoints after the forward attempt budget is exhausted.

Before aborting an absent capture, the host atomically seals its native claim as
cancelled, preventing delayed original dispatch from capturing later. Archive
verification failure cancels or fences the original isolated restore before
rejecting the backup. A live or unknown capture remains held; a verified-dead
capture or abandoned inert restore can return the unchanged old service without
claiming a passing backup.

The host prunes filesystem bundles after a successful executor return while
holding the original lane authority. It retains applied prepared publication
and every unfinished record's bundles. A terminal bundle is removed only after
the persisted deployment record reconstructs the exact verified public bytes.
Rollback can republish those bytes. Missing or inconsistent retention evidence
keeps the bundle and emits a warning; housekeeping does not fail a completed
release or recovery.

Disposable source checks include an opt-in native coordinator-death regression:

```bash
LAUNCHPLANE_TEST_RESTORE_IMAGE=<already-local-immutable-postgres-image> uv run --extra dev python -m unittest tests.test_cutover_restore_probe.NativeRestoreLifetimeTests
```

These checks do not qualify integrated Community 20 settings, representative Odoo
workflow data or a real infrastructure backup provider. Those prerequisites and
the installed forward/rollback qualification remain on #4844; #3268 retains its
real accepted-release finish line.
