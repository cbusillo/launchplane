---
title: Launchplane stored-record deploy preflight
---

The candidate rehearsal catches stored-record incompatibility before a
Launchplane service replacement. It does not authorize a deployment or change
the release, backup, repair or break-glass contracts in [operations](operations.md).

## Snapshot source and scrubbing

`GET /v1/service/stored-record-snapshot` uses the existing
`launchplane_service.read` authorization at Launchplane's service context. It
opens one repeatable-read, read-only transaction inside the running service.
The caller receives JSON, never a production database URL, SQL access, keys or
provider credentials. No grant or credential setup is required.

The export includes every row in five record families: authorization policies,
product profiles (including lanes), environment inventory, merge-train policies
and merge-train runs. Table coverage is mandatory; there is no truncated or
empty-policy success. The limits are 10,000 total rows, 16 MiB and a 20-second
export budget, with database statements bounded by the remaining budget and
15 seconds individually. Exceeding a bound refuses the export.

Managed-secret, runtime-value, provider, session, worker and all other tables
are excluded. Encrypted secret values are also excluded. The scrubber replaces
identity strings, free text, arbitrary map keys/values, override environment
values, URLs and command strings with per-export salted pseudonyms. URLs point
to `.invalid`; no plaintext override, audit or embedded credential is retained.
It retains structure, absent fields, booleans, numbers, valid timestamps,
schema-declared enum/constant values, code action/driver names, wildcard
selectors and required format markers such as `{number}`. Numeric identity
strings and hashes become format-compatible pseudonyms. The salt is discarded.

Raw payloads are not round-tripped through current model serialization: an
old policy's absent `instances` stays absent, and historical fields remain for
the candidate's actual storage readers to interpret. Source policy digests are
validated before scrubbing, and scrubbed policy digests are recomputed. Those
digests describe rehearsal data, never production authorization. The export is
a private compatibility fixture, not a backup, restore or authority artifact.

## Candidate rehearsal

`scripts/deploy/stored-record-preflight.sh` accepts the immutable candidate,
the exported HTTP response and an immutable PostgreSQL test image. It creates
a unique internal Docker network and an ephemeral PostgreSQL database. No
ports, production volumes, Docker socket, bootstrap secrets or host environment
are passed into the candidate. The candidate has a read-only root filesystem,
bounded CPU/memory/processes and a read-only snapshot mount. Only its temporary
database peer is reachable.

The candidate's `control_plane.stored_record_preflight` reconstructs the schema
at the export's Alembic revision, imports the raw scrubbed rows, runs the hosted
schema migration, verifies the schema, loads all included records through the
actual storage readers, and constructs the real service application. There is
no bootstrap-policy seed or authorization-unavailable success. HTTP listeners,
workers and schedulers are not started. Any migration, reader or construction
failure returns nonzero; diagnostics expose an error type, never row values.
The shell's exit trap removes only its own containers and network.

Snapshots expire after five minutes; export retrieval must succeed before the
rehearsal. A caller must bound the entire shell run with
`timeout --kill-after=15s 240s bash scripts/deploy/stored-record-preflight.sh ...`.
With a 30-second export request, preflight adds at most 4 minutes 45 seconds
including forced cleanup. Missing/stale/oversize snapshots, unavailable Docker,
unsupported revisions or images without the candidate module refuse. There is
no direct-database or empty-fixture fallback.

## Activation order

The exporter and candidate module must land and deploy first through the
existing path. Then the mandatory Deploy Launchplane gate can land. The
Supervisor verifies a scrubbed export from the deployed first image before
routing the gate PR. Landing both as one batch would make the first deployment
require a route the old service does not have. This is an ordered capability
rollout, not a bypass of an enabled gate.

This first change supplies the capability and tested runner. The gate change
on #3413 calls it after resolving the immutable image and before rendering or
requesting self-deploy. The workflow deletes snapshot material after every
attempt and retains only counts, revision and pass/fail evidence. The existing
manual, Director-gated emergency rollback remains governed by operations.

The local PostgreSQL proof exports a historical-shaped policy, restores it in
a separate database and constructs startup successfully. Reclassifying the
restart action as exclusively instance-scoped reproduces the Oct 11 error in
the restored reader and blocks the candidate. It uses synthetic data only;
deployed export, workflow and production replacement proof are separate.
