---
title: Disposable lanes
---

Overall direction permits agents to create, reset, rebuild and delete testing
and preview lanes and products that never went live. People using a lane,
the only needed copy of data, production resources and live credentials remain
protected. A label, missing staff hold, distinct volume name or distinct secret
version is not proof of those facts.

The source accepts exact-scope `disposable_lane_safety` observations on the
dedicated DB-backed lane record (and a separate collection for filesystem
rehearsals). Each observation binds the profile and current
tracked target/target ID (and the preview record for a preview). It records
usage (`unused`/`in_use`/`unknown`), data custody
(`reproducible`/`only_copy`/`unknown`) and production-resource isolation
(`isolated`/`shared`/`separation_ready`/`unknown`), with each known fact's source
reference and a timezone-aware observation/expiry window of at most fifteen
minutes. A changed binding, expired observation, positive
staff-testing hold or any required unknown fact refuses the new permission.
A target ID also bound to another lane refuses it independently of the observation.
The existing Odoo preview-refresh exception below uses direct resource inspection
and protected-use reports instead of positive occupancy/custody observations.
Global and lane runtime configuration and secret metadata participate in the binding.
For an Odoo preview, the binding also includes its copied template-lane runtime
settings, secret metadata and bootstrap override. Changing those inputs
invalidates preview safety even when no record under the preview slug changes.
Recording or refreshing an observation changes no profile or ownership digest.
Actual ownership and runtime/secret authority still must match, and each
destructive checkpoint validates fresh facts.

[Product retirement](product-retirement.md#direction-and-retirement-eligibility)
uses the existing prelaunch classification and protected-use exceptions instead
of requiring positive unused/reproducible/isolated observations. Its bounded
agent executor checks exact applications with empty mount inventories and
preserves production target and secret consumers. The positive-observation
requirements below continue to govern ordinary runtime sync and rebuild, which can
start or change a lane. Configuration-only separation below does not need occupancy
or custody qualification.

The bounded service API `POST /v1/disposable-lanes/observations` accepts only the
authenticated Director agent. `inspect` returns the binding digest and unknown
facts, without provider actions. `record` requires that digest and records the
authenticated actor and current time itself. Caller-supplied facts and references
are assertions: while an authoritative collector is unavailable, the service
stores usage, custody and isolation as `unknown`, even if the request asserts
`unused`, `reproducible` or `isolated`. Those assertions never permit an apply.
`clear` removes this product's exact-scope observation, including stale preview
observations; it changes no staff hold, custody inventory, provider resource or
historical operation. Missing observations continue to refuse writes requiring positive facts. The
generic profile-write API retains its original payload and behavior; it cannot
write these separate records. This change grants no profile-write access.
An absent preview can be inspected and
recorded without a tracking bundle; creating a target later invalidates that
observation. Known facts must be produced from authoritative people and
data-custody records and actual resource inspection, not be manufactured to
make an apply pass. `separation_ready` means the staged desired resources have
been verified independent, all existing execution is excluded, and nothing
shared with production changes. It permits runtime sync with deployment
disabled and the bounded empty replacement staging below. Starting a lane requires `isolated`; normal key-safety and sanitized
restore checks remain in their existing drivers.

## Existing Odoo preview refresh

A bounded Director-agent refresh of an active, uniquely owned PR preview collects
actual Docker resource evidence inside the normal service-issued plan/apply path.
It requires no invented `unused` or `reproducible` fact. A staff hold or recorded
`in_use`, `only_copy` or shared-resource exception still refuses, even after expiry.
Positive observations do not grant admission. Destroy and testing retain their
existing qualification paths.

The fixed inspector checks the daemon's default local-volume backing metadata,
complete container mounts (including stopped consumers and image-declared anonymous
volumes), current/proposed storage agreement, and absence of host ports/networking.
Anonymous image volumes retain Docker's provenance marker during Compose reuse;
explicitly named hash-like volumes do not qualify as anonymous. Backing aliases
are followed through daemon records, including unmounted intermediate volumes.
It runs on the target's placement, with no dependency on the schedule process's
filesystem namespace. Unsupported backing options, shared consumers, incomplete
or conflicting inspection and changed target placement/resources refuse. Unrelated
container churn and disjoint stale bind sources are not protected use. It neither
clears existing data nor creates a positive custody report. The qualified Director
refresh omits host SSH mounts and retains managed integration/source filtering.

Managed settings, aliases, protection reports, overrides and key policy are frozen
for each bounded dispatch, with a lock timeout. Deploy/job waits hold no global
transaction. A pre-effect admission refusal releases its reservation; a late
refusal preserves the original uncertain key for read-only reconciliation. A new
apply repeats inspection automatically. The inspection script has its own deadline;
a stale running inspection uses a separately named disabled read job, reused for
that stale deployment identity, without
cancelling the old job or modifying an application. Original-key retry remains
available after an inspection-only refusal. See the full
[authorization and recovery contract](authorization-authority.md#reviewed-never-live-classification-and-preview-execution).

## Staging generic-web testing separation

`POST /v1/live-target-runtime/apply` supports `separation_only=true` for an
active, uniquely owned generic-web testing compose target. This stages the
lane's existing managed configuration, including its intended new volume names,
ports and testing credentials, with deployment disabled. It does not need
#3611's unused, reproducible or only-copy observations: no existing container,
volume, port listener, database or credential record is changed. It neither
copies production storage nor creates a physical-isolation attestation.
Actual resource creation and a fresh deploy still need their own render,
physical-isolation and recovery qualification before anything runs.

The recipient must not alias any other lane's provider target. Runtime key
safety still refuses unsafe carried credentials. Known person-use evidence,
real staff holds, legacy unclassified holds and another caller's staging
interlock refuse staging. A purpose-tagged `isolation_staging` hold belongs to
the authenticated caller and continues to block every event-driven deployment.
Staff and staging purposes cannot replace or lift one another. A staging hold
also cannot change callers. Existing holds without a purpose keep their staff-protection semantics; an old reason
string is never interpreted as permission. In particular, this source change
does not relabel or lift RepairShopr's retained D6 interlock.

The dry run returns a binding of ownership, target, configuration, secret
metadata and key-safety policy; apply requires that binding and the original
idempotency key. PostgreSQL excludes authority insert/update races during the
bounded env-only write. Discovery and persistence reads run outside that lock.
The adapter shares the generic-web deployment fence:
an unknown historical deploy blocks it, and an uncertain staging write keeps
its own durable fence. Original-key retry performs read-only reconciliation:
it adopts the write only when the unchanged reviewed inputs and actual provider
environment agree. A mismatch, stale configuration or unavailable read keeps
the fence; no absence or safe-to-retry claim is made. It never supersedes an
older deploy or redispatches an unknown effect. Completed same-key responses
remain replayable. Changed inputs and persistent provider mismatch remain
protected unknown outcomes; [#3664](https://github.com/cbusillo/launchplane/issues/3664)
owns additional retained-evidence/replacement recovery. That follow-up is not
permission to clear the fence. Original-key reconciliation still reports these
cases as reconciliation-required rather than as a fresh-plan refusal.
SQLite tests are rehearsals, not proof of PostgreSQL exclusion.

Use `scripts/stage-testing-separation.py` with the installed authenticated
transport. First review `hold-dry-run`, then use `hold-apply` with the same
review safeguards below for a new staging interlock. Keep its evidence separate
from the runtime plan:

```bash
uv run python scripts/stage-testing-separation.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --context <testing-context> --reason '<reason>' \
  --evidence-file <new-private-review-file>
```

Review the runtime `dry-run` receipt and repeat with `apply --reviewed-dry-run`
and a stable `--idempotency-key`. A refusal stays refused; the client never
switches identity, uses a workflow or resets a fence. Staging apply never lifts
a hold. Keep the
original request, key and evidence after an uncertain response. The env
persistence readback does not establish that new volumes exist or testing is
healthy. Do not roll testing back to shared-production settings. Retain the
staging hold and old containers until the separately authorized recovery/deploy
gates are met. The same repo client supports `hold-read` without review-file
arguments, and `lift-dry-run` followed by reviewed `lift-apply` using a separate
private review file. Only an already authorized admin can apply that explicit
staging lift; the Director agent's apply denial remains enforced. The client
keeps the same configured identity and offers no fallback.
That lift, including a repeated/no-op lift, requests no reconciliation or deploy;
ordinary staff holds retain their existing reconcile-on-lift behavior. A lift
without the staging purpose refuses while that interlock is present. No
production rollback or volume deletion is part of this procedure.

The maintained catalog hold commands do not yet accept staging purpose; their
projection/option update is tracked in
[codex-skills#1881](https://github.com/cbusillo/codex-skills/issues/1881).
Use the repo client's tested read/plan/apply modes with the installed transport;
do not substitute hand-built authenticated calls or advertise catalog support
before that companion lands. Existing staff and legacy hold commands retain
their original shape and behavior.

The added hold purpose is read-compatible with existing records in this image.
Older images cannot read purpose-tagged holds; after using one, retain this
image or a compatible successor and forward-fix rather than editing the hold
or deleting evidence to make an older reader accept it.

## Preparing a testing production-copy policy

The Director agent can prepare an existing Odoo testing lane for a sanitized
production copy through
`POST /v1/product-profiles/{product}/testing-restore-policy`. This changes only
testing's `odoo_data_policy`: it adds
`upstream_restore` to the existing permitted sources, requires runtime identity,
and derives an upstream source reference from the same product's single recorded
production lane. It accepts no different product, source host, production
destination, credentials, runtime values, prelaunch approval, or full profile.
The production lane and all other profile authority remain unchanged.
Existing testing data authority is preserved, including an authoritative lane's
backup requirement and a resettable lane's empty-rebuild eligibility. Only
unknown data authority becomes conservatively `authoritative`, retaining its
existing backup-before-replacement requirement while allowing an upstream
source. This metadata preparation never waives a replacement backup requirement
and does not qualify unknown custody or isolation facts for execution.

Use the bounded repo client with the installed authenticated agent transport. Save its
review evidence outside the checkout:

```bash
uv run python scripts/prepare-testing-restore-policy.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --context <testing-context> --reason '<reason>' \
  --evidence-file <private-review-file>
```

After reviewing the policy change, repeat the same request with `apply`,
`--reviewed-dry-run` and a stable `--idempotency-key`. The service binds review
to the complete current profile and commits the testing policy and replay receipt
atomically. Profile drift refuses apply. A lost reply is retried with the original
evidence, request and key. The client then reads testing's policy back and reports
only digest, status and agreement. An interrupted accepted readback remains
`accepted_unverified`; keep the original mutation key.

This is metadata preparation, not a restore, isolation finding or sanitization
attestation. Operational upstream settings still come from managed runtime
records; the source reference is not an SSH address. Existing rebuild,
current-resource safety and pre-web integration readback checks remain in force.
Before a restored copy runs, its live keys and outbound email, payment, carrier,
analytics and similar connections must be removed or neutralized and verified
without exposing their values. A successful policy readback does not prove that
work happened or permit apply while required physical isolation is unknown.

## Independent storage under a retained generic-web hold and deploy fence

The [complete activation continuation](testing-activation.md) consumes this
original allocation on a fresh recipient, retaining the old target, unchanged
hold and historical fence. Its exact-receipt hold continuation is the bounded
exception to the ordinary Director-agent staging-lift refusal described above.
It proves separation before requesting normal testing reconciliation.

`POST /v1/products/{product}/testing-separation-resources` provides preparation
when the old testing recipient cannot be changed. It accepts only the exact
owned generic-web testing context, a reason, mode and reviewed plan digest.
The Director agent must be the existing hold's recorded caller. An omitted
legacy purpose remains a staff hold; no reason text is interpreted, no hold is
converted or lifted, and even a genuine staff hold continues protecting the old
lane. Other callers' holds and production aliases refuse preparation.

Apply requires PostgreSQL, complete current provider/executor exclusion, the
reviewed configuration/secret-metadata/hold/reservation/provider-placement snapshot and a stable
idempotency key. It acquires the original executor lock, but never settles or
releases the original deployment reservation. The only new effect is a disabled,
manual host schedule with a fixed storage program. A separate preparation fence
protects that create-only effect; no old-recipient writer or deploy may use it.
The service derives two new volume identities from the snapshot. No volume name,
path, port, credential, source code or provider placement is accepted from the
caller. The program creates only local volumes without backing options, then
qualifies their labels, empty backing directories, distinct filesystem identities
and absence of overlap with every container's mounts, including stopped ones.
It starts no container, publishes no host port, installs no credential, copies no
data and changes no lane record or provider environment. Existing unsafe keys
cannot block empty storage preparation because no key is carried. Credential
inputs are forbidden; the env-staging route still enforces actual key safety.

Use the existing supported same-identity repository client:

```bash
uv run python scripts/stage-testing-separation.py resources-dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --context <testing-context> --reason '<reason>' \
  --evidence-file <private-review-file>
```

After reviewing that receipt, repeat with `resources-apply`,
`--reviewed-dry-run` and the original `--idempotency-key`. The client saves the
qualified volume roles (`database`, `config`) and resource digest in an adjacent
private `.receipt.json`; it prints no topology. A completed same-key replay
returns the original receipt without another creation dispatch. For interrupted
preparation, repeat the exact original apply and key: only unchanged reviewed
inputs, an exact disabled original script, complete uniquely correlated terminal
schedule history and an actual readback of all empty, unused, independently
backed volumes permit adoption. Readback uses a separate fixed inspection-only
schedule; it never repeats volume creation or rewrites the original schedule.
Active, uncorrelated, missing, partial or drifted outcomes retain both fences.
Drifted or partial original-allocation recovery is separately tracked in
[#3674](https://github.com/cbusillo/launchplane/issues/3674); it is not clearance
to retry creation.
Queue absence, an unchanged desired build or newer configuration cannot prove no
effect. Do not delete partial volumes or schedules to make a retry pass.

Use `resources-read` with the original `--idempotency-key` and the same product,
context and private evidence-file path to recover a completed receipt from the
service even after configuration changes. The DB-backed idempotency record owns
the allocation; a workstation file is only a convenience copy. Its original
`qualified_at` describes the recorded observation, not current storage freshness.

This is empty-resource preparation, not activation, rendered application
isolation or permission to lift a hold. The original hold, request, target and
unknown outcome remain unchanged. A later supported continuation must bind only
independently qualified resources, verify its actual render and safe keys, and
meet every retained protection before dispatch. The original env-staging route
still refuses legacy/staff holds and the historical deployment fence. Actual
separation, rebuild, health, exact runtime identity and naturally resolved
incidents remain with the operational consumer; preparation is not green proof.
No new schema migration is needed. Retain the qualified image and both original
receipts for recovery; roll forward on uncertainty instead of removing storage
or restoring unsafe settings.

## Fresh Odoo testing replacement resources

`POST /v1/products/{product}/testing-replacement-resources` prepares a separate,
empty Odoo testing target without requiring an assertion about unknown historical
effects. Its input names the owned testing context, retained service operation,
reason and mode. The service derives the placement from the retained testing
target and allocates its own resource namespace. It accepts no production
destination, existing volume, credential, domain or caller-selected placement.

The dry run captures the retained target identity as inert preparation evidence
and returns a review digest. Apply uses the existing durable provider-operation
runner and the original idempotency key. Only new project, environment and empty
raw-compose creation calls are permitted; the old target, operation, routes and
storage are preserved. An interrupted create with no complete receipt stays
unknown and is never blindly repeated.

The preparation schedule creates three local volumes with no driver options,
bound to the service allocation. Readback checks their labels, actual backing
directories, distinct filesystem identities, emptiness and every existing
container's mounts, including stopped containers and parent bind mounts. An
existing foreign volume, nonempty directory, external backing option, active
preparation or incomplete provider response refuses qualification. Nothing is
deleted to recover. The new compose must remain empty, unconfigured, unrouted and without
deployment history. No application, host port or credential is installed.
Dokploy's unconfigured default source type is accepted, and its returned compose
project name is used for actual container inspection.

PostgreSQL excludes authority inserts, updates, aliases and evidence writers
while metadata is checked or published. Provider I/O holds no global authority
lock; each effect and final publication rechecks the captured configuration.
The retained executor stays excluded throughout. Referenced secret
metadata participates in that snapshot, including cross-context references.
SQLite remains a rehearsal backend and cannot publish these enabling facts.
After actual provider readback, only the testing instance's volume settings are
changed; other configuration and production records remain unchanged.

Use the bounded client with the installed authenticated transport:

```bash
uv run python scripts/prepare-testing-replacement-resources.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --context <testing-context> \
  --operation-id <retained-operation> --reason '<reason>' \
  --evidence-file <private-review-file>
```

Review the private receipt, then repeat with `apply`, `--reviewed-dry-run` and
the original stable `--idempotency-key`. Use `readback` with the same product,
context, operation and reason for a separate actual resource read. Readback
requires the current testing volume authority to agree with the allocation;
drift cannot refresh positive evidence. A complete allocation with a lost final
HTTP receipt can be adopted only after another actual readback. Target adoption
and its allocation identity commit together. If terminal readback proves only
missing service-named volumes while all existing ones are empty, correctly
labelled, independently backed and unused, original-key apply can resume storage
preparation on that same target. It cannot create another target. Active remote
jobs, foreign resources or uncertain creation stay unknown. Keep the original
key and retained resources; never delete them to make recovery pass.

This producer establishes unused, empty replacement resources and
`separation_ready`, not permission to start restored testing. It supplies no
production-capable key and performs no production copy. The separate continuation
must render without host SSH mounts or host ports, use the qualified volume
authority, sanitize the copy before any Odoo registry or module code runs, and
prove credentials and integrations safe before web starts. Supersession still
requires `isolated`; this preparation alone cannot release it. The old hostname
is not moved by preparation. Recovery retains partial new resources and their
service namespace in an additive allocation table. The retained custody payload
is unchanged; downgrade retains allocation metadata and re-upgrade adopts it.
An older image cannot continue the new resource route. After activation, retain
the qualifying image and forward-fix rather than erasing custody or restoring
an old database to clear the fence.

## First service collector

`collect` on the observation API resolves two existing service sources itself:
an exact lane's staff-testing hold establishes protected usage, and the same
Dokploy target ID and target kind bound to a `prod` lane establishes a shared
production target. The source references bind the lane snapshot and a digest
of any matched production records. The authenticated agent requests collection;
its supplied facts and references cannot affect the result. The existing stored
actor kind and observation schema remain compatible with the previous image.

This producer publishes only `in_use` and `shared`. Every fact permitting an
apply stays unknown. Missing holds, distinct target IDs and old preview-generation
states do not establish occupancy, custody, physical isolation or executor
exclusion. The response and bounded client name these coverage limits. Collection calls no provider,
changes only lane evidence and uses its existing expiry cap. It does not qualify
replacement resources or change production/Client permissions. Refresh preserves
any existing retained-operation linkage, but keeps replacement eligibility unknown.

Use the bounded repo client with the installed skill's existing private transport:

```bash
uv run python scripts/collect-disposable-lane.py collect \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --context <context> --instance <instance>
```

For a preview add its exact `--preview-id`, using the current profile's context
and slug. The client inspects the binding first and collects against that digest;
changed bindings refuse. Repeated collection refreshes inert evidence only.
Refresh after hold, target, profile, runtime or secret-metadata changes. Existing
`clear` removes only the observation and restores missing evidence. Rollback
retains observations; the previous service rejects the new `collect` request.
The service catalog deliberately publishes all existing API modes. The repo
client supports only `inspect` and `collect`. Catalog-helper integration is
coordinated on codex-skills#1308; that consumer must refresh its vendored
projection before advertising its own support.

**Remaining producer gap:** Launchplane has a positive staff-testing hold, but no
complete authoritative occupancy or only-copy inventory and no supported
inspection that proves every writable mount/backing store, live database,
published port and credential relation. The schema does not pretend to supply
that inspection. There is no universal producer of positive occupancy/custody facts permitting a
disposable apply. The bounded existing-preview refresh above uses its own real
resource inspection and protected-use rule instead. The assertion API cannot create one and a direct DB write is not a
qualification procedure. Until a server-backed collector is available, apply paths
requiring positive facts return `disposable_lane_safety_unknown`; absent observations never mean unused,
reproducible or isolated. Operational qualification records the missing sources
on the owning item. The producer is tracked in
[#3611](https://github.com/cbusillo/launchplane/issues/3611).
Building the missing producer needs no access grant or new
Director lane decision. Do not fill observations from names, IDs, old comments
or successful plans.

A disposable Odoo testing rebuild first calls `prepare-rebuild` on the same API,
with the old binding digest and `replaces_operation_id`. The service excludes
the old executor and captures the retained target ID immutably, without proving
past provider effects or changing its held status or serialized payload. The
captured identity lives in a separate preparation record keyed by operation ID.
With authoritative current
usage/custody/separation facts, `POST /v1/dokploy-targets/setup` supports an agent
`create-compose` request naming that operation and the exact
`expected_current_provider_target`. It creates a fresh project, environment and
empty raw compose, then binds it to the owned testing lane. Supply new project
and environment names and the existing permitted server placement; project,
environment and target IDs, repository source, domains and runtime ports are
forbidden on this path. It can issue only the three provider create calls,
holds the old execution lock, checkpoints current safety and commits against
the owned profile and old binding. It never updates or deletes the old target
or its storage, copies credentials, starts containers or changes production.
No service-scoped setup grant is needed. Missing current facts still stop staging.

Inspect the new binding and obtain fresh authoritative facts naming that
operation. The public assertion API records the gap until the collector exists.
The service derives
`replaced_target_id` from its preparation; a caller cannot invent it. The worker acquires the old
operation's execution lock, locks the lane, operation and current bindings,
and atomically records `superseded` only when the new target differs and the
current safety facts permit replacement. Surviving execution or changed records
refuse publication. The normal reconciler then admits a fresh testing operation;
supersession does not consume its failed-deploy retry budget.

Supersession retains the old request, error, target reference and journal (if
one exists). It creates no `safe_to_release` or no-effect attestation. Unknown
historical effects stay unknown; no provider call, old retry, old deletion or
production-fence release occurs. This avoids requiring unavailable past-effect
proof for a replaced disposable lane. Historical recovery on an unchanged target
and every production, backup, promotion and release path remain as before.

Deployment and lane qualification are separate from these source changes.
Before any use, preserve the previous image and the normal DB recovery point.
The additive migration's downgrade retains the observation and preparation
tables and their evidence; its upgrade adopts those existing tables again.
Profiles and held-operation payloads remain unchanged, so recording a gap or
preparing a rebuild cannot break an older record reader. Before an older-image
rollback, use the service's normal schema rollback procedure; do not drop these
tables or erase captured target references. Filesystem rehearsals likewise keep
separate collections. After an actual supersession, the new terminal status and
replacement evidence need compatible readers: retain such an image and
forward-fix the service; do not rewrite history or restore an old DB snapshot to
clear a fence. This source session performs no live qualification or deployment.

Creation recovery reads the unique service namespace from complete provider
project/environment/compose inventory. Each create dispatch records its exact
phase. A realized project can resume environment creation, and a realized
environment can resume compose creation; a missing object for the last dispatched
phase remains unknown. A unique, empty, unrouted compose with complete empty
deployment history can be captured atomically after a lost adoption transaction.
No missing response or inventory entry alone permits repeating a dispatched call.
Host qualification resolves the Docker data-root path and permits read-only host
monitoring binds; writable parent mounts and any exact existing named-volume
consumer still refuse qualification. No backing directory or monitor is changed.

Target adoption also snapshots the complete retained target record in allocation
custody, in the same transaction as the new binding. Old domains, public hosts,
pending effects and target settings remain recoverable there. Protection policies
and the test gate carry onto the new target; old routes and writable settings do
not. Current target overrides and secret bindings cannot replace qualified volume
names when positive evidence is published.

Only protection policies carry onto the fresh target. Integration allowances stay
in the retained snapshot and are not granted to the new generation. Retained
hostnames remain reserved across target and public-host writers, including a
claim from the replacement lane itself. Preparation refuses unresolved pending
route effects so their provider identities cannot be detached from their target.
The reviewed plan authorizes this exact empty allocation; recovery re-derives
current separation evidence and never starts an application under that receipt.
After target adoption, an older image may read the unchanged custody identity but
cannot enforce the new hostname reservations. Keep the qualifying service image
or forward-fix with compatible readers; retaining additive tables through schema
rollback does not make old code a supported continuation.

## Bootstrap a qualified fresh Odoo testing generation

After resource qualification and the testing-only `upstream_restore` data policy,
`POST /v1/products/{product}/testing-replacement-bootstrap` binds that exact
allocation to a new independent hostname. It does not move the retained hostname
or change production. Use the bounded repository client with its original private
evidence directory and idempotency key:

```bash
uv run python scripts/prepare-testing-replacement-bootstrap.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product example-odoo --context example \
  --operation-id retained-testing-operation \
  --domain-host fresh-testing.example.invalid \
  --reason 'Restore a sanitized testing copy on independent resources' \
  --evidence-file /private/operator-evidence/example-testing-bootstrap.json
```

Review the plan, then use the client's `apply` mode with the same inputs,
`--reviewed-dry-run`, the same `--evidence-file` receipt, and the original
`--idempotency-key`. Use `readback` to verify the exact generation. The private
receipt proves route/metadata/supersession/queue preparation; `runtime_verified`
remains false. Actual reconciliation, health, runtime identity and incident
readback must separately prove the restored lane is green.

For a containerized production source without host SSH restore coordinates, add
`--source-mode container-export` to the same client's dry-run/apply/readback
request. The service derives its single same-product production source from
managed records and binds the reviewed database, filestore and source
coordinates to the new generation. Unrelated settings or bookkeeping timestamps
do not invalidate an unstarted capture. Existing SSH requests, review files, request
fingerprints and generation identities retain their previous meaning. Source
mode is explicit; a failed SSH restore never selects another transport.

Container export uses the provider's existing registered remote server terminal
and identity. Both source and recipient must use registered remote servers;
preflight refuses local or missing recipients before testing writes. It refuses
the provider's local key-generating terminal. Fixed
commands read `pg_dump` output inside the recorded database container and stream
the recorded web container's filestore; they create no production dump/archive,
schedule, attachment pin, password or other persistent source file. A read-only
probe and source identity checks precede the first testing provider write.
The source web container's injected runtime identity must match the registered
product, context and stable production instance. Before a live capture, read
production's current operation and provider deployment state and wait for active
maintenance to finish. This is an operational precaution, not an atomic exclusion
of future production work. The bounded dump takes ordinary PostgreSQL read locks;
this flow does not pause production or create a production reservation.
An authorization refusal stops under the same identity. No new access grant or
source credential is delivered to the testing containers.

Before capture or upload, an inspection-only execution of the same restore
guard qualifies the actual testing artifact, containers, private data network,
volume backing identities and every backing consumer. The service retains raw
source bytes only in its private `LAUNCHPLANE_STATE_DIR/private-testing-source/`
cache, with directories and files readable only by the service user. Each artifact is bounded to 128 MiB;
expanded attachments are bounded to 512 MiB. An immutable PostgreSQL receipt
binds the generation, source observation, byte lengths and SHA256 digests before
delivery. A dedicated persistent `launchplane-odoo-copy` volume overlays this
directory only in the API and Odoo worker. Other workers do not receive it. This
is private copy custody, not a public asset store or runtime-configuration authority.
Capture refuses when this dedicated mount is missing. Qualification checks every
other worker role for absence of the private copy.

Delivery uses the same registered terminal and existing identity, with no
alternate-route fallback. A fixed writer receives private chunks over stdin and
writes only their exact receiver-container filenames. No raw chunk appears in
host process arguments or host temporary files. Terminal echo/history and
transport logging are disabled before private input; proxies are disabled.
Delivery uses at most 64 KiB per provider upload into the qualified runner's
writable temporary layer, with metadata-only effect journaling. Provider upload
success is provisional: actual receiver sizes and hashes must agree before the
restore schedule runs. Retry retains the original receipt and bytes, skips
already matching chunks. A partial chunk is repaired only when its actual bytes
are proven to be an exact prefix of the same immutable original; other differing
chunks remain held. Attachment publication is atomic, so an interrupted write
does not publish a partial content-addressed file. An existing
database must hold the exact generation's committed copy witness; that resume
does not recapture or require the raw cache. Complete receiver artifacts whose
actual whole-file sizes and hashes match the immutable receipt also permit
recovery without the private cache. Missing original bytes in both places,
without a database witness, are held for recovery, never silently replaced with
a newer production snapshot.

The receiver verifies whole artifacts and rejects archive traversal, symlinks,
hardlinks and special files. Only content-addressed Odoo attachment files enter
the testing filestore. The custom database dump goes through the existing
SQL-only credential and connection sanitization boundary before a registry
process runs. Whole-artifact hashes verify copy fidelity. The copy preserves
pre-existing attachment data and missing-file references as the source export
contains them; it does not introduce an attachment-repair or completeness policy.
Module, integration and website readback still precede the web gate. Successful
readback removes raw receiver chunks and private cached bytes, retaining the
immutable DB receipt. Failed attempts retain original chunks/cache and keep the
generation for supported same-key recovery. Never delete retained old storage
or fabricate a completion witness as cleanup.

Chunk dispatches share one aggregate recovery-journal effect; its acknowledgement
describes the current synchronous upload. After acknowledged delivery, existing
terminal-provider/executor exclusion permits reconciliation and reuse of the
same generation's receipt. Only the fixed writer's witness after closing the
receiver file acknowledges a terminated upload. An explicit receiver refusal
is terminal; a missing witness, Docker transport failure or timeout remains
fenced. Authorization refusals stop. No HTTP status is used as remote-process
exclusion evidence.
An unacknowledged upload reply stays fenced under
the existing uncertain-dispatch rules; file presence alone never proves the
remote call ended. Once a capture receipt is recorded, retry/readback uses that
original source binding rather than probing live production or requiring its
later runtime settings to remain unchanged. Current testing recipient and
credential isolation checks still apply. A lost receipt-commit reply retains
the private bytes because the DB commit may already have succeeded.

A source-size refusal is recorded in the operation journal with the failed
capacity. Automatic recovery does not recapture the same oversized source.
Preserve the generation and forward-fix with a compatible, qualified image that
supports the required capacity; recovery can then qualify quiescence and retry
the original request. Tar file-change or read errors do not publish a capture
receipt. The provider retains terminal stdout per session, so source capture
also needs memory headroom for base64/framing overhead. Verify actual source
size, provider memory and transfer duration during live qualification. Fixtures
do not prove capacity on an installed provider; this flow does not modify Dokploy.

For updates, run the schema migration and deploy this image to the API and Odoo
worker with the dedicated copy volume before selecting container export.
Verify their running revision, actual volume mounts and the bootstrap dry-run.
The Compose qualification fixture proves API/Odoo custody across recreation and
that every unrelated worker role cannot read the copy. After a completed copy,
ordinary deployments use existing data; another production refresh needs a
separately qualified generation. On a failed or interrupted copy, preserve the
original request/key, generation, immutable capture receipt and private cache;
repeat supported observation/recovery rather than changing source mode or
credentials. Images predating container export cannot interpret these generation
records: retain this image or a compatible successor and forward-fix instead of
rolling records back or deleting evidence. Unreferenced pre-receipt cache
directories may be removed only after the owning executor is stopped and their
absence from retained capture receipts is verified; published failed captures
remain recovery evidence. There is no automatic raw-copy expiry: do not remove
ambiguous or failed published captures merely because time has elapsed. Default
SSH generations retain their previous stored field shape; this does not promise
that older images can interpret native generations or newer schema migrations.

The service compares the existing local testing passwords with the supported
production source's managed versions internally, without returning or retaining
plaintext or password hashes. Missing versions or overlap refuse preparation;
this route never creates, copies or grants a credential. Shared service-user and
mail credentials are retired from testing delivery. Existing shared requirements
are narrowed to their other lanes so production's effective required settings
remain the same. Production secret records, bindings and values are not changed.

Bootstrap dry-run prerequisite refusals identify whether existing credentials lack
lane-local safety proof (`testing_credentials_unsafe`), their existing managed
versions cannot prove separation (`testing_credentials_unverified`), or the
supported source configuration is incomplete (`testing_restore_source_unconfigured`).
Known missing-version and password-overlap cases are distinguished as
`testing_credential_versions_missing` and `testing_credentials_overlap`.
Other bounded failures within a named prerequisite report that its proof is
unavailable; the phase classification does not assert the underlying cause.
These HTTP 409 results expose fixed descriptions, never the private exception or
credential values. Correct the named prerequisite and repeat the dry run before
any apply; a refused dry run has prepared no generation, route or restore.
Other failures remain `testing_bootstrap_unverified`. These classifications
neither grant authority nor relax the source, isolation or sanitization checks.
Apply and readback failures retain the generic original-key recovery guidance,
including when a prerequisite changes after review or during provider effects.

The new compose uses only its three qualified local volumes, no host ports and no
SSH mounts. Its database and script runner use an internal network. The host's
existing restore identity performs only the supported database dump and filestore
read; the application receives copy data, never the identity. Before source
capture, the worker verifies actual image/container/volume/network identities,
stops web and never overwrites an existing destination database. An existing copy
resumes only with this exact generation's completed-restore witness. The artifact must expose
the SQL-only pre-registry credential boundary and have declared module work.
The runner proves module states and changed update witnesses before returning
completion; the host does not invent module success.

Before each registry boundary, the copy runner strips source authentication and
connections, disables cron/mail, commits and checks through separate database
connections. Strict integration readback has no inherited allowances and also
checks reapplied instance settings before hooks. A final readback gates web start.
Fresh copies include the deploy-phase website settings inside that guarded restore;
they do not run a second ordinary maintenance pass with inherited allowances.
The same native website-settings program used by ordinary testing deployments
excludes analytics and search indexing after the guarded restore. Its failure
keeps web stopped; final integration readback follows that program before opening
the web gate.
The shared testing connection-isolation pass also clears analytics on existing
testing lanes; production behavior is unchanged.
Completion consumes the post-deploy readback markers before publishing successful
inventory. Changed resource or credential authority holds reconciliation without
queueing another operation.
Browser-facing responses restrict external connections through the testing route.
Only a completed, healthy deployment marks the generation complete; subsequent
reconciliation uses its existing data rather than restoring production again.
An incomplete generation refuses an existing-data deploy. After completion,
deployments using existing data remain supported; another production refresh
requires a separately qualified generation and is refused at planning time.
Plans report incomplete or unverified authority as blockers without starting work.

The web service uses the provider's shared ingress bridge for HTTPS. Its data
services use only the internal testing network; storage, database credentials and
host ports remain independent. The restored connection settings and live keys
are removed before the first registry process, and browser responses restrict
external connections.

An interrupted route write is adopted only from exact provider readback. An
unobservable dispatched write remains unknown; never repeat it under a new key.
Route custody, metadata, fence supersession and the atomic queue receipt let the
original operation finish a partially prepared generation without creating
another target, reassigning the retained host or queueing a second restore.
Host dump/filestore staging is removed on success and ordinary failure. The raw
container dump is removed before any registry hook. Failures leave retained
resources and the stopped new copy available for exact recovery. A failed initial
copy may drop only its own newly restored database; a resumed copy is preserved.
The host holds an exclusive generation lock across capture and startup, uses a
unique dump path, clears the prior web-start gate, and rechecks database absence
under the artifact's workflow lock before an initial restore. A stale workflow
lock is removed only after terminal schedule proof and confirmation that the
process and its data-workflow children have exited.
Do not retry the historical operation, fabricate a recovery journal, delete old storage,
or point the old hostname at the replacement to clear a refusal.

The generation table survives schema downgrade. Older service images cannot
continue this route or enforce its guarantees; keep a compatible image or
forward-fix. There is no production rollback or cleanup step in this testing-only
procedure. Retained old targets, storage, partial-copy evidence and hostnames stay
in custody until separately authorized disposition.

## Scheduled Odoo testing rebuild drill

The API service owns a daily drill for each enabled active Odoo testing lane. It queues
an ordinary durable target-replacement operation; the Odoo worker performs the
existing upstream restore, module work, integration sanitization, website
exclusions and health checks. There is no GitHub workflow or product-repository
call. The drill uses the lane's recorded artifact and same-product production
source. Platform-input updates remain the separate devkit#131 grouped-update
workstream; a drill does not rebuild or change the product artifact's inputs.

The invariant is repeatable rebuildability, independently of a currently healthy
web process: a lane must be restorable from production through sanitization and
module work before someone needs it. A queue that never starts the work is a
failure too. A durable daily slot and per-lane observer lease prevent duplicate
queueing and reporting across API replicas and restarts. A two-hour unfinished
operation records a failed completion deadline, alerts, and retains its original
operation rather than recapturing or replacing it. Recurring lane slots are spread
deterministically across the day. A late success updates the current evidence
with `late_completion`; the original deadline failure remains alertable.

Admission requires a completed qualified fresh testing generation and current
unused/reproducible/isolated evidence, with its original expiry and exact binding.
The same-product restore policy, existing lane-local credential separation,
exclusive context/target ownership, actual storage/network inspection and
pre-registry sanitization remain mandatory. A staff hold, recorded use,
only-copy custody or another lane operation skips that day's drill. Missing,
expired or conflicting safety facts record a failed prerequisite and alert;
they never become inferred permission. The general positive-fact producer gap
on #3611 therefore remains a real activation prerequisite. This feature cannot
make an unqualified lane rebuild automatically.
Deployment leaves the per-lane `odoo_testing_drill_enabled` flag false. After
landing, configure it through the existing authenticated `POST /v1/product-profiles`
path: read and preserve the current complete profile, change only that testing
lane's flag, and post the updated profile with its normal idempotency key and
existing `product_profile.write` authority. No new grant is needed. Refresh the
actual safety facts against the resulting binding before admission. Production
lanes cannot enable the flag. An unchanged failed prerequisite produces no
repeated admission; corrected facts/configuration can retry the same daily slot
before an operation has been queued.

Queueing archives the prior generation and planned copy in the private slot
receipt without changing the running lane's settings. After its hold check,
the worker atomically selects a new service-named testing database and matching
filestore path and activates that exact generation under its current lease.
It repeats usage, credential and resource admission; changed source/configuration
refuses before selection. The original collector row is never rebound or renewed.
Its accepted snapshot is stored on the active worker attempt; an unchanged snapshot
does not expire in the middle of that continuously leased restore. A reclaimed
attempt requires fresh admission against the selected database. Every provider
checkpoint rechecks the generation, admitted use/custody/isolation facts and
current hold. Routine renewal of the same facts does not cancel a running copy;
changed bindings require fresh current proof. A person starting a test session
or placing a hold therefore protects the lane even after queueing. Protect a
lane through the existing staff-testing hold before starting a session. Unknown
or expired observations cannot substitute for that hold.

A drill can stop the testing web service, replace its disposable contents, load
the production source with the existing bounded read-only export, and consume
testing storage. It never changes production data, ports, storage, routes,
credentials or permissions. Its copy has live keys and outbound connections
removed before the first registry process. After the new copy passes sanitization,
module and integration readback, the restore guard may dispose of the older
service-created drill database and its filestore. The immediate completed
predecessor is retained through final web/health checks, including when those
checks fail. Only the copy two generations behind can be disposed of, and it must carry
its exact committed copy witness; DROP uses no FORCE, so an unexpected connection
refuses disposal. The new copy is distinct and the initial bootstrap database
is retained. Successful cleanup bounds steady-state drill storage to the initial copy, current
copy, immediate predecessor and in-progress copy. Cleanup runs after the sanitized
new web process starts; a cleanup refusal preserves that working copy, emits
`testing_drill_disposal_deferred=true` in the operation logs, and retains the old
copy under the slot receipt's custody. Deferred cleanup and failed partial
captures/copies require explicit disposition and can accumulate; admission does
not establish available filesystem capacity. Durable deferred-disposal telemetry,
capacity admission and bounded cleanup reconciliation are tracked in
[#3729](https://github.com/cbusillo/launchplane/issues/3729).
A failed copy never deletes its predecessor. Final web and health verification
still gates a passing operation and generation completion.

`GET /v1/products/{product}/environments/testing` includes value-free
`testing_rebuild_drill` evidence: queued/pass/fail/skipped, observation time,
operation/deployment IDs, failing step, trace and alert delivery status. A pass
requires the worker's health result and this exact generation's committed copy
completion. It does not replace monitor freshness or prove platform updates.
Failures use the lane's existing enabled Discord notification destinations and
managed sender. No destination or unreadable sender leaves `alert_delivered=false`;
retry occurs at five-minute intervals and remains visible without blocking later
drill slots. Successful destinations are recorded individually, so another
destination's outage does not repeat their notices. Webhook delivery is
at least once: a lost acknowledgement may repeat a notice. Arbitrary provider
errors, source settings, copy bytes and secrets never enter the notice or lane
projection.
Pending notices are coalesced to the latest failure per lane while retaining
older failure receipts. Alert retries never refresh the drill's observation
time, and scheduler projection/delivery updates preserve the worker's admission
fields under a row lock. Retired or removed lanes stop participating.

For updates, deploy the compatible API and Odoo worker together after the PR
lands; no schema migration or new grant is needed. Before the first live run,
configure the flag only after initial-copy qualification, source configuration
and the existing Discord policy are prepared. Then obtain current hold/use/
custody/isolation facts through the service before admission. A failed
prerequisite is corrected on its supported service path, then a later slot can
admit work. The first live run is separate from source/CI qualification and must
never run against a lane in use. Inspect the lane receipt and its exact operation
for a failed or overdue drill; preserve original keys, capture receipts and
partial copies for supported recovery. Do not clear fences, fabricate safety
facts, delete the initial copy or change the source transport to obtain a pass.

Once a drill generation exists, older images cannot interpret its additional
identity fields. Retain this image or a compatible successor and forward-fix;
do not erase generation history or restore an old DB snapshot to clear a failure.
Staff holds provide the supported per-lane maintenance pause. A held queued
operation has not selected its new database. A refusal after selection but before
any provider dispatch can restore the exact previous generation and runtime
atomically with the terminal operation or its no-effect source-read retry back
to pending. This is limited to the continuously held
worker recovery guard, the same live lease/attempt, empty dispatch history and
unchanged selected settings; it never overwrites intervening settings. An
abandoned attempt, dispatched effect or later hold/failure retains the exact
generation for supported recovery rather than reverting possibly used settings.
A hold arriving after destructive restore starts can therefore leave testing
unavailable pending recovery; continuing provider writes past that protection
is not authorized. Private slot receipts retain prior generation identities;
they are provenance, not a promise that a successfully superseded drill database
still exists after bounded disposal. Remove failed raw-copy evidence only under
the existing custody procedure above. No production rollback is part of this drill.
