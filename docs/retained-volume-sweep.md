---
title: Retained volume sweep
---

Product retirement records retain a private inventory of the named volumes kept
by the testing application and its owned previews, together with product, lane,
retirement time and production-use snapshot. Capture runs at planning; later
retirement observations keep the existing provider guards without requiring host
inspection to remain available. Capture uses the registered remote
server transport to inspect the actual Swarm service, all its tasks and its
containers, including stopped containers. Complete capture binds the Docker
engine, full volume metadata and backing directory device and inode. Configured
names remain recorded with unknown identity when capture cannot qualify them.
The primary Dokploy server, detached applications, older retirement records and
unsupported storage remain ineligible; their existing retirement guards remain.
No public application resource counts gain volume names or server coordinates.
Every capture and sweep Docker command explicitly selects the managed server's
local `/var/run/docker.sock`; inherited Docker host or context selectors cannot
redirect inventory or exact-name removal away from that local engine.

`POST /v1/retained-volume-sweep` is a DB-backed service operation for a signed-in
admin or the Director's service agent. Supply `retirement_record_id` and
`minimum_age_seconds` (default one day) with `mode: dry-run`. The response gives
only identity digests, eligibility/refusal reasons and `plan_sha256`. Apply uses
the same inputs with `mode: apply`, `reviewed_plan_sha256` and an
`Idempotency-Key`. Readback repeats the original inputs, digest and key with
`mode: readback`; changing any bound input refuses reuse. Successful receipts
replay without another host mutation. A readback that proves the original host
program never began removal, or records a completed failed removal with unchanged
identity, settles as held. After that settled receipt, a new dry-run and reviewed
key can continue. Unknown or partial effects without this proof remain held. An unfinished operation only reconciles
its original host receipt; it never blindly redispatches removal.

The sweep requires completed provider absence and a prelaunch snapshot and
current profile. Production lanes, live or unknown product use, unknown custody,
incomplete inventory and insufficient age remain held. Existing backup gates,
backup import/restore operations, retained and current generations, allocations,
copy captures, rehearsals, preview generations, replacement operations and held
provider mutations supply recovery custody. No new table or schema head is
required. Referenced volumes stay held, including completed backup operations
whose retained source has no recorded release.

Before removal, fresh complete Dokploy inventory must exclude mount references.
The fixed host program checks the captured identity under a host lock, inspects
all containers, services, tasks and other volume backing paths, checks host
mounts and actual directory emptiness, and repeats qualification before invoking
exact-name `docker volume rm` without force or prune. Inventory uses bounded
batches; capture selects only the retired service's containers. Removal reserves
its full timeout before writing a started receipt, so qualification-budget
exhaustion leaves no started receipt. It writes a durable host
receipt before dispatch and verifies same-engine absence afterward. Uncertain
effects stay attached to the original operation for readback.

Provider database resources need explicit complete mount details; absent details
are unknown. Compose declarations are checked through converted provider source.
Namespace references, ancestor bind mounts and
unrelated held provider reservations also conservatively retain volumes.

## Failed native testing candidates

The same route accepts `kind: native-testing`, `product`, `context`,
`instance: testing` and `operation_id` identifying the receiver's existing native
rebuild original. This is independent of product retirement and works for an
active product's unused failed testing candidates. It never edits or resumes
that receiver original. The existing administrator/Director authorization applies.

Start with `mode: dry-run` and no `candidates` to inspect every recorded candidate
in the exact product and testing context. Each result identifies its original,
namespace, target, ownership digest, source-evidence digest and per-volume
eligibility or refusal. There is no truncated first page. Unbound provider
objects are not adopted. Selected/current targets, receiver candidates, active
originals or leases, completed prepared copies, provider jobs, schedules, public
use, current/recovery references and explicit only-copy observations hold their
resources. A missing historical journal or legacy `safe_to_release: false` alone
does not hold an otherwise qualified failed copy.

Repeat dry-run with the exact candidate namespaces in `candidates`. Review its
`plan_sha256` and resource results. Apply that same request with `mode: apply`,
`reviewed_plan_sha256` and a new bounded `Idempotency-Key`. Readback uses the same
candidate list, original, digest and key with `mode: readback`. A changed request
cannot reuse the key. An interrupted request only reads its existing receipts;
it never repeats deletion. No supported catalog command is claimed until its
caller has adopted this optional request contract.

Apply holds the candidate executor exclusion, original row lock and existing
product-configuration lock. Immediately before each effect it rechecks candidate
ownership, current references, provider activity, exact physical identity and
source evidence. Nonempty failed copies require independently verified preserved
source artifacts or an originally empty build; staged `.lp-native-*` source
directories always stay. A prior web gate or unknown completed-copy evidence
holds nonempty resources. Original operations, failure records, candidates,
provider targets and source input pairs remain recorded and unchanged.

Mounted volumes remain held unless the candidate's existing terminal abandonment,
preserved source and exact native container custody qualify detachment. A stopped
container alone is insufficient. The lifecycle snapshot binds the original IDs,
images, configuration, mount identities and process starts. Active web processes,
execs, published ports, foreign writable mounts and changed snapshots refuse.
For a qualified abandoned candidate, the operation uses the existing native
Compose stop followed by exact non-force container removal and absence readback.
Each phase has its own original-bound durable provider receipt. A timeout only
reconciles that phase; it never replays stop or removal blindly. The compose
target, deployment history and schedules remain as evidence.

Only after detachment readback does the existing host sweep remove positively
bound, unneeded local volumes. Its apply path still refuses every container
mount. The planning-only allowance for reviewed container IDs cannot authorize
volume deletion. A fresh short-lived host permit is captured before each volume
effect; original readback uses the same ownership digest and host receipt without
renewing dispatch permission. Foreign volumes, production labels, aliases and
unknown backings refuse. There is no compose deletion, storage resize or lane
deletion. Staged source volumes and original input artifacts remain intact.

The receiver's `capacity` groups demand by actual backing, reporting observed
free bytes, required additional bytes, reserve and remaining deficit. It uses
the restore's existing capacity calculation, including populated filestore,
remaining migrated staging and any enabled clean-snapshot capture. It neither
adds configured budgets into a universal minimum nor changes them. Unqualified
receiver requirements return `capacity_refusal`, never a guessed zero deficit.
Physical results report `before_bytes`, `after_bytes` and the signed observed
free-space change on the same backing. These observations include concurrent
filesystem activity; logical volume size is never reported as freed space.
Unknown backing readback remains explicit.
