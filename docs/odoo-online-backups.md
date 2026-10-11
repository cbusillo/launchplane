# Online Odoo recovery tuples

The Odoo logical backup keeps the existing web container running. It captures
one PostgreSQL MVCC snapshot and the local, content-addressed attachment files
referenced by that snapshot. The matching immutable web/script-runner image ID,
snapshot ID, recovery timestamp, attachment inventory and artifact digests are
recorded in the schema-2 manifest and backup-gate evidence. The recovery point
is the snapshot cut, not the later time at which compression finishes.

## Consistency and retention

1. A registry-free PostgreSQL connection takes `SHARE ROW EXCLUSIVE` on
   `ir_attachment`, with a bounded lock wait. This drains attachment mutations
   and excludes Odoo's `SHARE`-mode filestore garbage collection. Reads and
   unrelated database writes continue; attachment requests can briefly wait.
2. A separate read-only repeatable-read transaction exports the database
   snapshot and reads its attachment references. While the fence is held,
   hard links pin each distinct referenced file into a private snapshot
   directory on the same mounted data filesystem.
3. The fence commits immediately after pinning. Attachment creates, deletes
   and garbage collection resume while `pg_dump --snapshot` uses the still-live
   exported snapshot. An attachment deleted after the cut remains in the pinned
   directory; a later attachment is absent from both recovery artifacts.
4. Size/SHA-1 checks compare every pinned file with its database reference.
   The archive copies the pinned bytes, excludes the live GC checklist and
   unreferenced files, and is read back against that inventory. The dump is
   checked with `pg_restore`. Only then are hashes, the inventory and the
   manifest published. Pins are removed on success or ordinary failure.

This uses the standard Odoo local filestore contract: files are named by SHA-1,
written before the attachment transaction commits, and removed by GC under its
table lock. It does not load an Odoo registry, module hook, cron or integration.
See the [Odoo attachment implementation](https://github.com/odoo/odoo/blob/19.0/odoo/addons/base/models/ir_attachment.py)
and [PostgreSQL snapshot synchronization](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION).
Custom attachment backends or writers that overwrite these files in place need
their own proved retention protocol; they cannot silently use this one.

Configure `ODOO_BACKUP_ROOT` through the lane's runtime-environment record on
the same data filesystem as `ODOO_FILESTORE_PATH`; the default mounted data
paths already satisfy this. Cross-filesystem links, unsafe paths, missing or
corrupt attachments, ambiguous containers, image disagreement, a lost snapshot,
an incomplete dump/archive or failed verification fail the backup/release.
There is no separately timed copy fallback and no web stop/start recovery.
Use a new backup-record ID after a failed capture; partial artifacts are not a
passing backup. A killed process can leave private pins/partial artifacts for
ordinary evidence-backed cleanup; they never publish success by themselves.

## Verification and restore boundary

Verification binds the manifest to the persisted capture evidence, preventing
schema downgrade or substitution. It retains the existing hash, dump, safe-tar
and staging-space checks and additionally verifies every archived attachment
against the bound inventory. Existing retained-volume imports and historical
manifests keep their original verification path; they are not relabeled as an
online snapshot. Restore must use the matching image/database/filestore tuple;
this capture neither permits live restore nor proves an image-only rollback.

Independent infrastructure/PBS protection and release admission remain owned by
[the production backup provider contract](production-backup-provider.md).
Metadata invocation/policy and host activation stay on
[launchplane#3242](https://github.com/cbusillo/launchplane/issues/3242) and
[launchplane#3248](https://github.com/cbusillo/launchplane/pull/3248).
Logical capture and its attachment proof do not replace either PBS or restore
verification, change destinations/encryption, or authorize production writes.

## Isolated proof

`tests.test_odoo_online_backup` exercises retained-file GC, missing/corrupt
files, cross-filesystem pin refusal, failed dump/archive, incomplete archives
even with matching hashes, and capture-bound manifest downgrade rejection.

For a local Odoo image containing `/odoo/odoo-bin`, `psycopg2` and PostgreSQL
tools, with a locally available `postgres:17` image:

```bash
LAUNCHPLANE_TEST_ODOO_BACKUP_IMAGE=<local-odoo-image> uv run --extra dev python -m unittest tests.test_odoo_online_backup_rehearsal
```

The opt-in rehearsal creates its own internal Docker network, empty PostgreSQL
database and worktree-local data directory. It probes an actual Odoo login page,
creates/deletes attachments and runs Odoo GC after the cut while the real dump
waits, then restores to a separate database/directory and verifies every
referenced attachment. Missing samples and HTTP/transport errors fail the proof.
The same fixture exercises a missing committed file, termination of the real
snapshot keeper, a failed dump and a corrupt archive, with web probes continuing.
It uses no provider API, live lane, existing data, outbound network or real key,
pulls no images and removes only its generated fixture containers/network/data.

