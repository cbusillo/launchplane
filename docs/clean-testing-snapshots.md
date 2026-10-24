---
title: Clean testing snapshots for previews
---

Q788 separates the source used to refresh testing from the source used to
create a PR preview. Testing reads the product's qualified real source.
Previews read a frozen, sanitized snapshot captured immediately after a
successful refresh, before testing serves staff.

The source contract is tracked in [#4756](https://github.com/cbusillo/launchplane/issues/4756).
The preview API/deployment hooks and CM import-completion integration remain
prerequisites until that issue's implementation is complete. This page does
not establish live activation or qualify any product's data source.

## Refresh and publication

The existing Odoo testing drill owns scheduled upstream refreshes, and the
native testing rebuild owns on-demand upstream refreshes. Both use the
existing restore, sanitizer and independent integration readback. Holds,
staff use, needed-copy custody and uncertain original effects retain their
existing admission checks.

`preview.clean_testing_snapshot` opts a product into clean snapshots. It
requires the testing lane's reviewed `odoo_testing_drill_storage` budget.
Scheduled refresh remains separately enabled by `odoo_testing_drill_enabled`.
For snapshot products, `preview.clean_snapshot_refresh_hour_utc` selects the
nightly UTC hour; products are staggered inside that hour. The default is
05:00 UTC. A missed slot can catch up when the scheduler resumes. The same
existing worker executes the operation.

Capture occurs after sanitization and readback, while testing web is stopped.
The host saves a bounded PostgreSQL dump and an independent filestore copy
outside every application volume. It verifies their hashes and atomically
renames the completed directory. It uses the existing clone-or-copy behavior
for the filestore; full-copy headroom remains required.

The database-backed published receipt is separate from the current testing
generation. It advances with successful worker completion. Failed restore,
sanitization, capture or health checks keep the previous receipt. A testing
hold does not remove that receipt or turn preview selection into a read of
the staff-used database. Partial captures cannot become preview sources.

## Preview copy

A new preview selects one completed same-product snapshot. There is no
production or live-testing fallback. The preview and snapshot must be on
the same execution server; cross-host transport is not implied.

The copy receives no source SSH mount or upstream connection. Its filestore
is cloned or copied into exclusively owned storage with the preview writer
stopped and runner paused. It restores the frozen dump into a missing
database, then uses the existing credential, sanitizer, module-update and
connection-readback boundary before starting web. Existing preview databases
and Client edits are preserved.

PostgreSQL 17 and separate preview database volumes still require a full
database restore. The #4057 capability provides filestore reflinks where the
actual filesystem supports them, with full-copy fallback. It does not provide
database copy-on-write.

## Product transition and custody

OPW first needs its current preview repair, the Director's actual testing
hold lift and the existing source/artifact/custody qualification. A successful
testing refresh can then publish the first clean snapshot before fresh
previews switch to it. This changes no production key or access grant.

CM first needs CMSEED's qualified Fishbowl/RepairShopr fill and supported
sanitizer/import-completion execution. Capture must attach to that completion;
an ordinary code deploy must never snapshot staff-edited testing. CM's
production reader is the later source once CM runs its business on Odoo.
Existing Client-used previews remain protected.

Snapshot bytes remain in private host custody, independently of testing or
preview volume retirement. No new automatic deletion is introduced. Reviewed
headroom and the capture bound refuse a refresh before filling the snapshot
filesystem; refusal preserves the last usable source. Retirement or a wider
retention policy requires its own proved custody and active-reader handling.
