from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerAdoptionRejectedError,
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.github_app_configuration import ADVISORY_GITHUB_APP_ID_ENV_KEY
from control_plane.merge_train_controller_run_once import (
    MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
    MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
    MergeTrainControllerLeaseContext,
)
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    MergeTrainGitHubMergeRejectedError,
)
from control_plane.product_review_status import OWNER_REVIEW_STATUS_CONTEXT
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerError,
    TenantAdmissionControllerStaleCandidateError,
    TenantAdmissionControllerRunOnceResult,
    evaluate_tenant_admission_candidate,
    execute_tenant_admission_controller_run_once,
)
from tests.support.profiles import product_profile_payload
from tests.test_tenant_admission_controller import _TenantControllerTransport, _request
from tests.test_tenant_admission_status import _candidate, _classification


class TenantAdmissionClientReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = FilesystemRecordStore(state_dir=Path(directory))
        candidate = _candidate()
        payload = product_profile_payload(candidate.product)
        payload.update(
            repository=candidate.repository,
            owner={
                "github_login": "client",
                "github_id": "901",
                "review_label": "client-preview-for-test",
            },
            release_on_acceptance="held",
        )
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        self.store.write_tenant_repository_classification_record(_classification())
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="launchplane",
                env={ADVISORY_GITHUB_APP_ID_ENV_KEY: "77"},
                updated_at="2026-10-22T08:00:00Z",
            )
        )

    def test_saved_veto_blocks_evaluation_and_merge_despite_successful_projection(self) -> None:
        self.store.write_product_review_decision_record(_decision())
        for body in (
            "## Client test notes\nTry the changed page.",
            "## Client test notes\nNothing for the Client to test: CI only.",
        ):
            for mutate in (False, True):
                with self.subTest(body=body, mutate=mutate):
                    transport = _ClientReviewTransport(body=body)
                    result = execute_tenant_admission_controller_run_once(
                        request=_request(mutate=mutate),
                        store=self.store,
                        token="token",
                        trace_id="saved-veto",
                        transport_factory=lambda _token: transport,
                    )
                    self.assertEqual(result.outcome, "blocked")
                    self.assertFalse(transport.merge_called)

    def test_missing_notes_or_unaccepted_review_cannot_be_ready(self) -> None:
        cases = (
            {"body": ""},
            {"body": "## Client test notes\n<!-- fill this in -->"},
            {"body": "## Client test notes\nNothing for the Client to test."},
            {"review_state": "pending"},
            {"review_state": "failure"},
            {"review_state": "missing"},
            {"review_app": 99},
        )
        for case in cases:
            with self.subTest(case=case):
                transport = _ClientReviewTransport(**case)
                result = evaluate_tenant_admission_candidate(
                    request=_request(mutate=False),
                    store=self.store,
                    token="token",
                    transport_factory=lambda _token: transport,
                )
                self.assertEqual(result.outcome, "blocked")
                self.assertFalse(transport.merge_called)

    def test_wrong_head_projection_refuses_the_merge(self) -> None:
        transport = _ClientReviewTransport(review_head="2" * 40)
        with self.assertRaises(TenantAdmissionControllerStaleCandidateError):
            self._run(transport)
        self.assertFalse(transport.merge_called)

    def test_accepted_review_permits_merge_but_does_not_replace_technical_gates(self) -> None:
        self.store.write_product_review_decision_record(_decision(decision="accepted"))
        for technical_state, expected in (("failure", "blocked"), ("success", "merged")):
            with self.subTest(technical_state=technical_state):
                transport = _ClientReviewTransport(technical_state=technical_state)
                result = execute_tenant_admission_controller_run_once(
                    request=_request(mutate=True),
                    store=self.store,
                    token="token",
                    trace_id="accepted-review",
                    transport_factory=lambda _token: transport,
                )
                self.assertEqual(result.outcome, expected)
                self.assertEqual(transport.merge_called, expected == "merged")

    def test_engineering_notes_need_no_preview_acceptance_unless_review_is_requested(self) -> None:
        for review_label, expected in ((False, "merged"), (True, "blocked")):
            with self.subTest(review_label=review_label):
                transport = _ClientReviewTransport(
                    body="## Client test notes\nNothing for the Client to test: CI only.",
                    review_state="missing",
                    review_label=self.profile.owner.review_label if review_label else "",
                )
                self.assertEqual(self._run(transport).outcome, expected)
                self.assertEqual(transport.merge_called, expected == "merged")

    def test_missing_advisory_app_configuration_cannot_accept_a_client_change(self) -> None:
        with patch.object(self.store, "list_runtime_environment_records", return_value=()):
            transport = _ClientReviewTransport()
            self.assertEqual(self._run(transport).outcome, "blocked")
            self.assertFalse(transport.merge_called)

    def test_later_current_head_acceptance_clears_the_saved_veto(self) -> None:
        self.store.write_product_review_decision_record(_decision())
        self.store.write_product_review_decision_record(
            _decision(decision="accepted", record_id="accepted", decided_at="2026-10-22T09:00:00Z")
        )
        transport = _ClientReviewTransport()
        self.assertEqual(self._run(transport).outcome, "merged")
        self.assertTrue(transport.merge_called)

    def test_changed_decision_is_read_at_recheck_and_immediately_before_merge(self) -> None:
        original_checkpoint = MergeTrainControllerLeaseContext.checkpoint
        for phase in ("pre_merge_recheck", "merge_pull_request"):
            with self.subTest(phase=phase):
                self.store.write_product_review_decision_record(
                    _decision(
                        decision="accepted", record_id="latest", decided_at="2026-10-22T09:00:00Z"
                    )
                )

                def checkpoint(
                    lease: MergeTrainControllerLeaseContext, **updates: object
                ) -> MergeTrainControllerStateRecord:
                    record = original_checkpoint(lease, **updates)
                    if updates.get("active_phase") == phase:
                        self.store.write_product_review_decision_record(
                            _decision(record_id="latest", decided_at="2026-10-22T09:00:00Z")
                        )
                    return record

                transport = _ClientReviewTransport()
                with patch.object(MergeTrainControllerLeaseContext, "checkpoint", checkpoint):
                    if phase == "merge_pull_request":
                        with self.assertRaises(TenantAdmissionControllerStaleCandidateError):
                            self._run(transport)
                    else:
                        self.assertEqual(self._run(transport).outcome, "blocked")
                self.assertFalse(transport.merge_called)
                state = self.store.list_merge_train_controller_state_records(
                    repository=_candidate().repository, base_branch="main"
                )[0]
                self.assertEqual(state.status, "idle")

    def test_resumed_unmerged_effect_rechecks_veto_but_merged_effect_is_adopted(self) -> None:
        for merged, expected in ((False, "blocked"), (True, "adopted")):
            with self.subTest(merged=merged):
                self.store.write_product_review_decision_record(
                    _decision(decision="accepted", record_id="latest")
                )
                with self.assertRaises(MergeTrainGitHubError):
                    self._run(_ClientReviewTransport(uncertain_merge=True))
                state = self.store.list_merge_train_controller_state_records(
                    repository=_candidate().repository, base_branch="main"
                )[0]
                self.assertEqual(state.status, "reconcile_required")
                with self.assertRaises(MergeTrainControllerAdoptionRejectedError):
                    self._acquire_train_lease()
                self.store = FilesystemRecordStore(state_dir=self.store.state_dir)
                self.store.write_product_review_decision_record(_decision(record_id="latest"))
                resumed = _ClientReviewTransport(initially_merged=merged)
                result = self._run(resumed)
                self.assertEqual(result.outcome, expected)
                self.assertFalse(resumed.merge_called)

    def test_failed_final_review_read_does_not_retain_an_uncertain_merge_fence(self) -> None:
        for endpoint in ("pulls", "check-runs"):
            for failure in (
                MergeTrainGitHubError("provider unavailable", status_code=503),
                TimeoutError("read timed out"),
            ):
                with self.subTest(endpoint=endpoint, failure=type(failure).__name__):
                    transport = _ClientReviewTransport()
                    original_request = transport.request

                    def request(
                        *, method: str, path: str, body: dict[str, object] | None = None
                    ) -> object:
                        state = self.store.list_merge_train_controller_state_records(
                            repository=_candidate().repository, base_branch="main"
                        )[0]
                        if (
                            state.active_phase == "merge_pull_request"
                            and method == "GET"
                            and f"/{endpoint}" in path
                        ):
                            raise failure
                        return original_request(method=method, path=path, body=body)

                    with patch.object(transport, "request", side_effect=request):
                        with self.assertRaises(type(failure)):
                            self._run(transport)
                    self.assertFalse(transport.merge_called)
                    state = self.store.list_merge_train_controller_state_records(
                        repository=_candidate().repository, base_branch="main"
                    )[0]
                    lease = self._acquire_train_lease()
                    lease.release()
                    self.assertEqual(state.status, "idle")

    def test_definite_merge_refusal_releases_the_shared_controller_fence(self) -> None:
        transport = _ClientReviewTransport()
        original_request = transport.request

        def request(*, method: str, path: str, body: dict[str, object] | None = None) -> object:
            if method == "PUT" and path.endswith("/merge"):
                transport.merge_called = True
                raise MergeTrainGitHubError("merge refused", status_code=405)
            return original_request(method=method, path=path, body=body)

        with patch.object(transport, "request", side_effect=request):
            with self.assertRaises(MergeTrainGitHubMergeRejectedError):
                self._run(transport)
        self.assertTrue(transport.merge_called)
        self.assertFalse(transport.merged)
        state = self.store.list_merge_train_controller_state_records(
            repository=_candidate().repository, base_branch="main"
        )[0]
        lease = self._acquire_train_lease()
        lease.release()
        self.assertEqual(state.status, "idle")

    def test_missing_saved_decision_reader_fails_closed(self) -> None:
        with patch.object(self.store, "list_product_review_decision_records", None):
            for mutate in (False, True):
                with self.subTest(mutate=mutate):
                    transport = _ClientReviewTransport()
                    with self.assertRaises(TenantAdmissionControllerError):
                        execute_tenant_admission_controller_run_once(
                            request=_request(mutate=mutate),
                            store=self.store,
                            token="token",
                            trace_id="missing-decisions",
                            transport_factory=lambda _token: transport,
                        )
                    self.assertFalse(transport.merge_called)

    def test_unrelated_or_superseded_decisions_do_not_veto_the_current_head(self) -> None:
        for override in (
            {"head_sha": "2" * 40},
            {"owner_github_id": "902"},
            {"product": "other-site"},
            {"decision": "accepted"},
        ):
            with self.subTest(override=override):
                self.store.write_product_review_decision_record(
                    _decision().model_copy(update={"record_id": "latest", **override})
                )
                transport = _ClientReviewTransport()
                self.assertEqual(self._run(transport).outcome, "merged")
                self.assertTrue(transport.merge_called)

    def _run(self, transport: _ClientReviewTransport) -> TenantAdmissionControllerRunOnceResult:
        return execute_tenant_admission_controller_run_once(
            request=_request(mutate=True),
            store=self.store,
            token="token",
            trace_id="client-review",
            transport_factory=lambda _token: transport,
        )

    def _acquire_train_lease(self) -> MergeTrainControllerLeaseContext:
        state = self.store.acquire_merge_train_controller_state_record(
            repository=_candidate().repository,
            base_branch="main",
            policy_key="main-train-policy",
            policy_sha256="a" * 64,
            lease_owner="main-train-worker",
            lease_seconds=300,
            initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            initial_active_phase="plan",
            adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
        )
        return MergeTrainControllerLeaseContext(record=state, record_store=self.store)


class _ClientReviewTransport(_TenantControllerTransport):
    def __init__(
        self,
        *,
        body: str = "## Client test notes\nTry the changed page.",
        review_state: str = "success",
        review_head: str = "",
        review_app: int = 77,
        technical_state: str = "success",
        uncertain_merge: bool = False,
        initially_merged: bool = False,
        review_label: str = "",
    ) -> None:
        super().__init__(
            technical_state=technical_state,
            uncertain_merge=uncertain_merge,
            initially_merged=initially_merged,
        )
        self.body = body
        self.review_state = review_state
        self.review_head = review_head or _candidate().head_sha
        self.review_app = review_app
        self.review_label = review_label

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        result = super().request(method=method, path=path, body=body)
        if isinstance(result, dict) and method == "GET" and "/pulls/" in path:
            result.update(
                body=self.body, labels=[{"name": self.review_label}] if self.review_label else []
            )
        if isinstance(result, dict) and "check_runs" in result and self.review_state != "missing":
            checks = cast(list[dict[str, object]], result["check_runs"])
            checks.append(
                {
                    "name": OWNER_REVIEW_STATUS_CONTEXT,
                    "head_sha": self.review_head,
                    "app": {"id": self.review_app},
                    "status": "in_progress" if self.review_state == "pending" else "completed",
                    "conclusion": None if self.review_state == "pending" else self.review_state,
                }
            )
            result["total_count"] = len(checks)
        return result


def _decision(
    *,
    decision: str = "changes_requested",
    record_id: str = "client-decision",
    decided_at: str = "2026-10-22T08:00:00Z",
) -> ProductReviewDecisionRecord:
    candidate = _candidate()
    return ProductReviewDecisionRecord.model_validate(
        {
            "record_id": record_id,
            "product": candidate.product,
            "repository": candidate.repository,
            "pull_request_number": candidate.pull_request_number,
            "head_sha": candidate.head_sha,
            "decision": decision,
            "reason": "Check the changed page.",
            "owner_github_id": "901",
            "owner_github_login": "client",
            "decided_at": decided_at,
        }
    )
