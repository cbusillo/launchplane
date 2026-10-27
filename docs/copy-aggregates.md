---
title: Read-only counts on disposable Odoo copies
---

`POST /v1/products/{product}/environments/testing/aggregate-counts` returns
four integers from the current sanitized testing database. It accepts only
`table`, `key`, optional `partition`, and `active` (default `active`) column
identifiers. It accepts no SQL, filters, database, host or credentials. Use the
product's existing `operations.read` authority, including the Director agent's
standing read permission. No grant, rebuild or source capture is needed.

The route requires the product's resettable testing policy, completed current
copy generation, and the matching published [Q788 clean snapshot](clean-testing-snapshots.md).
The snapshot records successful restore, sanitizer and independent integration
readback. Its generation digest, database hash, capture time, source mode/source
binding and operation identify the provenance. A later testing refresh without
its own clean snapshot refuses. Missing evidence remains unknown; this route
never qualifies an unknown copy by assertion.

Before running counts, the existing native receiver inspects exact copy storage,
local database endpoint, containers, network and production separation. The
reader rejects source credentials and connections in the application environment
and foreign members of the private database network. It opens the existing copy
lock without creating it, preventing overlap with a supported rebuild. It checks
the database copy witness and repeats the existing sanitizer's SQL-only
connection and integration checks with no allowances. Sanitization and counts
share one repeatable-read transaction enforced read-only by PostgreSQL. The
reader never loads an Odoo registry, opens a production connection, invokes a
source reader, creates a session or changes a lane or record. It always rolls
back. Runtime inspection and service authority are checked again before release
of the result. Failures return no counts.

Only local base tables with built-in text keys, a boolean active column and an
optional text or integer partition qualify. Views, foreign tables and row
policies refuse. `ONLY` excludes inherited tables. Names are quoted identifiers.
The query has a ten-second statement timeout, a two-second lock timeout and a
thirty-second process deadline. Output is limited to four signed-64-bit
nonnegative counts. Neither grouping keys, source rows, personal data nor
provider exception text are returned.

- `active_rows`: rows whose active column is true.
- `duplicate_groups`: nonblank normalized keys sharing the same partition
  with more than one row.
- `duplicate_rows`: rows in those groups.
- `missing_key_rows`: rows with a null, empty or space-only key.

Normalization lowercases and trims surrounding spaces. Duplicate and missing-key
counts cover all rows, including archived rows; only `active_rows` filters on
active status. These are counts from the current copy at observation time,
which can include staff edits since its recorded production-source refresh.
They are not a claim about the live production database at observation time.
The response exposes aggregate business volume and grouping density. Keep it
on the owning product's record; it cannot export rows or group identities.

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
supported read: a published clean copy must be inspectable for bounded aggregate
work without a live database read or a new grant. The previous inspection did
not join generation/snapshot provenance to physical qualification and had no
aggregate operation. Its unknowns correctly stopped an unqualified query, but
left this capability gap visible only when product research needed counts.
