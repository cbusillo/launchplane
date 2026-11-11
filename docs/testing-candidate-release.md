---
title: Testing candidate storage
---

A native testing rebuild creates a private Dokploy candidate with three local
Docker volumes, `lp-testing-<id>-db`, `-data` and `-logs`. Each volume has the
candidate resource digest and its role label. This route releases failed copies
under the Director's disposable testing rule.

## Admission

Migrated restore admission reads the testing server's free space and groups paths
that share a backing. It charges the database, filestore, staged pair, runner copy,
optional clean snapshot and reserve using `migrated_storage_budget`. A shortfall
returns `testing_storage_capacity_insufficient`; unavailable backing information
returns `testing_storage_capacity_unavailable`. Neither creates a candidate.

## Release a failed copy

`POST /v1/products/{product}/testing-rebuild/{operation_id}/candidate-release`
uses the existing signed-in admin or Director service-agent boundary. The path
identifies the failed original whose candidate is selected for release. This
selection is different from the lane's selected serving target.

The dry run lists recorded candidates in that exact product/testing context.
Unselected siblings are listed from records without remote probes; their
unobserved status remains unknown.
Only the path candidate has `selected: true`. Optional `receiver_operation_id`
identifies the restore whose remaining capacity should be measured. That receiver
is excluded from release. Production originals and foreign contexts are rejected.

Three current protections apply:

- A selected or serving target is kept. The route reads lane target IDs,
  publication and public routes. The host also keeps a candidate with published
  ports.
- A lane with a sign-in inside the existing usage probe's 24-hour window is
  untouched. Unknown sign-in observations remain unknown and are reported.
- Effects address only the recorded candidate's testing server, namespace and
  three local volumes. Ownership/role labels, ordinary Docker backing, mount
  paths and writable container mounts keep production storage unreachable.

Historical record references, missing custody receipts, provider inventory,
container state, web gates, copy completion and unavailable source proof do not
hold a failed copy. An unavailable storage observation is listed in `references`
without changing eligibility. Optional `include_evidence: true` retains a private,
`Cache-Control: no-store` observation packet for the selected candidate; it is not
an approval step and does not reintroduce custody requirements.

The source archive and qualified database/filestore source pair are outside the
candidate set. The request accepts an operation ID, never a path or volume name.
Resources derive only from that candidate's `OdooTestingResources` namespace.
The release program has no source archive/cache deletion operation, and cannot
address the service's retained native-copy cache. The receiver candidate is also
excluded when supplied. Successful-rebuild source cleanup remains a separate
existing path; release never calls it.

Apply uses the dry run's `plan_sha256`, the same optional receiver and a bounded
`Idempotency-Key`. It rechecks current selection and sign-ins under the original
executor exclusion and durable provider effect admission. The host stops and
removes only the candidate's containers, then removes its volumes with the data
volume last. Docker removal errors are reported as unknown effects, not converted
to success or retried with force.

After apply, the route verifies physical volume absence. `physical_readback`
reports actual filesystem `before_bytes`, `after_bytes` and signed
`observed_change_bytes`, keyed by volume identity. These measurements include
concurrent filesystem activity. Optional receiver `capacity` uses actual backing
observations and the existing migrated-data calculation; unqualified requirements
remain explicit in `capacity_refusal`. `reclaimable_bytes` is not a freed-space
claim.

Readback uses the original request, digest and key. Completed receipts replay.
Interrupted effects reconcile only their original receipts and never repeat a
deletion blindly. The host retains the same operation key under
`DockerRootDir/launchplane-state/testing-candidate-release`, so a lost reply can
recover the terminal refusal or completed physical measurements. A refusal before
any effect releases the service reservation; a definite host refusal settles held. Older unfinished custody-based receipts cannot dispatch this
new effect path. Preserve their original keys and evidence for reconciliation.

## Automatic release

After the worker commits a native testing rebuild as failed with an `error_code`,
it plans and applies this same route once with the key `automatic`, outside the
execution lock. Other outcomes and production do not reach the host. Existing
automatic receipts are never reapplied.

`GET .../testing-rebuild/operations/{operation_id}` exposes the automatic receipt
as `candidate_release`, including status, bounded reasons, plan digest and key.
A held plan reports its protection; an unknown effect keeps
`mutation_reconciliation_required`. Readback can reconcile that original effect;
manual apply with the `automatic` key remains refused. Failures committed before
this behavior shipped are not released automatically.
