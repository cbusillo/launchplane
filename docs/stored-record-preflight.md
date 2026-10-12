---
title: Launchplane stored-record deploy preflight
---

The candidate rehearsal checks compatibility of the exported record families
before a Launchplane replacement. It changes no deployment, release, backup,
repair or break-glass authority; [operations](operations.md) owns those paths.

## Snapshot source and scrubbing

`GET /v1/service/stored-record-snapshot` reuses `launchplane_service.read` at
the service context. It opens one repeatable-read, read-only transaction inside
the running service. The caller receives JSON, never a production database URL,
SQL access, keys or provider credentials. No grant or credential setup is needed.

Version 1 exports every authorization policy, product profile (including lanes),
environment inventory and merge-train policy, plus the latest merge-train run
for each repository/base-branch pair. Train history is neither deleted nor
exported; its unbounded growth cannot consume the snapshot budget. Limits are
10,000 total rows, 16 MiB and a 20-second export budget. Each database statement
has at most 15 seconds, clipped to the remaining budget. Exceeding a limit
refuses; there is no truncated or empty-policy success.

Managed-secret, runtime-value, provider, session, worker and all other tables
are excluded, including encrypted secret values. Identity strings and numbers,
free text, arbitrary map values, override environment values, URLs and command
strings become per-export salted pseudonyms. URLs point to `.invalid`. No
plaintext override or audit value is retained; free-form numeric values are
pseudonymized too. The salt is discarded.

Dictionary keys, absent fields, booleans, schema-defined numeric data, valid
timestamps, schema enum/constant values, code action/driver names and required
format markers such as `{number}` survive. Known code protocol fields can mark
`x-launchplane-snapshot-protocol` in their schema. Arbitrary free text is never
retained because it looks like protocol. Train run dictionaries use their
producer schemas, and a valid repository/base-branch policy-key relationship
survives. Raw payloads are not reserialized through current models: an old
policy's absent `instances` stays absent and historical keys remain for the
candidate's supported readers. Source policy digests are validated first;
scrubbed digests are recomputed and attest only rehearsal data. This private
fixture is not a backup, restore or authorization artifact.

## Candidate rehearsal

`scripts/deploy/stored-record-preflight.sh` takes an immutable candidate, the
exported HTTP response and an immutable PostgreSQL test image. It creates a
unique internal Docker network and ephemeral database. No ports, production
volumes, Docker socket, bootstrap secrets or host environment are passed to
the candidate. Its root filesystem and snapshot mount are read-only, its
CPU/memory/processes are bounded, and only its temporary database is reachable.

The candidate module reconstructs the schema at the exported Alembic revision,
imports raw scrubbed rows, runs hosted migrations, verifies the schema, loads
all included records through actual storage readers and constructs the service
application. Superseded policy rows are deliberately checked because supported
history readers load them too: stricter schemas must retain their compatibility
or migrate them. Startup's resolved policy source must be `db` with the restored
active record's digest. Authorization-closed startup is a failure, even though
the recovery behavior keeps the process alive.

HTTP listeners, workers and schedulers are not started. Excluded families'
populated migrations, worker startup, monitoring and provider effects are not
covered. Scrubbed values cannot prove every original value constraint; a field
format change must account for its scrubbed representation. Failures expose
error types, bounded code-owned validation paths, record-family metadata and
export limit reasons, never record inputs. Exit cleanup removes only the
rehearsal's containers, anonymous volumes and network. The workflow also runs
always-cleanup using the exact current job's resource label, covering forced
termination without sweeping other jobs' resources. Qualification uses its own
temporary registry config and removes it; persistent runner credentials stay
untouched. Authentication writes Docker's supported `auths` format into a new
private file instead of invoking login, which could select a persistent
credential helper. Qualification and the mandatory gate use the same temporary job-token
authentication file and fresh Docker context; the existing image-build login is unchanged. Verify
that this context reaches the actual deployment runner's intended Docker daemon.
A passing rehearsal on a different daemon cannot qualify that runner. The job
summary retains the safe result, never the snapshot.

Snapshots expire after five minutes. The shared workflow wrapper bounds image
pulls and the complete rehearsal to 240 seconds, then allows 15 seconds before
forced termination. Export is capped at one minute and current-job cleanup at
one minute: those three operations have a combined budget of 6 minutes
15 seconds, excluding job preparation. Missing/stale/oversize snapshots, unavailable Docker,
unsupported revisions or candidates without the module refuse. There is no
production-database, empty-fixture or coverage fallback.

## Activation order and recovery

The exporter and candidate module must land and deploy first through the
existing path. The Supervisor then dispatches this same deploy workflow on the
default branch with `stored_record_preflight_only=true` and an immutable
`image_reference`, using the installed protected-workflow helper. This read-only
job authenticates the existing image read, fetches the supported scrubbed export
and runs the rehearsal on the actual deploy runner. Both deployment jobs are
excluded in this mode. Verify its per-family counts, bounds and successful
runner result before routing the mandatory deploy gate PR. Qualification and
manual deploys validate the same bootstrap image repository and immutable digest. A refused read is
reported as missing standing authority; this mode creates no new grant. Landing both in one batch would require a route the old service lacks.
This ordered capability rollout supplies a supported way to keep deploying.

The mandatory Deploy Launchplane gate runs after immutable image resolution and before
rendering or requesting self-deploy. It deletes snapshot material after every
attempt and retains only counts, revision and pass/fail evidence. The existing
Director-gated emergency rollback remains governed by operations.

The PostgreSQL test image is read from CI's canonical pin. The workflow first
requires a successful authenticated export, then runs the isolated candidate
under the bounded timeout. Its normal failure propagation stops before even
rendering a self-deploy request; no provider effect or replacement is possible
on a preflight failure. The gate has no continue-on-error or missing-route skip.

After a rollback, verify that the running service still exports the required
format and that the candidate supports its database revision. A missing route
or newer columns unavailable to the rolled-back exporter is an explicit
recovery prerequisite; use the separately governed emergency recovery path in
operations, not an automatic gate exception. Version 1 owns the five families
above. A future family or table-format change must ship its version-compatible,
ordered producer/consumer rollout while retaining the previous supported
format. Missing coverage refuses rather than passing as "not covered".

Local PostgreSQL proof exports a historical-shaped policy, restores it in a
separate database and constructs the application. Reclassifying the restart
action as exclusively instance-scoped reproduces the Oct 11 failure on the
restored reader. Fixtures are synthetic; deployed export and workflow readback
remain separate Supervisor proof.
