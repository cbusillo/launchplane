import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import timedelta

from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    MergeTrainControllerReconciliationRequiredError,
    build_merge_train_controller_state_record,
)
from control_plane.merge_train_admission import (
    MergeTrainControllerStatusReadModel,
    _latest_controller_state,
    build_merge_train_controller_status_read_model,
)
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerLeaseContext,
    latest_merge_train_controller_state_record,
)
from control_plane.contracts.merge_train_admission import MergeTrainAdmissionDecision
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.storage.filesystem import (
    FilesystemRecordStore,
    _merge_train_controller_storage_id,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneMergeTrainPrFeedbackRow,
    LaunchplaneMergeTrainControllerStateRow,
    LaunchplaneMergeTrainStackCollapsePlanRow,
    PostgresRecordStore,
)
from tests.test_merge_train_controller import _stack_collapse_record
from tests.test_merge_readiness import _merge_admission
from tests.test_merge_train_quota import _failure, _FAILED_AT, _stamp
from tests.test_merge_train_scheduler import _policy_record
from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy


REPOSITORY = "Example/Merge-Train-Repo"


def _feedback() -> MergeTrainPrFeedbackRecord:
    return MergeTrainPrFeedbackRecord(
        feedback_id="legacy-feedback",
        repository=REPOSITORY,
        base_branch="main",
        pull_request_number=1,
        pull_request_url=f"https://github.com/{REPOSITORY}/pull/1",
        event="completed",
        marker="<!-- retained-feedback-marker -->",
        comment_markdown="Retained completion evidence.",
        source="test",
        recorded_at="2026-10-01T00:00:00Z",
        delivery_status="failed",
    )


def _collapse() -> MergeTrainStackCollapsePlanRecord:
    record = _stack_collapse_record(status="ready_for_train")
    return record.model_copy(
        update={
            "plan": MergeTrainStackCollapsePlan.model_validate(
                record.plan.model_dump() | {"repository": REPOSITORY}
            )
        }
    )


class RepositoryNameStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.create_store()

    def create_store(self) -> FilesystemRecordStore | PostgresRecordStore:
        return FilesystemRecordStore(self.directory)

    def test_mixed_case_writes_resolve_through_each_repository_spelling(self) -> None:
        feedback, collapse = _feedback(), _collapse()
        self.store.write_merge_train_pr_feedback_record(feedback)
        self.store.write_merge_train_stack_collapse_plan_record(collapse)
        self.assertEqual(feedback.repository, REPOSITORY.lower())
        self.assertEqual(collapse.plan.repository, REPOSITORY.lower())
        for repository in (REPOSITORY, REPOSITORY.lower(), REPOSITORY.swapcase()):
            with self.subTest(repository=repository):
                self.assertEqual(
                    self.store.list_merge_train_pr_feedback_records(repository=repository),
                    (feedback,),
                )
                self.assertEqual(
                    self.store.list_merge_train_stack_collapse_plan_records(repository=repository),
                    (collapse,),
                )
        self.assertEqual(
            self.store.list_merge_train_pr_feedback_records(repository="example/elsewhere"), ()
        )
        self.assertEqual(
            self.store.list_merge_train_stack_collapse_plan_records(
                repository=REPOSITORY, base_branch="Main"
            ),
            (),
        )

    def test_legacy_repository_lookup_preserves_stored_payloads_and_identities(self) -> None:
        feedback, collapse = self._seed_legacy()
        before = self._legacy_payloads()
        found_feedback = self.store.list_merge_train_pr_feedback_records(
            repository=REPOSITORY.lower(),
            base_branch=feedback.base_branch,
            pr_number=feedback.pull_request_number,
        )
        found_collapse = self.store.list_merge_train_stack_collapse_plan_records(
            repository=REPOSITORY.lower(),
            base_branch=collapse.plan.base_branch,
            root_pull_request_number=collapse.plan.root_pull_request_number,
            status=collapse.status,
        )
        self.assertEqual([record.feedback_id for record in found_feedback], [feedback.feedback_id])
        self.assertEqual([record.record_id for record in found_collapse], [collapse.record_id])
        self.assertEqual(found_feedback[0].marker, feedback.marker)
        self.assertEqual(found_collapse[0].plan.collapse_id, collapse.plan.collapse_id)
        self.assertEqual(self._legacy_payloads(), before)

    def test_new_status_suppresses_legacy_retry_across_repository_case(self) -> None:
        feedback, _ = self._seed_legacy()
        newer = MergeTrainPrFeedbackRecord.model_validate(
            feedback.model_dump()
            | {
                "feedback_id": "new-status",
                "repository": REPOSITORY.lower(),
                "recorded_at": "2026-10-01T00:01:00Z",
                "event": "waiting",
                "delivery_status": "delivered",
            }
        )
        other = newer.model_copy(
            update={
                "feedback_id": "independent-retry",
                "pull_request_number": 2,
                "event": "completed",
                "delivery_status": "failed",
            }
        )
        self.store.write_merge_train_pr_feedback_record(newer)
        self.store.write_merge_train_pr_feedback_record(other)
        latest = self.store.list_merge_train_pr_feedback_records(latest_per_pr=True)
        self.assertEqual(
            {record.feedback_id for record in latest}, {newer.feedback_id, other.feedback_id}
        )
        retries = self.store.list_merge_train_pr_feedback_records(
            latest_per_pr=True,
            terminal_retry_candidates=True,
            delivery_status="failed",
        )
        self.assertEqual(tuple(record.feedback_id for record in retries), (other.feedback_id,))

    def _seed_legacy(self) -> tuple[MergeTrainPrFeedbackRecord, MergeTrainStackCollapsePlanRecord]:
        feedback = _feedback().model_copy(update={"repository": REPOSITORY})
        collapse = _collapse()
        collapse = collapse.model_copy(
            update={"plan": collapse.plan.model_copy(update={"repository": REPOSITORY})}
        )
        if isinstance(self.store, FilesystemRecordStore):
            self.legacy_paths = (
                self.store._write_model(
                    "launchplane_merge_train_pr_feedback", feedback.feedback_id, feedback
                ),
                self.store._write_model(
                    "launchplane_merge_train_stack_collapse_plans", collapse.record_id, collapse
                ),
            )
        else:
            with self.store._session_factory() as session, session.begin():
                session.add(
                    LaunchplaneMergeTrainPrFeedbackRow(
                        **{
                            name: getattr(feedback, name)
                            for name in (
                                "feedback_id",
                                "repository",
                                "base_branch",
                                "pull_request_number",
                                "event",
                                "delivery_status",
                                "recorded_at",
                            )
                        },
                        payload=feedback.model_dump(mode="json"),
                    )
                )
                session.add(
                    LaunchplaneMergeTrainStackCollapsePlanRow(
                        record_id=collapse.record_id,
                        status=collapse.status,
                        source=collapse.source,
                        updated_at=collapse.updated_at,
                        repository=collapse.plan.repository,
                        base_branch=collapse.plan.base_branch,
                        collapse_id=collapse.plan.collapse_id,
                        root_pull_request_number=collapse.plan.root_pull_request_number,
                        plan_status=collapse.plan.status,
                        payload=collapse.model_dump(mode="json"),
                    )
                )
        return feedback, collapse

    def _legacy_payloads(self) -> tuple[object, object]:
        if isinstance(self.store, FilesystemRecordStore):
            return json.loads(self.legacy_paths[0].read_text()), json.loads(
                self.legacy_paths[1].read_text()
            )
        with self.store._session_factory() as session:
            feedback = session.get(LaunchplaneMergeTrainPrFeedbackRow, _feedback().feedback_id)
            collapse = session.get(LaunchplaneMergeTrainStackCollapsePlanRow, _collapse().record_id)
            assert feedback is not None and collapse is not None
            return feedback.payload, collapse.payload

    def _seed_controller_alias(
        self, *, repository: str = REPOSITORY, base_branch: str = "Release", **changes: object
    ) -> MergeTrainControllerStateRecord:
        record = build_merge_train_controller_state_record(
            repository=repository,
            base_branch=base_branch,
            policy_key=f"{repository.lower()}:{base_branch}",
            policy_sha256="a" * 64,
            updated_at="2026-09-29T00:00:00Z",
        ).model_copy(
            update={
                "repository": repository,
                "controller_key": f"merge-train-controller:{repository.replace('/', ':')}:{base_branch}",
                **changes,
            }
        )
        if isinstance(self.store, FilesystemRecordStore):
            self.store._write_model(
                "launchplane_merge_train_controller_states",
                _merge_train_controller_storage_id(record.controller_key),
                record,
            )
        else:
            with self.store._session_factory() as session, session.begin():
                session.merge(
                    LaunchplaneMergeTrainControllerStateRow(
                        **{
                            name: getattr(record, name)
                            for name in (
                                "controller_key",
                                "repository",
                                "base_branch",
                                "status",
                                "policy_key",
                                "policy_sha256",
                                "updated_at",
                                "lease_owner",
                                "lease_expires_at",
                                "active_action",
                                "active_phase",
                            )
                        },
                        payload=record.model_dump(mode="json"),
                    )
                )
        return record

    def test_controller_history_keeps_its_key_and_remains_readable_in_status(self) -> None:
        legacy = self._seed_controller_alias()
        canonical = build_merge_train_controller_state_record(
            repository=REPOSITORY,
            base_branch=legacy.base_branch,
            policy_key=legacy.policy_key,
            policy_sha256=legacy.policy_sha256,
            updated_at="2026-10-01T00:00:00Z",
        )
        self.store.write_merge_train_controller_state_record(canonical)
        for repository in (REPOSITORY, REPOSITORY.lower(), REPOSITORY.swapcase()):
            found = self.store.list_merge_train_controller_state_records(
                repository=repository, base_branch=legacy.base_branch
            )
            self.assertEqual(
                {item.controller_key for item in found},
                {canonical.controller_key},
            )
            self.assertEqual(
                self.store.list_merge_train_controller_state_alias_records(
                    repository=repository,
                    base_branch=legacy.base_branch,
                ),
                (legacy,),
            )
        observed = self.store.read_merge_train_controller_state_record(legacy.controller_key)
        self.assertEqual(observed.model_dump(), legacy.model_dump())
        status = MergeTrainControllerStatusReadModel(
            repository=REPOSITORY,
            base_branch=legacy.base_branch,
            generated_at=canonical.updated_at,
            admission=MergeTrainAdmissionDecision(
                repository=REPOSITORY,
                base_branch=legacy.base_branch,
                status="admitted",
                reason_code="no_prior_run",
                requested_at=canonical.updated_at,
                next_allowed_at=canonical.updated_at,
                detail="Synthetic controller history read.",
            ),
            controller_state=observed,
            controller_records=(),
        )
        self.assertEqual(status.controller_state, observed)
        self.assertEqual(
            self.store.list_merge_train_controller_state_records(
                repository=REPOSITORY, base_branch=legacy.base_branch.swapcase()
            ),
            (),
        )

    def test_historical_controller_identity_cannot_be_written_as_mutation_authority(self) -> None:
        legacy = self._seed_controller_alias()
        with self.assertRaises(ValueError):
            self.store.write_merge_train_controller_state_record(legacy)
        with self.assertRaises(ValueError):
            self.store.compare_and_set_merge_train_controller_state_record(
                record=legacy,
                expected_lease_owner="",
                expected_lease_acquired_at="",
                lease_seconds=60,
            )
        self.assertEqual(
            self.store.read_merge_train_controller_state_record(legacy.controller_key), legacy
        )

    def _acquire_controller(
        self, *, base_branch: str = "Release"
    ) -> MergeTrainControllerStateRecord:
        return self.store.acquire_merge_train_controller_state_record(
            repository=REPOSITORY.lower(),
            base_branch=base_branch,
            policy_key=f"{REPOSITORY.lower()}:{base_branch}",
            policy_sha256="a" * 64,
            lease_owner="canonical-owner",
            lease_seconds=60,
            initial_active_action="merge_train_controller_run_once",
            initial_active_phase="select_next_action",
            adoptable_active_actions=("merge_train_controller_run_once",),
        )

    def test_canonical_lease_and_cas_leave_inert_historical_identity_unchanged(self) -> None:
        legacy = self._seed_controller_alias()
        leased = self._acquire_controller()
        checkpoint = self.store.compare_and_set_merge_train_controller_state_record(
            record=leased.model_copy(update={"active_record_id": "selected-candidate"}),
            expected_lease_owner=leased.lease_owner,
            expected_lease_acquired_at=leased.lease_acquired_at,
            lease_seconds=60,
        )
        self.assertEqual(checkpoint.active_record_id, "selected-candidate")
        self.assertEqual(
            self.store.read_merge_train_controller_state_record(legacy.controller_key), legacy
        )

    def test_bound_alias_blocks_canonical_cas_without_releasing_its_lease(self) -> None:
        leased = self._acquire_controller()
        self._seed_controller_alias(
            ordinary_job_binding=OrdinaryAgentJobBinding(
                request_id="retained-request",
                scope_sha256="b" * 64,
                binding_revision=1,
            )
        )
        with self.assertRaises(MergeTrainControllerReconciliationRequiredError):
            self.store.compare_and_set_merge_train_controller_state_record(
                record=leased,
                expected_lease_owner=leased.lease_owner,
                expected_lease_acquired_at=leased.lease_acquired_at,
                lease_seconds=60,
            )
        self.assertEqual(
            self.store.read_merge_train_controller_state_record(leased.controller_key), leased
        )

    def test_alias_progress_and_reconciliation_cannot_hide_behind_idle_status(self) -> None:
        for changes in (
            {"active_record_id": "pending-provider-effect"},
            {"active_pull_request_number": 1},
            {"step_payload": {"expected_effect_sha": "retained-effect"}},
            {"reconciliation_status": "adopted"},
            {"reconciliation_detail": "unresolved historical observation"},
        ):
            with self.subTest(changes=changes):
                self._seed_controller_alias(**changes)
                with self.assertRaises(MergeTrainControllerReconciliationRequiredError):
                    self._acquire_controller()

    def test_bound_controller_alias_still_blocks_an_existing_guarded_admission(self) -> None:
        admission = _merge_admission()
        policy_sha256 = admission.readiness.policy.fingerprints.merge_train.current_sha256
        assert policy_sha256 is not None
        controller = MergeTrainControllerStateRecord(
            controller_key=admission.controller_key,
            repository=admission.repository,
            base_branch=admission.base_branch,
            policy_key=f"{admission.repository}:{admission.base_branch}",
            policy_sha256=policy_sha256,
            status="running",
            updated_at=admission.created_at,
            lease_owner=admission.lease_owner,
            lease_acquired_at=admission.lease_acquired_at,
            lease_expires_at=admission.lease_expires_at,
            heartbeat_at=admission.lease_acquired_at,
            active_action="land_batch",
            active_phase="merge_batch_entries",
            active_pull_request_number=admission.pull_request_number,
            step_payload={
                "expected_effect_sha": admission.expected_effect_sha,
                "landing_plan_id": admission.landing_plan_id,
            },
        )
        self.store.write_merge_train_controller_state_record(controller)
        self._seed_controller_alias(
            repository=admission.repository.swapcase(), base_branch=admission.base_branch
        )
        stored, created = self.store.create_guarded_merge_admission_record_if_absent(
            admission, admitted_at=admission.created_at
        )
        self.assertTrue(created)
        self._seed_controller_alias(
            repository=admission.repository.swapcase(),
            base_branch=admission.base_branch,
            ordinary_job_binding=OrdinaryAgentJobBinding(
                request_id="retained-request",
                scope_sha256="b" * 64,
                binding_revision=1,
            ),
        )
        with self.assertRaises(MergeTrainControllerReconciliationRequiredError):
            self.store.create_guarded_merge_admission_record_if_absent(
                admission, admitted_at=admission.created_at
            )
        self.assertEqual(self.store.list_merge_admission_records(), (stored,))

    def test_current_readers_never_select_newer_history_as_the_canonical_controller(self) -> None:
        legacy = self._seed_controller_alias()
        for present in (False, True):
            canonical = build_merge_train_controller_state_record(
                repository=REPOSITORY,
                base_branch=legacy.base_branch,
                policy_key=legacy.policy_key,
                policy_sha256=legacy.policy_sha256,
                updated_at="2026-09-28T00:00:00Z",
            )
            if present:
                self.store.write_merge_train_controller_state_record(canonical)
            expected = canonical if present else None
            self.assertEqual(
                _latest_controller_state(
                    store=self.store,
                    repository=REPOSITORY,
                    base_branch=legacy.base_branch,
                ),
                expected,
            )
            self.assertEqual(
                latest_merge_train_controller_state_record(
                    record_store=self.store,
                    repository=REPOSITORY,
                    base_branch=legacy.base_branch,
                ),
                expected,
            )
            if present:
                self.assertEqual(
                    MergeTrainControllerLeaseContext(
                        record=canonical,
                        record_store=self.store,
                    ).read_current(),
                    canonical,
                )

    def test_controller_alias_fence_does_not_change_progress_record_write_contract(self) -> None:
        progress = _collapse()
        self._seed_controller_alias(
            base_branch=progress.plan.base_branch,
            ordinary_job_binding=OrdinaryAgentJobBinding(
                request_id="retained-request",
                scope_sha256="d" * 64,
                binding_revision=1,
            ),
        )
        self.store.write_merge_train_stack_collapse_plan_record(progress)
        self.assertEqual(
            self.store.list_merge_train_stack_collapse_plan_records(
                repository=REPOSITORY,
                base_branch=progress.plan.base_branch,
            ),
            (progress,),
        )

    def test_peer_quota_evidence_survives_newer_inert_or_unresolved_aliases(self) -> None:
        policy = _policy_record(
            *(
                (repository, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
                for repository in ("cbusillo/alpha", "cbusillo/beta")
            )
        )
        canonical = _failure(
            "cbusillo/alpha", "retryable:github_rate_limited; retry_after_seconds:600"
        ).model_copy(update={"policy_sha256": policy.policy_sha256})
        self.store.write_merge_train_controller_state_record(canonical)
        for unresolved in (False, True):
            self._seed_controller_alias(
                repository="CBusillo/Alpha",
                base_branch="main",
                updated_at=_stamp(_FAILED_AT + timedelta(seconds=1)),
                active_action="land_batch" if unresolved else "",
                active_phase="merge_batch_entries" if unresolved else "",
            )
            model = build_merge_train_controller_status_read_model(
                store=self.store,
                repository="cbusillo/beta",
                base_branch="main",
                generated_at=_stamp(_FAILED_AT + timedelta(seconds=2)),
                policy_record=policy,
            )
            self.assertFalse(model.admission.admitted)
            self.assertEqual(model.admission.reason_code, "github_rate_limit_pending")
            self.assertEqual(
                self.store.read_merge_train_controller_state_record(canonical.controller_key),
                canonical,
            )

    def test_status_reports_alias_blocker_without_rewriting_current_state(self) -> None:
        legacy = self._seed_controller_alias()
        canonical = build_merge_train_controller_state_record(
            repository=REPOSITORY,
            base_branch=legacy.base_branch,
            policy_key=legacy.policy_key,
            policy_sha256=legacy.policy_sha256,
            updated_at="2026-10-01T00:00:00Z",
        )
        self.store.write_merge_train_controller_state_record(canonical)
        before = build_merge_train_controller_status_read_model(
            store=self.store,
            repository=REPOSITORY,
            base_branch=legacy.base_branch,
            generated_at=canonical.updated_at,
        )
        self._seed_controller_alias(active_action="land_batch", active_phase="merge_batch_entries")
        status = build_merge_train_controller_status_read_model(
            store=self.store,
            repository=REPOSITORY,
            base_branch=legacy.base_branch,
            generated_at=canonical.updated_at,
        )
        self.assertEqual(status.controller_state, canonical)
        self.assertEqual(status.admission, before.admission)
        self.assertIsNotNone(status.controller_diagnostics)
        assert status.controller_diagnostics is not None
        self.assertEqual(status.controller_diagnostics.reconciliation_status, "required")
        self.assertTrue(status.controller_diagnostics.reconciliation_detail)
        self.assertEqual(
            self.store.read_merge_train_controller_state_record(canonical.controller_key), canonical
        )

    def test_expired_historical_controller_still_fences_canonical_acquisition(self) -> None:
        legacy = self._seed_controller_alias(
            status="running",
            lease_owner="historical-owner",
            lease_acquired_at="2026-09-29T00:00:00Z",
            lease_expires_at="2026-09-29T00:01:00Z",
            heartbeat_at="2026-09-29T00:00:00Z",
            active_action="merge_train_controller_run_once",
            active_phase="select_next_action",
        )
        with self.assertRaises(MergeTrainControllerReconciliationRequiredError):
            self.store.acquire_merge_train_controller_state_record(
                repository=REPOSITORY.lower(),
                base_branch=legacy.base_branch,
                policy_key=legacy.policy_key,
                policy_sha256=legacy.policy_sha256,
                lease_owner="canonical-owner",
                lease_seconds=60,
                initial_active_action="merge_train_controller_run_once",
                initial_active_phase="select_next_action",
                adoptable_active_actions=("merge_train_controller_run_once",),
            )


class RepositoryNameSQLStorageTests(RepositoryNameStorageTests):
    def test_native_write_refuses_legacy_bound_stack_across_repository_case(self) -> None:
        _, collapse = self._seed_legacy()
        assert isinstance(self.store, PostgresRecordStore)
        with self.store._session_factory() as session, session.begin():
            row = session.get(LaunchplaneMergeTrainStackCollapsePlanRow, collapse.record_id)
            assert row is not None
            row.payload = row.payload | {
                "ordinary_job_binding": {
                    "request_id": "retained-request",
                    "scope_sha256": "a" * 64,
                    "binding_revision": 1,
                }
            }
        before = self._legacy_payloads()
        for repository in (REPOSITORY.lower(), REPOSITORY.swapcase()):
            self.assertTrue(
                self.store.has_ordinary_merge_train_target_fence(
                    repository=repository,
                    base_branch=collapse.plan.base_branch,
                )
            )
        self.assertFalse(
            self.store.has_ordinary_merge_train_target_fence(
                repository=REPOSITORY,
                base_branch=collapse.plan.base_branch.swapcase(),
            )
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "ordinary_progress_requires_joined_write"
        ):
            self.store.write_merge_train_stack_collapse_plan_record(
                _collapse().model_copy(update={"record_id": "native-successor"})
            )
        self.assertEqual(self._legacy_payloads(), before)
        self.assertEqual(
            [
                record.record_id
                for record in self.store.list_merge_train_stack_collapse_plan_records()
            ],
            [collapse.record_id],
        )

    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.directory / 'records.sqlite3'}"

    def create_store(self) -> PostgresRecordStore:
        store = PostgresRecordStore(database_url=self.database_url())
        self.addCleanup(store.close)
        store.ensure_schema()
        return store
