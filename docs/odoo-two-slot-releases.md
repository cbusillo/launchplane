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
database-changing release refuses. Use the existing authorized coherent-tuple
restore workflow; source cutover work never authorizes restoring a live site's
data or overwriting later business writes.

## Records, retries and activation

`DeploymentCutoverStore` stores typed checkpoints, slot/route identities,
classification, maintenance/readiness/backup observations and the full
`PublicPauseRecord` in the existing DB-backed deployment payload. There is no
new parallel shared truth or schema table. File/SQLite stores are isolated
rehearsal paths. Provider exclusivity and the existing worker lease protect
effects; payload storage is not itself a distributed lock.
The native worker can reacquire its existing held operation for a bounded retry
only when the matching cutover records a timeout at publication, switching,
resumption or drain. It uses the same lane lock, service drain admission, lease,
accepted release and fresh authorization checks. An uncertain maintenance or
backup operation is not automatically repeated. Recovery reuses the original
passing logical backup rather than relabeling later data with the old image.
The infrastructure backup guard validates continuation against the exact
persisted cutover and admitted promotion. It evaluates capture freshness at
that original admission, while still checking current authority, retention and
competing consumption. A failed continuation keeps unfinished work held. New
releases cannot bypass another unfinished cutover; recover it first. Configured
two-slot lanes also refuse an in-place rollback after a switch-back event.
Authorization is checked again at recovery boundaries. Denial performs no
effects and keeps the unfinished cutover's lane hold without consuming the
provider retry budget. A refused finished-release replay preserves its existing
promotion audit record. Switch-back intent persists its start time so a crash
before publishing the deployment event cannot replace it with retry times.
The direct promotion
route uses the original logical-backup identity for a stable recovery key;
native queued releases retain their existing operation identity.

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
`website_settings` capability. The former merges the selected snapshot and its legacy indexing parameter
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
