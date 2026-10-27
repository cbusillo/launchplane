---
title: Read-only counts on disposable Odoo snapshots
---

`POST /v1/products/{product}/environments/testing/aggregate-counts` returns
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

The supported repository client reuses the installed operator helper's private
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
