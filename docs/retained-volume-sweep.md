---
title: Retained volume sweep
---

Product retirement records retain a private inventory of the named volumes kept
by the testing application and its owned previews, together with product, lane,
retirement time and production-use snapshot. Capture uses the registered remote
server transport to inspect the actual Swarm service, all its tasks and its
containers, including stopped containers. Complete capture binds the Docker
engine, full volume metadata and backing directory device and inode. Configured
names remain recorded with unknown identity when capture cannot qualify them.
The primary Dokploy server, detached applications, older retirement records and
unsupported storage remain ineligible; their existing retirement guards remain.
No public application resource counts gain volume names or server coordinates.

`POST /v1/retained-volume-sweep` is a DB-backed service operation for a signed-in
admin or the Director's service agent. Supply `retirement_record_id` and
`minimum_age_seconds` (default one day) with `mode: dry-run`. The response gives
only identity digests, eligibility/refusal reasons and `plan_sha256`. Apply uses
the same inputs with `mode: apply`, `reviewed_plan_sha256` and an
`Idempotency-Key`. Readback repeats the original inputs, digest and key with
`mode: readback`; changing any bound input refuses reuse. Successful receipts
replay without another host mutation. An unfinished operation only reconciles
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
exact-name `docker volume rm` without force or prune. It writes a durable host
receipt before dispatch and verifies same-engine absence afterward. Uncertain
effects stay attached to the original operation for readback. No deployment,
production disposition or live deletion is performed by this source delivery.
