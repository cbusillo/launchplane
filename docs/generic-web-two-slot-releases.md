# Generic-web two-slot releases

A lane's DB-backed `generic_web_cutover` owns two distinct private application
or Compose targets, one route, the managed settings fingerprint and its explicit
singleton writer inventory. Source support does not configure or activate a
lane. Existing-lane seeding, installed service/host capabilities and real
Client-accepted release qualification remain on #3268.

The existing generic-web deploy entrypoint uses that authority under its product
operation serializer. Promotion retains the accepted-release and independent
backup guard. The cutover compiler reads the exact verified artifact pair and
current serving inventory; release requests cannot choose provider coordinates.
A stale caller profile cannot bypass the current lane record. Re-onboarding
preserves its traffic authority. A configured lane, or a recorded cutover whose
lane configuration disappeared, refuses the legacy in-place fallback.

The shared full-input classifier must prove read/write compatibility. Unknown,
database-changing and unexamined releases record a blocked classification and
never start a candidate. Generic-web has no implicit migration/maintenance
fallback. Application lifecycle extensions require separate explicit cutover
qualification; VeriReel's own driver and data-recovery path are unchanged.

## Preparation and switching

`workflows/generic_web_cutover.py` uses the common private-readiness and
applied/public-identity checks extracted from the Odoo implementation, plus the
same `TraefikFileRoute` atomic publisher. The provider-neutral contracts live in
`contracts/generic_web_cutover.py`; the Dokploy adapter lives separately in
`dokploy/generic_web_cutover.py`.

The service supplies a `DokployGenericWebDeployProvider` with its installed
`cutover_provider_factory`. The factory only binds capabilities; it performs
no provider effects. `WebSlotDriver` owns the existing managed-settings delivery,
image/endpoint observations, warming, exclusive lease, background writer handoff
and connection drain. Preflight must verify the admitted targets have no public
routers or public ports, candidate startup fences background jobs, and any
immutable application provider tag resolves to the exact image digest before
startup. This is capability binding, never a new grant or credential path.

Preparation stages and reads back the exact settings on the inactive target
before deployment. The Dokploy adapter then uses the existing deployment helper
for that distinct application/Swarm or Compose target. Independent private
readiness checks image digest, settings, release/slot identity, renders, warmed
assets, background fencing and retained old assets. Old traffic stays intact.

Switching publishes one owned route file atomically and checks Traefik's actual
applied router/service, then independently probes public runtime identity. A
file rename or deploy acknowledgement is insufficient. The watched mount must
be a directory. If application of an already published owned file remains
unobserved, recovery may re-notify that exact intent after checking file and
applied authority again. Foreign routes/configuration are never overwritten.

Only verified traffic application permits singleton writer handoff; the
predecessor drains before exactly one successor is observed per inventoried
writer. Connections drain without deleting the retained slot or its assets.

## Records and recovery

Typed checkpoints use `DeploymentRecord.generic_web_cutover` in the existing
DB-backed deployment payload. Intent precedes effects. Same-release retries
must retain the immutable plan, lane coordinates and settings fingerprint.
Unfinished cutovers hold the lane. Recovery reads applied/public authority before
publishing again, so a successful switch with a lost response is not repeated.
Neither uncertain publication nor a failed drain records completion.

Compatible rollback targets the original retained artifact, privately verifies
it again, persists reverse-switch intent, publishes/read-backs the old target,
hands off writers and drains the candidate. It produces a separate passing
deployment event and inventory with the actually serving retained runtime
identity. Incompatible rollback requires separately authorized data recovery.
A missing provider refuses before the old deployment path can be called.

## Isolated proof

The focused tests cover both target categories, file/SQLite payload round trips,
failed/stale readiness, wrong digest/public identity, settings refusal, drain,
uncertain forward/reverse publication and restart. The opt-in
`tests.test_generic_web_cutover_rehearsal` uses real Swarm services and Compose
projects in a disposable nested Docker engine, the production adapter/controller
and real Traefik with continuous public identity/asset probes. Its synthetic
Dokploy edge translates the selected private target into Docker operations;
it does not qualify installed Dokploy/service bindings or a live release.

Run the rehearsal with `LAUNCHPLANE_TEST_WEB_CUTOVER=1`, explicit
`LAUNCHPLANE_TEST_DIND_IMAGE`, `LAUNCHPLANE_TEST_TRAEFIK_IMAGE`, digest-pinned
`LAUNCHPLANE_TEST_WEB_OLD_IMAGE` and `LAUNCHPLANE_TEST_WEB_NEW_IMAGE` (Python
runtime fixtures), and optional `LAUNCHPLANE_TEST_WEB_EVIDENCE` for receipts.
It creates only disposable local containers/networks and removes them on exit.
It never changes the workstation's Swarm membership. Full repository CI and the
current local gates remain required for delivery; source proof authorizes no
production switch.

The durable HTTP deploy adapter reserves the lane's traffic route rather than a
legacy single recipient. Reconciliation reads the exact persisted checkpoint
and actual route/public identity. `cutover_checkpoint_recoverable` means the
terminal operation result is absent and the same controller can resume under a
renewed lease; it does **not** report provider effects or a target deployment as
absent. Missing checkpoints after an effect journal, foreign authority and
unavailable readback stay unknown. A completed replay reads the cutover result
without invoking legacy deployment adoption.

The rollback planner accepts a digest-bound retained slot on an admitted
cutover lane without requiring a provider tag: switching traffic back never
redeploys that application. Candidate application preparation still uses its
validated immutable provider tag. Readiness requires background fencing for
inactive slots; aborting before the switch verifies the serving slot while its
existing writer ownership remains intact. A broken candidate's public health
does not hide independently applied route authority: rollback privately checks
the old runtime and verifies its public identity after restoring that route.
Blocked and aborted attempts finish as failed deployment events rather than
remaining pending. Readiness persists first/warmed request timings alongside
its render/asset and identity evidence.

Writer ownership is independent of traffic authority. Old workers may still be
running after the new route applies and before handoff finishes. Switch-back
therefore verifies old renders/identity without demanding that those old workers
be fenced; the reverse handoff then drains the successor before confirming old
ownership. An inverse publication reconciles the owned file even when the proxy
still serves old, cancelling a pending candidate intent before a delayed watch
event or proxy restart. The common publisher requires both durable intent and
applied authority to agree for an idempotent success. Odoo's compatible inverse
uses this same correction.

Promotion consumes bounded cutover failures through its existing automatic
rollback/finalization path. A rollback's newer checkpoint cannot be overwritten
by the promotion's earlier deployment snapshot. New candidates must belong to
the product's configured image and source repository. These checks grant no
access and leave installed-provider/actual release qualification on #3268.
