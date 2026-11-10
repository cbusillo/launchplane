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

## Releasing a failed candidate

`POST /v1/products/{product}/testing-rebuild/{operation_id}/candidate-release`
is for a signed-in admin or the Director's service agent.

- `mode: dry-run` returns `plan_sha256`, `status` (`eligible` or `absent`) and
  `reclaimable_bytes`, the candidate's volume usage plus its containers'
  writable layers.
- `mode: apply` with `reviewed_plan_sha256` and an `Idempotency-Key` stops and
  removes the candidate's own containers and volumes, then verifies they are
  gone.
- `mode: readback` with the same digest and key reads the original receipt
  without planning again. If an apply was interrupted part way, readback
  reports it unsettled; apply with the same key finishes it, after the same
  checks.

It refuses, naming the reason, unless all of these hold:

- the operation is a native testing rebuild whose status is `fail` with a
  recorded `error_code` (`candidate_release_not_testing`,
  `candidate_release_outcome_unrecorded`);
- no lane target names the candidate, its publication is not `applied`, and it
  has no public domain (`candidate_release_selected`);
- the testing lane holds no recorded only-needed copy and shares no production
  resources (`testing_needed_copy`, `testing_production_resources_shared`);
- no person is using the lane: a staff testing hold (`testing_staff_hold`) or a
  recent sign-in (`testing_in_use`) refuses. Unknown use does not refuse; the
  release never touches the serving copy;
- on the host, every volume carries this candidate's ownership label and no
  other container mounts it (`candidate_release_held`).

The host program accepts only an `lp-testing-<32 hex>` namespace whose resource
record says `testing`. It never touches a volume without the candidate's label,
so a production volume cannot match. The operation record, its failure receipts,
the native evidence records, the Dokploy compose record and the uploaded
migrated pair on the service host are kept.

A failed restore does not release its candidate automatically yet; run the dry
run and apply after the failure is recorded.
