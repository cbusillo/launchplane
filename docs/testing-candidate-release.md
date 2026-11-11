---
title: Testing candidate storage
---

A native testing rebuild builds each copy in a private candidate: a Dokploy
compose target with three Docker volumes, `lp-testing-<id>-db`, `-data` and
`-logs`, labeled with the digest of the candidate's resource record. A failed
rebuild keeps them. This page covers how admission avoids filling that storage
and how a failed candidate's storage is released.

## Admission refuses a migrated restore that cannot fit

Before a migrated restore is queued, the rebuild preflight reads the testing
server's free space for Docker volumes, container writable layers and, when
clean snapshots are on, the clean-snapshot directory, grouping paths that share
a ZFS pool or device. It charges the same terms the receiver checks before delivery: the database
budget, the filestore budget plus the whole staged pair, the runner's copy of
the database, the clean snapshot when enabled, and the reserve once per
backing. The migrated budget comes from `migrated_storage_budget`, which the
candidate's generation also uses. A shortfall refuses with
`testing_storage_capacity_insufficient` and the missing bytes; an unreadable
backing refuses with `testing_storage_capacity_unavailable`. No operation or
candidate is created.

## Inspecting and releasing a failed candidate

`POST /v1/products/{product}/testing-rebuild/{operation_id}/candidate-release`
uses the existing signed-in admin or Director service-agent boundary. The path
original identifies the selected candidate. Optional `receiver_operation_id`
identifies a different original in the same product/testing context whose
remaining capacity is measured. It is included unchanged in dry-run, apply and
readback; neither original is edited or resumed.

A dry run lists every recorded candidate in that exact context in `candidates`,
including concrete per-resource refusal reasons. Only the path candidate has
`selected: true`. Repeat the same public route with another candidate's original
to select it. This is complete recorded-candidate enumeration, not adoption of
unbound provider objects. Each item exposes original, product/context/testing,
namespace/target, ownership/source/provider/lifecycle digests, container count,
resource identities and backing groups. Top-level `status` is `eligible`, `held`
or `absent`. An active original may be inspected, but cannot be released.

Apply uses the saved eligible dry run's `plan_sha256`, the same optional receiver
and a bounded `Idempotency-Key`. It rechecks selected/current references, active
originals and leases, publication, public domains, people use and only-needed
copies, then verifies exact container and physical volume custody before each
effect. Missing historical journals or a legacy release flag alone do not veto
current positive qualification. No new grant or assertion of empty data is used.
The executor exclusion and original row lock stay held; the product-authority
lock covers only effect admission, not remote work.

A candidate's own record and its original operation record are its custody and
failure evidence. They stay stored and never hold its storage. Any other record
that names the candidate's namespace, target or volumes holds it, and the
candidate's `references` list each one with its `reason`, `kind`, and
`record_id` or, for current records, `record_sha256`. `references` is omitted
when nothing else names the candidate.

Mounted storage qualifies only through the existing native retained-copy
lifecycle. The candidate must be terminally abandoned, or a completed copy whose
original failed, and its source must be preserved. A completed copy without a
preserved source stays held as `completed_copy_requires_disposition`. A stopped
container alone is insufficient. Unknown web processes, execs, published ports,
foreign mounts and changed IDs/configuration refuse. A web container still in
the original integration-gate wait can qualify only with the exact command
Compose created from the rendered held web command, runtime identity, data
mount, absent gate and positive shell/sleep process evidence from `docker top`
with its required PID column. A completed copy's serving web qualifies only with the exact command,
runtime identity, data mount and its regular gate file; it has no route, since
a routed or published candidate is refused. A missing web container still
requires runtime identity on its remaining runner. The existing Compose stop and exact non-force container
removal have durable receipts and absence readback. Only then may the existing
volume sweep remove reviewed eligible volumes, the data volume that holds the
gate last; its apply still refuses mounts.

Staged `.lp-native-*` source directories, unknown copy gates, completed copies
without a preserved source, current lanes, only-needed data, original pairs and
operation/failure/provider records stay protected. Production labels, wrong ownership, aliases and unknown backings
refuse. There is no compose-target deletion or resize. `released` means the
reviewed eligible resources are absent; explicitly preserved source resources
remain listed, not falsely reported as deleted.

Readback uses the original request, digest and key and does not plan again.
Completed receipts replay their stored result. Unknown/interrupted effects only
reconcile their original receipts and never blindly repeat a deletion. A definite
refusal or fully observed incomplete lifecycle settles as `held`, with its reasons;
a new eligible plan is needed for another attempt. Pre-convergence completed
receipts remain replayable, and incomplete older receipts retain read-only absence
reconciliation. They cannot dispatch the removed less-bound host effect path.
`mutation_reconciliation_required` means preserve that original key and evidence.

## Automatic release after a failed rebuild

When the worker commits a native testing rebuild as `fail` with an
`error_code`, the failed record's lease ends at its finish time. After that
commit, and outside the execution lock, the worker plans this same release and
applies it once under the route's scope with the key `automatic`; an existing
automatic receipt is never planned or applied again. The fresh checks,
effects and physical readback are the route's. A plan that is held or absent,
or a fresh check that refuses before any effect, settles as a `held` (or
`absent`) receipt naming its reasons, and nothing is removed. The operation's
status, cause and evidence are never changed. Other outcomes, other instances
and production never reach the host.

`GET .../testing-rebuild/operations/{operation_id}` reports that receipt as
`candidate_release`: `status` (`released`, `absent`, `held` or `unknown`),
bounded `reasons`, `plan_sha256` and `idempotency_key`. It is omitted when no
automatic receipt exists, including when the worker stops between the failed
commit and the release; the candidate then stays in place for the route.
Failures committed before this behavior shipped are not released automatically. `unknown` means an effect's outcome is unproven. Its
original receipt is only observed: route readback with the `automatic` key and
that `plan_sha256` reconciles it, and apply with that key always refuses
`idempotency_key_reused`. A later attempt is a new reviewed plan with a new key.
The release response's optional `reason_code` names a refusal no candidate row
carries.

`capacity` groups the optional receiver's actual backing observations, reporting
`backing_sha256`, `free_bytes`, `required_additional_bytes`, `reserve_bytes` and
`deficit_bytes`. It uses the receiver's existing migrated-data calculation,
including already populated filestore, remaining staged pair and any configured
clean-snapshot capture, with reserve once per backing. It does not turn configured
budgets into a new universal minimum. Missing/unqualified requirements remain
explicit in `capacity_refusal`; a non-migrated receiver is currently unqualified.

Each candidate's `physical_readback` maps volume identity to actual `before_bytes`,
`after_bytes` and signed `observed_change_bytes`; each resource names its backing. These values
include concurrent filesystem activity. Legacy `reclaimable_bytes` remains
optional/null and is never claimed as freed space. Apply/readback preserve the
reviewed plan digest even when physical free space changes.

The tracked exporter and workloads in `tests/fixtures/native-testing-retention/`
freeze current host programs for the catalog locked heavy-run entrypoint. Protocol
fixtures prove fabricated Docker protocol plus filesystem/subprocess behavior;
the separate QEMU engine fixture needs retained qualified images. Neither proves
real product eligibility. Unavailable intake and unknown effects retain evidence.
