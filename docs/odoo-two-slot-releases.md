# Odoo two-slot releases

`workflows/odoo_cutover.py` executes both release classes from one immutable
artifact pair. `OdooCutoverPlan` recomputes the classification using the
[verified declaration contract](release-database-compatibility.md) and refuses
an incomplete module plan. Each slot binds its source/image/runtime identity
and selected database. The serving slot's deployment identity remains its own;
the prepared copy's release binding names the new cutover operation.

The existing production-promotion executor accepts a cutover plan only inside
its Client-release and independent-backup guard. It compares both manifests
with stored current production/testing authority. A configured two-slot request
without a bound provider refuses; it cannot fall back to an in-place deploy.
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

`slot_maintenance.py` supplies the normalized candidate/production manifests,
resolved compatibility record and selected database to devkit's full
`--post-deploy-maintenance` entrypoint. It verifies the immutable runner image,
the baked declaration/actual graph, applied updates and module readback, then
reads the committed credential boundary before releasing web. The SQL-only
credential transaction in devkit remains the sole sanitizing/override policy;
no registry, shell or module hook is substituted for it. A started attempt with
no passing bound receipt stays uncertain rather than rerunning an upgrade.
Secrets stay in the existing managed container environment, outside plans and
records. The legacy empty install pass is never called on this path.

`TraefikFileRoute` publishes one YAML route file with an atomic rename and
reads the actual router/service from Traefik's API, followed by the provider's
independent public identity probe. The watched directory must be mounted, not
the individual renamed file. The publisher rejects duplicate host routers,
foreign targets and missing applied evidence. An asynchronous apply can return
an uncertain result; the persisted controller reads authority before retrying.
See [Traefik's file-provider documentation](https://doc.traefik.io/traefik/v3.3/providers/file/).

## Compatible and database-changing paths

A compatible release runs the full planned credential/settings/readback
sequence with no install or update, warms the candidate, and obtains fresh
private database/Host/render/asset/browser/identity evidence. The old slot keeps
serving through preparation. Then one route changes, public identity is read
back, the predecessor writer/cron owner drains before its successor resumes,
and old connections drain. Keep the old slot and referenced assets retained.
Switch-back requires fresh old-slot readiness and the exact current route;
its intent is persisted so an uncertain switch-back can be reconciled.

A database-changing release first prepares and browser-checks the bound
[public bundle](prepared-public-serving.md). The pause begins before form
blocking or writer fencing. Prepared serving is published/read back, every
writer drains, and a coherent image/database/filestore recovery point is
captured after the pause begins. The [online logical backup](odoo-online-backups.md)
and existing independent infrastructure protection are both required. Only
then may targeted maintenance, candidate startup/warming and private readiness
run. The candidate route is published/read back before exactly one owner per
writer target resumes. Prepared serving never falls back to the updating DB.

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

Persist intent before publication, maintenance, switch and resumption. Failed
preparation/readiness keeps old traffic or verified prepared traffic intact.
An uncertain switch reads current applied/public authority; an uncertain
resumption retains its fixed recovery target and idempotent writer receipt.
Missing evidence never invents a success, pause end, zero active connections
or owner count. The pause duration includes fence/drain, backup, maintenance,
startup, warming, readiness, route application and writer resumption. Zero 5xx
while forms pause is reduced service, which remains visible in the bundle and
record.

Activate through the exact Client-accepted service release with explicit slot,
volume/network, prepared-serving, proxy and runtime capabilities already
available; this is separate from source/component acceptance. Missing provider
access or activation prerequisites are reported, never bypassed with a local
live-target command. Real-release acceptance remains on #3268 and the parent
#3237. Executing agents hand reviewed green PRs to the Supervisor; they do not
enqueue or merge them.
