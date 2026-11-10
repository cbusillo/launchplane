from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.contracts.merge_train_batch import MergeTrainCandidateBlockIntent
from control_plane.github_required_checks import CheckRequirement
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train_base_retest_receipt import BaseRetestReceipt, reserve_base_retest
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _asgi_get
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_batch_candidate_record,
    _seed_merge_train_policy,
)


class CandidateEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name) / "state"
        self.store = FilesystemRecordStore(state)
        self.policy = _seed_merge_train_policy(state)
        original = _seed_merge_train_batch_candidate_record(
            state, status="failed", required_checks_status="fail", policy=self.policy.policy
        )
        self.record = original.model_copy(
            update={
                "status": "superseded",
                "block_intent": MergeTrainCandidateBlockIntent(
                    label="merge-blocked",
                    prior_label_event_id=0,
                    applied_label_event_id=50,
                    label_write_succeeded=True,
                    source_failed_record_id=original.record_id,
                ),
            }
        )
        self.store.write_merge_train_batch_candidate_record(self.record)
        self.candidate = self.record.candidate
        self.url = (
            "/v1/work-graph/merge-train/controller/status"
            f"?repository={self.candidate.repository}&base_branch={self.candidate.base_branch}"
            f"&candidate_record_id={self.record.record_id}"
        )
        self.headers = {"Authorization": "Bearer valid-token"}

    def app(self, *, denied: bool = False) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=LaunchplaneAuthzPolicy() if denied else _merge_train_service_policy(),
            record_store_factory=lambda: self.store,
        )

    async def test_exact_retired_candidate_and_reservations_survive_summary_bounds(self) -> None:
        entry = self.candidate.entries[0]
        receipt = BaseRetestReceipt(
            failed_record_id=self.record.record_id,
            failed_base_sha=self.candidate.base_sha,
            head_sha=entry.head_sha,
            applied_label_event_id=50,
            retest_base_sha="new-base",
            required_checks=(CheckRequirement(name="ci-gate", app_id=15368),),
        )
        reserve_base_retest(
            store=self.store,
            candidate=self.record,
            policy=self.policy.policy.find_repository_policy(
                repository=self.candidate.repository, base_branch=self.candidate.base_branch
            ),
            receipt=receipt,
            recorded_at=self.record.updated_at,
        )
        before = {path: path.read_bytes() for path in self.store.state_dir.rglob("*.json")}
        with patch(
            "control_plane.merge_train_github.UrllibMergeTrainGitHubTransport.request",
            side_effect=AssertionError("Diagnostic read cannot call provider"),
        ):
            response = await _asgi_get(self.app(), self.url, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        evidence = response.json()["candidate_evidence"]
        self.assertEqual(evidence["record"], self.record.model_dump(mode="json"))
        self.assertNotIn(
            self.record.record_id,
            {
                row["record_id"]
                for row in response.json()["controller_status"]["controller_records"]
            },
        )
        reservations = evidence["recovery_reservations"]
        self.assertEqual(reservations[0]["base_retest"], receipt.model_dump(mode="json"))
        self.assertIsNone(reservations[0]["setup_recovery"])
        self.assertEqual(len(reservations), len(self.candidate.entries))
        self.assertEqual(
            {path: path.read_bytes() for path in self.store.state_dir.rglob("*.json")}, before
        )

    async def test_exact_record_missing_or_wrong_scope_is_not_empty_success(self) -> None:
        for url in (
            self.url.replace(self.record.record_id, "missing-record"),
            self.url.replace("base_branch=main", "base_branch=other"),
        ):
            response = await _asgi_get(self.app(), url, headers=self.headers)
            self.assertNotEqual(response.status_code, 200, response.text)

    async def test_denial_precedes_candidate_and_reservation_reads(self) -> None:
        with patch.object(
            self.store,
            "list_merge_train_batch_candidate_records",
            side_effect=AssertionError("Denied caller cannot read candidates"),
        ):
            denied = await _asgi_get(self.app(denied=True), self.url, headers=self.headers)
            anonymous = await _asgi_get(self.app(), self.url)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(anonymous.status_code, 401, anonymous.text)

    async def test_unreadable_reservation_does_not_claim_unspent_allowance(self) -> None:
        with patch.object(
            self.store, "read_merge_train_pr_feedback_record", side_effect=ValueError("invalid")
        ):
            response = await _asgi_get(self.app(), self.url, headers=self.headers)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "candidate_evidence_unavailable")
