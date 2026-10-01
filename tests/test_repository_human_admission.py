import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.repository_human_admission import (
    RepositoryHumanManagerDelegation,
    RepositoryHumanRolePolicyStatus,
    RepositoryHumanRolePolicyRecord,
    repository_human_role_policy_digest,
)
from control_plane.repository_human_admission import (
    RepositoryHumanRolePolicyConflictError,
    RepositoryHumanRolePolicySequenceError,
    apply_repository_human_role_policy,
    get_repository_human_role_policy_read_model,
    plan_repository_human_role_policy_apply,
    plan_repository_human_role_policy_append,
)
from control_plane.storage.filesystem import FilesystemRecordStore


PRODUCT = "launchplane"
CONTEXT = "production"
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/tenant-site"


class RepositoryHumanAdmissionTests(unittest.TestCase):
    def test_role_policy_digest_ignores_lifecycle_status(self) -> None:
        active = _role_policy(repository_owner_ids=(301,))
        superseded = _role_policy(
            repository_owner_ids=(301,),
            status="superseded",
        )

        self.assertEqual(active.record_id, superseded.record_id)
        self.assertEqual(active.role_policy_digest, superseded.role_policy_digest)
        self.assertEqual(
            repository_human_role_policy_digest(superseded),
            active.role_policy_digest,
        )

    def test_role_policy_append_plan_supersedes_current_without_digest_churn(
        self,
    ) -> None:
        revision_1 = _role_policy(repository_owner_ids=(301,))
        revision_2 = _role_policy(
            repository_owner_ids=(302,),
            revision=2,
            supersedes_record_id=revision_1.record_id,
        )

        plan = plan_repository_human_role_policy_append(
            records=(revision_1,),
            record=revision_2,
        )

        self.assertEqual(plan.status, "written")
        self.assertEqual(plan.current_record, revision_1)
        self.assertIsNotNone(plan.superseded_current_record)
        superseded_current = plan.superseded_current_record
        assert superseded_current is not None
        self.assertEqual(superseded_current.status, "superseded")
        self.assertEqual(superseded_current.record_id, revision_1.record_id)
        self.assertEqual(
            superseded_current.role_policy_digest,
            revision_1.role_policy_digest,
        )

        replay = plan_repository_human_role_policy_append(
            records=(superseded_current, revision_2),
            record=revision_2,
        )
        self.assertEqual(replay.status, "replayed")

        historical_replay = plan_repository_human_role_policy_append(
            records=(superseded_current, revision_2),
            record=revision_1,
        )
        self.assertEqual(historical_replay.status, "replayed")

        with self.assertRaises(RepositoryHumanRolePolicyConflictError):
            plan_repository_human_role_policy_append(
                records=(superseded_current, revision_2),
                record=_role_policy(
                    repository_owner_ids=(301,),
                    reason="different historical payload",
                ),
            )

        with self.assertRaises(RepositoryHumanRolePolicyConflictError):
            plan_repository_human_role_policy_append(
                records=(revision_1,),
                record=revision_1.model_copy(update={"status": "superseded"}),
            )

    def test_role_policy_current_read_model_is_scope_bound_and_ambiguous_safe(
        self,
    ) -> None:
        class AmbiguousReadStore:
            def list_repository_human_role_policy_records(
                self,
                *,
                repository_id: str = "",
                repository_owner_id: str = "",
                repository: str = "",
                product: str = "",
                context: str = "",
                status: str = "",
                limit: int | None = None,
            ) -> tuple[RepositoryHumanRolePolicyRecord, ...]:
                del repository_id, repository_owner_id, repository, product, context, status, limit
                revision_1 = _role_policy(repository_owner_ids=(301,))
                revision_2 = _role_policy(
                    repository_owner_ids=(302,),
                    revision=2,
                    supersedes_record_id=revision_1.record_id,
                )
                return (revision_1, revision_2)

        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            missing = get_repository_human_role_policy_read_model(
                repository_id=REPOSITORY_ID,
                product=PRODUCT,
                context=CONTEXT,
                store=store,
            )
            revision_1 = _role_policy(repository_owner_ids=(301,))
            revision_2 = _role_policy(
                repository_owner_ids=(302,),
                revision=2,
                supersedes_record_id=revision_1.record_id,
            )
            store.write_repository_human_role_policy_record(revision_1)
            store.write_repository_human_role_policy_record(revision_2)
            available = get_repository_human_role_policy_read_model(
                repository_id=REPOSITORY_ID,
                product=PRODUCT,
                context=CONTEXT,
                store=store,
            )

        ambiguous = get_repository_human_role_policy_read_model(
            repository_id=REPOSITORY_ID,
            product=PRODUCT,
            context=CONTEXT,
            store=AmbiguousReadStore(),
        )

        self.assertEqual(missing.status, "missing")
        self.assertIsNone(missing.current_record)
        self.assertEqual(available.status, "available")
        self.assertEqual(available.current_record, revision_2)
        self.assertEqual(available.history_count, 2)
        self.assertEqual(ambiguous.status, "ambiguous")
        self.assertIsNone(ambiguous.current_record)

    def test_role_policy_apply_requires_explicit_expected_tip_id_and_digest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            revision_1 = _role_policy(repository_owner_ids=(301,))
            revision_2 = _role_policy(
                repository_owner_ids=(302,),
                revision=2,
                supersedes_record_id=revision_1.record_id,
            )

            applied_1 = apply_repository_human_role_policy(
                store=store,
                record=revision_1,
                mode="apply",
            )

            with self.assertRaisesRegex(ValueError, "expected current record ID and digest"):
                apply_repository_human_role_policy(
                    store=store,
                    record=revision_2,
                    expected_current_record_id=revision_1.record_id,
                    mode="apply",
                )
            with self.assertRaises(RepositoryHumanRolePolicyConflictError):
                apply_repository_human_role_policy(
                    store=store,
                    record=revision_2,
                    expected_current_record_id="wrong-record-id",
                    expected_current_role_policy_digest=revision_1.role_policy_digest,
                    mode="apply",
                )
            with self.assertRaises(RepositoryHumanRolePolicyConflictError):
                apply_repository_human_role_policy(
                    store=store,
                    record=revision_2,
                    expected_current_record_id=revision_1.record_id,
                    expected_current_role_policy_digest="f" * 64,
                    mode="apply",
                )

            applied_2 = apply_repository_human_role_policy(
                store=store,
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
                mode="apply",
            )
            replay_2 = apply_repository_human_role_policy(
                store=store,
                record=revision_2,
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
                mode="apply",
            )

        self.assertEqual(applied_1.status, "applied")
        self.assertEqual(applied_2.status, "applied")
        self.assertEqual(replay_2.status, "replayed")

    def test_role_policy_apply_rejects_inactive_candidate_and_sequence_gaps(
        self,
    ) -> None:
        revision_1 = _role_policy(repository_owner_ids=(301,))
        revision_2 = _role_policy(
            repository_owner_ids=(302,),
            revision=2,
            supersedes_record_id=revision_1.record_id,
        )
        with self.assertRaisesRegex(ValueError, "active candidate"):
            plan_repository_human_role_policy_apply(
                records=(),
                record=revision_1.model_copy(update={"status": "superseded"}),
                expected_current_record_id="",
                expected_current_role_policy_digest="",
            )
        with self.assertRaises(RepositoryHumanRolePolicySequenceError):
            plan_repository_human_role_policy_apply(
                records=(revision_1,),
                record=revision_2.model_copy(update={"role_policy_revision": 3}),
                expected_current_record_id=revision_1.record_id,
                expected_current_role_policy_digest=revision_1.role_policy_digest,
            )


def _role_policy(
    *,
    repository_owner_ids: tuple[int, ...],
    manager_primary_ids: tuple[int, ...] = (501,),
    manager_backup_ids: tuple[int, ...] = (),
    manager_delegations: tuple[RepositoryHumanManagerDelegation, ...] = (),
    status: RepositoryHumanRolePolicyStatus = "active",
    revision: int = 1,
    effective_at: str = "2026-07-31T11:00:00Z",
    supersedes_record_id: str | None = None,
    reason: str = "test role policy",
) -> RepositoryHumanRolePolicyRecord:
    return RepositoryHumanRolePolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        product=PRODUCT,
        context=CONTEXT,
        status=status,
        role_policy_revision=revision,
        repository_owner_github_ids=repository_owner_ids,
        manager_primary_github_ids=manager_primary_ids,
        manager_backup_github_ids=manager_backup_ids,
        manager_delegations=manager_delegations,
        effective_at=effective_at,
        source="test:role-policy",
        reason=reason,
        supersedes_record_id=supersedes_record_id,
    )


if __name__ == "__main__":
    unittest.main()
