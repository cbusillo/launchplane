---
title: Read-only counts on disposable Odoo snapshots
---

`GET /v1/products/{product}/environments/testing/aggregate-counts` returns
four integers from the lane's frozen [Q788 clean snapshot](clean-testing-snapshots.md).
It accepts only `table`, `key`, optional `partition`, and `active` (default
`active`) column identifiers. It accepts no SQL, filters, database, host or
credentials. It uses the existing product `operations.read` authority,
including the Director agent's standing read permission.

The service requires a published clean snapshot and its matching completed
production-copy generation. Publication already requires successful restore,
sanitization, independent integration readback and capture, before staff use.
The result identifies the generation/source binding, original operation,
snapshot database hash and capture time. The snapshot ID is the source generation
binding, rather than a second copy of that digest. Missing or mismatched provenance
refuses before any provider read. A testing hold or an unknown occupancy
observation does not prevent reading this independent immutable source.
No policy change or access grant is needed.

The host reuses the clean snapshot receipt reader without creating directories
or files. It opens the archive read-only, verifies its hash against both the
receipt and service record, checks its size against the reviewed storage bound,
and confirms inspected container mounts do not expose that private snapshot path.
This is a direct-mount check, not a claim that an administrator cannot reach it.
It passes that same open descriptor to `pg_restore --data-only --file=-` in the
existing testing database container, solely as an archive decoder. The process
receives an empty environment except PATH and no database argument. It never
opens a database connection or executes the emitted SQL. The host parses the
selected table's COPY data in memory; raw rows and grouping keys stay on the
host. A changed archive, incomplete or malformed extraction, missing table or
missing column returns no counts. File identity and modification stamps are
checked again before the result is returned.

This reads neither staff-edited testing nor production. It creates no database,
restore, session, snapshot, lock file, service record or credential, and takes
no rebuild lock. Live application credentials and outbound connections are not
loaded or usable by the inert archive decoder. Deliberate testing integrations
do not need to be disabled to perform this read. No row, key, personal data,
provider output or exception text is returned.

The operation has a ninety-second host deadline, with bounds on row length,
extracted bytes, row count and grouping memory. Output contains only four
nonnegative signed-64-bit integers:

- `active_rows`: rows whose active column is true.
- `duplicate_groups`: nonblank normalized keys sharing the same partition
  with more than one row.
- `duplicate_rows`: rows in those groups.
- `missing_key_rows`: rows with a null, empty or space-only key.

Normalization uses Unicode lowercase and trims surrounding ASCII spaces.
Duplicate and missing-key counts include archived rows; only `active_rows`
filters on active status. These describe the recorded snapshot at capture time,
not the live production database now. They expose aggregate business volume
and grouping density, without individual or group identities.

The supported repository client reuses the installed admin helper's private
configuration, identity and transport:

```bash
uv run python scripts/read-copy-aggregates.py \
  --operator-helper /installed/launchplane/scripts/launchplane-write-action.py \
  --product example-odoo --table product_template --key first_mpn \
  --partition manufacturer
```

`disposable-lane-inspect` intentionally reads only a binding and retained
observations. Its unknown usage, custody and isolation facts never claimed a
physical probe or a sanitized source. The missing invariant was an end-to-end
supported read: a published clean copy must be usable for bounded aggregate
work without a live database connection or a new grant. The former inspector
had no snapshot-provenance projection or aggregate operation. Its unknowns
correctly stopped an unqualified query, but the capability gap surfaced only
when product research needed counts. This operation consumes existing Q788
qualification rather than changing those unknown observations into a pass.

## Original snapshot source version

`GET /v1/products/{product}/preview-snapshot-version` accepts only
`original_reservation_id`, `expected_snapshot_id` and
`expected_database_sha256`. It uses the product's existing `preview.read`
authority. The service validates the reservation-owned original intent, selects
its immutable clean snapshot and checks all three requested bindings before
any provider read. It never substitutes the latest published snapshot, the
current testing database or the preview image version. Original records and
operation state are unchanged, including failed or held operations.

The read reuses the same bounded, hash-verified, read-only archive descriptor
and [inert decoder](https://www.postgresql.org/docs/current/app-pgrestore.html) described above, with the fixed table `ir_module_module`.
It requires exactly one installed `base` row with a numeric recorded version;
missing, ambiguous, malformed, incomplete or changed evidence refuses. Only
that version is returned, with snapshot hash/capture and original-reservation
identity. No other module or business row leaves the host.
`metadata.preparation_stage` is null: neither this version nor the historical
snapshot receipt proves migration or preparation completion. No unknown fact
is inferred from a target image or a successful archive decode.

The existing client selects this mode with an original reservation instead of
a table. The operation owner supplies the exact private bindings:

```bash
uv run python scripts/read-copy-aggregates.py \
  --operator-helper /installed/launchplane/scripts/launchplane-write-action.py \
  --product example-odoo \
  --original-reservation-id "$ORIGINAL_RESERVATION_ID" \
  --expected-snapshot-id "$SNAPSHOT_ID" \
  --expected-database-sha256 "$SNAPSHOT_DATABASE_SHA256"
```

The same exclusive testing utility target and snapshot host must still be
available; otherwise the read remains unavailable. It creates no restore,
database session, grant, credential or successor operation. This diagnostic
GET adds no gate to the train, deploys, snapshot publication or recovery.
Existing native job failure-stage diagnostics and original-operation monitoring
remain the incident evidence. They expose a failed `module_update`, but cannot
supply the missing source version. A repeated failed migration remains a
separate cause to resolve before another preview attempt; reading metadata does
not settle or retry it.
