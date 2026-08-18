from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerAdoptionRejectedError,
)
from control_plane.merge_train_controller_run_once import (
    MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
    MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
    MergeTrainControllerLeaseContext,
    merge_train_controller_mutation_fence,
)
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerRunOnceEnvelope,
    execute_tenant_admission_controller_run_once,
)
from control_plane.tenant_admission_status import get_tenant_admission_status
from tests.test_tenant_admission_status import (
    EVALUATED_AT,
    _candidate,
    _classification,
    _classification_only_store,
    _status_with_path_states,
)


BASE_SHA = "b" * 40
MERGE_COMMIT_SHA = "c" * 40


class TenantAdmissionControllerTests(unittest.TestCase):
    def test_each_tenant_admission_path_can_be_ready(self) -> None:
        cases = (
            _status_with_path_states(
                trusted_state="satisfied",
                waiver_state="pending",
                manager_state="pending",
            ),
            _status_with_path_states(
                trusted_state="pending",
                waiver_state="satisfied",
                manager_state="pending",
            ),
            _status_with_path_states(
                trusted_state="pending",
                waiver_state="pending",
                manager_state="satisfied",
            ),
        )
        for admission in cases:
            with self.subTest(category=admission.category), TemporaryDirectory() as temporary_name:
                transport = _TenantControllerTransport()
                with patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ):
                    result = execute_tenant_admission_controller_run_once(
                        request=_request(mutate=False),
                        store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                        token="token",
                        trace_id="trace-ready",
                        transport_factory=lambda _token: transport,
                    )

                self.assertEqual(result.outcome, "ready")
                checks = result.technical_checks
                assert checks is not None
                self.assertEqual(checks.status, "pass")
                self.assertFalse(transport.merge_called)

    def test_launchplane_advisory_checks_are_excluded_from_technical_inputs(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="pending",
            manager_state="satisfied",
        )
        with TemporaryDirectory() as temporary_name, TemporaryDirectory() as baseline_name:
            transport = _TenantControllerTransport(include_launchplane_projection_signals=True)
            baseline_transport = _TenantControllerTransport()
            with (
                patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ),
                patch(
                    "control_plane.tenant_admission_controller.utc_now_timestamp",
                    return_value=EVALUATED_AT,
                ),
            ):
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=False),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-advisory-exclusion",
                    transport_factory=lambda _token: transport,
                )
                baseline = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=False),
                    store=FilesystemRecordStore(state_dir=Path(baseline_name)),
                    token="token",
                    trace_id="trace-advisory-baseline",
                    transport_factory=lambda _token: baseline_transport,
                )

        self.assertEqual(result.outcome, baseline.outcome)
        assert result.technical_checks is not None
        self.assertEqual(result.technical_checks, baseline.technical_checks)

    def test_mutate_merges_exact_head_and_excludes_admission_statuses(self) -> None:
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            transport = _TenantControllerTransport()
            admission = _status_with_path_states(
                trusted_state="pending",
                waiver_state="pending",
                manager_state="satisfied",
            )
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ) as status_read:
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=store,
                    token="token",
                    trace_id="trace-merge",
                    transport_factory=lambda _token: transport,
                )

            self.assertEqual(result.outcome, "merged")
            self.assertTrue(result.mutated)
            self.assertEqual(result.merge_commit_sha, MERGE_COMMIT_SHA)
            self.assertEqual(status_read.call_count, 2)
            self.assertTrue(transport.merge_called)
            checks = result.technical_checks
            assert checks is not None
            self.assertEqual(
                {signal.name for signal in checks.signals},
                {"ci/build", "unit-tests"},
            )
            controller_state = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]
            self.assertEqual(controller_state.status, "idle")
            self.assertEqual(controller_state.last_action, "tenant_admission_merge")

    def test_engineering_repository_keeps_existing_flow(self) -> None:
        candidate = _candidate()
        admission = get_tenant_admission_status(
            store=_classification_only_store((_classification(kind="engineering"),)),
            candidate=candidate,
            evaluated_at=EVALUATED_AT,
        )
        with TemporaryDirectory() as temporary_name:
            transport = _TenantControllerTransport()
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-engineering",
                    transport_factory=lambda _token: transport,
                )

        self.assertEqual(result.outcome, "not_applicable")
        self.assertFalse(transport.merge_called)
        self.assertFalse(transport.technical_checks_read)

    def test_pending_tenant_admission_still_reports_technical_readiness(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="pending",
            manager_state="pending",
        )
        with TemporaryDirectory() as temporary_name:
            transport = _TenantControllerTransport()
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=False),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-pending-admission",
                    transport_factory=lambda _token: transport,
                )

        self.assertEqual(result.outcome, "blocked")
        self.assertTrue(transport.technical_checks_read)
        self.assertIsNotNone(result.technical_checks)
        assert result.technical_checks is not None
        self.assertEqual(result.technical_checks.status, "pass")
        self.assertIn("Tenant admission is pending", result.detail)

    def test_failed_or_missing_technical_checks_block_merge(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="satisfied",
            manager_state="pending",
        )
        cases = (
            _TenantControllerTransport(technical_state="failure"),
            _TenantControllerTransport(technical_state="pending"),
            _TenantControllerTransport(technical_state="unknown"),
            _TenantControllerTransport(include_technical_signals=False),
            _TenantControllerTransport(include_required_checks=False),
            _TenantControllerTransport(check_run_app_id=999),
            _TenantControllerTransport(
                include_technical_signals=False,
                include_optional_signal=True,
            ),
        )
        for transport in cases:
            with self.subTest(transport=transport), TemporaryDirectory() as temporary_name:
                with patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ):
                    result = execute_tenant_admission_controller_run_once(
                        request=_request(mutate=True),
                        store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                        token="token",
                        trace_id="trace-blocked-checks",
                        transport_factory=lambda _token: transport,
                    )

                self.assertEqual(result.outcome, "blocked")
                self.assertFalse(transport.merge_called)
                checks = result.technical_checks
                assert checks is not None
                self.assertIn(checks.status, {"fail", "pending", "unavailable"})

    def test_optional_failed_check_does_not_replace_required_gate_policy(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="satisfied",
            manager_state="pending",
        )
        with TemporaryDirectory() as temporary_name:
            transport = _TenantControllerTransport(
                include_optional_signal=True,
                optional_signal_state="failure",
            )
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=False),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-optional-check",
                    transport_factory=lambda _token: transport,
                )

        checks = result.technical_checks
        assert checks is not None
        self.assertEqual(result.outcome, "ready")
        self.assertEqual(checks.status, "pass")
        self.assertEqual(
            {required.name for required in checks.required_checks},
            {"ci/build", "unit-tests"},
        )

    def test_draft_or_unmergeable_pull_request_blocks_before_checks(self) -> None:
        admission = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="pending",
            manager_state="pending",
        )
        cases = (
            _pull_request_payload(draft=True),
            _pull_request_payload(mergeable=False),
            _pull_request_payload(mergeable=None),
        )
        for pull_request_payload in cases:
            with self.subTest(payload=pull_request_payload), TemporaryDirectory() as temporary_name:
                transport = _TenantControllerTransport(
                    pull_request_payloads=(pull_request_payload,)
                )
                with patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ):
                    result = execute_tenant_admission_controller_run_once(
                        request=_request(mutate=True),
                        store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                        token="token",
                        trace_id="trace-unmergeable",
                        transport_factory=lambda _token: transport,
                    )

                self.assertEqual(result.outcome, "blocked")
                self.assertFalse(transport.technical_checks_read)
                self.assertFalse(transport.merge_called)

    def test_missing_or_malformed_draft_and_merged_evidence_fails_closed(self) -> None:
        admission = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="pending",
            manager_state="pending",
        )
        cases: list[dict[str, object]] = []
        for field_name, malformed_value in (("draft", "false"), ("merged", "false")):
            missing_payload = _pull_request_payload()
            missing_payload.pop(field_name)
            cases.append(missing_payload)
            malformed_payload = _pull_request_payload()
            malformed_payload[field_name] = malformed_value
            cases.append(malformed_payload)
        for pull_request_payload in cases:
            with self.subTest(payload=pull_request_payload), TemporaryDirectory() as temporary_name:
                transport = _TenantControllerTransport(
                    pull_request_payloads=(pull_request_payload,)
                )
                with (
                    patch(
                        "control_plane.tenant_admission_controller.get_tenant_admission_status",
                        return_value=admission,
                    ),
                    self.assertRaisesRegex(ValueError, "must be boolean"),
                ):
                    execute_tenant_admission_controller_run_once(
                        request=_request(mutate=True),
                        store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                        token="token",
                        trace_id="trace-malformed-pr",
                        transport_factory=lambda _token: transport,
                    )

                self.assertFalse(transport.merge_called)

    def test_strict_required_checks_require_head_to_contain_current_base(self) -> None:
        admission = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="pending",
            manager_state="pending",
        )
        with TemporaryDirectory() as temporary_name:
            strict_transport = _TenantControllerTransport(head_contains_base=False)
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                blocked = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-behind-base",
                    transport_factory=lambda _token: strict_transport,
                )

        strict_checks = blocked.technical_checks
        assert strict_checks is not None
        self.assertEqual(blocked.outcome, "blocked")
        self.assertEqual(strict_checks.status, "fail")
        self.assertFalse(strict_checks.base_up_to_date)
        self.assertFalse(strict_transport.merge_called)

        with TemporaryDirectory() as temporary_name:
            non_strict_transport = _TenantControllerTransport(
                strict_required_checks=False,
                head_contains_base=False,
            )
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                ready = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=False),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-nonstrict-base",
                    transport_factory=lambda _token: non_strict_transport,
                )

        non_strict_checks = ready.technical_checks
        assert non_strict_checks is not None
        self.assertEqual(ready.outcome, "ready")
        self.assertFalse(non_strict_checks.strict)
        self.assertIsNone(non_strict_checks.base_up_to_date)

    def test_missing_or_malformed_required_check_strict_policy_fails_closed(self) -> None:
        admission = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="pending",
            manager_state="pending",
        )
        for strict_policy_value in (None, "true"):
            with self.subTest(strict=strict_policy_value), TemporaryDirectory() as temporary_name:
                transport = _TenantControllerTransport(
                    strict_required_checks=strict_policy_value,
                )
                with (
                    patch(
                        "control_plane.tenant_admission_controller.get_tenant_admission_status",
                        return_value=admission,
                    ),
                    self.assertRaisesRegex(ValueError, "strict must be boolean"),
                ):
                    execute_tenant_admission_controller_run_once(
                        request=_request(mutate=True),
                        store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                        token="token",
                        trace_id="trace-malformed-strict",
                        transport_factory=lambda _token: transport,
                    )

                self.assertFalse(transport.merge_called)

    def test_same_repository_head_identity_is_required(self) -> None:
        candidate = _candidate()
        admission = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="pending",
            manager_state="pending",
        )
        transport = _TenantControllerTransport(
            pull_request_payloads=(
                _pull_request_payload(head_repository_id=int(candidate.repository_id) + 1),
            )
        )
        with TemporaryDirectory() as temporary_name:
            with (
                patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ),
                self.assertRaisesRegex(ValueError, "identity changed"),
            ):
                execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-fork",
                    transport_factory=lambda _token: transport,
                )

        self.assertFalse(transport.merge_called)

    def test_admission_revoked_between_rechecks_blocks_merge(self) -> None:
        admitted = _status_with_path_states(
            trusted_state="pending",
            waiver_state="satisfied",
            manager_state="pending",
        )
        blocked = _status_with_path_states(
            trusted_state="pending",
            waiver_state="pending",
            manager_state="pending",
        )
        with TemporaryDirectory() as temporary_name:
            transport = _TenantControllerTransport()
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                side_effect=(admitted, blocked),
            ):
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-revoked",
                    transport_factory=lambda _token: transport,
                )

        self.assertEqual(result.outcome, "blocked")
        self.assertFalse(transport.merge_called)

    def test_required_check_policy_drift_between_rechecks_blocks_merge(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="satisfied",
            manager_state="pending",
        )
        with TemporaryDirectory() as temporary_name:
            transport = _TenantControllerTransport(required_check_app_ids=(15368, 999))
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-check-policy-drift",
                    transport_factory=lambda _token: transport,
                )

        self.assertEqual(result.outcome, "blocked")
        self.assertFalse(transport.merge_called)

    def test_technical_evidence_must_match_exact_head(self) -> None:
        admission = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="pending",
            manager_state="pending",
        )
        cases = (
            _TenantControllerTransport(status_response_sha="d" * 40),
            _TenantControllerTransport(check_run_head_sha="d" * 40),
        )
        for transport in cases:
            with self.subTest(transport=transport), TemporaryDirectory() as temporary_name:
                with (
                    patch(
                        "control_plane.tenant_admission_controller.get_tenant_admission_status",
                        return_value=admission,
                    ),
                    self.assertRaisesRegex(ValueError, "expected head"),
                ):
                    execute_tenant_admission_controller_run_once(
                        request=_request(mutate=True),
                        store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                        token="token",
                        trace_id="trace-stale-evidence",
                        transport_factory=lambda _token: transport,
                    )

                self.assertFalse(transport.merge_called)

    def test_head_move_between_rechecks_fails_before_merge(self) -> None:
        admission = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="pending",
            manager_state="pending",
        )
        transport = _TenantControllerTransport(
            pull_request_payloads=(
                _pull_request_payload(),
                _pull_request_payload(head_sha="d" * 40),
            )
        )
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            with (
                patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ),
                self.assertRaisesRegex(ValueError, "identity changed"),
            ):
                execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=store,
                    token="token",
                    trace_id="trace-head-move",
                    transport_factory=lambda _token: transport,
                )

            controller_state = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]

        self.assertFalse(transport.merge_called)
        self.assertEqual(controller_state.status, "idle")

    def test_base_move_between_rechecks_fails_before_merge(self) -> None:
        admission = _status_with_path_states(
            trusted_state="satisfied",
            waiver_state="pending",
            manager_state="pending",
        )
        transport = _TenantControllerTransport(
            pull_request_payloads=(
                _pull_request_payload(),
                _pull_request_payload(base_sha="d" * 40),
            )
        )
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            with (
                patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ),
                self.assertRaisesRegex(ValueError, "base moved"),
            ):
                execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=store,
                    token="token",
                    trace_id="trace-base-move",
                    transport_factory=lambda _token: transport,
                )

        self.assertFalse(transport.merge_called)

    def test_merge_confirmation_mismatch_requires_reconciliation(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="pending",
            manager_state="satisfied",
        )
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            transport = _TenantControllerTransport(merge_response_sha="d" * 40)
            with (
                patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ),
                self.assertRaisesRegex(ValueError, "did not confirm"),
            ):
                execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=store,
                    token="token",
                    trace_id="trace-confirm-mismatch",
                    transport_factory=lambda _token: transport,
                )
            controller_state = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]

        self.assertTrue(transport.merge_called)
        self.assertEqual(controller_state.status, "reconcile_required")

    def test_expected_sha_rejection_is_stale_without_reconciliation(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="pending",
            manager_state="satisfied",
        )
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            transport = _TenantControllerTransport(stale_merge=True)
            with (
                patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ),
                self.assertRaisesRegex(ValueError, "head moved"),
            ):
                execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=store,
                    token="token",
                    trace_id="trace-stale-merge",
                    transport_factory=lambda _token: transport,
                )
            controller_state = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]

        self.assertTrue(transport.merge_called)
        self.assertEqual(controller_state.status, "idle")

    def test_exact_already_merged_pull_request_is_idempotent(self) -> None:
        transport = _TenantControllerTransport(initially_merged=True)
        with TemporaryDirectory() as temporary_name:
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status"
            ) as status_read:
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    trace_id="trace-already-merged",
                    transport_factory=lambda _token: transport,
                )

        self.assertEqual(result.outcome, "already_merged")
        self.assertEqual(result.merge_commit_sha, MERGE_COMMIT_SHA)
        self.assertFalse(transport.merge_called)
        status_read.assert_not_called()

    def test_foreign_controller_state_remains_attributed_to_original_action(self) -> None:
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            foreign_record = store.acquire_merge_train_controller_state_record(
                repository=_candidate().repository,
                base_branch="main",
                policy_key="foreign-policy",
                policy_sha256="a" * 64,
                lease_owner="foreign-owner",
                lease_seconds=300,
                initial_active_action="merge_train_land",
                initial_active_phase="confirm_batch",
                adoptable_active_actions=("merge_train_land",),
            )
            foreign_lease = MergeTrainControllerLeaseContext(
                record=foreign_record,
                record_store=store,
            )
            foreign_lease.checkpoint(
                active_action="merge_train_land",
                active_phase="confirm_batch",
                active_record_id="batch-1",
            )
            foreign_lease.release(
                reconciliation_status="required",
                reconciliation_detail="retryable:merge_train_land:confirm_batch",
                clear_active_state=False,
            )

            original_state = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]
            transport = _TenantControllerTransport()
            with self.assertRaises(MergeTrainControllerAdoptionRejectedError):
                execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=store,
                    token="token",
                    trace_id="trace-foreign-action",
                    transport_factory=lambda _token: transport,
                )
            controller_state = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]

        self.assertFalse(transport.merge_called)
        self.assertEqual(controller_state, original_state)

    def test_merge_train_cannot_adopt_tenant_controller_state(self) -> None:
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            tenant_record = store.acquire_merge_train_controller_state_record(
                repository=_candidate().repository,
                base_branch="main",
                policy_key="tenant-policy",
                policy_sha256="a" * 64,
                lease_owner="tenant-owner",
                lease_seconds=300,
                initial_active_action="tenant_admission_merge",
                initial_active_phase="confirm_merge",
                adoptable_active_actions=("tenant_admission_merge",),
            )
            tenant_lease = MergeTrainControllerLeaseContext(
                record=tenant_record,
                record_store=store,
            )
            tenant_lease.checkpoint(
                active_action="tenant_admission_merge",
                active_phase="confirm_merge",
                active_record_id="tenant-operation",
            )
            tenant_state = tenant_lease.release(
                reconciliation_status="required",
                reconciliation_detail="retryable:tenant_admission_merge:confirm_merge",
                clear_active_state=False,
            )

            with self.assertRaises(MergeTrainControllerAdoptionRejectedError):
                store.acquire_merge_train_controller_state_record(
                    repository=_candidate().repository,
                    base_branch="main",
                    policy_key="merge-train-policy",
                    policy_sha256="b" * 64,
                    lease_owner="merge-train-owner",
                    lease_seconds=300,
                    initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                    initial_active_phase="select_next_action",
                    adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
                )
            observed = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]

        self.assertEqual(observed, tenant_state)

    def test_tenant_and_generic_fences_cannot_adopt_merge_train_initial_state(self) -> None:
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            merge_train_record = store.acquire_merge_train_controller_state_record(
                repository=_candidate().repository,
                base_branch="main",
                policy_key="merge-train-policy",
                policy_sha256="a" * 64,
                lease_owner="merge-train-owner",
                lease_seconds=300,
                initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                initial_active_phase="select_next_action",
                adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
            )
            merge_train_lease = MergeTrainControllerLeaseContext(
                record=merge_train_record,
                record_store=store,
            )
            merge_train_state = merge_train_lease.release(
                reconciliation_status="required",
                reconciliation_detail=(
                    "retryable:merge_train_controller_run_once:select_next_action"
                ),
                clear_active_state=False,
            )

            transport = _TenantControllerTransport()
            with self.assertRaises(MergeTrainControllerAdoptionRejectedError):
                execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=store,
                    token="token",
                    trace_id="trace-main-bootstrap",
                    transport_factory=lambda _token: transport,
                )
            with self.assertRaises(MergeTrainControllerAdoptionRejectedError):
                with merge_train_controller_mutation_fence(
                    record_store=store,
                    repository=_candidate().repository,
                    base_branch="main",
                    policy_key="generic-policy",
                    policy_sha256="b" * 64,
                    trace_id="trace-generic-bootstrap",
                    active_action="batch_candidate_run_once",
                    active_phase="plan",
                ):
                    self.fail("Foreign generic mutation fence unexpectedly acquired state.")
            observed = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]

        self.assertFalse(transport.merge_called)
        self.assertEqual(observed, merge_train_state)

    def test_merge_train_can_adopt_every_main_controller_checkpoint_action(self) -> None:
        expected_actions = {
            "admit_collapsed_root",
            "build_candidate",
            "execute_stack_collapse",
            "land_batch",
            MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            "observe_candidate",
            "plan_candidate",
            "plan_landing",
            "plan_stack_collapse",
            "reflow_candidate",
        }
        self.assertEqual(set(MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS), expected_actions)

        for active_action in sorted(expected_actions):
            with self.subTest(active_action=active_action), TemporaryDirectory() as temporary_name:
                store = FilesystemRecordStore(state_dir=Path(temporary_name))
                initial = store.acquire_merge_train_controller_state_record(
                    repository=_candidate().repository,
                    base_branch="main",
                    policy_key="merge-train-policy",
                    policy_sha256="a" * 64,
                    lease_owner="merge-train-owner-a",
                    lease_seconds=300,
                    initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                    initial_active_phase="select_next_action",
                    adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
                )
                first_lease = MergeTrainControllerLeaseContext(
                    record=initial,
                    record_store=store,
                )
                first_lease.checkpoint(
                    active_action=active_action,
                    active_phase="checkpointed",
                )
                first_lease.release(
                    reconciliation_status="required",
                    reconciliation_detail=f"retryable:{active_action}:checkpointed",
                    clear_active_state=False,
                )

                adopted = store.acquire_merge_train_controller_state_record(
                    repository=_candidate().repository,
                    base_branch="main",
                    policy_key="merge-train-policy",
                    policy_sha256="a" * 64,
                    lease_owner="merge-train-owner-b",
                    lease_seconds=300,
                    initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                    initial_active_phase="select_next_action",
                    adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
                )

            self.assertEqual(adopted.reconciliation_status, "adopted")
            self.assertEqual(adopted.active_action, active_action)

    def test_provider_uncertainty_adopts_exact_merged_pull_request(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="pending",
            manager_state="satisfied",
        )
        request = _request(mutate=True)
        with TemporaryDirectory() as temporary_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_name))
            uncertain_transport = _TenantControllerTransport(uncertain_merge=True)
            with (
                patch(
                    "control_plane.tenant_admission_controller.get_tenant_admission_status",
                    return_value=admission,
                ),
                self.assertRaises(MergeTrainGitHubError),
            ):
                execute_tenant_admission_controller_run_once(
                    request=request,
                    store=store,
                    token="token",
                    trace_id="trace-uncertain",
                    transport_factory=lambda _token: uncertain_transport,
                )
            uncertain_state = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]
            self.assertEqual(uncertain_state.status, "reconcile_required")

            adoption_transport = _TenantControllerTransport(initially_merged=True)
            adopted = execute_tenant_admission_controller_run_once(
                request=request,
                store=store,
                token="token",
                trace_id="trace-adopt",
                transport_factory=lambda _token: adoption_transport,
            )
            adopted_state = store.list_merge_train_controller_state_records(
                repository=_candidate().repository,
                base_branch="main",
            )[0]

        self.assertEqual(adopted.outcome, "adopted")
        self.assertEqual(adopted.merge_commit_sha, MERGE_COMMIT_SHA)
        self.assertFalse(adoption_transport.merge_called)
        self.assertEqual(adopted_state.status, "idle")


class _TenantControllerTransport:
    def __init__(
        self,
        *,
        technical_state: str = "success",
        include_technical_signals: bool = True,
        pull_request_payloads: tuple[dict[str, object], ...] = (),
        uncertain_merge: bool = False,
        initially_merged: bool = False,
        merge_response_sha: str = MERGE_COMMIT_SHA,
        status_response_sha: str = "",
        check_run_head_sha: str = "",
        check_run_app_id: int = 15368,
        stale_merge: bool = False,
        include_required_checks: bool = True,
        include_optional_signal: bool = False,
        optional_signal_state: str = "success",
        required_check_app_ids: tuple[int, ...] = (),
        strict_required_checks: object = True,
        head_contains_base: bool = True,
        include_launchplane_projection_signals: bool = False,
    ) -> None:
        self.technical_state = technical_state
        self.include_technical_signals = include_technical_signals
        self.pull_request_payloads = list(pull_request_payloads)
        self.uncertain_merge = uncertain_merge
        self.merged = initially_merged
        self.merge_response_sha = merge_response_sha
        self.status_response_sha = status_response_sha
        self.check_run_head_sha = check_run_head_sha
        self.check_run_app_id = check_run_app_id
        self.stale_merge = stale_merge
        self.include_required_checks = include_required_checks
        self.include_optional_signal = include_optional_signal
        self.optional_signal_state = optional_signal_state
        self.required_check_app_ids = list(required_check_app_ids)
        self.strict_required_checks = strict_required_checks
        self.head_contains_base = head_contains_base
        self.include_launchplane_projection_signals = include_launchplane_projection_signals
        self.merge_called = False
        self.technical_checks_read = False
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self,
        *,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> object:
        self.requests.append((method, path, body))
        if method == "GET" and "/pulls/" in path:
            if self.pull_request_payloads:
                return self.pull_request_payloads.pop(0)
            return _pull_request_payload(merged=self.merged)
        if method == "GET" and path.endswith("/protection/required_status_checks"):
            self.technical_checks_read = True
            if not self.include_required_checks:
                return {"strict": True, "contexts": [], "checks": []}
            required_check_app_id = (
                self.required_check_app_ids.pop(0) if self.required_check_app_ids else 15368
            )
            contexts = ["ci/build", "unit-tests"]
            checks = [
                {"context": "ci/build", "app_id": None},
                {"context": "unit-tests", "app_id": required_check_app_id},
            ]
            if self.include_launchplane_projection_signals:
                contexts.extend(["launchplane/engineering-review", "launchplane/owner-acceptance"])
                checks.extend(
                    [
                        {"context": "launchplane/engineering-review", "app_id": 42},
                        {"context": "launchplane/owner-acceptance", "app_id": 42},
                    ]
                )
            return {
                **(
                    {"strict": self.strict_required_checks}
                    if self.strict_required_checks is not None
                    else {}
                ),
                "contexts": contexts,
                "checks": checks,
            }
        if method == "GET" and path.endswith("/status"):
            self.technical_checks_read = True
            statuses: list[dict[str, object]] = [
                {"context": "tenant-admission", "state": "failure"},
                {"context": "manager-preview-approval", "state": "failure"},
            ]
            if self.include_technical_signals:
                statuses.append({"context": "ci/build", "state": self.technical_state, "app": None})
            if self.include_optional_signal:
                statuses.append(
                    {
                        "context": "optional-lint",
                        "state": self.optional_signal_state,
                        "app": None,
                    }
                )
            if self.include_launchplane_projection_signals:
                statuses.extend(
                    [
                        {
                            "context": "launchplane/engineering-review-shadow",
                            "state": "failure",
                        },
                        {"context": "launchplane/owner-acceptance", "state": "failure"},
                    ]
                )
            return {
                "sha": self.status_response_sha or _candidate().head_sha,
                "total_count": len(statuses),
                "statuses": statuses,
            }
        if method == "GET" and "/check-runs?" in path:
            self.technical_checks_read = True
            check_runs: list[dict[str, object]] = (
                [
                    {
                        "name": "unit-tests",
                        "head_sha": self.check_run_head_sha or _candidate().head_sha,
                        "app": {"id": self.check_run_app_id},
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
                if self.include_technical_signals
                else []
            )
            if self.include_launchplane_projection_signals:
                check_runs.extend(
                    [
                        {
                            "name": "launchplane/engineering-review",
                            "head_sha": _candidate().head_sha,
                            "app": {"id": 42},
                            "status": "completed",
                            "conclusion": "failure",
                        },
                        {
                            "name": "launchplane/owner-acceptance",
                            "head_sha": _candidate().head_sha,
                            "app": {"id": 42},
                            "status": "in_progress",
                            "conclusion": None,
                        },
                    ]
                )
            return {"total_count": len(check_runs), "check_runs": check_runs}
        if method == "PUT" and path.endswith("/merge"):
            self.merge_called = True
            if self.stale_merge:
                raise MergeTrainGitHubStaleHeadError("head moved", status_code=409)
            self.merged = True
            if self.uncertain_merge:
                raise MergeTrainGitHubError("provider response lost")
            return {"sha": self.merge_response_sha, "merged": True}
        if method == "GET" and "/compare/" in path:
            if f"/compare/{BASE_SHA}...{_candidate().head_sha}" in path:
                return {"status": "ahead" if self.head_contains_base else "diverged"}
            return {"status": "ahead"}
        raise AssertionError(f"Unexpected GitHub request: {method} {path} {body}")


def _request(*, mutate: bool) -> TenantAdmissionControllerRunOnceEnvelope:
    return TenantAdmissionControllerRunOnceEnvelope(
        candidate=_candidate(),
        base_branch="main",
        merge_method="merge",
        mutate=mutate,
    )


def _pull_request_payload(
    *,
    head_sha: str = "",
    base_sha: str = BASE_SHA,
    head_repository_id: int | None = None,
    draft: bool = False,
    mergeable: bool | None = True,
    merged: bool = False,
) -> dict[str, object]:
    candidate = _candidate()
    effective_head_sha = head_sha or candidate.head_sha
    return {
        "number": candidate.pull_request_number,
        "state": "closed" if merged else "open",
        "merged": merged,
        "draft": draft,
        "mergeable": mergeable,
        "merge_commit_sha": MERGE_COMMIT_SHA if merged else None,
        "html_url": (
            f"https://github.com/{candidate.repository}/pull/{candidate.pull_request_number}"
        ),
        "base": {
            "ref": "main",
            "sha": base_sha,
            "repo": {
                "id": int(candidate.repository_id),
                "full_name": candidate.repository,
                "owner": {"id": int(candidate.repository_owner_id)},
            },
        },
        "head": {
            "sha": effective_head_sha,
            "repo": {
                "id": head_repository_id or int(candidate.repository_id),
                "full_name": candidate.repository,
                "owner": {"id": int(candidate.repository_owner_id)},
            },
        },
    }
