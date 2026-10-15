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
requirements below continue to govern runtime sync and rebuild, which can
start or change a lane.

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
that inspection. There is no supported producer of facts permitting a disposable
apply. The assertion API cannot create one and a direct DB write is not a
qualification procedure. Until a server-backed collector is available, the new apply path
returns `disposable_lane_safety_unknown`; absent observations never mean unused,
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
