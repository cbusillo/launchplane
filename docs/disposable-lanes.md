---
title: Disposable lanes
---

Overall direction permits agents to create, reset, rebuild and delete testing
and preview lanes and products that never went live. People using a lane,
the only needed copy of data, production resources and live credentials remain
protected. A label, missing staff hold, distinct volume name or distinct secret
version is not proof of those facts.

## Repair usage rule

`control_plane/lane_usage.py` owns the usage decision for disposable repair.
A current staff-testing hold always protects the lane. Without a hold,
automated UNKNOWN or positive usage (including recent login/session reports)
cannot block repair of a verified-down non-production lane. The usage observation stays UNKNOWN; this
is repair eligibility, not an assertion that nobody ever used the copy.
Serving or unverified lanes retain their existing usage protections.
Production admission and release gates are unchanged.

Verified down means every effective application HTTP check has failed in each
of the same two consecutive monitor scheduler slots inside the preceding two
cadences, with each latest result less than one cadence old. The scheduler's
existing slot rule owns this timing; variation in the run's offset inside its
slot does not disqualify a continuously failing lane.
All observations must follow the current profile, tracked target and target-ID
updates and match the monitor's current URLs and expected runtime identity.
Every probed URL must fail: network/DNS/TLS failure or server error qualifies;
successful HTTP responses, access refusals, identity mismatch, missing checks,
skipped checks, unresolved endpoints, future timestamps and stale observations
do not. Public and declared private checks both count. Provider-only or
unmonitored states remain unverified. No probe or live action is initiated by
this decision. The guard logs the durable monitor record IDs and interval;
the drill also preserves those references in its private admission receipt.

These observations describe Launchplane's monitor vantage. A network, DNS or
TLS fault in that vantage can look like a lane outage even if a person can reach
it elsewhere. A staff-testing hold remains authoritative regardless of monitor
results. [Director decision Q380](https://github.com/cbusillo/launchplane/issues/3812#issuecomment-6217513435)
accepts this bounded monitor-vantage rule for an unheld non-production lane.

The common disposable guard covers runtime repair/deploy, preview operations,
replacement target setup, held-operation supersession and rehearsal admission.
The Odoo daily drill uses the same usage decision before queueing and worker
activation. Its down-lane read still runs the original container, storage,
network, credential and copy-witness inspections, including protection of an
already-opened replacement copy; it skips login-history collection.
UNKNOWN is preserved in that scoped receipt, which remains usable only by
the same continuously leased attempt. Its captured usage revision covers the
unchanged historical observation if monitoring expires or recovers during the
attempt; a newer use observation is evaluated again, and holds, needed-copy and
resource protections are always rechecked. Admission uses the scheduler's clock.
Reclaim needs new admission and a new captured usage revision.
Connection-isolation and environment-staging guards also use the shared usage
decision. Acknowledged activation/interlock lifting keeps its original explicit
hold and known-use checks; it has no UNKNOWN usage requirement. Preview operations
use the common guard, but previews without stable-lane monitor authority remain
unverified and retain their existing admission paths.
Existing preview refresh, never-live retirement and non-destructive isolation
paths continue to protect known use without demanding a universal negative
occupancy observation. This change adds no UNKNOWN restriction to those paths.
Walkthrough account and browser operations also retain known-use protection;
they do not use the downtime repair exception.

Downtime does not qualify needed-copy custody, physical isolation, secret
separation, backup, authorization, deployment fences or unknown provider
effects. Their existing checks still run. In particular, no missing usage
record becomes a reproducible-data or production-isolation fact. Preserve old
operations, keys and retained resources when replacing a stranded generation.

For an update, deploy the compatible API and Odoo worker together through the
normal service path; no migration, grant, new runtime flag or per-product rule
is required. Use the bounded collection client from that compatible source
revision; older coverage/enum validation refuses the newly qualified resource
facts. Use the existing monitor history and collect immediately before
apply; a hold or changed recipient still refuses. Source tests do not qualify
a real lane. Before a queued drill uses a downtime receipt, retain this image
or a compatible successor and forward-fix: older workers require a negative
usage result and cannot continue the new UNKNOWN admission. Do not rewrite
receipts or clear fences to roll back. No live lane action is part of source
delivery.

The source accepts exact-scope `disposable_lane_safety` observations on the
dedicated DB-backed lane record (and a separate collection for filesystem
rehearsals). Each observation binds the profile and current
tracked target/target ID (and the preview record for a preview). It records
usage (`unused`/`in_use`/`unknown`), data custody
(`reproducible`/`only_copy`/`unknown`) and production-resource isolation
(`isolated`/`shared`/`separation_ready`/`unknown`), with each known fact's source
reference and a timezone-aware observation/expiry window of at most fifteen
minutes. A changed binding, expired observation, positive
staff-testing hold or any required unknown custody/isolation fact refuses the new
permission. UNKNOWN usage alone has the verified-down repair exception above.
A target ID also bound to another lane refuses it independently of the observation.
The existing Odoo preview-refresh exception below uses direct resource inspection
and protected-use reports instead of positive occupancy/custody observations.
Global and lane runtime configuration and secret metadata participate in the binding.

For an Odoo testing lane, `collect` now reads recent successful logins from
`res_users_log` in a transaction enforced read-only by PostgreSQL and checks the
running web container's configured filesystem session store. The preceding
24 hours count for interactive and `authenticate()` logins on every account,
including administrator and portal accounts. Restored login rows can
conservatively count as use. Any authenticated
or partially authenticated session for that database counts regardless of age;
device-log rows and old login timestamps cannot prove that a browser session has
ended. Session contents, identifiers, account names and credentials never leave
the container. The result exposes only two booleans and a bounded `reason_code`. The filesystem check follows
[Odoo's session storage contract](https://github.com/odoo/odoo/blob/19.0/odoo/http.py),
without calling methods that create, refresh, migrate or delete session files.

The service uses its existing registered Dokploy transport and fixed read-only
programs; the agent needs no host shell or new SSH trust. The tracked compose,
testing context/instance, local database endpoint, supported web configuration,
complete single-web/database inventory and unchanged container/placement snapshots
must agree. Missing databases or session directories, unreadable or malformed
files, replicas, custom startup, oversized inventories and incomplete/provider
failures leave usage unknown. A provider authorization denial returns its service
trace and is never retried with another identity. Complete negative login
and session reads can produce `unused`. The separate
[never-served rule](#never-served-testing-generations) can also qualify non-use
from complete durable generation records; staff holds protect both paths.
Other drivers and previews retain their existing collection behavior.

Collection returns the first failed checkpoint as an allowlisted code, never
exception text, command lines, session identifiers, account names or topology.
The bounded client preserves that code and rejects malformed diagnostics.
A code introduced by a newer service is mapped to `diagnostics_unrecognized`
without echoing it, preserving validated facts and the trace. Such a code cannot
qualify non-use: when diagnostics are present, `unused` requires `complete`
for runtime reads or `never_served` for the durable generation proof. A
service image predating diagnostics is reported as `diagnostics_unavailable`;
that means deploy the compatible collector before diagnosing the lane, not that
any particular runtime checkpoint failed. Inspecting only the binding does not
run the probe.

An unhealthy web container can qualify if the supported Odoo process still
serves the matching configuration and both login and session reads complete
negative, with unchanged container and provider snapshots. Health alone proves
neither use nor non-use. `serving_process_absent` means the running container has
no qualifying Odoo process (for example, it is waiting at a startup gate);
`serving_configuration_mismatch` means the observed process arguments do not
qualify the configured session store. Neither permits treating an empty or
unreadable store as historical non-use. `web_configuration_unavailable`,
`session_store_unavailable`, `session_file_unreadable`,
`database_connection_unavailable` and `login_history_unavailable` identify the
missing read. Provider transport, ambiguous/stopped services and changed runtime
snapshots have separate codes. All these failures leave usage UNKNOWN with null
booleans; `complete` accompanies the qualified positive or negative read.

For a stranded runtime, deliver the diagnostics image through the normal service
deploy and collect again through the same client, without starting, restarting or
repairing that lane to obtain evidence. Record its code and service trace. If it
cannot read the original served configuration/database/session store, that exact
prerequisite remains unsupported for a negative usage assertion: a stopped process,
missing database or absent staff hold is not a substitute. The verified-down
repair rule can independently qualify usage eligibility while preserving the
original lane's custody and every other rebuild gate. No alternative credential, direct database qualification or
new grant is needed for diagnosis. The service read is the proof of the concrete
failed checkpoint; a fixture qualification or an old generic UNKNOWN is not.

This is a bounded snapshot of browser/authenticate-login use, not a reservation
against a future login or proof about anonymous browsing, unsaved work or
stateless RPC/API-key traffic. Per-call API-key authentication need not create a
login row or session file; the probe does not attest absence of that traffic.
Collect immediately before a governed rebuild and use the staff-testing hold
for a browser testing session or a stateless API test suite.
The observation's expiry starts before provider collection; changed configuration
or an elapsed lifetime rejects publication. A delayed collection cannot replace
newer stored evidence; publication compares observation times under the shared
authority lock. An unreadable collection also cannot clear observed use of the
lane after a binding change, and caller assertions cannot clear it either;
a complete subsequent negative read can. Production API replicas use the shared
database clock to order these observations. Data custody and production isolation
remain separate prerequisites and this read never starts a rebuild. Deploy the
compatible service image, then use the existing `collect-disposable-lane.py`
`inspect`/`collect` client. No schema migration or new grant is needed. Rollback to
the prior image restores unknown usage collection; retained facts remain subject
to their original binding and expiry. A lane with no readable application database
still needs its supported bootstrap/never-served qualification; absence alone is
not turned into an unused assertion.

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

## Service collection

`collect` on the observation API resolves existing service sources itself:
an exact lane's staff-testing hold establishes protected usage, and the same
Dokploy target ID and target kind bound to a `prod` lane establishes a shared
production target. The source references bind the lane snapshot and a digest
of any matched production records. The authenticated agent requests collection;
its supplied facts and references cannot affect the result. The existing stored
actor kind and observation schema remain compatible with the previous image.

For Odoo testing, the read-only login/session probe described above can also
publish `unused`. On a verified-down lane with a resettable data policy and a
current completed service-created testing generation, collection can independently
reinspect its original physical containers, storage, networks and copy binding
through the fixed receiver, without requiring login history. Only a passing
inspection supplies reproducible-data and isolated-resource facts. Unsupported
or failed inspection leaves those facts unknown; it never substitutes downtime
for a resource proof. A provider refusal returns authorization_denied with its
trace, without another identity or transport.

Collection retains known use, only-copy, shared-resource and separation-staging
protection. Use the existing acknowledged activation path for staged separation.
The existing `clear` then fresh `collect` path removes the whole observation, so
use it only after verifying that every protection on that row is resolved,
including any needed copy or current use; otherwise retain the row and its
protections. Collection alone never erases resource protection. It also retains independent
fresh custody/isolation facts on the same binding, capped at their original expiry;
collection cannot renew an old proof. A new physical inspection can publish a fresh
proof; a failed attempted inspection replaces those independent facts with UNKNOWN
instead of reusing the previous proof. The response reports the stored facts, including retained historical use,
while the login/session diagnostic remains UNKNOWN. An unheld down lane can use
that historical positive usage for repair eligibility without erasing it.
Missing holds, distinct target IDs and old preview-generation
states do not establish occupancy, custody, physical isolation or executor
exclusion. The response and bounded client name these coverage limits. Collection
changes only lane evidence, performs no provider mutation and uses its existing
expiry cap. It does not qualify
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

**Remaining producer gap:** bounded Odoo authenticated-use evidence does not supply
a universal occupancy or only-copy inventory, or a general
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

Readback verifies the committed request, generation, target, independent route,
supersession and queue receipt. It does not rerun bootstrap planning after that
queue has started deploying. Omitted delivery keys come from the generation's
immutable receipt; readback is never permission to replay bootstrap.

New fresh-generation recovery journals bind override intent independently of
apply status, time, detail, `updated_at` and `source_label`. Website-setting
history that still affects rendering remains bound. Publishing post-deploy success or
failure does not change that intent. During the initial copy, deployment and
post-deploy render the same generation hostname without rewriting the stored
override intent. Later ordinary deployments retain their canonical-record
normalization. Product, target,
resource, configuration, secret-version and source checks remain bound; executor
exclusion and acknowledged terminal-provider proof still precede recovery.
The database checks the same fresh/legacy digest under its configuration-writer
lock before publishing the existing supported recovery transition. An eligible
read view alone does not release an operation.
Legacy full-record journal digests retain their original meaning. A legacy
binding mismatch stays fenced: without its original authority snapshot there is
no safe automatic conversion to the new digest. Preserve the original operation,
generation, key and copy custody; do not edit its digest or invent a safe-release
attestation. A forward fix for new journals does not establish recovery of an
already stranded legacy operation.

### Retire an incomplete legacy generation into custody

When the original full authority preimage is unavailable, a Director agent can
use `POST /v1/products/{product}/testing-generation-retirement`. This retires the
active generation pointer, **not the operation fence**. It is not an in-place
network repair or permission to retry an old copy. The exact incomplete,
previously queued generation must agree with the held service operation's
legacy journal, current owned testing target and original resource allocation.
Completed copies, new-format journals, staff holds, pending routes, target
aliases and unavailable current service authority refuse retirement.

Use the bounded repository client with the installed authenticated transport:

```bash
uv run python scripts/retire-testing-generation.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product example-odoo --context example \
  --operation-id stranded-testing-operation \
  --reason 'Retain the incomplete original for an isolated successor' \
  --evidence-file /private/operator-evidence/testing-retirement.json
```

Review the receipt, then repeat with `apply`, `--reviewed-dry-run` and a stable
`--idempotency-key`, keeping the same request and evidence file. The service
rechecks operation, generation, allocation, source-capture receipt and current
profile, target, override and configuration metadata under executor, lane and
configuration locks. PostgreSQL commits the immutable retained generation and
idempotency receipt atomically. A lost reply uses the original request, review
and key; the client never retries automatically. `readback` reads retained
custody without a review file. It reports unverified original authority and
unknown historical effects; it never returns a safe-release assertion.

Retirement sends no provider command and never cancels, changes a journal digest,
recaptures, restores, edits credentials, starts web or queues reconciliation.
The archive preserves the entire generation, held operation snapshot, target
snapshot and original capture receipt if present. Capture records, private cache
bytes, targets, storage and hostname remain untouched. The old hostname stays
reserved, and the retired generation cannot be reactivated. Automatic recovery
cannot cancel or retry that held operation.
The original recipient can retain its existing hostname during metadata updates,
including placing a staff hold. A different recipient cannot inherit that hostname,
including through an identity-only write or a combined target/identity bundle.

The existing replacement-resources and bootstrap clients can subsequently name
the **still-held operation** as the retained operation. They must qualify a
distinct target and namespace, use an independent hostname, preserve the old
target/storage, and pass their normal current isolation and sanitization gates.
Only that supported successor supersession releases the old lane fence. Its
new generation never adopts the old capture receipt. A decision about preserved
user logins or separate automation credentials remains effective for the new
restore and startup; retirement does not resolve it or run either path.

The additive retirement table is retained on schema downgrade and re-upgrade.
Keep a compatible service image for retirement readback and hostname protection;
forward-fix a failed transition using its original receipt rather than deleting
custody or restoring old database state. Retained generations and copy artifacts
require a separately authorized later disposal; this procedure cleans up none.

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

Isolated testing declares Compose's default network as internal for all three
services; web additionally joins the ingress network. Dokploy's domain conversion
adds that same default network, so it cannot create a second outbound bridge.
The receiver still rejects any additional network reach. Existing networks are
not repaired by readback; any corrective deployment follows the supported
generation/recovery path and must pass the same receiver inspection.

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

Replacement planning preflights the fresh generation's effective restore overrides
against the strict integration policy. Integration or analytics settings, connection
fence replacements, and Shopify actions other than clear block the plan with a
fixed, value-free description. Use sanitized overrides to continue. Execution
rechecks the intent before replacement effects and before post-deploy environment
or schedule writes; drift records `deploy_blocked.fresh_testing_intent_invalid`
instead of an unexpected failure. Completed generations and production keep their
ordinary deployment paths.

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

## Never-served testing generations

Before consulting records, the service must observe no serving Odoo process, or
two unchanged complete inventories with no containers for the exact project, and
read a complete empty provider deployment history for that exact target. The
empty-inventory path supports fresh bootstrap resources before the first container
exists; unreadable or changing inventories never qualify. Process
inventory is read before the serving configuration, so a closed startup gate can
be inspected without creating a configuration. A running process with an unreadable
configuration/session/login store, an incomplete history, or any provider deployment
cannot be overridden by this rule. These are collection-time checks, not a reservation
against someone starting a lane afterward; collect immediately before apply.

The service collector can qualify non-use without reading a serving configuration
only for a generation with a complete lifetime witness, created by the supported
fresh-resource bootstrap. It requires all of the following:

- The exact current target has one current generation,
  its original bootstrap operation and its matching fresh-resource allocation.
- Lifetime tracking began when that generation was prepared. The bootstrap has
  not completed, no drill database was selected, and the witness has never been
  revoked. Legacy records have no witness and stay UNKNOWN; there is no backfill.
- Complete public and private HTTP histories since preparation both contain
  failed public checks whose base, health and probed URLs bind to the generation's
  hostname, and failed private checks
  bound to its active, lane-owned private endpoint authority. Private observations
  have no public base URL. Neither history may contain a successful or skipped
  check or target. Missing either history or private endpoint authority stays UNKNOWN.
- No deployment record since preparation exists, even a failed deployment, and
  no current inventory publication since preparation exists. This includes
  uncertain serving attempts, not just successful final health checks.
- No current hold or recorded use exists. Setting a hold permanently revokes
  the generation's witness, even if the hold is later lifted. Known use also
  revokes it even when a delayed positive-use observation is rejected by ordering.
  A hold already present at generation creation also revokes it. Retired custody
  is immutable and supplies no current-generation proof. A testing deployment
  record revokes it atomically when written, before provider dispatch, covering
  both service and other replacement routes. The guarded
  replacement worker also revokes it before its first provider dispatch, including
  a dispatch that times out; that revocation covers all later chunks in the
  attempt. This deliberately excludes more states than only an acknowledged web
  start; an incomplete restore is not assumed safe.

Only their conjunction produces `usage=unused`, reason `never_served`. Session
and login booleans remain null: this is a separate proof, not an empty-session
claim. Source reads are complete DB queries, without a history limit. Pure collection
reads do not hold the global authority write lock; revoked generations return
early. Publication and consumption use their existing locked transaction. The proof
binds to the generation rather than a growing list of failed checks. Failed checks
against an earlier route do not count toward coverage. The predicate is rechecked
under the shared authority lock before publication and again when
consumed, including through the supersession snapshot adapter. Dispatch/hold
revocation removes an already-published never-served
observation; a delayed collector cannot republish it. Data custody, physical
isolation, backup gates and retained-operation fences remain independent.

Use the existing evidence-only `scripts/collect-disposable-lane.py collect`
procedure. When runtime occupancy is unavailable, the service tries this rule
and returns a bounded `never_served_qualification` result. Skipped qualification
is `never_served_not_attempted`; an older response without the field is
`never_served_diagnostics_unavailable` in the client. The client cross-checks a
positive qualification against the matching usage reason. A missing source,
legacy witness, uncertain dispatch, served-once record or incomplete history
stays UNKNOWN. Never start or repair a lane to manufacture negative evidence.
A newer client rejects an older service's different coverage; deliver compatible
API and Odoo workers together before using this path. After tracked generations
exist, retain a compatible image and forward-fix: older images cannot read the
new generation fields or enforce lifetime revocation. No migration, grant or
production change enables the rule.

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

Admission requires a completed qualified fresh testing generation. Under Director
decision Q347, the service reads successful-login history (`res_users_log`) on
that exact testing database over the preceding 24 hours, starting no earlier
than preparation of the testing copy, and the existing staff
hold. Every account counts, including staff, Client, portal and administrator
accounts; no maintained account list or current-use registry is required. The
read excludes logins older than copy preparation, uses a read-only SQL transaction,
creates no Odoo login, and returns only a
boolean. A hold skips the daily slot. On a serving or unverified lane, recent
sign-ins or an unreadable check also skip the slot with
a value-free reason on the lane's drill evidence. Missing tables/databases,
runtime mismatch, provider refusal, incomplete reads and unavailable credentials
cannot mean idle. This is the agreed recent-use check, not proof that an old
browser session has ended; staff should hold the lane before testing.
Preparation is the earliest existing safe boundary: a testing sign-in before
final health completion still counts. Production logins captured after preparation
can also cause a conservative skip; no later timestamp is used to erase that
uncertainty or testing activity.
The same-product restore policy, existing lane-local credential separation,
exclusive context/target ownership, actual storage/network inspection and
pre-registry sanitization remain mandatory. The same bounded receiver inspection
used by restoration checks actual containers, storage backing paths, network
isolation and the completed database's copy witness before the login read.
Recorded use, only-copy custody, shared resources or another lane operation also
skip that day's drill, even if the protective report expired. Failed source,
artifact or restore prerequisites still fail and alert. Unknown or expired
general observations do not block this scoped check and are never renewed or
published as universal positive facts. The accepted evidence lives only in the
private drill receipt; it cannot qualify other disposable-lane operations.
Deployment leaves the per-lane `odoo_testing_drill_enabled` flag false. After
landing, configure it through the existing authenticated `POST /v1/product-profiles`
path: read and preserve the current complete profile, change only that testing
lane's flag, and post the updated profile with its normal idempotency key and
existing `product_profile.write` authority. No new grant is needed. The service
collects this drill's current evidence before queueing and again at worker
activation against the exact lane/configuration/resource snapshot. Provider reads
run outside shared authority locks; the short locked commit rechecks the snapshot,
holds, credentials and aliases, and refuses a changed or expired observation. Production
lanes cannot enable the flag. An unchanged failed prerequisite produces no
repeated admission; corrected facts/configuration can retry the same daily slot
before an operation has been queued.

Queueing archives the prior generation and planned copy in the private slot
receipt without changing the running lane's settings. After its hold check,
the worker atomically selects a new service-named testing database and matching
filestore path and activates that exact generation under its current lease.
It repeats login-history, hold, credential and resource admission; changed
source/configuration refuses before selection. General collector rows are never
rebound or renewed. Its accepted snapshot is stored on the active worker attempt;
an unchanged snapshot
does not expire in the middle of that continuously leased restore. A reclaimed
attempt requires fresh admission. It reads login history from the original served database and its committed
copy witness, including when web is stopped or the new copy is incomplete;
copied production login rows in the new database cannot stand in for testing use.
An unavailable original login database normally skips rather than assuming
non-use. The verified-down repair rule above can instead qualify usage while
preserving UNKNOWN; the independent resource/copy inspection must still pass.
This also applies when Launchplane's own stop or stalled operation caused the
downtime: excluding that cause would recreate a repair deadlock. The original
database is retained and the already-opened new-copy gate below still protects
testers; an explicit staff-testing hold always refuses the operation.
If the selected new copy already has its web-start gate, reclaim
skips and preserves it: people may have used it before final completion was
recorded. A stopped web process alone does not erase that gate's protection.
A running container still waiting behind that gate can resume an unopened copy.
Every provider checkpoint rechecks the generation, admitted use/custody/isolation facts and
current hold. The normal replacement recovery guard also binds the profile,
runtime configuration and source/secret metadata before provider effects and
refuses authority drift. Routine renewal of the general observation does not
cancel a running copy. A sign-in before activation
or a staff hold therefore protects the lane even after queueing. Protect a
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
