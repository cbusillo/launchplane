---
title: Disposable lanes
---

Overall direction permits agents to create, reset, rebuild and delete testing
and preview lanes and products that never went live. People using a lane,
the only needed copy of data, production resources and live credentials remain
protected. A label, missing staff hold, distinct volume name or distinct secret
version is not proof of those facts.

## Stable-address rebuild lifecycle

`workflows/testing_rebuild.py` provides the service-side lifecycle for a testing
lane with one stable HTTPS address and a separate candidate copy. It is a source
component: the existing resource/bootstrap endpoints below do not yet call it.
It exposes no new HTTP operation, UI control or permission. Its tests qualify the
controller and checkpoint storage, not a real provider, restored data or a live
lane.

The immutable plan binds the existing route/address, old and new target/resource
identities, the candidate build and a managed source reference. Empty rebuilds,
production-copy restores and configured upstream restores use the same lifecycle.
The service adapter resolves those references from admitted records; these are
not caller-supplied provider coordinates. Production destinations and shared
copy identities are rejected. Different resource names are only a contract
check; the adapter must independently inspect actual backing resources.

Preparation leaves the serving copy and lane metadata authoritative. A bound
receipt must verify independent resources, local credentials and removal of live
connections before candidate startup. Fresh private health and exact runtime
identity precede switching the existing route. Applied/public health and identity
at the original address precede metadata adoption and old-copy retirement.
Retirement drains/stops the old runtime and preserves its storage and custody;
physical deletion is outside this operation. Public probe URLs must use the
stable lane origin. Missing, stale, unverified or skipped evidence preserves the
intent for observation; only a fresh verified failure or identity mismatch
starts automatic rollback.

Failed preparation or private readiness leaves the old copy serving. Failed
public verification, including immediately before metadata adoption or before
the old stop begins, persists an inverse route intent. The controller verifies
the original copy at the same address and restores its original metadata before
reporting failure. Publication is a DB-only atomic compare/write/readback under
the original operation and authority-writer lock. An unavailable publication
outcome cannot be replaced with an assertion that the old metadata still wins.
The checkpoint retains whether forward publication started and passes that fact
to inverse publication, including after a lost commit reply.

`testing_rebuild_storage.py` stores checkpoints in the existing DB-backed record
payloads, scoped by product, context and operation key. The envelope is completed
checkpoint evidence, while the payload keeps the actual rebuild stage. It is not
an execution lease or a replacement for a
durable provider fence. The adapter must hold the existing lane/worker lease and
recheck authorization, holds/use, custody and production isolation before effects.
The native rebuild protects a current staff hold or positive use unless the
existing repair rule qualifies a historical report through verified downtime.
A staff hold always wins. UNKNOWN usage alone does not require downtime or a declaration of non-use; it does not supply
any missing custody, resource-isolation or credential evidence.
Provider calls reconcile their original dispatch keys before any replay. A
foreign route, changed plan or inaccessible authority refuses continuation.
Permission and transport exceptions propagate without a fallback or automatic
retry.

After an interrupted route switch, read actual applied authority and public
identity; never issue a second switch merely because its acknowledgement was
lost. Resumption calls the adapter in reconciliation mode, which can redispatch
only with the original provider fence's explicit proof that the effect was
never dispatched and is safe to run. After an interrupted stop, preserve the
retirement intent: the old copy may already be stopped, so it cannot be
advertised as a healthy rollback target.
The supported exception requires an original stop proven terminal and never
dispatched, plus fresh private readiness for the old copy. The checkpoint keeps
that recovery proof before moving back; its contract refuses rollback without
that proof once a stop intent exists. Pending, dispatched and unknown stops
cannot use this path. A refusal propagates before any inverse action.
Preparation and failure evidence cannot be replaced once recorded; readiness
and route observations continue to refresh as the operation progresses.
An interrupted preparation retains its candidate, copy receipt and original key.
Use a compatible controller/adapter for continuation and preserve unresolved
provider effects, rather than deleting checkpoints or replaying a legacy
bootstrap against that copy. Terminal checkpoint reads return their historical
receipt even after a later rebuild moves the lane. They do not reacquire a
mutation lease or claim current live health; the service caller still authorizes
the read. Worker/HTTP integration in [#3968](https://github.com/cbusillo/launchplane/issues/3968)
must supply cancellation/abandonment for failed preparation with retained
candidate custody before this component is activated as a rebuild control.

## Repair usage rule

`control_plane/lane_usage.py` owns the usage decision for disposable repair.
A current staff-testing hold always protects the lane. Without a hold,
UNKNOWN occupancy alone does not require downtime or a declaration of non-use
for a disposable non-production lane. It remains UNKNOWN, never `unused`.
Positive usage, including recent login/session reports, protects a serving or
unverified lane. Q380's existing verified-down rule can qualify those automated
positive reports for repair. This is repair eligibility, not an assertion that
nobody ever used the copy. Initial generation, native rebuild and shared
disposable admission all use this rule; an invalid lifetime non-use witness
is never treated more strictly than UNKNOWN or turned into a requirement for
negative evidence.
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

For positive-use qualification during initial Odoo generation preparation and its resource/bootstrap rechecks,
HTTP 404 also qualifies when the same call's fixed host probe independently
verifies the exact testing web container is stopped. Complete container
inventories before and after must agree on identity, running state, start time,
configuration, mounts and networks, and the provider placement must remain
unchanged. Ambiguous services, an unavailable database, custom web commands or
changed snapshots do not qualify. The normal two-slot, every-URL monitor rule
still applies. A 404 alone, HTTP 401/403, a running web container, production or
a staff hold never gains this exception. This proves present unavailability
for repair; login/session usage remains UNKNOWN and no custody fact is supplied.

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
already-opened replacement copy. It attempts login-history collection; an
unavailable history stays UNKNOWN after the independent resource checks pass.
UNKNOWN is preserved in that scoped receipt, which remains usable only by
the same continuously leased attempt. Its captured usage revision covers the
unchanged historical observation if monitoring expires or recovers during the
attempt; a newer use observation is evaluated again, and holds, needed-copy and
resource protections are always rechecked. Admission uses the scheduler's clock.
Reclaim needs new admission and a new captured usage revision.
Storage maintenance uses the same usage rule before dispatch and at readback:
deleting retained copies still requires the full fresh custody/isolation guard,
while capacity-only inspection keeps its independent receiver and copy-witness
checks without requiring a negative usage read.
Connection-isolation and environment-staging guards also use the shared usage
decision. Acknowledged activation/interlock lifting keeps its original explicit
hold and known-use checks; it has no UNKNOWN usage requirement. Preview operations
use the common guard. Without stable-lane monitor authority, UNKNOWN preview
usage may proceed with fresh reproducible-data and isolated-resource evidence;
positive use still protects the preview.
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
permission. UNKNOWN usage alone does not require downtime.
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
Resource collection inspects eligible resettable Odoo testing copies on both
healthy and down lanes when no staff-testing hold is active. A successful
inspection can supply reproducible-data and isolated-resource facts independently
of UNKNOWN usage. An unavailable inspection clears inherited resource facts;
it cannot qualify them from the earlier observation.

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
`serving_container_stopped` identifies a stable, exact stopped testing web
container. It remains UNKNOWN for usage; only the bounded repair rule above
can combine it with current monitor evidence. It does not qualify walkthrough
accounts, never-served history, data custody or production isolation.

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
authenticated actor and current time itself. For an exact owned preview it
records the Director agent's observed usage, data custody and isolation facts
as cleanup-only observations. They cannot qualify runtime sync, deploy or other
ordinary preview mutations. Preview refresh and retirement still read protective
reports under their existing rules. Recording wakes normal preview reconciliation.
The existing client supports `record` with `--preview-id`, `--usage`, `--data`,
`--production-resources` and the `--usage-evidence`, `--data-evidence`,
`--isolation-evidence` references. Each observation remains bound
with required evidence references, the current binding and the existing expiry.
Use `--lifetime-seconds` for a slower queue, within the service's existing
fifteen-minute cap; expiry still requires fresh facts before another effect.
Unknown facts stay unknown. Existing protected reports cannot be overwritten by
an unverified safe assertion. This supplies scheduled preview cleanup's facts
through the existing service API, without direct record edits.
Scheduled preview cleanup requires current unused/reproducible facts and a
confirmed closed PR. It does not require `production_resources: isolated`:
credentials the preview reads are outside its destroy set. Production lanes,
inexact preview identities and targets shared with another lane still refuse.
The destroy path remains limited to the preview's own target and storage;
production-shared targets, volumes or databases are not disposable. Runtime
sync, rebuild and refresh retain their existing isolation rules. Credential
separation is independent work, not a prerequisite for deleting a preview.
Driver-managed preview database destruction checks the database and role against
that preview's generated identifiers and the template server before any effect.
A foreign database, role or server is held without deleting the application or
issuing database commands. The shared resolver also protects refresh from
altering a foreign role.
For testing lanes, caller-supplied facts and references remain assertions: while an authoritative collector is unavailable, the service
stores usage, custody and isolation as `unknown`, even if the request asserts
`unused`, `reproducible` or `isolated`. Those assertions never permit an apply.
`clear` removes this product's exact-scope observation only when
`expected_binding_sha256` matches the reviewed current `inspect` binding,
`expected_observation_sha256` matches its complete recorded observation (or
absence), and the lane is still its declared testing lane or exact owned preview. Validation
and deletion share a database transaction that excludes binding and preview
changes. A missing or stale digest, undeclared lane, production lane or changed
preview refuses with `409 disposable_observation_refused` without deleting
evidence. A later protective report also refuses an older review on an unchanged
lane. Inspect exposes the observation digest and a safe stored protection summary;
these are retained reports, not newly collected facts. Invalid ownership, including
a destroyed or ambiguous preview, leaves its observation in place.
Clear changes no staff hold, custody inventory, provider resource or
historical operation. Clear a protective report only after resolving its source:
the person has stopped using the lane, the needed copy is preserved, or resource
separation is complete. Then record fresh facts and references. A safe assertion
cannot silently overwrite an in-use, only-copy or shared report.
Missing observations continue to refuse writes requiring positive facts. The
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

## Stable testing traffic between disposable copies

The native testing route adapter gives the lane one stable dynamic route identity.
It binds the existing HTTPS hostname to a unique, physically verified private
DNS alias for the predecessor, never a reusable container IP,
and the prepared candidate backend. A first migration from Docker-owned routing
publishes a lane-owned file at strictly higher numeric priority, initially still
serving the predecessor. The adapter verifies every applicable router and its
actual backend; it does not treat a duplicate or tied router as proven authority.
HTTP and HTTPS entrypoints remain distinct when the predecessor uses both.
Subsequent rebuilds require the recorded owned template and reuse the same route.

The managed testing connection policy is rendered from the same source as Odoo
Compose routes. It lives in the owned file and survives retirement of its former
Docker producer. This enforces the existing browser connection-isolation rule
on the owned testing response, including a legacy predecessor rendered without
generation headers. It can therefore block external connections that the old
render allowed; that is the testing isolation policy, not transparent header
preservation. A router that only redirects to HTTPS keeps its redirect middleware
without adding a response CSP. Unknown/broad host rules, unsupported middleware, changed
predecessor configuration and unexpected services refuse admission. The host-rule
check evaluates boolean ownership conservatively; a foreign Host clause joined
with OR cannot hide another matching path, and Go regex syntax is not evaluated
using Python regex semantics.

The fixed managed-server program discovers the running Dokploy Traefik process,
its watched writable directory mount and private applied API. It reads only the
lane-qualified dynamic filename. Applied-route observation uses a read-only exec
in that same identified proxy to fetch its existing loopback API. It requires no
host route to the proxy's Docker bridge address or published API port. The reader
still requires managed-ingress membership and a private network namespace;
host networking and sharing another container's network namespace refuse.
It disables HTTP proxies, bounds emitted bytes, and applies a read-idle timeout
within the existing Docker-client deadline. The client deadline bounds observation
latency; it does not prove the container's read-only exec has stopped. An oversized,
incomplete or failed response refuses before any route writer starts. It never
enables an API, changes proxy configuration or tries a different access path on
failure or denial. The emitted-program regression makes host HTTP unreachable
while container-local reads and the ordinary cutover checks still work.
CI also runs the actual reader in a digest-pinned isolated Traefik container with
no bridge or published API port; the provider-free unit fixture is not its only proof.
Both CI paths invoke the rehearsal command from repository quality metadata.
Publication rechecks the static configuration,
provider state, current file digest and retained physical web identity, then uses
the existing proxy container to write a temporary file and atomically rename it.
Immediately before that writer, a forward publication also requires exactly one
running web process owning the candidate alias across every proxy network,
including Docker DNSNames, network aliases and attached container names compared
without case distinctions. The candidate must share a proxy network, bind the
requested Odoo port, and match its expected project and admitted lane/runtime
identity. A shared, missing, stopped or rebound
alias refuses without touching the route. Inverse identity comparison excludes
only the observation's deployment timestamp, as the runtime identity contract does.
No application receives a provider credential or host mount. No global Traefik
configuration, shared file, process or other product route is updated. The caller
must hold and renew the original authorized lane/provider operation throughout,
including immediately before dispatch. A real provider denial propagates through
the existing transport; there is no second writer or identity fallback.

An absent reply does not prove the proxy writer stopped. The caller retains the
original durable dispatch fence and continues observing it; an old file alone
cannot exclude a still-running writer or authorize an inverse decision.
A fixed publish program that exits before invoking the atomic writer returns an
explicit `route_write_not_started` receipt. The caller can close that original
child effect as excluded after verifying the old route. Once the writer may have
started, a timeout or generic failed frame remains unknown; killing its Docker
client does not prove that the daemon's exec has stopped. Permission denials
remain denials and never become permission to retry. A denial after writer
invocation leaves its effect unknown: an authorization plugin can refuse the
response after execution. Only the explicit not-started receipt excludes it.
A written
but partly/unapplied group stays pending without another dispatch. Public health
and exact runtime identity remain the controller's separate proof before serving
publication or old-copy retirement. The old physical copy must still be qualified
before switch-back; an already stopped predecessor does not prevent observing the
current candidate, but cannot authorize an inverse switch.

The supported host capability requires Dokploy's directory-mounted dynamic file
provider, its private applied API and the existing managed terminal/Docker
transport. Missing capability remains unqualified; no host configuration or access
grant is invented to make it pass. Source delivery alone does not activate this
adapter, establish canonical publication or prove CM. Those stages remain #3963.
Maintenance and rollback use the same recorded route identity, original effect
and admitted backends; retain the owned template/receipts and old-copy custody.
Do not remove a route or a data volume to clear uncertainty.

The isolated Traefik rehearsal executes the actual Linux atomic writer and checks
continuous public old/new/switch-back responses while retaining the old copy. It
cleans up its own containers/networks. Initial Docker migration, stale provider
state, partial application, foreign routing and denial paths also have fixture
behavior tests. These are distinct from managed-host and live CM qualification.

## Testing candidate storage before adoption

The rebuild candidate allocator keeps custody in
`launchplane_testing_candidates`, keyed by its service-generated namespace and
bound to the original operation, intent and serving target. This record is
separate from the lane's current generation. Allocation creates a private
project/environment/compose and independent local volumes without changing
the lane profile, canonical target records, runtime values, serving inventory or
public address. It does create a physical provider-target reservation under the
candidate namespace. Audit and backfill recognize that reservation only while its
identity matches durable candidate custody, using the same rule for rehearsal
reservations. They neither invent a serving target pair nor block unrelated
backfill for a valid reservation; an orphan or mismatched reservation stays blocked.
The target is reserved in the existing physical provider registry in the same
transaction as its candidate custody; another lane cannot adopt it.
Compatibility target-ID records are also fenced, both at capture and before
subsequent provider effects; an older lane reference cannot become permission
to qualify or change its target as a candidate.

The caller must hold the authorized lane operation and recheck its authority,
holds, use, custody and isolation at every checkpoint. Provider effect intent
and each returned identity are recorded before proceeding. Lost creation
responses require original-key observation of the complete uniquely named
placement. Confirmed parents can continue; absent uncertain creates remain
unknown. Storage retries inspect the original empty, labelled resources and
refuse changed or in-use storage. Nothing is deleted to make a retry work.

This source component reuses the isolated placement creator used by upgrade
rehearsals and the existing testing backing-store readback program. It removes
the need to adopt a candidate as the serving target merely to qualify its
storage. It does not activate a rebuild endpoint, restore data, move traffic,
publish a generation or retire the serving copy. Those stages remain part of
the stable-address rebuild work in [#3963](https://github.com/cbusillo/launchplane/issues/3963)
and [#3968](https://github.com/cbusillo/launchplane/issues/3968).

The schema migration adds custody without modifying existing lane records.
Use the normal service schema-migration procedure on delivery. Rolling back
code retains the candidate table and physical reservations, including uncertain
effects; recover with the original operation rather than clearing custody or
reusing its target. Local SQLite tests exercise record semantics; the matching
PostgreSQL integration cases qualify the actual database path in CI.

## Private candidate build and copy preparation

Candidate application preparation binds an admitted artifact and source to the
allocation before provider configuration. It reads the existing three generated
testing-local credentials. Secret selection happens before version lookup and
decryption, so other managed runtime secrets are never materialized for this
copy. It does not rotate passwords or write the serving runtime, profile,
overrides, inventory, target or current-generation records.

The rendered candidate has independent storage, a pinned image, no public
hostname/router or host ports, and a namespace-qualified private ingress alias.
Its database and runner remain on the internal data network. The existing
SQL-before-registry restore runner handles production copies and upstream
copies; the existing bootstrap handles an empty database. Projected overrides
remove integration connections and retain the lane's public address. A candidate
uses its own manual host schedule, so preparation cannot replace the serving
lane's data-workflow schedule.

Before each provider effect the caller rechecks its exclusive operation lease,
authority, holds, usage and physical custody. Configuration and deployment
intent are persisted first. A continuation only accepts the exact configuration
or the uniquely titled original deployment. An absent or ambiguous effect stays
unknown. Disabled schedule setup and native artifact preparation do not attest
restore dispatch. Native preparation reuses its existing immutable capture and
receiver-chunk readback. After all checks and the provider-effect lease succeed,
one compare/write records restore dispatch with the original schedule and prior
job IDs before the callback permits the trigger;
continuation observes that one terminal job instead of triggering another.
Private readiness then checks actual storage/network isolation, pinned installed
images, runtime and copy identity, integration settings and the web-start gate,
and verifies the testing-local credentials without signing in. The private HTTP
health request bypasses proxy environment settings and refuses redirects. A
read-only connection using the running web configuration checks the generated
database password and the active local administrator's stored password hash.
The login follows startup's trimmed value or `admin` default. Local database
authentication or SQL/file permission failure in either the integration readback
or credential program is a failed candidate check. Only a Docker socket/API,
daemon or authorization-plugin refusal is reported as a provider denial. The
candidate, restore and route host programs share one classification and drop
private driver stderr; none retries or selects another identity after a refusal.
The plaintext master value and database settings are checked for consistency
with the running container environment. These local comparisons do not attest
that a stored provider environment update was deployed; provider configuration,
installed runtime identity and image are checked separately. Readiness never creates a session
or login-history row, which would otherwise protect the new copy from its next
rebuild. Passwords, hashes and raw program output stay private to the provider
transport. This checks credential installation, not an interactive browser
sign-in or Client acceptance.

A terminal original build before restore dispatch, or a terminal restore, may
be marked abandoned while keeping its candidate and custody records. A failed
setup with explicit undispatched evidence can therefore resume its original
setup or retain the private copy without starting a job. This action does not
stop or remove either copy. Once restore dispatch started, running, absent and
ambiguous provider jobs cannot be abandoned or redispatched. Older records
without dispatch evidence remain unknown; they cannot be reset to undispatched.
The
service worker must preserve their original-key recovery path. A prepared copy
is still private: stable-route switching, atomic serving publication, old-copy
retirement and the service/UI action are separate stages under #3963. This
source component does not activate them or establish live CM proof.

Maintenance uses the original candidate operation and its stored receipts.
Rollback retains candidate application intent, storage and physical reservations;
it does not clear fences or adopt a candidate as the serving lane. No schema
migration is added beyond the candidate custody table.

## Atomic serving publication for a rebuilt testing copy

The native publication stage stores the exact predecessor and prepared successor
metadata before any traffic switch, using existing DB evidence storage. This is
an internal worker capability, not a caller-supplied target or permission. The
original provider reservation, current lease holder and expiry, exclusive product
context, candidate custody, delivered context/instance runtime inputs and generated
credential metadata are checked within the authority transaction. An input change
since private preparation refuses adoption. Password values are never added to
the successor runtime record and no secret or version is rotated. Local database,
HTTP and disabled-cron coordinates are the same projection used to prepare the
copy; the HTTP port comes from its managed profile. Canonical adoption cannot
leave an older database endpoint for a subsequent deploy to select.

After fresh applied routing and verified public health/identity at the unchanged
origin, one transaction adopts target representations, resource selection,
neutralized overrides, route provenance, inventory and the deployment receipt.
A restored copy publishes its real generation witness. An empty rebuild has no
invented restore generation. Existing ingress/TLS ownership and the profile's
address remain unchanged. Both physical copies stay reserved; legacy predecessor
custody is captured before replacing the canonical target. Storage is retained.

The publication receipt commits with those records. A lost reply is resolved from
that original receipt. An inverse waits for an in-flight original transaction,
then restores the exact preimage only when the current records still match that
operation's committed successor. Unknown database outcomes stay pending. An
uncommitted original transaction proves that no adoption occurred. An inverse
of a planned forward updates only its receipt, preserving later runtime or
credential changes. An inverse that restores serving records checks the retained
predecessor for foreign compatibility aliases, just as forward adoption checks
the candidate. Completed publication provenance remains readable by an authorized
service caller so a later rebuild can qualify its existing owned route template.

Rebuild checkpoints now also compare-and-write under a database lock; a stale
writer cannot erase a terminal receipt, effect intent or recovery evidence.
Legacy predecessor runtime identity fields are retained as actually observed;
new candidates require an explicit product and immutable image digest. This
compatibility does not supply lane ownership or authorize a mutation.

This stage replaces sequential canonical record adoption for the native rebuild.
It does not activate a service/UI action, stop an old copy or prove CM testing.
The parent integration still wires the controller/provider lifecycle, preserves
the private backend alias during later ordinary deploys, and qualifies live CM.
No production mutation or new authorization grant is part of this stage.

### Continuing native route authority

The existing Odoo testing route-binding refresh controller renews a published
native copy from its original publication and route custody. It does not require
or create provider-owned domains or a legacy ingress audit. Each run reads the
actual owned file and applied managed-proxy routing, including runs before the
attestation is due. The route must still select the published copy at the same
address with unchanged ingress/TLS ownership and canonical target. The runtime
identity comes from the current inventory, so an ordinary deployment can update
the build without replacing the original route provenance. A stopped or missing
web process, a mismatched runtime identity or an absent private alias cannot renew
the attestation even if the proxy still contains the expected route file.

A successful due refresh retains the publication key and route digest and records
a new observation digest and 24-hour expiry. Historical publication/readiness
timestamps alone cannot renew routing evidence. Missing custody, a foreign target
alias, unavailable observation or routing drift returns the existing controller's
attention outcome without restamping the binding. Provider authorization denials
remain explicit denials; there is no alternate caller or transport.

The existing exact-lane authorization is checked before observation. The final
CAS write locks product authority and the binding and rechecks the observed source
records and target aliases. An authority change can make a refresh conflict for a
later fresh run. Freshness-only changes preserve publication, rebuild guards and
the original inverse: their comparisons retain the publication key, route digest
and all serving authority, while excluding the observation clock and digest.

Native observations run in the service thread pool, one lane immediately before
its own write. They do not block the HTTP event loop or expire earlier lanes'
observations while planning a batch. The existing shared half-life/expiry policy
owns renewal timing. Completed-key replay returns its receipt before remote IO.
Dry-run observes without writing. A provider authorization denial ends the batch
with its trace; any earlier per-lane writes retain their original child receipts.
Legacy managed and external bindings keep their existing paths and bounded failure
reporting. Manual/browser single-binding reconcile identifies the native controller
as the supported renewal path rather than returning a provider-domain error.

## Native testing worker integration

The existing durable target-replacement action accepts testing-only
`replace-and-cutover` requests with an explicit `empty`, `production_restore`
or `upstream_restore` source and an immutable artifact/source pair. The native
worker joins candidate allocation, private application/copy preparation,
stable-address routing, atomic publication and retained-copy retirement. It
uses the same action authorization and queue; it adds no grant or alternate
provider identity. The shared service record store is required.

An original intent retains the serving metadata, observed runtime, actual
private backend, server and container/data custody before candidate writes.
Every boundary rechecks current authorization, exclusive execution and
provider leases, protected-copy reports, physical target aliases and resource
isolation. Forward admission also checks staff holds, profile/serving authority
and actual login/session use immediately before the route effect is checkpointed.
Profile binding covers this lane's driver, address, port, health path, image
repository and exclusive context. Unrelated product labels, timestamps or other
lanes do not invalidate the admitted copy. Its original complete profile remains
the immutable private-input provenance; publication compares the current runtime
binding and uses the current locked profile for its authority transaction.
That original cutover admission is not re-qualified against downtime after the
rebuild itself restores health or changes the monitor identity. A new forward
effect still respects a current hold or changed authority. Observation and an
inverse of the original route can proceed while preserving those changed
records. DB-only publication records an already observed healthy cutover under a
new staff hold, carrying that hold forward; retirement still refuses it. Serving
comparison excludes that current hold and the hold writer's target timestamp
and source label. Publication preparation uses the same comparison. Serving
identity and runtime configuration remain strict. The real hold writer is
exercised during preparation, healthy publication and interrupted inverse.

A lasting local admission change before cutover, such as changed runtime inputs,
credential selection or context ownership, records a finite failed result after
proving the old route is still selected and neither original route writer has
dispatched. Unknown candidate creation must first be observed under recovery;
unresolved storage or configuration, and dispatched build or restore jobs without
terminal custody evidence, remain pending. The candidate and its independent
effect fences are retained. An
existing preparation receipt is preserved in the failed checkpoint, which also
records the safe refusal cause. A lost checkpoint reply returns that original
failure even while application preparation is incomplete, provided no original
candidate writer remains unresolved. Terminal recovery binds the original
authority digest, candidate target and available application plan. A fresh inspection
can then admit a new rebuild using current settings. Staff holds, current use
and unfinished provider work remain pending with their concrete reason. After
route or retirement intent, recovery continues under the original fences and
requires outcome/exclusion proof. Pending causes are retained in queue evidence
and logged at warning level; provider payloads are omitted. Storage qualification
runs before application execution. A resumed populated candidate uses its
original custody and private proof instead of requiring empty storage again.
The outer dispatch marker is written before an actual candidate or route/stop
effect, rather than before initial read-only admission checks. A refused initial
check with no effect can end without locking the lane indefinitely. Unexpected
post-dispatch errors, including lost execution locks and malformed observations,
remain pending on the original keys. Real authorization denials stay denied.
Renewing an outer attempt clears only its current dispatch marker. Historical
ownership comes from the existing candidate custody, child effects, publication
and checkpoint intents; worker failures, denials and cancellation use that
history. Completion uses only the live attempt's marker. A denial after renewal
therefore retains the original lane fence and cannot become a terminal generic
failure. Temporary source or metadata observations do not become permanent input
drift. After publication, original-copy retirement uses recovery safety and the
admitted physical port, while a current staff hold still pauses its dispatch.
The published-copy recovery check is bound to that original operation; ordinary
maintenance continues to require the current profile port. A fresh rebuild
qualifies the predecessor against its recorded physical port and prepares its
successor at the desired current port, without reverting the profile or changing
the public address.

Native route admission derives listeners conservatively from application routes
that may match the host; non-exact conflicts on another listener still refuse.
The proxy's canonical internal API/dashboard listeners do not become
application ingress. Its enabled, canonical high-priority ACME challenge route
remains outside takeover and above every application winner. A changed internal
route on an application listener still refuses. Duplicate Docker router families
may describe one predecessor only when every alias binds the same physically
verified backend and has equivalent host, listeners, effective priority,
middleware, TLS and load-balancer behavior. The plan retains every alias for
subsequent drift checks and emits one owned winner per equivalent group. It
never deletes a live router or changes proxy configuration to make admission pass.

Inspection and the native plan return fixed, traced HTTP 409 checkpoint codes
for observation, route compilation and retained-input failures, without exposing
exception/provider payloads. Authorization denials stay HTTP 403. Transient
observation failures in an admitted worker stay pending on the same operation;
only the inspection layer maps them to a diagnostic refusal. The signed-in submit adapter uses
the stable `testing_rebuild_not_ready` admission code for a refusal proven before
enqueue, with the safe specific message. Browser recovery can then return a
first refused submit to inspection without maintaining a copy of checkpoint
codes. Key collisions and uncertain or previously dispatched requests retain
their original request and key.

Staff holds and the hold writer's timestamp/source label are excluded from the
shared serving-target input projection. A hold pauses preparation and cutover;
lifting it resumes the same candidate rather than changing its input authority.
After admission changes, original private storage is inspected only in readback
mode. A still-running or unknown schedule stays pending; the inspector's terminal
empty/incomplete result cannot dispatch preparation again. Configuration records
bind the exact intended private configuration before the update. Recovery reads
that binding without resolving changed inputs or sending another update. A
nonempty mismatched readback remains pending; an older missing binding cannot
qualify a configured copy.

If the same owned compose is still empty, on its original server/environment and
namespace, with no environment, source, domain or deployment history, readback can
instead retain it as abandoned. No application build/restore receipt may exist.
The provider's generated optional app-name suffix is accepted while the exact
compose id, namespace name, server and environment remain bound.
The evidence binds the original plan and any recorded configuration fingerprint;
an older absent fingerprint stays absent. The operation can finish failed without
holding the lane forever. A late original update can only configure that retained
private copy with deployment disabled, and its abandoned record/lease cannot
advance to a build. Even an empty readback may later receive that original full
configuration, including credentials. Retention and eventual cleanup must treat
every such copy as credential-bearing; the unapplied receipt is an observation,
not proof of cancellation or absence of secrets. Original request evidence,
physical custody and volumes stay retained; no update is retried, excluded or
declared cancelled, and the next
intent uses its own namespace.

If original configuration is positively matched but preparation now fails before
any deployment, the same observation may retain that configured private copy
with a configuration-retained receipt binding its original plan and fingerprint.
This also handles a lost update reply and a testing secret rotation before the
first build. Admission-change recovery uses this same retention for both
configuring and configured copies; it does not advance an observed copy to a
runnable phase after refusing the operation. The original configuration must
match, deployment history and restore/build receipts must be absent, custody must
remain private and current guards/leases must pass. A nonempty mismatch stays unknown. No fresh inputs are
written or deployed. A replay uses the retained receipt and keeps old traffic
selected.

These observations can update only the original candidate's storage receipt,
configuring-to-configured phase, or exact configuration-only abandonment, under
its custody CAS, physical reservation and unchanged serving target;
they cannot allocate, change intent, deploy, publish or retire a copy.
The trusted store validates the exact successor shape and intent binding; its
CAS is not a separate attestation that a provider read occurred. That read is
the guarded observer's responsibility. Newly recorded protected data, production
sharing or aliases pause recovery with an
explicit cause instead of being reclassified as a new admission refusal.

An explicit resume of the same held operation is described below (#4016).
Managed-proxy execution receipts for interrupted writers are described below
(#4017). These continuations are integrated in the joined native source. Its PR
remains held from ready until their reviews are weighed and that head is
validated. Neither clearing a queue/browser
record nor replaying an unknown writer is a supported substitute; #3963 is
incomplete.
Candidate reservations are rechecked before cutover, and unresolved predecessor
deployments refuse further effects. Whole-project retirement requires only the
owned Odoo services in that project, not an unrelated application sharing its
project label.

The restore intent distinguishes a new workflow with no dispatched effect from
an older or unknown dispatch. A crash before the first effect can continue the
original workflow; unknown intents are observed without repeating it. A known
terminal failed build or restore can be abandoned while retaining candidate
custody and the old serving selection. Historical terminal receipts replay
without fresh runtime resolution or provider credential reads.
For private readiness, a received failed probe frame starts a persisted grace
period using the target's deployment timeout. A failed probe after that period
can report terminal failure only before any forward route dispatch and after
fresh proof of the original terminal build/restore and retained private custody.
The failed candidate remains retained, with its historic receipts intact; no
container, volume or credential is removed. A lost connection, incomplete probe,
running/unknown original job or permission denial cannot qualify that failure.
The first failure timestamp cannot be reset to prolong the same operation.

Route writes and retirement have separate original-effect fences within the
lane operation. A lost reply is observed before continuation; an old or absent
outcome does not permit a blind duplicate. A crashed queue worker resumes
observation with the same operation and keys. Explicit provider permission
failures are distinct from a fixed program's `route_write_not_started` receipt.
That receipt completes the original forward child fence without a route effect;
after verifying the old route, the rebuild reports failure and retains its copy.
An interrupted worker reads that receipt without rechecking candidate readiness
or starting another forward writer. For an inverse, the receipt proves only that
the same original writer may be retried under its existing lease and guards.
A lost frame or any failure after invoking the atomic writer proves no such
exclusion and remains observation-only. Explicit provider permission
failures remain authorization failures and are not retried as pending work.
If effects have already begun, a denial holds the operation in
`reconciliation_required`, preserving its denial code and lane exclusion without
automatic worker replay. Generic cancellation cannot release a native rebuild
with an outer dispatched effect or a child effect receipt. An unstarted request
can still be cancelled; otherwise the original worker's observation/recovery
path owns completion, not a caller assertion that the provider looks idle.
Only after fresh public health and identity at the unchanged address and atomic
canonical publication does retirement disable the predecessor's automatic
deploys and call the existing [Compose stop API](https://docs.dokploy.com/docs/api/compose).
Readback requires every service in the old testing project to be stopped. Its
provider object, containers, volumes and custody remain; no volume is deleted.
The host helper only reads custody and health. It shares the existing backing-path
inspection with copy/rehearsal code, including refusing bind aliases of testing
volumes used by another project. A restarted process with the same runtime and
data is still the same retained copy.
If the original stop is proven never dispatched and rollback restores that copy,
its recorded original automatic-deploy setting is restored through a separate
effect fence after canonical publication. An unknown stop or an older intent
without that original setting cannot silently qualify this recovery.

The old copy does not need its passwords rotated or compared with production.
The new private copy independently proves its testing-only generated credentials
and connection isolation before receiving traffic. Existing current local secret
versions are preserved, including across successive rebuilds.

Ordinary deploys of a published native copy preserve its private backend alias,
local database coordinates and recorded restore witness. Credential selection
excludes production/shared bindings before reading values. Existing local
credential versions are retained; installing changed local versions requires a
fresh private copy through the same rebuild action.

This is staged integration under [#3987](https://github.com/cbusillo/launchplane/issues/3987),
not live qualification. Supported agent activation, recovery from a
fully stopped initial legacy predecessor, a live host second-rebuild/next-deploy
rehearsal and CM testing proof remain under #3963. The legacy local apply command
is not a live-service fallback. No CM or production mutation is proved by these
source tests.

### Signed-in testing rebuild and native preflight

The testing Actions page replaces the unavailable generic replacement cards with
a typed administrator control for fresh empty,
sanitized production and configured upstream copies. It shows the unchanged
address and selected short commit. Selection uses the ordinary testing planner's
current-release inputs and forward-build guard; an unavailable current manifest
cannot silently move testing to its ancestor. The audit reason is prefilled and may be edited; a ready
lane can be rebuilt with the primary button without entering build IDs or text.
`GET /v1/products/{product}/testing-rebuild/inputs`
reads that selection, actual retained-copy/route admission, current use/hold and
generated credential provenance. It does not record an artifact, reserve a copy,
create a lease or dispatch a mutation. Its `candidate_verification: pending`
does not prove a new copy or completed restore. The worker still checks the
source, generated values, independent resources, health and identity before
cutover. The machine target-replacement plan route supports the same native
read without a provider write.

`POST /v1/products/{product}/testing-rebuild` takes the inspected build/source
pair, a concrete reason and the original request key. Existing administrator
cookie/CSRF and current policy checks precede admission. The server re-verifies
the selected current build, records its existing immutable artifact contract,
captures existing durable administrator authority and enqueues the same native
replacement operation. It accepts no destination instance, target, credential or
arbitrary replacement payload. New native machine admissions also require a
reason; already admitted operations remain readable and recoverable.

The browser persists the original payload/key and accepted operation ID. A
reload watches a known operation with GET; a lost enqueue reply requires an
explicit recovery of that same request. A denied request is retained and never
automatically resubmitted. Typed operation views expose status and phase without
provider payloads, authorization records or requester text. The browser
distinguishes a confirmed first-submit denial from an unavailable watch or denied
recovery of an earlier uncertain dispatch. The latter retain the original request
and operation. After a denied recovery, `POST /testing-rebuild/lookup` on the same
product prefix only reads the original key and exact intent; it cannot enqueue,
reselect a build or call a provider. Like the existing browser POSTs, lookup
requires the signed-in administrator's current Origin/CSRF evidence; the browser
client supplies it through the same supported session path. A new attempt is
marked dispatched by that client's existing callback after session lookup;
failures proven before POST dispatch return to inspection. Absence is not proof
that an earlier request cannot still arrive, so lookup retains the uncertain
request. Authoritative settlement of an unobserved request is tracked in #4014
before full activation; clearing browser state alone cannot exclude it. Known operation IDs
resume with GET after sign-in. A confirmed first-submit denial can be discarded
locally to refresh inspection, without another submit. Provider 401/403 preflight
refusals carry a denial trace. Other known pre-admission failures use a fixed
not-ready response; an uncertain create/commit reply remains uncertain. GitHub
rate limits and expired artifact-storage URLs stop build selection as unavailable,
without trying an older candidate or misreporting an identity denial.
The source feature
requires normal train delivery and service activation; its tests and mocked
browser journey do not establish CM health or rebuild timing.

`POST /v1/products/{product}/testing-rebuild/operations/{operation_id}/resume`
explicitly resumes a held native rebuild. It requires the original request's
`Idempotency-Key`, the observed `expected_updated_at` and `expected_attempt`, and
fresh authority for the existing target-replacement action from the original
caller. A signed-in human is bound by immutable GitHub ID; supported
`local_operator`/`local_admin` callers retain their original type, subject and token label.
This route uses the existing `read_operator_mutation_identity` dependency, which
refuses terminal-agent credentials. It follows the current target-replacement
action policy rather than adding the administrator gate used by the browser's
inspect/read routes. A caller admitted only by an action rule may resume through
the API but still be unable to use that administrator browser view.
The configured bearer value can rotate while those identity fields remain
unchanged. Changing the label changes the caller; resume does not transfer a
held request to that new identity or to an administrator.
A workflow or service identity cannot substitute. Cookie callers also need the
normal Origin/CSRF evidence. This action creates no authorization grant.
New native rebuilds reject workflow/service original callers at admission,
before creating an operation or dispatching an effect, using the same caller
boundary as resume. Existing receipts still replay before this new-admission
check. Historical workflow/service originals remain fenced; their independent
successor recovery is tracked in #4013, without transferring authority or
claiming the old effect ended.

Under the existing lane and row locks, resume records its fresh authorization
and prior held error in an audit receipt, and queues the same operation in one
transaction. It preserves the original request, build, source, operation ID,
request key and all provider dispatch fences. Another operation holding the lane
or a changed held revision refuses the transition. Repeating the same resume receipt returns
the current original operation without resetting a running worker. The worker
rechecks current authority and observes existing effects before continuing.
Requeueing does not certify that an original writer is excluded, regardless of
the held error code. Only the original provider fences can qualify dispatch.
This is not the legacy denied-operation fence-release flow: it releases no
provider fence and grants no alternate identity a replay.

The browser offers **Resume this rebuild** only for a known held operation.
Reload and sign-in recovery only read that operation; neither resumes it.
After a denied or lost resume reply, the browser retains the request and requires
a fresh operation read before another explicit resume. An unknown original
route writer still waits for managed-host outcome proof (#4017); resume does not
make an old route file or a timeout proof that dispatch was excluded.

Each native route effect retains its logical intent in the existing private
original-intent record. Each physical writer child separately retains its actual
proxy process, boot/clock epoch, directory identity, original fence, preimage and
execution key before dispatch. A replacement directory gets a new physical child;
the first child's custody never changes. No application receives a provider
credential or host mount, and there is no direct host-file fallback.

The managed proxy keeps one permanent descriptor lock and one latest-generation
receipt beside the owned route. The writer pins that directory before checking
its identity; rename and sync children inherit the descriptor. It removes only
abandoned staging files while holding that lock. Completed rebuilds do not add
per-key directories or files. The previous per-execution writer is removed.

Lock allocation has its own creation-only durable child.
An empty lock with no resource receipt for this directory in the current boot
can be bound before the first nonce, including after a reboot interrupted
initialization. A historical resource still requires its original actual lock.
Each actual generation initializer invocation gets a unique durable nonce and
never resends that nonce.
A positive witness can be adopted read-only; a lost pre-nonce call can be fenced
by another unique invocation under the same original lock. Ordinary writers and
settlements cannot create a missing lock or generation. They compare the exact
generation and revision before changing the latest slot. An old settlement cannot
seal a successor, and losing a same-boot historical lock is not permission to
bootstrap another one.

Every mutating shell checks its captured private proxy process identity, boot ID
and boot-time clock offset before filesystem writes. Capture requires the process
to have aged beyond its exported start tick and rechecks Docker's start identity.
A proxy restart can read and reuse a preserved directory fence. Its prior runtime
cannot write if an older directory returns. After a confirmed fresh boot, a
preserved known generation can advance under its actual current lock even if
Linux renumbered the device. The transition records the retired boot and original
slot; all older boot-bound calls reject. Unrecorded generations are not adopted.

After a lost writer reply, authorized recovery settles the immutable physical
attempt under its original lock. A busy lock remains unknown, including when a
killed shell left a child able to rename. Exclusive settlement records either the
exact applied document or exclusion. A retired runtime/root proves that its
writer cannot affect the current route; it does not claim the old writer never
ran. A confirmed non-applied forward attempt keeps the old copy serving. An
excluded or retired inverse may obtain a fresh physical attempt through the
existing durable retry transaction, preserving the rebuild and request key.
A completed busy reply proves that its call never entered the writer's critical
section. It retries with a fresh attempt after current admission checks, instead
of sealing that contention as a failed forward cutover. An in-flight busy lock
without that completed reply remains unknown.
Timeouts and denials never establish exclusion. Legacy dispatches remain
observation-only; recovery never guesses their original custody.

These are provider execution receipts in the existing operation journal, not
another desired-state journal. The isolated Traefik rehearsal disconnects a real
Docker exec client before rename and observes its inherited lock. Shell and
emitted-host tests also cover surviving children, generation/revision races,
missing fences, returning roots, restart adoption and fresh-boot recovery. They
do not replace managed-host qualification or the CM proof required by #3963.

The worker regression executes a second application's configure/deploy/initialize
stages and same-address adoption, then the ordinary replacement and post-deploy
workflows for a newer artifact. It verifies retained route, storage, generated
credential versions, private alias and both contract-owned image references.
Provider IO and build ordering are mocked, storage is prequalified, and this
does not constitute a live host rehearsal.
Continuing native-route refresh is tracked in [#3995](https://github.com/cbusillo/launchplane/issues/3995).
Scheduled native restores, retained-copy maintenance and capacity integration
are tracked in [#3996](https://github.com/cbusillo/launchplane/issues/3996).

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
`clear` removes only the observation and restores missing evidence. Run the
client's `inspect` command, review the returned `binding_sha256` and resolve
every protection, then pass that exact digest to `clear` with
`--expected-binding-sha256 <reviewed-binding>` and
`--expected-observation-sha256 <reviewed-observation>`, retaining the same product,
context, instance and preview ID. Clear sends one request; it never silently
re-inspects changed bindings or retries an uncertain result. A refused clear
requires a new inspection and review. A new clear client against an older service
fails request validation on the unknown observation-digest field before deletion;
an older clear client against this service refuses its missing digest. Non-clear
client requests retain their older shape. Rollback retains observations; the
previous service rejects the new `collect` request.
The service catalog deliberately publishes all existing API modes. The repo
client supports `inspect`, `collect`, preview `record` and binding-bound `clear`. Catalog-helper integration is
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

### Continue a restored generation's public edge

An independent Dokploy hostname does not provision its public edge. With a
separate TLS-terminating proxy, origin ACME HTTP-01 can hit that proxy's default
host instead of the origin: HTTP redirects to HTTPS, the default certificate
fails public verification, and the challenge returns 404. Qualify DNS, the
effective edge host and origin evidence before changing anything. A healthy
container does not prove public TLS.

`POST /v1/products/{product}/testing-public-ingress` supplies the bounded
Director-agent continuation for the **current** fresh Odoo testing generation.
It uses the same Director-agent identity boundary as testing preparation;
generic instance ingress remains adoption-only, and no policy grant, credential,
workflow or broader ingress authority is added. The request names only the
product, context and original generation operation, reason and mode. It accepts
no hostname, provider ID, upstream, certificate, raw config or source-copy input.

The service derives the new domain from the current generation/profile/target,
and its edge from the lane's existing active service-owned NPMplus binding.
Live provider placement must agree with that recorded endpoint. The existing
edge host must be public, enabled, free of custom locations/config/access
controls, and use an existing unexpired certificate covering the new domain.
The only allowed template config is this operation's own audited identity
comment. Its generated comment is preserved by the current provider API;
legacy `meta` is neither accepted on writes nor returned by that API.
Fresh provider reads must show loaded Nginx configuration and no private TLS,
extra location/directory config or authentication upstream before publication
and during readback.
Only that certificate's public metadata is read; DNS credentials and private
keys never leave the provider. No certificate is issued by this operation.
Unknown placement, certificate coverage, foreign/wildcard edge authority,
staff hold, shared target or changed records refuse the operation. Missing
prerequisites are reported, not repaired through another identity or provider.

```bash
uv run python scripts/prepare-testing-public-ingress.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product example-odoo --context example \
  --operation-id original-testing-generation-operation \
  --reason 'Publish the current restored testing hostname at its existing edge' \
  --evidence-file /private/operator-evidence/testing-public-ingress.json
```

After reviewing the receipt, use `apply` with exactly the same inputs and
private evidence, `--reviewed-dry-run` and one stable `--idempotency-key`. Then
use `readback` with that same evidence. The deployed API must include this
capability and use PostgreSQL fencing; running the client from a newer checkout
does not activate it. Keep the original bootstrap/resource requests, keys,
generation and copy-capture witness. The operation creates only the new exact
edge host, writes its audit and compare-and-writes the testing route binding
against every reviewed source. It preserves the profile, origin routes,
targets, storage, passwords, restored database and capture records.

Recovery is forward-only under the original edge key: a lost create response
can adopt only the exact provider route carrying the reviewed plan's marker.
That marker is a generated Nginx comment, with no directives or caller-supplied
config. The service also refuses active redirection/404 host claims before
creation. Provider legacy protocol settings must retain their supported
defaults; enable/disable remains the provider's separate lifecycle operation.
Existing disabled hosts retain that state during updates and can use the normal
lifecycle endpoints. The provider always creates hosts enabled, so a request
to create a disabled host refuses before transport rather than briefly exposing
it. This continuation deliberately publishes an enabled public testing route.
Its pending audit binds the generation, route authority and exact provider
payload, so certificate renewal with the same ID and unrelated metadata refresh
do not strand adoption. Meaningful authority or payload drift still refuses;
publication revalidates current records and uses a fresh compare-and-write.
An absent or foreign route after a dispatched create stays unknown and is
never created again. `readback` checks actual route, certificate and binding;
a terminal apply replay is historical evidence, not a fresh health check.
Do not issue a new key, edit records, replay bootstrap/restore, recapture data,
change the origin certificate policy, or dismiss an incident to resolve TLS.
Retain private review/receipts outside the checkout for same-key recovery.
The retained edge host and its certificate are not changed or removed.

Existing provider certificate renewal remains its maintenance path. A changed
certificate ID or route is drift requiring the supported ingress maintenance
path and fresh review; this create-only continuation never updates, disables
or deletes a host. No global proxy restart or verification setting changes.
After edge publication the operational parent still proves strict public
HTTPS, exact runtime identity and monitor-driven incident resolution through
the existing service checks. `public_health_verified` remains false in these
configuration receipts; source tests or private HTTP success cannot clear the
real incident.

### Complete verification after public TLS repair

A completed copy and post-deploy pass can precede a public TLS failure. Until
public verification finishes, the generation remains incomplete and inventory
still names the previous deployment. Repeating bootstrap or restore would not
repair that publication gap. This applies to every fresh Odoo testing lane.

`POST /v1/products/{product}/testing-verification` is the Director-agent
continuation for the same generation and installed terminal deployment. It
accepts only context, `instance: testing`, original generation operation ID,
deployment record ID, reason, mode and reviewed plan digest. It derives the
artifact, target, database, hostname and copy witness from service records.
Production and slot-cutover lanes are excluded.

```bash
uv run python scripts/complete-testing-verification.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product example-odoo --context example \
  --operation-id original-testing-generation-operation \
  --deployment-record-id installed-terminal-deployment \
  --reason 'Finish public verification after TLS repair' \
  --evidence-file /private/operator-evidence/testing-verification.json
```

Review the private evidence, then repeat the identical arguments with `apply`,
`--reviewed-dry-run` and one stable `--idempotency-key`. Use `readback` with
the same evidence afterward. The service must already expose this capability;
apply requires PostgreSQL. A newer local checkout does not activate the API.

Both planning and application qualify the current target, actual running web
and runner image digests and deployment identity, original database copy marker,
isolated volumes/networks and payload-bound web gate. The fixed host inspection
acquires the shared copy lock (recreating only its empty coordination file after
host reboot or temporary-file cleanup) and performs no restore,
registry initialization, setting change, container restart or provider schedule.
All public HTTPS health, canonical, logo and runtime-identity checks must pass.
A hold, active operation/fence, changed generation/authority, missing copy
witness, wrong artifact or later deployment refuses publication.

Application holds the generation executor lock and existing synchronous lane
reservation, preventing another deploy from starting. After the live reads,
a short authority comparison transaction preserves the failed deployment in a
separate prior-evidence record, completes the original deployment, and publishes
inventory, testing release tuple and generation completion atomically. Original
operation requests, keys, terminal results, retry budgets, capture receipts,
retained custody and the restored password are unchanged. The new deployment's
`testing_verification_previous_record_id` links the preserved failure.

Recovery uses the exact reviewed request and original key. Once the original
request commits, a lost response replays its atomic receipt without another
provider effect. An active original executor reports `testing_verification_in_progress`;
observe its completion before same-key recovery. `readback` separately
checks current records and live evidence. A failure before commit publishes
nothing. Preserve evidence and record a refusal with its trace; never change
records by hand, reset attempt budgets or restore again. If authority changed,
review the changed evidence before planning further. Incidents resolve through
normal monitor checks after truthful inventory publication; this endpoint never
dismisses or edits an incident. Keep operational acceptance open until that
monitor observation is recorded.

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

Unused override secret pointers may remain in a testing lane's retirement record.
The shared retirement check allows their removal only on an exactly owned testing
lane when no current product declaration, plain lane setting, or stored override
in any phase requires the pointer. A pointer still used by an override remains a
conflict: sanitize the stored override through its supported service path first.
Generation omissions remain bootstrap evidence; generation readiness does not
govern ordinary retirement or unrelated settings edits. Other protected driver,
identity and database keys cannot be retired this way. Replacement, runtime sync,
product-config and artifact sync use the same check. Missing ownership or consumer
authority refuses the exception, and production retains strict prefix protection.

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

### Sanitize retained testing override intent

An unfinished fresh generation may retain integration overrides from its previous
lane. `POST /v1/products/{product}/testing-override-sanitization` is the
Director-agent record-only continuation. It reuses the candidate neutralizer:
remove integration/analytics parameters and secret-backed override references,
remove Shopify settings, preserve unrelated literal settings and website intent,
and retain the current testing origin and connection fences. It changes no
credential, provider environment, allocation, source capture, copy or queue.
The ordinary fresh-restore intent guard remains enforced by replacement.

Use the bounded client from the exact source that supplies the service route:

```bash
uv run python scripts/sanitize-testing-overrides.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --context <context> --operation-id <original-preparation> \
  --reason <unchanged-reason> --evidence-file <private-review-file>
```

Review the removed setting/key names in the private evidence file. Run
`apply` with the same arguments, `--reviewed-dry-run` and the original stable
sanitization key in `--idempotency-key`, then `readback` with the same review
file. The key belongs to this record operation; retain the original resource,
bootstrap and copy keys independently. The service refuses a different or used,
held, completed or unqualified generation, an active executor, stale authority,
and a changed reviewed record. Apply requires PostgreSQL fencing. Override,
private prior/replacement snapshots and the public idempotency receipt commit
atomically in existing storage. Prior values and binding references stay inside
the service; responses contain names and digests only. Readback checks the same
generation and current override intent while tolerating ordinary apply observations.

After an uncertain response, preserve the review and original key, inspect
readback and replay only that key through the service. Never bootstrap, restore,
allocate, use a direct DB/provider write or invent a credential to repair this
record operation. A successful sanitization receipt does not prove copy
completion, runtime identity, public health or TLS; those remain their own gates.

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
claim. Source reads are complete DB queries, without a history limit. Every
undated, malformed, offset or payload-mismatched timestamp still reaches the
full rule. A deployment or check whose indexed time and payload time are the same
canonical UTC text, from a calendar date the filter can verify, in a whole second
before preparation, cannot defeat the proof. The service still validates that
record's whole payload; it skips revalidation only when the same process has
already validated the identical payload, matched by SHA-256 digest. An
invalid payload anywhere in the history still leaves the read UNKNOWN. A long
history therefore costs a per-row timestamp and digest pass in the database under
the lock once the collection read has validated it, not a decoded record each; a
fresh process pays full validation once. Pure collection
reads do not hold the global authority write lock; revoked generations return
early. Publication and consumption use their existing locked transaction. The proof
binds to the generation rather than a growing list of failed checks. Failed checks
against an earlier route do not count toward coverage. The predicate is rechecked
under the shared authority lock before publication and again when
consumed, including through the supersession snapshot adapter. Dispatch/hold
revocation removes an already-published never-served usage witness; a delayed
collector cannot republish it. An observation with needed-copy, shared-resource
or separation-staging protection retains those facts, binding and expiry with
usage set to UNKNOWN, so revocation cannot erase independent protection. Data custody, physical
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
An unavailable original login database or failed history query leaves occupancy
UNKNOWN after the independent resource/copy inspection succeeds. It never
becomes non-use. The service writes the scoped activation receipt under its
activation lock, and continuation checks its worker attempt and current generation
binding. The existing verified-down rule still qualifies historical
positive use; new positive use and a current hold retain their checks. A provider
authorization denial remains a denial and escapes admission without another read
or identity. The scheduler records `authorization_denied` against the original
admission revision, with no queued operation, so an unchanged poll cannot repeat
that denied read. A failed physical/copy inspection supplies no resource proof.
The fixed host program reports actual Docker-daemon authority refusals as denied
in its inspection modes; this does not describe storage maintenance's per-copy
deferral handling. A command failing inside the container, including a local file
permission or database-password failure, remains a non-denial: a failed operation,
or UNKNOWN occupancy for the login-history read after resource checks pass. It
does not claim that the provider denied its identity.
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
removed before the first registry process. The immediate completed predecessor,
initial bootstrap database and current selection are retained. Disposal runs
before a later drill selects its new database, independently of that new copy's
health. Up to four outstanding slot receipts are reconciled per worker pass,
oldest first, against fresh authoritative unused/reproducible/isolated facts,
no hold, unchanged resource allocation and actual container/backing-consumer
inspection. The same host lock excludes restore and cleanup processes. A copy
must have its exact committed database witness; DROP uses neither FORCE nor
connection termination. A connected database or failed filestore removal stays
deferred. No receipt or failed raw-copy evidence is erased.
Copies from different resource generations, protected current/predecessor copies,
and legacy absent databases with a remaining filestore but no host write-ahead
receipt become retained attention. They are never deleted, remain queryable and
alert through the existing policy, but do not permanently block the new lane.
An outstanding retriable disposal prevents the next capture from starting;
the last healthy copy keeps serving. Later scheduled attempts reconcile the
same original identities instead of adding another copy to a growing backlog.
Successful bounded batches with more work requeue the same slot for another worker
pass before capture, without a cleanup-failure alert or waiting for tomorrow.
Completed batch snapshots remain immutable when the latest intent is reused.

Before DROP, the host fsyncs a write-ahead receipt in its account's private
`~/.local/state/launchplane-testing-disposal/<namespace>/` directory, outside
PGDATA and container mounts, binding the original database and witness. If DROP succeeds but filestore
removal or transport fails, the original-key retry reacquires the host lock and
uses that receipt. An absent database with a remaining filestore and no receipt
is retained without deleting files; absence of both needs no deletion. Foreign witnesses, symlinks and
current/immediate-predecessor identities are refused. Directory-relative,
no-follow opens protect receipt creation/readback, and actual mount inspection
refuses any writable container access to the host state directory. Historical slot receipts
remain queryable after cleanup. Legacy `testing_drill_disposal_deferred` log
markers are consumed into service evidence, but new copies no longer perform
inline best-effort deletion after web startup. Activated failed/cancelled slots
stay in the ledger too; success is not the source of disposal custody.

Each testing lane's `odoo_testing_drill_storage` record supplies positive integer
`database_bytes`, `filestore_bytes`, `staging_bytes` and `reserve_bytes`.
`staging_bytes` covers the complete working staging peak: SSH host staging must
include both dump and rsynced filestore; the runner copy holds the dump. Native
staging must include chunks and assembled artifacts. Already verified staged
native bytes are deducted at the later host check, while space for the immutable
capture's assembly remains required. Set these
working-headroom requirements through the existing authenticated product-profile
write path, preserving the rest of the current profile. There are no invented
real sizes, default budgets, new grants or assumed observation lifetimes.
Unconfigured requirements refuse a drill before database selection. Choose the
requirements from qualified source size and restore/staging overhead; they are
an explicit capacity requirement, **not a quota or a claim about future source
growth**. A budget is not established by a healthy deployment or yesterday's
free-space observation.

A capacity-only pass sends no deletion candidates and requires no general
positive custody observation. Recorded holds, use, only-copy or shared-resource
protection still wins, including expired protection. Receiver inspection proves
actual resource isolation and the committed current-copy witness. This read
does not authorize disposal: any old-copy deletion still requires fresh general
custody facts for its original receipt; drill-scoped successful-login evidence
is not silently generalized to other copies.

On every new worker attempt, the shared resource guard reads current filesystem
capacity, using non-root available bytes and the actual backing device. Demands
for DB, filestore and capture staging on the same filesystem are summed before
adding its reserve. SSH capture accounts for both host staging and the runner's
simultaneous dump copy. Native staging requires a known runner writable-layer
backing; unsupported storage layouts stay unverified. Cleanup can reclaim old
copies even when that pass then refuses capacity. Capacity evidence is bound to
the exact generation, authority and worker attempt before activation, and the
host repeats the free-space check before touching the web gate or capturing.
A witnessed existing-copy resume does not demand another complete copy or staging
allocation. Holds and the existing destructive authorization checkpoint are
rechecked under the lane/authority locks immediately before sending the remote
program. Locks are released for the remote wait, then results are recorded under
locks; changed authority preserves completed cleanup evidence while refusing
activation from that capacity snapshot.
Insufficient or unavailable capacity alerts through the existing Discord policy
and preserves the selected generation and its usable predecessor. This is a
current admission check, not a filesystem reservation: unrelated writers or a
source larger than the recorded working requirements can still consume space.
A failed copy never deletes its predecessor. Final web and health verification
still gates a passing operation and generation completion.

`GET /v1/products/{product}/environments/testing` includes value-free
`testing_rebuild_drill` evidence: queued/pass/fail/skipped, observation time,
operation/deployment IDs, failing step, trace, alert delivery status, pending
disposal count, up to four original pending discard-copy bindings,
retained-attention count and capacity status. A pass
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

Once a profile or generation contains a drill storage budget, use this image or a
compatible successor for recovery. Older images cannot interpret those fields;
forward-fix rather than removing custody evidence to roll back. Disabling the
existing drill flag prevents new scheduled captures and changes no retained copy.

For a deliberately retained copy, the existing authenticated profile path accepts
`odoo_testing_drill_storage.retained_copy_bindings`, a list of exact original
committed discard-copy bindings exposed as `disposal_pending_bindings` in that
existing environment evidence. The bounded service
pass records these as retained attention without deleting data, erasing receipts,
changing identity or granting access. This is the supported continuation for a
copy that must stay connected or cannot be removed: it prevents an indefinite
retriable-cleanup block while preserving its storage/custody. Capacity and fresh
lane safety still gate each new capture. It is not a recurring manual usage
approval or permission to delete a retained copy. Budget or retention edits made
before activation are accepted only after fresh capacity qualification and only
while every other source/copy identity remains unchanged. Once selected, recovery
keeps the original generation and its budget.

The guarded terminal program send has its own bounded deadline; expiry shuts
down that connection and retains original-key uncertainty. Alert delivery writes
only delivery fields for its original failure, and cannot overwrite worker
storage/disposal evidence. A hold or positive use report arriving while a completed
cleanup result is in flight preserves that result but skips the new activation.

## Initial fresh Odoo testing generation

An existing Odoo testing lane with no current fresh generation and no active or
stranded lane operation uses the same resource and bootstrap producers with
`initial_generation: true`. This is a separate preparation identity, not a
replacement operation or a historical supersession. Completed operations,
journals, old storage, target, hostname and credentials stay intact. It never
qualifies the current database as a fresh copy.

Start with the resource client, omitting `--operation-id`:

```bash
uv run python scripts/prepare-testing-replacement-resources.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --context <testing-context> --initial-generation \
  --source-mode <ssh-or-container-export> \
  --reason '<reason>' --idempotency-key <resource-key> \
  --evidence-file <private-resource-review>
```

The service derives the preparation ID from the exact product, context,
authenticated principal and original stable key; the receipt returns it as
`operation_id`. Preserve that ID and key. Repeating the same dry run returns the
same preparation while its recorded snapshot is unchanged; a changed intent or
configuration requires a new key and new review. A refused, unactivated plan
reserves neither an executor nor its original hostnames. Planning writes only
its own custody/allocation evidence and reserves no executor. Before review it
checks the existing bootstrap restore policy, distinct managed credentials and
selected production source, then current original-lane usage through the existing
read-only login/session probe or authoritative downtime rule. Missing evidence stays unknown. Holds,
known use, production aliases and an existing generation or active operation
refuse. Original copy custody is preserved by construction, not declared safe.

An initial resource dry-run refusal identifies its first failed checkpoint:
`testing_initial_preparation_unavailable` covers the original intent, lane,
ownership, generation and operation admission (original intent/key validation
also returns this code in apply, readback and release);
`testing_initial_copy_prerequisite_unverified` covers copy prerequisites outside
the classified bootstrap checks; and `testing_initial_usage_unverified` covers
the original-lane usage decision. Classified bootstrap credential and restore
source failures retain their existing fixed codes, such as
`testing_credentials_unverified` and `testing_restore_source_unconfigured`.
These codes include unreadable dependencies and do not establish a particular
bad record or authorize changing it. Read current supported metadata and usage
evidence for that checkpoint; preserve unknowns and the original intent. Provider
authorization denial remains `authorization_denied`. No private exception text
or provider payload is exposed. Older service images return the grouped
`testing_resources_refused`; an old trace cannot identify its failed checkpoint.

Review the receipt, then repeat with `apply --reviewed-dry-run` and exactly the
same arguments/key/evidence file. Separately run `readback` with the same key.
Only reviewed apply establishes the initial preparation's durable lane fence;
competing synchronous and durable operations cannot start until its exact
bootstrap queue transaction commits. Unknown or partial effects retain that
fence and all resources. Recover through the original key and actual provider
readback; do not create a replacement key to escape an uncertain effect.
If apply refuses before any dispatch, its lane checks now precede activation.
For an interrupted no-effect attempt that already took the fence, run the same
resource client with `release --reviewed-dry-run`, the original resource key,
intent and private review file. The service excludes its executor, independently
reads that its named provider placement is absent, and atomically verifies no
dispatch checkpoint, captured target, resource receipt or generation exists.
It atomically records terminal release for both the lane fence and original
provider reservation, without deleting custody or rewriting the original request.
Repeated release is idempotent; that released intent can never apply. The client
reports `released` explicitly if the original apply is replayed after release.
The lane can continue normally, or a new initial intent can be reviewed with a
new key. Any partial placement, recorded dispatch or unknown provider read keeps
the original fence; use original-key reconciliation, never release to bypass it.

Configuration drift before adoption refuses the reviewed plan. Each bounded
provider effect and final publication rechecks current identity, holds, use and
aliases. Independent resource readback is the same as stranded recovery.

Next use `scripts/prepare-testing-replacement-bootstrap.py` with
`--initial-generation --operation-id <returned-preparation-id>`, the same product
and context, a new unbound hostname, and the same reviewed `--source-mode` (`ssh` or
`container-export`) for the recorded same-product production source. Run its
`dry-run --evidence-file <private-bootstrap-review>`, review, then
`apply --reviewed-dry-run --idempotency-key <bootstrap-key>` and separate
`readback`. Keep the complete original bootstrap intent and key for recovery.
The existing testing-only upstream-restore policy and distinct managed testing
credentials are prerequisites; this path creates no credential or grant.

Bootstrap uses the existing isolated renderer and sanitized production-copy
worker: live keys and outbound integrations are removed before any copied Odoo
registry, module or web code runs. It commits only a generation and queue
receipt, never copy completion. Its atomic queue releases the preparation fence
without modifying any historical operation. Genuine copy completion, physical
isolation, runtime identity, integration and usage checks remain required before
walkthrough provisioning. Read back that completion through the supported lane
and walkthrough paths after the worker finishes. A ready source PR does not
establish those runtime facts.

Testing reconciliation follows the original artifact attempt chain after bootstrap.
Terminal failures from operations created before this qualified generation was
prepared remain in history but do not consume its retry budget. Operations
created at or after preparation keep the ordinary retry limit. After correcting
a prerequisite, use the [reviewed exact-artifact retry](testing-deploy-retry.md)
to give this exact qualified generation another bounded budget. It preserves
the original upstream-restore mode and custody; a predecessor or existing-data
retry receipt cannot apply to this generation. An unresolved earlier operation
still prevents a new attempt; no key, fence or effect is cleared.

Keep the service image that understands initial preparation fences active while
one is open; forward-fix an interrupted preparation. An older image cannot
interpret its exclusion contract. Never roll back storage or erase custody to
clear a fence. The stranded-operation procedure above retains its original
admission and supersession meaning.

## Standalone Odoo upgrade rehearsal

`POST /v1/products/{product}/odoo-upgrade-rehearsal` and the repository client
`scripts/odoo-upgrade-rehearsal.py` operate a separately identified rehearsal.
This is not an initial testing generation. It has its own record, provider
placement, private Docker network and storage; it writes no product profile,
canonical target, runtime environment, production release or testing binding.
Its runtime instance is `rehearsal`. The tenant's testing-only migration hook is
not enabled by changing that name.

The implementation reuses the disposable volume producer and actual backing
checks, uniquely named placement discovery, the durable provider-operation
journal, disabled manual schedules, the standard isolated Odoo Compose renderer,
SQL-before-registry copy runner, connection isolation, integration readback and
storage headroom checks. The existing local-password primitive supplies only the
new rehearsal's own scope; no existing password or integration key is copied or
rotated. It creates no credential that reaches a live system and grants nobody
access. No workflow or alternate identity substitutes for a denied service call.
The source and target copies use the same SQL boundary independently, with a
sanitized source-version snapshot preserved before the target registry runs.

The necessary new boundary is one additive rehearsal record, not another lane or
release controller. Its compose is reserved under the new context in the existing
physical-target registry, which prevents canonical lane aliases. The service derives its `lp-rehearsal-…` identity from the
product, authenticated principal and original prepare key. Sizing comes from the
product's production records; application, database and runner topology comes
from the common Odoo renderer. Only explicit sizing keys are inherited. Routing,
volumes, credentials, database names, cron and connections are isolated.

Before planning, use the read-only
`GET /v1/products/{product}/odoo-upgrade-rehearsal/preflight?source_context=…`
through the service's existing registered testing reader:

```bash
uv run python scripts/odoo-upgrade-rehearsal.py preflight \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --source-context <source-reader-context> \
  --evidence-file <private-preflight-evidence.json>
```

No rehearsal identity, intent or operation key is needed. The optional private
evidence file is created exclusively. The response includes a trace, observation
time, registered reader placement and opaque host identity, source database size
from `pg_database_size`, actual source Odoo version, apparent and allocated
filestore bytes, and opaque backing groups with non-root available bytes.
Source reads reuse capture's existing SSH identity and strict known-host trust.
This requires no workstation-to-host SSH login. It creates no rehearsal record,
password, capture, provider schedule, application or host-custody directory.
Existing `operations.read` authority covers it; a refusal never selects another
identity or transport.

Before prepare, the rehearsal's volumes and writable layers do not exist. The
preflight therefore labels its backing basis `allocation-parent`: existing
Docker local-volume and overlay2 directories, and the nearest existing custody
ancestor on the recorded reader host. Grouping uses their actual `st_dev` and
minimum available bytes per device. Unsupported or unavailable backing refuses;
it never guesses the host root filesystem. This observation reserves no space
and certifies no future runtime or budget. Each allocating phase still checks
the actual rehearsal mountpoints and writable layers. Source measurements alone
do not budget archive overhead, block allocation, database growth or migration
work; choose reviewed headroom from fresh evidence.

Then supply a private intent file outside the checkout. It contains
`source_context` (the product's existing testing source-reader context),
`source_odoo_version`, `target_artifact_id`, `storage` and `reason`. `storage` uses
`database_bytes`, `filestore_bytes`, `staging_bytes` and `reserve_bytes`. The
first two bound each streamed capture as well as supplying working headroom.
Budget four database/filestore working copies and all three immutable recovery
snapshots over the lifecycle. Before each allocating action, the guard charges
only that action's new allocations against current free space. Existing copies
already consume space and are never charged a second time. With `D` and `F` as
the reviewed per-copy bounds, the conservative charges are:

| Action | DB volume | Data volume | Runner writable layer | DB writable layer | Host custody |
| --- | --- | --- | --- | --- | --- |
| `execute` | `2D` | `2F` | `D` | `0` | `2D + 3F + staging_bytes` |
| `simulate-final` | `2D` | `2F` | `0` | `D` | `D + 2F + staging_bytes` |
| `verify` | `0` | `0` | `0` | `0` | `D + F + staging_bytes` |

Host custody includes the new recovery snapshot and, where applicable, the raw
dump, archive and extracted filestore. Runner scratch holds only the initial
sanitizer dump; final-source restore puts its temporary dump in the database
container instead. `staging_bytes` is additional host headroom for archive and
allocation overhead. Choose working bounds that also cover restored/upgraded
database growth and migration/WAL requirements; a measured dump size alone does
not bound restored size. Each action rechecks actual volume mountpoints,
writable-layer backing and custody using `st_dev` and non-root available bytes,
with `reserve_bytes` added once per backing device. Unknown backing fails closed.
Known paths with zero new allocation are validated but do not consume a reserve.
These checks do not reserve space or impose filesystem quotas. All four working
copies and every recovery tuple remain preserved.

A failed read-only action precheck returns a determinate HTTP 409 with
`rehearsal_storage_capacity_insufficient` or `rehearsal_precheck_refused`.
Its empty, atomically created refusal marker binds the exact operation and
record and proves no rehearsal action started, even if the process stops before
replying. The same key returns that refusal. After correcting and requalifying
the prerequisite, review the unchanged action and use a new operation key. A lost response is still
reconciled by repeating the original apply request and key first; ordinary
readback does not identify an operation's refusal. Once an action's start marker
exists, its uncertain-effect recovery rules remain unchanged.

The candidate artifact must already be registered, immutable, attributable to
this product's repository, and declare its Odoo version and exact module intent.
Its version must be newer than `source_odoo_version`; the restored database's
actual base-module version must agree with that declared source version. The
artifact must declare the supported source versions as a comma-separated
`build_flags.values.sql_copy_source_versions` value. Missing source-major support
refuses before a source read; this declaration must come from the artifact's
qualification, not an agent inventing compatibility metadata.
The existing SQL-only sanitizer runs on the old schema without loading a candidate
registry. It preserves the sanitized old-version recovery tuple before any
upgrade. No separate source-version application image is required.

The candidate needs the native SQL-before-registry credential boundary,
OpenUpgrade assets and module-update witnesses. Preparing the independent runtime
checks its runner before any business source is read. Metadata planning alone
does not qualify an image's executable behavior. Unsupported native contracts
fail closed; before-registry and before-web readback remain mandatory on the
actual copied data.

The source reader must already have its service-recorded `ODOO_UPSTREAM_*` and
`OPENUPGRADE_*` settings, existing host SSH identity, known-host trust and source
read access. No command installs a key, changes source trust, grants access,
freezes the business host or edits its database. Placement uses the testing
source reader's already registered remote server, read-only. Only sizing comes
from production. Local/unregistered placement
is refused. A missing record or unavailable prerequisite is not authorization to
infer coordinates from an issue or workstation file.

Use the installed `launchplane-write-action.py` helper as the authenticated, redacted transport:

```bash
uv run python scripts/odoo-upgrade-rehearsal.py dry-run \
  --operator-helper <installed-launchplane-skill>/scripts/launchplane-write-action.py \
  --product <product> --payload-file <private-intent.json> \
  --idempotency-key <original-prepare-key> --evidence-file <private-prepare-review>
```

Review its result, then repeat with `apply --reviewed-dry-run`, exactly the same
intent, key and evidence file. Metadata planning writes its own rehearsal record;
it does not perform the source/backing preflight above.
Apply is restricted to the authenticated Director agent and PostgreSQL-backed
execution. Existing read authority covers planning and readback. It is a source
capability until the completing PR is deployed; a missing route is unavailable,
not a reason to invoke a local live-target command.

For a held initial execute that never published its recovery copies, use
`action: retire-incomplete` with the **unchanged original execute intent and
execute key**. Take it through the same client dry-run, saved review, apply and
readback. It accepts only `prepared` with no copy receipts and an exact original
`reconcile_required` execute checkpoint. Its native scope covers both the
source-selected runtime before the switch and an idle upgrade-selected runtime
after the initial switch. The latter requires a verified initial sanitized
source snapshot, with exact witness, capture lineage and database/filestore
hashes. The review binds that receipt into its native disposal manifest;
apply and readback retain it as `host_disposal.initial_source_snapshot` after
disposal. It is initial input evidence, never a final-source, upgraded or
post-write recovery tuple. The web writer must be stopped. Later-phase incomplete
runtimes remain held for their qualified disposition. Verified, running,
unbound or production-aliased operations refuse. Ordinary `retire` still requires both
verified final recovery tuples; incomplete retirement produces neither tuple.

The dry-run acquires executor exclusion and inspects the exact private placement,
containers, private network, labeled local volumes and backing identities. It
checks that no container can see raw host custody and that the rehearsal web
writer is stopped. A provider schedule reporting success without a valid,
operation-bound host result is a refusal, not a qualified plan. It preserves
the original hold and custody. Apply rechecks the reviewed binding, acquires
the existing host lock, and records a private disposal manifest before
stopping/removing only those containers, volumes and network. It independently
confirms all three resource classes absent before deleting raw capture and
partial host storage, including the verified initial source snapshot. Unknown
or failed absence checks preserve that host custody for same-key continuation.
It retains only disposal/refusal
receipts and the unlocked lock file. It removes
the isolated provider compose through the existing compose retirement path,
then its empty project. No source read, source mutation, credential creation,
public-route operation or grant is involved.

After native absence checks and placement absence, one DB transaction marks the
rehearsal retired, removes its physical target registration and completes the
original held execute with an explicit incomplete-retirement result. This frees
its operation hold without advertising a successful execute. The retirement's
own reservation uses a separate scope under the same principal and original
key, so it cannot displace the held execute before disposal. A typed checkpoint
binds the reviewed plan and native manifest before deletion; completed host
disposal is persisted before provider placement removal. This checkpoint is
excluded from the original execute binding and does not produce recovery
copies. Legacy records without it retain their original binding. Use a
compatible service to finish an admitted retirement before rolling back to a
version that cannot read that checkpoint.

Completed same-key apply replay returns its stored result; readback validates
the shared typed receipt and provider placement absence. Interrupted calls keep
both journals held. An explicit same-key apply can resume only after read-only
qualification of the bound host manifest or persisted host disposal under the
executor lock; it skips the removed host runtime once disposal is proved.
Unavailable or running resources stay held. Changed inventory refuses before
deletion and records an attempt-bound, review-specific native refusal under the
host lock. If dispatch failed before that fence ran, dry-run reports
`review_refusal_required`: retry the saved old review once to establish its
no-effect refusal. Then obtain and review a fresh dry-run and apply with the
same original execute key. The retirement's stable request fingerprint binds
the original intent; its checkpoint and native admission enforce each review.
A replacement review requires a durable refusal with no disposal manifest or
persisted host disposal. Refused reviews stay refused, including delayed host
scripts. Replacement plan hashes include the refusal lineage, so restored
inventory cannot revive an old admission. Once disposal starts, only its bound
manifest can resume. Planning reports native absence and remaining work without
claiming preserved resources. Completed readback uses the persisted host receipt
and current placement absence; it does not dispatch a new host inspection after
the placement has been removed. The client rejects a
retirement result when an old execute request is replayed, so cancellation
cannot be reported as a successful execute. A failed readback never authorizes
a new capture.

Retirement does not rerun the failed upgrade or reconstruct missing diagnostics.
If a historical failure discarded the child error or exit status, they remain
unknown. A later fresh rehearsal records its own error through the current
diagnostic path; retirement itself supplies no diagnosis or upgrade proof.

The existing disabled manual server schedule and purpose-local managed-secret
records remain as audit/configuration metadata with no runtime consumer. The
retirement schedule carries no source configuration or local passwords and is
not enabled for recurring execution. This action changes neither credential
bindings nor access.

After prepare, keep the returned `rehearsal_id` in the private intent and take
each next `action` through its own dry-run/review/apply with a distinct stable
key and new evidence file:

1. `execute` captures the external source through fixed read-only SSH commands.
   Database and filestore streams are bounded. Source-copy lineage is hashed,
   referenced attachments are checked against the restored database, and raw
   failure material stays outside all container mounts. The SQL-only
   source phase sanitizes with no registry execution. The service preserves that
   source recovery tuple, clones it, and runs OpenUpgrade under the target
   artifact. Strict connection readback gates private web startup.
2. `simulate-final` uses the already sanitized source copy. It simulates the
   final capture and switches only the rehearsal web's database to the final
   upgraded copy. It never recaptures or fences the real business system.
3. `verify` probes health identity and the login page inside the private
   network, stops the disposable web writer, and preserves the final upgraded
   database/filestore tuple. Product business validation remains part of the
   consuming rehearsal; health/login proof is not Client acceptance.
4. `retire` stops web, runner and database and preserves all recovery custody.
   It requires both final source and upgraded tuples. It does not delete
   volumes, provider placement, snapshots or keys. Their custodian carries them
   into the rollback rehearsal before a separately reviewed physical disposal.

Use `readback` with the original intent/action to inspect its completed effect.
Retirement readback verifies stopped containers and immutable files without
starting the database. Snapshot hashes, source binding, copy witnesses,
artifact identities and elapsed times are value-free receipts. Provider ids,
source coordinates and local passwords never reach the client's output.

An uncertain effect stays fenced under its original key. Readback is not replay;
repeat the original apply only for a supported original-operation observation,
never with a new key. An incomplete provider create or partial host operation
keeps its namespace, phase journal, raw failure custody and storage for diagnosis.
The route does not guess that absence or a timeout permits another create,
restore, capture or deletion. Fixed prerequisite codes and the trace identify
unavailable source, artifact, placement, credentials, state or recovery evidence;
no provider exception text is returned.
For an incomplete action, original-key readback also returns the operation's
state, effect phase and checkpoint time. An uncertain effect remains
`hold_unknown`; a matching completed prerequisite refusal is reported as
`completed_refusal`. These observations do not authorize another apply. Value-free `custody` reports
whether an effect marker, capture receipt, pending capture receipt, extracted
filestore directory and source snapshot directory exist, plus raw dump/archive
byte counts. Presence and size do not prove a complete or usable copy. Readback
holds the existing host execution lock while inspecting custody and reports a
fixed busy observation if a host process still owns it. It creates no replacement lock.
For a bound, checkpointed original execute with a retained raw capture and no
source snapshot, readback may also return `capture_attachment_validation`.
New capture executions retain `execute.attachment-mismatch.json` when attachment
validation rejects a size or checksum mismatch. This mode-0600, content-free
receipt records `store_fname`, attachment `name`, nullable `res_model`, `res_id`
and `mimetype`, and the database/file sizes and SHA-1 checksums. The validator
selects this metadata in its existing attachment cursor and hashes the same
open file that failed; it adds no scan or related-record lookup. The original
size/hash refusal still stops capture. Successful validation creates no mismatch
receipt and does not change the capture's success result.
Long name/model/mimetype values retain a prefix and are explicitly listed in
`truncated_fields`; the store path, related record ID and compared facts stay
exact. If a size failure's checksum cannot be measured, `file_checksum` is null
and `file_status` says `unreadable` or `changed`; that does not erase the observed
size mismatch or its identity. Stable measurements use `file_status: measured`.

The receipt binds to the rehearsal intent, full record binding and provider
operation attempt. Checkpointed original-key execute readback returns it as
`attachment_mismatch` under the existing execution lock, exporting these allowed
metadata fields in the authenticated HTTP response and supported CLI output,
without a runtime,
archive or database scan. Changed bindings, malformed metadata, content fields
and unsafe receipt custody refuse readback. It identifies the attachment this
operation rejected, not a unique conflict across the database or today's
production state, and does not authorize recovery or accepting inconsistent
Client data. No new grant is required. Older held operations have no such
receipt; their historical failure identity cannot be reconstructed by this fix.

For historical executions without that saved evidence, the existing readback
probe below remains available:

Under that existing host lock, the verified isolated runtime must have stopped
web before the probe reads the restored source database and copied filestore.
The probe uses the same emitted validator as capture, an explicitly read-only
database session, connection/statement timeouts and a bounded streaming scan.
Its fixed code distinguishes runtime/login/query availability, invalid metadata,
unsafe or missing files, size/hash mismatches, verification and an incomplete
scan. It returns no attachment values or exception text and writes no host
receipts, markers or Python bytecode. `verified` applies only to this attachment
scan; it proves neither the raw capture hashes nor completed sanitization or
recovery eligibility. Every probe result retains `hold_unknown` and the original
key. It never restores, recaptures, sanitizes, starts a runtime or resumes execute.
Read the code together with the original failure locations: capture receipt
presence alone does not establish that restore or sanitization finished. The
probe checks metadata, file presence and sizes before hashing, within one
read-only repeatable-read snapshot and the same bounded budget. A failed Docker
command reports `runtime_unavailable` while retaining the custody observation;
isolation contradictions and malformed observation output still refuse.
The observation allows up to ten minutes of scanning within the existing host
job limit, so a large retained filestore can reach its content checks. It holds
the existing execution lock for that scan; an incomplete scan still grants no
continuation and starts from the beginning on a later observation.
When the first size mismatch is found, readback hashes that same open regular
file within the remaining budget. `attachment_content_matches_checksum_size_mismatch`
means its bytes match the database checksum despite different `file_size`
metadata; `attachment_size_and_hash_mismatch` means both disagree. Neither code
proves the retained archive, other attachments, sanitization or recovery
eligibility. That legacy probe exports no attachment path, checksum or size. The file must
remain stable while hashed. Capture/apply still refuses a size mismatch;
measuring the rejected file for its evidence does not relax that gate or authorize a
metadata rewrite. A timeout retains `incomplete` and the original hold.
If both size and content disagree, the same bound observation may compare that
exact row/file with the retained original tar. That legacy probe's canonical reference stays
inside the runner-to-host pipe and host memory; public output contains only a
fixed code. Under the existing host lock, the probe reads a bounded regular
`capture.json`, checks the original source binding, then streams the retained
regular archive against the recorded digest. The standard tar reader visits
bounded headers and reads the one matching member without filesystem extraction.
It rejects unsafe/duplicate matching members and trailing archive data, keeps
metadata memory bounded, and requires stable custody before publishing a comparison.
The archive budget scales with its actual byte count for a full digest and
member read, capped by a server-generated fraction of the existing native job
limit. It leaves room for the copied-file scan and host overhead.
Unsupported, changed, unavailable or untrusted evidence returns
`capture_comparison_unavailable`; an expired archive budget returns
`capture_comparison_incomplete`, preserving the established size/hash disagreement.

`capture_attachment_matches_reference_copy_differs` means the captured member
matches both database references while the inspected runner copy differs.
`capture_attachment_checksum_matches_size_metadata_differs` means the captured
content matches the checksum but its size also conflicts with the metadata.
`capture_attachment_matches_copy_reference_differs` means the captured member
and inspected copy agree with each other while differing from the database
reference. `capture_attachment_differs_from_copy_and_reference` records a third
set of bytes; `capture_attachment_not_present` means no matching regular member
was found in the verified archive. These are observations of one row and the
retained archive, not proof about the current business source, other attachment
rows, the raw database dump, sanitization or recovery eligibility. They retain
`hold_unknown` and never recopy, rewrite, recapture, restore or resume execute.
When the captured member and copied file agree but conflict with the observed
reference, the same original-bound readback may additionally report
`capture_database_reference_validation`. It first verifies the retained dump's
recorded digest and stable regular-file custody. The existing database
container's `pg_restore` decodes only public `ir_attachment` data to a pipe,
without a database connection, SQL execution, output file or dump copy. A
bounded parser compares the canonical path, checksum and size tuple. It exports
no table rows or private values and leaves the archive classification intact.
`reference_present` proves that the conflicting tuple also occurs in the
captured database. Together with the verified archive comparison, this proves
an inconsistency within those captured inputs, without diagnosing current
production data or identifying which attachment row originally failed.
`reference_not_found` means that exact tuple was absent from the completely
decoded selected table; it does not establish a sanitizer defect.
Unsupported, incomplete, changed or unavailable input returns `unavailable`;
the ten-minute dump/hash/decode budget returns `incomplete` on expiry. Decoded
output is bounded by the reviewed database byte budget. The parser compares
only the three required fields incrementally and discards other content, so a
large `index_content` or `db_datas` value does not require buffering its row.
SQL metadata is bounded separately. The decoder has a native timeout and its local process
and pipes are closed on every exit. These codes keep the original journal and
`hold_unknown`; they authorize neither data repair nor resumed execution.
`failure_events_after_checkpoint` projects bounded provider event identities,
times, fixed provider statuses, traceback presence, host function names and
native traceback line numbers from
this namespace's logs within the provider's existing checkpoint clock-skew
window within the provider-returned list. Provider retention can already have
removed older candidates. Locations refer to the exact emitted program of the
original effect, including its transport prefix and embedded libraries. Rebuild
that deployed program before mapping a location to code; repository file lines
and a newer program are not equivalent. Missing or bounded evidence is explicitly unavailable. An unavailable event log
retains its event metadata with `log_available: false`; the oldest candidates
are read first. If host observation is unavailable, journal and event evidence
survive with `host_observation_unavailable`, and unobserved custody/copy values
are omitted. `operation_journal_status` distinguishes bound, missing, unbound
and unrequested journals; no checkpoint is never proof that no effect happened.
Events are read
before launching the observation schedule. They are diagnostic candidates, not proof
that an event belongs to the original apply. The event projection withholds
provider prose. A failed rehearsal child preserves `child_failure` in private host
custody: the failing step, nonzero exit status and bounded redacted child output.
Only the failure escaping the effect is saved; a recovered startup error cannot
replace it. Original-key readback checks its intent, attempt and record
binding before returning it. Arguments, input and environment are never exported.
The native execution lock keeps observation separate from a running effect.
For an older initial-upgrade failure with an independently verified source
snapshot, readback can also return `retained_upgrade_logs`: bounded redacted
tails from the rehearsal's exclusive log volume and container logs, with source
metadata. It reads regular log files only and executes no upgrade, registry or SQL.
The combined result uses the native transport's shared byte budget, shedding
oldest optional log tails before child failure or custody evidence. Partial logs
are marked truncated. If diagnostic storage is full or its interrupted pending
receipt remains, a bounded operation-bound fallback stays in the existing job
log; `failure_events_after_checkpoint` returns it only for the exact original
intent, attempt and record. It is diagnostic evidence, never custody proof.
That provider job log is outside private host-file custody; the marker is
encoding, not encryption, and retains the same private-data handling rule.
An unavailable log observation leaves child failure and custody readable and sets
`retained_upgrade_logs_unavailable`. Credential redaction and byte limits apply
before transport. These diagnostic fields carry `diagnostics_private: true`:
they may contain Client record names or exception context. Keep full readbacks
in private evidence; post only the necessary reviewed error, without Client row
values. Common PostgreSQL DETAIL/STATEMENT lines are filtered; other SQL or
Client values may remain, so the private-evidence rule still applies.
An older discarded child exit status cannot be reconstructed from a log tail;
neither a log nor a child diagnostic proves successful migration or authorizes
continuation, replay, retirement or a replacement key.
Preserve the original request, key and all partial custody until a
supported continuation is established. Any failed readback preserves a supplied
original key, including authorization and transport refusals. A read failure
cannot establish release. Lock loss and integrity contradictions remain
refusals; a fresh pre-effect rejection is determinate only after its reservation
is released. A completed native receipt makes no claim that a journal bound to
a pre-publication record matches the published record.
Before a compose exists, prepare readback uses the original intent and key to
read its exact journal instead of trying to run a host schedule. It returns
`completed: false`, the operation state, effect phase and checkpoint time, and
either `continue_original_prepare` or `hold_unknown`. The read neither changes
custody nor completes the operation.
Its fixed `recovery_reason` distinguishes unavailable observation from
unqualified placement or source/credential prerequisites; provider authorization
denials retain their HTTP refusal and trace rather than becoming a hold.

`continue_original_prepare` is limited to an interrupted `project_id` effect:
the original reviewed intent, unchanged source/artifact/placement and existing
purpose-local credential bindings must agree, and complete provider inventory
must prove exactly one service-named project with its original description and
only its empty default environment. Under the original journal and reacquired
execution lease, the service rechecks that project, records its identity, and
creates only the remaining children and storage. It never repeats project
creation or regenerates the existing passwords. Both flat and nested provider
create receipts record parent custody before creating children.

After reviewing that readback, submit **once** with the original private prepare
intent, original saved dry-run evidence and original key through the same client
`apply --reviewed-dry-run` command, then read back. Preserve the original review;
a fresh plan does not replace the interrupted operation's fingerprint. Missing,
ambiguous, populated or changed projects, credential/source drift, later effect
phases and partial host work remain held for diagnosis. No new-key abandonment,
direct provider cleanup, source capture or later rehearsal action is implied.

Host schedules carry only the artifact identity fields the host consumes; the
full artifact and record binding remain in service custody. The reviewed host
program and its input use a compressed stdin wrapper, avoiding large Python
arguments and Dokploy's base64-wrapped remote shell materialization limit.
The shared schedule writer bounds the materialized command before provider
mutation. Rehearsal renders and validates the exact storage/apply script before
advancing its effect checkpoint, then executes that same string. Local size
rejection cannot advance the effect checkpoint. Oversized
scripts require compact delivery, rather than a provider row written before a
shell-size failure. Exact apply scripts include purpose-local values resolved
at execution, so this bound is enforced on the rendered script at that point.

If prepare reached its host checkpoint but never started there, original-key
readback can return `continue_original_prepare` with reason
`prepare_host_not_started`. This requires the native `prepare_not_started`
proof: no started marker, receipt path, raw source, copy receipts or project
containers, with fresh unchanged service inputs and credential bindings. An old
host response supplies no such proof. Continuation rechecks recorded placement
and existing storage through readback, without recreating placement, storage or
passwords. The host's execution lock and started marker still fence its first
effect; an already started or uncertain host action remains held. This rule is
limited to prepare, and never retries capture, migration or a later action.
These two supported prepare continuations explicitly preserve the preceding
effect phase and timestamp when atomically reacquiring their original lease.
A local refusal or storage readback failure during continuation therefore keeps
the original reservation and custody; it cannot release it as a fresh attempt.
Other provider-operation retries retain their existing default behavior.
Once placement exists, a readback without the matching original key still
returns the host observation and `original_key_required`; it cannot supply a
journal recovery disposition or authorize a retry.

Elapsed times describe this isolated simulation. Real source writer drain,
public DNS/TLS switching, business integration resumption and the actual
production downtime window remain unmeasured. The consuming product issue must
keep those limits with its rehearsal evidence. Neither a ready existing-data
production plan nor a successful rehearsal authorizes production promotion.

A partial operation's readback reports `completed: false`, any independently
verified snapshot receipts and whether raw source custody remains. It does not
start a runtime or clear the provider journal. Missing terminal evidence never
becomes a successful migration or retirement; keep the original operation key.
