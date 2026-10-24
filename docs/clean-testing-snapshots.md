---
title: Clean testing snapshots for previews
---

Q788 separates the source used to refresh testing from the source used to
create a PR preview. Testing reads the product's qualified real source.
Previews read a frozen, sanitized snapshot captured immediately after a
successful refresh, before testing serves staff.

The source contract is tracked in [#4756](https://github.com/cbusillo/launchplane/issues/4756).
The preview API binds its selected snapshot into the existing issued plan.
CM import-completion integration remains a prerequisite on [#4753](https://github.com/cbusillo/launchplane/issues/4753).
This page does not establish live activation or qualify any product's data source.

## Refresh and publication

The existing Odoo testing drill owns scheduled upstream refreshes, and the
native testing rebuild owns on-demand upstream refreshes. Both use the
existing restore, sanitizer and independent integration readback. Holds,
staff use, needed-copy custody and uncertain original effects retain their
existing admission checks.

`preview.clean_testing_snapshot` opts a product into clean snapshots. It
requires the testing lane's reviewed `odoo_testing_drill_storage` budget.
Scheduled refresh remains separately enabled by `odoo_testing_drill_enabled`.
Snapshot products are staggered inside the scheduler's 5:00–6:00 AM UTC nightly window.
A missed slot can catch up when the scheduler resumes. The same
existing worker executes the operation.

Capture occurs after sanitization and readback, while testing web is stopped.
The host saves a bounded PostgreSQL dump and an independent filestore copy
outside every application volume. It verifies their hashes and atomically
renames the completed directory. It uses the existing clone-or-copy behavior
for the filestore; full-copy headroom remains required.

The newest qualified immutable database-backed capture is the preview source,
separate from the current testing generation. Worker completion records it
atomically; there is no second mutable pointer. Repeated completion preserves
the original capture receipt. A snapshot-domain publication refusal records
`clean_snapshot_published=false` on the terminal operation and preserves the
previous source; database failures still roll back the transaction. Failed restore,
sanitization, capture or health checks keep the previous receipt. A testing
hold does not remove that receipt or turn preview selection into a read of
the staff-used database. Partial captures cannot become preview sources.

## Preview copy

A new preview selects one completed same-product snapshot. An existing preview
with a qualified completed deployment keeps its data and needs no snapshot
lookup, including before the product has published its first snapshot. There
is no production or live-testing fallback. The preview and snapshot must be on
the same execution server; cross-host transport is not implied.

The copy receives no source SSH mount or upstream connection. Its filestore
is cloned or copied into exclusively owned storage with the preview writer
stopped and runner paused. It restores the frozen dump into a missing
database, then uses the existing credential, sanitizer, module-update and
connection-readback boundary before starting web. Existing preview databases
and Client edits are preserved. A failed initial clone can resume using its
private target-bound restore receipt and a preview-specific completion
witness. The marker copied from testing cannot prove a preview restore. If a
failed import left no database,
filestore replacement requires proof that every remaining byte is also in the
retained frozen source. Unknown files are preserved and execution refuses.
Existing successfully deployed previews keep their own data on later builds.

PostgreSQL 17 and separate preview database volumes still require a full
database restore. The #4057 capability provides filestore reflinks where the
actual filesystem supports them, with full-copy fallback. It does not provide
database copy-on-write.

## Product transition and custody

OPW's preview repair landed in PR4734. Its transition still needs the Director's
actual testing hold lift and the existing source/artifact/custody qualification.
After source deployment, opt the product in and run its first admitted
refresh. New preview creation refuses until that refresh publishes a clean
snapshot. Existing preview data remains intact. Enable the existing daily
drill only with its qualified source, storage budget and custody checks. This changes no production key or access grant.

CM first needs CMSEED's qualified Fishbowl/RepairShopr fill and supported
sanitizer/import-completion execution. Capture must attach to that completion;
an ordinary code deploy must never snapshot staff-edited testing. CM's
production reader is the later source once CM runs its business on Odoo.
Existing Client-used previews remain protected.

Snapshot bytes remain in private host custody, independently of testing or
preview volume retirement. Cleanup keeps the published source and its predecessor,
unexpired issued preview plans, active or uncertain preview operations, and
incomplete host clone attempts. Only obsolete copies with matching immutable
service receipts may be removed. Host copying uses a shared read lock; retirement uses an exclusive lock. The
read lock is released after copying, before sanitizer and module work. Attempt
metadata is retired only after every original volume and physical backing is
proved gone or replaced. Unknown hard-crash partial directories remain in
custody; they are never swept by filename.
Unknown reader evidence preserves copies. Reviewed headroom is checked before
testing stops; insufficient capacity keeps the previous source usable.
PostgreSQL restore and full filestore-copy headroom are also required for a new
preview. Neither cleanup path removes testing originals, backups or unknown files.
