---
title: Independent generic-web testing activation
---

`POST /v1/products/{product}/testing-activation` consumes the original caller's
completed independent-storage preparation receipt. The legacy Compose request creates a new empty
project, environment and Compose recipient on the original placement. It never
starts, updates, stops or deletes the old recipient, changes production, creates
credentials, settles historical deployment effects or deploys an application.

## Conventional application recipients

An explicit `application` specification on both resource preparation and
activation selects the conventional Dokploy application continuation. It keeps
the retained application, its unresolved execution and its data unchanged. The
service creates a fresh application ID rather than claiming the old commands,
maintenance or verifier have stopped. The strict provider/executor exclusion
read continues to report the unresolved original. Create-only admission uses
that exact held original's custody and execution lock; it does not weaken the
ordinary deploy fence.

Use `scripts/stage-testing-separation.py resources-dry-run` and
`resources-apply` with `--application-file <private-specification.json>` for
preparation. Put that identical object in the activation request's `application`
field, then use `scripts/activate-generic-web-testing.py --request-file ...`
through the installed admin transport. Keep both reviews and original keys.

The reviewed specification supplies the profile's actual `runtime_port` and
`health_path`, the existing provider network name and one explicit state shape:

- `none` allocates no volume or state service.
- `volume` allocates one qualified fresh local volume at the reviewed safe
  application data path.

Independent PostgreSQL recipient/bootstrap and resource-derived connection URLs
are deferred to [#4290](https://github.com/cbusillo/launchplane/issues/4290).
Those request variants refuse before provider effects; existing Odoo/Compose
PostgreSQL and control-plane PostgreSQL storage remain unchanged.

`local_credentials` names every existing delivered application-local binding
that must be replaced, with its required `urlsafe`, `base64` or `hex` encoding.
The reviewed request supplies generation authority for these local values; it
does not require the older profile generation flag. An applicable `owner_input`
requirement refuses local generation.

Prospective application-local runtime values are
encrypted under the original operation's state purpose. They are not delivered
by the canonical runtime resolver before publication. Target publication and
installation of the application-local runtime bindings commit atomically under
the current original execution lease. Existing resolver precedence lets the new
managed testing binding override an older plain same-key setting without
deleting shared configuration used by other lanes. Every carried credential must have existing
read-only-source sharing evidence; an external writable database, bot token or
other shared writable capability requires its own independent provider resource.
Random generation cannot create a new external account.

Before fresh creation, the fixed inspector attributes the retained application's
actual service/tasks/containers to its captured original deployment or exact
captured predecessor. It refuses a Docker socket mount, inherited platform
credentials or missing attribution. Inspecting a currently clean application
record is not historical execution proof. Missing historical evidence remains
unknown and keeps the original key and hold.

An addressed original may be unhealthy, exited or awaiting a replacement task.
Its current service template must still positively identify an admitted original
image and deployment, attach to the proxy's actual network, and uniquely own the
service-name backend. The first positive route admission saves the service ID,
full canonical TaskTemplate digest and runtime identity before capability or
candidate effects. Every later old-role read and native forward/inverse dispatch
rechecks that witness; a recreated service or changed template cannot become the
original. Task replacement between complete observations is allowed; changing
inventory during a snapshot remains unknown and uses the same key to retry.

The retained inspector checks prospective immutable TaskSpec environment and
mounts even when no ContainerID exists, including a rejected task. It also checks
all extant service-labelled containers for capability and mount hazards, and
requires actual image evidence for attributed task/container branches. This is
captured-input capability proof, not proof that execution never started or
ceased. Node-local container inventory is accepted only after a complete, stable
manager-side node enumeration positively establishes one local Swarm node, with
any supplied task NodeID agreeing. Multi-node, unavailable or changing scope
requires additional inventory evidence and remains unknown; it never treats a
remote container as removed. Existing Compose paths do not require this Swarm
scope proof.

The candidate still needs an exact running task/container, actual immutable image,
private readiness and the selected edge identity before publication. After
publication, ordinary private monitoring proves the canonical selected file and
applied route without depending on the superseded original's continued physical
existence. Unpublished route work and inverse recovery retain the original
service/template and native writer custody. Monitoring does not release the
original unknown operation or its hold.

The new application's image, registry settings, mounts, effective runtime
environment, single Swarm task and private runtime identity are read back.

For addressed lanes, the existing native route journal and writer fence retain
the public host while switching only after private readiness. The first
application cutover admits only the physically read, domain-bound Dokploy
application file and its matching applied `@file` routers. Foreign file routers
are not adopted. The legacy Compose wrapper retains its Docker route behavior. The exact
retained file identity and later owned route template remain in original
operation custody. A lost writer reply must settle that original native journal;
it never permits a second blind route publication.

A completed original forward exclusion selects recovery rather than another
forward attempt. Apply proves the original route through its exact inverse and,
when the original candidate job is terminal, retains an honest
`publication_refused` result with all resources intact. Correcting that candidate
does not reopen the terminal plan. Continue with a fresh reviewed preparation and
activation key. Staff use, unknown writer outcomes and unknown or foreign
candidate jobs retain their existing custody instead of inventing completion.

A first cutover can restore the original route without creating an owned route
file. A fresh generation can then qualify the current initial domain, application
file, legacy routers and backend directly. This requires a complete positive
physical observation; an absent or unreadable file is not a fallback for a
published predecessor's recorded route custody.

Once an original route intent exists, its passing private result remains the
admission evidence for that route. Re-entry does not overwrite it when a candidate
later dies. Actual new forward dispatch still proves the current candidate, and
post-switch edge identity and health still qualify publication or select recovery.

Private and public health establish process/build identity readiness. They do
not establish product qualification or substitute for declared migration,
maintenance, verification or recurring-schedule activation. Those run through
the ordinary deployment after explicit activation-backed hold lift. A health
endpoint that requires initialized schema must have a supported fresh-resource
startup path; this action does not invent migration commands or run reset to
make readiness pass. The activation's image record retains pending product
health and no passing product inventory is fabricated.

Application progress is stored in the **real original activation intent** and
its existing provider receipt. Repeat generations use distinct preparation and
original operation keys while preserving all prior completed custody. Unknown
creates or deployment replies recover only through exact positive original
identity/job readback; absence is not permission to repeat. Final target,
runtime-binding and custody publication is one transaction, including lease,
predecessor inventory/deployment and route-authority checks. Failed publication
leaves the old canonical target and runtime delivery intact and reuses the
original staged credential versions. Application storage names bind the actual
original preparation operation as well as its reviewed authority. A new
preparation key therefore allocates independent resources after a refused
candidate; replaying the original key keeps its original resources. Legacy
Compose allocation names retain their existing contract.

A refusal before the first provider checkpoint releases that unused activation
reservation, so a fresh review and key can proceed. After the fixed read-only
retained-capability inspection, a refusal before recipient creation instead
completes a negative original receipt while preserving that inspection's schedule
custody. An exactly attributed failed private application deployment
can also complete negatively while retaining its recipient, credentials and
volumes. Neither outcome says that a running process stopped or that old unknown
work became terminal. A terminal original private job can also be retained after
a permanent authority correction, without requiring a passing private-health
result. Before any image deployment, exact answered placement, mount or
configuration markers can instead prove an unused private recipient. These
markers remain bound to durably admitted progress and complete empty
deployment history; staged local credentials do not authorize deployment. This
bounded path covers stateless and local-volume recipients, reports that no
deployment was requested, and preserves every allocated resource.
Guards and the current-lease effect checkpoint precede the durable may-dispatch
phase and actual dispatch. An admitted unanswered create, mount or deploy remains
unknown even when a later readonly inspection changes the latest checkpoint.
Absolute configuration writes can replay only against the positively owned,
unused recipient. Unsettled create, mount or host-program outcomes remain unknown. A stable
original task/container with the expected image, environment and state may prove
a failed task, HTTP error or health-identity mismatch. Empty task inventory,
startup pending, transport failures and unreadable responses prove no failure.
Missing, foreign, duplicate or running deployment evidence keeps the negative
receipt pending.

If addressed routing changed before canonical publication refused, apply first
records its restoration decision, settles the original forward writer, and uses
a separate original inverse writer to restore the captured predecessor. The old
executor lock, serving-target authority, route file, runtime identity and native
writer custody remain required. A verified public HTTP failure or identity
mismatch after the original forward switch also selects this recovery. Generic
publication errors, pending health and lost replies continue observation on the
original key. A newer serving target or foreign route pauses
recovery instead of restoring over a correction. An interrupted inverse resumes
only that inverse under the original activation key. A completed inverse can
supply the exact restored route template to another generation without qualifying
the refused activation for lifecycle or hold lifting. The inverse keeps its own
immutable original intent across proxy restarts; each physical attempt still
uses the native fence. Candidate job custody is checked for the final negative
receipt after restoring the owned route.

These completed negative outcomes return `outcome: refused`, `applied: false`
and `separation_verified: false` from apply, receipt and readback. The supported
client saves that result truthfully. Keep its evidence and retained resources;
review a new independent preparation and activation key to continue. Corrected
configuration and staff-use records stay unchanged. Recorded staff use pauses
both forward changes and route recovery, including an already selected inverse,
until an explicit reviewed clear. Its expiry does not clear that protection.
Public readiness observation and
publication freshness use the same database clock.

Private HTTP checks send one bounded request through the existing proxy's numeric
TCP client and parse the complete response without following redirects. The
selected-image rehearsal in `qualityGate.test.testingRouteReaderRehearsal` runs
the emitted program against that actual binary for healthy, redirect, HTTP
failure, malformed, truncated and wrong-identity responses. Set the existing
`LAUNCHPLANE_TEST_TRAEFIK_IMAGE` to an already-local immutable image to run it
locally. Its Swarm topology remains a finite fixture; it is no live-lane proof.

The exact completed activation permits an ordinary forward deployment to start
with a fresh lifecycle baseline on the new application. It skips only the
captured predecessor inventory/deployment, clears inherited prior-artifact and
rollback inputs, and preserves same-target retry and explicit rollback checks.
The old unresolved execution remains fenced on its old application. A plain
repoint supplies none of this proof. Same-artifact event reconciliation must
also qualify the current target; activation does not turn predecessor inventory
into current product qualification.

Use the existing `stage-testing-separation.py lift-dry-run` and `lift-apply`
with this completed activation's plan digest and the unchanged interlock's
actual purpose. The same caller must still own the hold. No application
activation step itself clears the hold or marks the old original terminal.

## Legacy Compose continuation

The authenticated Director agent uses `scripts/activate-generic-web-testing.py`
with the installed `launchplane-write-action.py` helper. Keep its request and evidence files private,
outside the checkout. The request identifies the existing context, original
`preparation_key`, exact `source_commit`, volume-role to existing setting-key
mapping, existing port-setting keys and an administrative reason. It also names
`interlock_record_sha256`: the exact target-record digest from the supported
testing-hold read, explicitly identifying the retained hold as this operation's
staging interlock. Legacy reason prose never supplies that intent. An absent or
foreign hold, known staff use, a production alias, changed allocation or unsafe
carried key refuses activation. The original hold is copied unchanged to the new
recipient and retained in immutable old-target custody; its purpose is not relabeled.
Choose existing, unretired lane setting keys for the volume roles; activation
refuses a retired key rather than reviving a setting that runtime sync removes.

For a product explicitly declaring no public website, retained origin-domain
records stay on the old recipient and in custody. The replacement has no domains
or public routes. Declared public hosts, pending public-host effects, or origin
domains on a public product still require their own accepted cutover. Changing
the website declaration after review invalidates activation.
Custody records version the configuration digest. Records created before website
authority was included retain their original digest and supported continuation.

The service reads the product's Compose source and local includes from one
immutable repository commit through its existing source reader, verifies that
commit's product-owned release build, and carries only the lane's managed
configuration. Existing testing credentials must be lane scoped and classified
for testing. Shared keys require recorded read-only-source evidence. Opaque
comparison against the product's production provider environment prevents
testing credentials with production values. Values never enter client output.
Key safety evaluates every secret actually delivered by the site resolver;
unrelated global/platform bindings excluded by that resolver stay excluded.
A real carried-key policy refusal returns the structured
`testing_activation_carried_keys_unverified` category and keeps all holds.

The fixed host inspector requalifies the original allocation's empty local
backing directories, labels and all existing container mounts, including stopped
containers. Every requested host port must be absent from every container's
configured bindings and available to bind. Nothing inspects production data
contents or mounts production storage. Host storage and port proofs must both
survive the shared bounded schedule-log parser; absent, malformed or false proof
keeps activation and hold lift refused. The new provider source and environment
are read back before its raw source is materialized through the provider's
source-fetch operation. This writes only the owned, unused recipient's code
directory; it does not deploy containers or alter volumes or ports. The service
records that dispatch before issuing it. A missing converted file permits one
source-fetch per original-key apply, with fresh exact configured source/environment,
original placement, unused-recipient, lease and storage/port checks. This also
supports recovery after an interruption or rejected fetch left no file. It never
repeats creation or configuration. A present qualified render is adopted without
another fetch; an incompatible render is never overwritten.
An existing incompatible render remains fenced rather than being overwritten.
The actual converted Compose is then interpolated with those
same managed values and checked against the reviewed source. It can use only
the two qualified volumes, explicit independent ports and its private Compose
network. Host mounts, host capabilities, external networks, routing labels,
unclassified credentials and unqualified external `*_HOST`/`*_URL` environment
endpoints refuse it. Application code remains responsible for the behavior of
its declared read-only source integration.

Dokploy can add an unused `dokploy-network: {external: true}` root declaration
to a domainless render. Qualification ignores only that exact provider-added
declaration when the reviewed source did not declare it and no service joins
it. Joining it, aliasing it, changing its options or declaring another external
network still refuses activation. The actual service/resource comparison
otherwise remains exact.

Use `dry-run` with a fresh `--evidence-file`, inspect the public result, then
`apply --reviewed-dry-run --idempotency-key ORIGINAL` against that saved review.
The request's `instance` accepts only `testing`. The client supplies no target,
volume name, port value, secret value or credential source. PostgreSQL provides
cross-process executor and authority exclusion. Final recipient publication and
its custody receipt commit atomically, with the unchanged hold still set.

Keep the original key after interruption. `receipt` reads its immutable completed
result; `readback` checks the current published recipient. An interrupted
configuration can also be diagnosed before publication. In the recorded
`configure_new_recipient` or `materialize_new_recipient` phase, `readback` checks
that caller's persisted recipient and reports a closed identity, options,
source, environment, labels or render qualification category, including a
missing materialized source.
It returns no provider values or exception
text, recovers no placement and performs no host inspection, publication,
reservation adoption or retry. Even a passing provider check still refuses an
unpublished recipient; it is not proof of separation or permission to lift a hold.
An original-key apply may adopt a fully persisted, exactly matching configuration
after a lost reply
and fresh storage/port readback. It does not redispatch creation. Missing creation
identities are recovered only from one unambiguous original namespace and
placement; only previously undispatched creation steps continue. Unknown absent
creates, drift, active work or incomplete evidence stay fenced. An interrupted
configuration can resume only when the unused recipient still exactly matches
its recorded pre-write configuration; a completed matching write is adopted.
Preserve resources and the original key; never delete or reallocate to retry.

The new recipient uses raw Compose from the reviewed immutable source commit.
Image deployments continue normally; later product Compose changes require a
separately reviewed source/configuration update. Publication persists only the
qualified volume and port overrides, so old recipient overrides cannot return
shared storage or production ports during ordinary reconciliation.

After separation is proved, use the existing supported testing-hold path:
`scripts/stage-testing-separation.py lift-dry-run`, then `lift-apply`, with the
unchanged hold's actual `--hold-purpose`, the activation's
`--activation-plan-sha256`, a fresh evidence file and stable lift key. The same
caller must still own that exact unchanged interlock. The service rechecks the
actual safe provider render and empty resources/ports before the lift, and
checks authority again while clearing it. A new or foreign staff hold remains
protected. This bounded continuation requests normal testing reconciliation;
an ordinary staging-hold lift without activation proof keeps its existing
no-reconcile behavior. A lost post-clear receipt can retry the original lift and
recover its reconcile request. Its dry-run reads metadata only; apply rechecks
the provider and executes the fixed read-only host inspection before clearing.

Only the exact retained reservation of a published qualified replacement stops
blocking the new recipient. The old reservation remains unchanged and continues
to fence the old target, with its unresolved outcome visible in the durable
reservation and activation custody receipt. The lane monitor excludes only
that proven old-recipient fence from the active lane's deploy-fence check; it
continues to report any fence on its actual new recipient. A
plain target repoint has no such effect. Existing incidents resolve through the
normal monitor when current health and runtime identity pass; they are never
manually closed or suppressed by activation.

Keep the new volumes and both recipients if qualification or deployment fails.
Rollback never restores shared production settings or starts the old containers.
Read back the original operation and fix the supported testing continuation.
The migration is additive and its downgrade preserves custody for recovery.
Older service code cannot continue versioned custody created by this build;
roll forward to a compatible build before resuming instead of editing records.
Qualification refusals use a closed set of public error categories, including
route cutover and changed authority; provider or unexpected exception text
remains private. The testing-hold continuation preserves those same categories.
Keep the request trace
and original allocation/key while addressing that check, then plan again.
Actual testing acceptance still requires passing health, matching expected and
observed runtime identity and no open incident. Source tests and activation's
no-deploy receipt alone do not establish that acceptance.

Private same-address application activation uses the original private endpoint,
edge receipt, certificate, access list and exact provider origin. It moves only
the existing native HTTP route; the old provider domain and edge payload stay
unchanged. The canonical private recipient has no copied domain or managed public
route binding. Ordinary private monitoring verifies the original receipt and
unbroken completed activation lineage, then independently reads the actual winning
native route. The next generation reuses that same original route template.
Disable, newer edge receipts and changed endpoint/certificate/access-list/server
authority retain their protection; activation never re-enables an edge.

Old port, root path and complete file/applied authority are admitted read-only
before candidate mutations and rechecked before switching. A proven contradiction
before any route writer can retain an exactly closed private candidate negatively;
unavailable transport and possibly dispatched writers keep the original key.
Readiness is observed within a bounded window with the canonical identity parser.
Warm-up and replacement tasks can become healthy on the original key. Redirects,
transport/framing/JSON errors and nonmapping bodies stay unknown; a final stable
attributed failure is recovery-only under the original receipt. No fixture or
source receipt substitutes for actual private TLS, authenticated WSS and product
qualification after delivery.

A predecessor health response is diagnostic and never requires repairing the old
unknown recipient before independent recovery. Physical route, task and ownership
custody remain mandatory. The candidate must independently pass private readiness
and exact identity at the unchanged edge. When the predecessor was unqualified,
a nonqualifying edge response alone leaves the original key pending; a later
passing response can finish without replaying the physical route write. Proven
candidate failure or authority drift still uses the existing exact inverse, which
restores the original route without calling the original application healthy.
