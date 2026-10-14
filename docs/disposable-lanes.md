---
title: Disposable lanes
---

Overall direction permits agents to create, reset, rebuild and delete testing
and preview lanes and products that never went live. People using a lane,
the only needed copy of data, production resources and live credentials remain
protected. A label, missing staff hold, distinct volume name or distinct secret
version is not proof of those facts.

The source accepts exact-scope `disposable_lane_safety` observations on the
DB-backed product profile. Each observation binds the profile and current
tracked target/target ID (and the preview record for a preview). It records
usage (`unused`/`in_use`/`unknown`), data custody
(`reproducible`/`only_copy`/`unknown`) and production-resource isolation
(`isolated`/`shared`/`separation_ready`/`unknown`), with each known fact's source
reference and a timezone-aware observation/expiry window of at most fifteen
minutes. A changed binding, duplicate observation, expired observation, positive
staff-testing hold or any required unknown fact refuses the new permission.
A target ID also bound to production refuses it independently of the observation.
Safety observations are excluded from retirement's ownership digest, so they can
be refreshed on the same reviewed retry; actual ownership and runtime/secret
authority still must match, and each destructive checkpoint validates fresh facts.

The bounded service API `POST /v1/disposable-lanes/observations` accepts only the
authenticated Director agent. `inspect` returns the binding digest and unknown
facts, without provider actions. `record` requires that digest and records the
authenticated actor and current time itself. Caller-supplied facts and references
are assertions: while an authoritative collector is unavailable, the service
stores usage, custody and isolation as `unknown`, even if the request asserts
`unused`, `reproducible` or `isolated`. Those assertions never permit an apply.
`clear` removes this product's exact-scope observation, including stale preview
observations; it changes no staff hold, custody inventory, provider resource or
historical operation. Missing observations continue to refuse disposable writes. The
generic profile-write API cannot create or change safety observations, and this
change grants no profile-write access. An absent preview can be inspected and
recorded without a tracking bundle; creating a target later invalidates that
observation. Known facts must be produced from authoritative people and
data-custody records and actual resource inspection, not be manufactured to
make an apply pass. `separation_ready` means the staged desired resources have
been verified independent, all existing execution is excluded, and nothing
shared with production changes. It permits runtime sync with deployment
disabled and the bounded empty replacement staging below. Starting a lane requires `isolated`; normal key-safety and sanitized
restore checks remain in their existing drivers.

**Current producer gap:** Launchplane has a positive staff-testing hold, but no
complete authoritative occupancy or only-copy inventory and no supported
inspection that proves every writable mount/backing store, live database,
published port and credential relation. The schema does not pretend to supply
that inspection. There is currently no supported producer of positive safety
facts. The assertion API cannot create one and a direct DB write is not a
qualification procedure. Until a server-backed collector is available, the new apply path
returns `disposable_lane_safety_unknown`; absent observations never mean unused,
reproducible or isolated. Operational qualification records the missing sources
on the owning item. Building the missing producer needs no access grant or new
Director lane decision. Do not fill observations from names, IDs, old comments
or successful plans.

A disposable Odoo testing rebuild first calls `prepare-rebuild` on the same API,
with the old binding digest and `replaces_operation_id`. The service excludes
the old executor and captures the retained target ID immutably, without proving
past provider effects or changing its held status. With authoritative current
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
For observation-only use, clear each observation through the supported API and
verify the profile omits `disposable_lane_safety` before rolling back to an older
image. Do not clear staff holds or data-custody records. After preparation or
supersession records are written, older validators reject their new fields and
terminal status; retain compatible readers and
forward-fix the service; do not rewrite history or restore an old DB snapshot to
clear a fence. This source session performs no live qualification or deployment.
