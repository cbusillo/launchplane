from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
import unittest

from control_plane.contracts.tenant_merge_eligibility import (
    TenantRepositoryClassificationRecord,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.tenant_repository_classification import (
    TenantRepositoryClassificationConflictError,
    TenantRepositoryClassificationSequenceError,
    apply_tenant_repository_classification,
    get_tenant_repository_classification_read_model,
    require_tenant_repository_classification_read_store,
    require_tenant_repository_classification_store,
)

PRODUCT = "launchplane"
CONTEXT = "production"
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/tenant-site"
CLASSIFIED_AT = "2026-07-31T11:00:00Z"
SOURCE = "operator"
REASON = "initial classification"


class TenantRepositoryClassificationDomainTests(unittest.TestCase):
    def test_initial_create_revision_1(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            rec1 = _record(revision=1)
            res = apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="apply",
            )
            self.assertEqual(res.status, "applied")
            self.assertEqual(res.classification_revision, 1)
            self.assertEqual(res.repository_id, REPOSITORY_ID)
            self.assertIsNone(res.supersedes_record_id)

            read_model = get_tenant_repository_classification_read_model(
                repository_id=REPOSITORY_ID,
                store=store,
            )
            self.assertEqual(read_model.status, "available")
            self.assertEqual(read_model.history_count, 1)
            self.assertIsNotNone(read_model.current_record)
            assert read_model.current_record is not None
            self.assertEqual(read_model.current_record.classification_revision, 1)

    def test_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            rec1 = _record(revision=1)
            res = apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="dry_run",
            )
            self.assertEqual(res.status, "would_apply")
            self.assertEqual(res.mode, "dry_run")

            read_model = get_tenant_repository_classification_read_model(
                repository_id=REPOSITORY_ID,
                store=store,
            )
            self.assertEqual(read_model.status, "missing")
            self.assertIsNone(read_model.current_record)
            self.assertEqual(read_model.history_count, 0)

    def test_revision_update_success(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            rec1 = _record(revision=1)
            res1 = apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="apply",
            )
            rec1_id = res1.record_id

            rec2 = _record(
                revision=2,
                kind="engineering",
                supersedes_record_id=rec1_id,
            )
            res2 = apply_tenant_repository_classification(
                store=store,
                record=rec2,
                expected_current_record_id=rec1_id,
                mode="apply",
            )
            self.assertEqual(res2.status, "applied")
            self.assertEqual(res2.classification_revision, 2)
            self.assertEqual(res2.supersedes_record_id, rec1_id)

            read_model = get_tenant_repository_classification_read_model(
                repository_id=REPOSITORY_ID,
                store=store,
            )
            self.assertEqual(read_model.status, "available")
            self.assertEqual(read_model.history_count, 2)
            self.assertIsNotNone(read_model.current_record)
            assert read_model.current_record is not None
            self.assertEqual(read_model.current_record.classification_revision, 2)

    def test_stale_expected_current_conflict(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            rec1 = _record(revision=1)
            res1 = apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="apply",
            )
            rec1_id = res1.record_id

            rec2 = _record(
                revision=2,
                supersedes_record_id=rec1_id,
            )
            res2 = apply_tenant_repository_classification(
                store=store,
                record=rec2,
                expected_current_record_id=rec1_id,
                mode="apply",
            )
            rec2_id = res2.record_id

            rec3 = _record(
                revision=3,
                supersedes_record_id=rec2_id,
            )
            with self.assertRaises(TenantRepositoryClassificationConflictError):
                apply_tenant_repository_classification(
                    store=store,
                    record=rec3,
                    expected_current_record_id=rec1_id,
                    mode="apply",
                )

    def test_skipped_revision_rejection(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            rec1 = _record(revision=1)
            res1 = apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="apply",
            )
            rec1_id = res1.record_id

            rec3 = _record(
                revision=3,
                supersedes_record_id=rec1_id,
            )
            with self.assertRaises(TenantRepositoryClassificationSequenceError):
                apply_tenant_repository_classification(
                    store=store,
                    record=rec3,
                    expected_current_record_id=rec1_id,
                    mode="apply",
                )

    def test_mismatched_supersedes_record_id_rejection(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            rec1 = _record(revision=1)
            res1 = apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="apply",
            )
            rec1_id = res1.record_id

            rec2 = _record(
                revision=2,
                supersedes_record_id="wrong-supersedes-id",
            )
            with self.assertRaises(TenantRepositoryClassificationSequenceError):
                apply_tenant_repository_classification(
                    store=store,
                    record=rec2,
                    expected_current_record_id=rec1_id,
                    mode="apply",
                )

    def test_idempotent_replay(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            rec1 = _record(revision=1)
            res1 = apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="apply",
            )
            self.assertEqual(res1.status, "applied")

            res2 = apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="apply",
            )
            self.assertEqual(res2.status, "replayed")

    def test_same_revision_different_payload_conflict(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            rec1 = _record(revision=1, kind="tenant_ui")
            apply_tenant_repository_classification(
                store=store,
                record=rec1,
                expected_current_record_id="",
                mode="apply",
            )

            rec1_different = _record(revision=1, kind="engineering")
            with self.assertRaises(TenantRepositoryClassificationConflictError):
                apply_tenant_repository_classification(
                    store=store,
                    record=rec1_different,
                    expected_current_record_id="",
                    mode="apply",
                )

    def test_ambiguous_highest_revision_fails_closed(self) -> None:
        revision_1 = _record(revision=1)
        revision_2a = _record(
            revision=2,
            kind="tenant_ui",
            supersedes_record_id=revision_1.record_id,
        )
        revision_2b = _record(
            revision=2,
            kind="engineering",
            supersedes_record_id=revision_1.record_id,
        )

        class AmbiguousStore:
            def list_tenant_repository_classification_records(
                self, *, repository_id: str = "", limit: int | None = None
            ) -> tuple[TenantRepositoryClassificationRecord, ...]:
                records = (revision_2a, revision_2b, revision_1)
                return records if limit is None else records[:limit]

            def write_tenant_repository_classification_record(
                self, record: TenantRepositoryClassificationRecord
            ) -> Literal["written", "replayed"]:
                raise AssertionError("ambiguous history must not be written")

            def read_tenant_repository_classification_record(
                self, record_id: str
            ) -> TenantRepositoryClassificationRecord:
                raise KeyError(record_id)

        store = AmbiguousStore()
        read_model = get_tenant_repository_classification_read_model(
            repository_id=REPOSITORY_ID,
            store=store,
        )
        self.assertEqual(read_model.status, "ambiguous")
        self.assertIsNone(read_model.current_record)

        with self.assertRaises(TenantRepositoryClassificationConflictError):
            apply_tenant_repository_classification(
                store=store,
                record=_record(
                    revision=3,
                    supersedes_record_id=revision_2a.record_id,
                ),
                expected_current_record_id=revision_2a.record_id,
                mode="apply",
            )

    def test_require_store_helpers(self) -> None:
        class IncompleteStore:
            pass

        with self.assertRaises(TypeError):
            require_tenant_repository_classification_store(IncompleteStore())

        with self.assertRaises(TypeError):
            require_tenant_repository_classification_read_store(IncompleteStore())


def _record(
    *,
    revision: int,
    kind: str = "tenant_ui",
    supersedes_record_id: str | None = None,
) -> TenantRepositoryClassificationRecord:
    payload = {
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "product": PRODUCT,
        "context": CONTEXT,
        "classification_kind": kind,
        "classification_revision": revision,
        "classified_at": CLASSIFIED_AT,
        "source": SOURCE,
        "reason": REASON,
    }
    if supersedes_record_id is not None:
        payload["supersedes_record_id"] = supersedes_record_id
    return TenantRepositoryClassificationRecord.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
