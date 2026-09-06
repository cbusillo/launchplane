from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.change_impact_github import (
    ChangeImpactRepositoryEvidenceError,
    ChangeImpactRepositoryEvidenceStaleError,
    GitHubChangeImpactRepositoryEvidenceProvider,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_admission import MergeAdmissionDeniedError
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.owner_acceptance import OwnerAcceptanceEvaluationUnavailableError
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _AdmissionInvokingMergeTrainGitHubClient,
    _FakeMergeTrainSnapshotReader,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
)
from tests.test_change_impact_github import _commit_payload, _pull_request_payload
from tests.test_merge_admission_live import (
    _queued_pull_request,
    _StaticSnapshotReader,
    _TechnicalCheckClient,
)
from tests.test_merge_admission_records import _guard_records
from tests.test_merge_readiness import BASE_SHA, HEAD_SHA, REPOSITORY, TREE_SHA


class _EvidenceApi:
    def __init__(
        self, files: list[dict[str, object]], *, stale: bool = False, failure_attempt: int = 1
    ) -> None:
        self.files = files
        self.stale = stale
        self.failure_attempt = failure_attempt
        self.pull_request_reads = 0
        self.file_reads = 0

    def __call__(self, *, path: str, token: str) -> object:
        repository = "/".join(path.split("/")[2:4])
        if "/files?" in path:
            self.file_reads += 1
            if self.file_reads < self.failure_attempt:
                return [{"filename": "private/path.py", "status": "modified"}]
            return self.files
        if "/commits?" in path:
            return [_commit_payload()]
        if "/git/commits/" in path:
            return {"tree": {"sha": "b" * 40}}
        if "/pulls/" in path:
            self.pull_request_reads += 1
            payload = _pull_request_payload(
                head_sha="c" * 40
                if self.stale and self.pull_request_reads >= 2 * self.failure_attempt
                else "a" * 40
            )
            payload["base"] = {
                "sha": "d" * 40,
                "ref": "main",
                "repo": {"id": 1001, "full_name": repository},
            }
            return payload
        return {"id": 1001, "full_name": repository, "owner": {"id": 2001}}

    def provider(self) -> GitHubChangeImpactRepositoryEvidenceProvider:
        return GitHubChangeImpactRepositoryEvidenceProvider(
            control_plane_root=Path("."),
            github_token=lambda **_: "test-token",
            github_api=self,
            token_context="launchplane",
        )


class MergeAdmissionEvidenceFailureTests(unittest.TestCase):
    def test_live_evaluator_denies_malformed_or_stale_real_provider_evidence(self) -> None:
        cases: tuple[
            tuple[list[dict[str, object]], bool, str, type[ChangeImpactRepositoryEvidenceError]],
            ...,
        ] = (
            (
                [
                    {"filename": "private/path.py", "status": "modified"},
                    {"filename": "private/path.py", "status": "modified"},
                ],
                False,
                "repository_evidence_unavailable",
                ChangeImpactRepositoryEvidenceError,
            ),
            (
                [{"filename": "private/path.py", "status": "renamed"}],
                False,
                "repository_evidence_unavailable",
                ChangeImpactRepositoryEvidenceError,
            ),
            (
                [{"filename": "private/path.py", "status": "modified"}],
                True,
                "repository_evidence_stale",
                ChangeImpactRepositoryEvidenceStaleError,
            ),
        )
        candidate, landing, controller, _ = _guard_records()
        for failure_attempt in (1, 2):
            for files, stale, reason, cause_type in cases:
                with (
                    self.subTest(reason=reason, stale=stale, files=files, attempt=failure_attempt),
                    TemporaryDirectory() as state_dir,
                ):
                    api = _EvidenceApi(files, stale=stale, failure_attempt=failure_attempt)
                    evaluator = LiveMergeAdmissionEvaluator(
                        store=FilesystemRecordStore(state_dir=Path(state_dir)),
                        repository_evidence_provider=api.provider(),
                        technical_check_client=_TechnicalCheckClient(),
                        policy_record_provider=lambda: build_test_merge_train_policy_record(
                            repository=REPOSITORY
                        ),
                        snapshot_reader=_StaticSnapshotReader(
                            MergeTrainDryRunSnapshot(
                                repository=REPOSITORY,
                                base_branch="main",
                                base_sha=BASE_SHA,
                                pull_requests=(
                                    _queued_pull_request(
                                        number=2083,
                                        head_sha=HEAD_SHA,
                                        created_at="2026-08-11T03:00:00Z",
                                    ),
                                ),
                            )
                        ),
                    )
                    with self.assertRaises(MergeAdmissionDeniedError) as raised:
                        evaluator.evaluate(
                            candidate_record=candidate,
                            landing_plan_record=landing,
                            entry=landing.landing_plan.entries[0],
                            observed_base_sha=BASE_SHA,
                            observed_base_tree_sha="5" * 40,
                            observed_head_sha=HEAD_SHA,
                            observed_head_tree_sha=TREE_SHA,
                            controller_state=controller,
                            expected_lease_owner=controller.lease_owner,
                            stack_collapse_record=None,
                            evaluated_at="2026-08-11T03:01:00Z",
                        )
                    self.assertEqual(raised.exception.reason_code, reason)
                    cause = raised.exception.__cause__
                    if failure_attempt == 2:
                        self.assertIsInstance(cause, OwnerAcceptanceEvaluationUnavailableError)
                        assert cause is not None
                        cause = cause.__cause__
                    self.assertIsInstance(cause, cause_type)
                    self.assertNotIn("private/path.py", str(raised.exception))
                    self.assertEqual(api.file_reads, failure_attempt)


class MergeAdmissionEvidenceFailureHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_evidence_blocks_controller_without_effect_or_reconciliation(
        self,
    ) -> None:
        for failure_attempt in (1, 2):
            with (
                TemporaryDirectory() as temporary_directory_name,
                patch.dict("os.environ", {"GH_TOKEN": "test-token"}, clear=True),
            ):
                state_dir = Path(temporary_directory_name) / "state"
                _seed_merge_train_policy(state_dir)
                store = FilesystemRecordStore(state_dir=state_dir)
                api = _EvidenceApi(
                    [
                        {"filename": "private/path.py", "status": "modified"},
                        {"filename": "private/path.py", "status": "modified"},
                    ],
                    failure_attempt=failure_attempt,
                )
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_merge_train_service_identity()),
                    authz_policy=_merge_train_service_policy(),
                    record_store_factory=lambda: store,
                    change_impact_repository_evidence_provider=api.provider(),
                )
                with (
                    patch(
                        "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                        _FakeMergeTrainSnapshotReader,
                    ),
                    patch(
                        "control_plane.merge_admission_live.GitHubMergeTrainSnapshotReader",
                        _FakeMergeTrainSnapshotReader,
                    ),
                    patch(
                        "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                        _AdmissionInvokingMergeTrainGitHubClient,
                    ),
                ):
                    responses = [
                        await _post_merge_train_controller_run_once(
                            app,
                            {
                                "schema_version": 1,
                                "repository": "cbusillo/sellyouroutboard",
                                "base_branch": "main",
                                "mutate": True,
                            },
                        )
                        for _ in range(5)
                    ]
                controller_state = store.list_merge_train_controller_state_records(
                    repository="cbusillo/sellyouroutboard", base_branch="main", limit=1
                )[0]
                self.assertEqual(store.list_merge_admission_records(), ())
                self.assertEqual(store.list_merge_landing_outcome_records(), ())
            self.assertTrue(all(response.status_code == 202 for response in responses))
            result = responses[-1].json()["result"]
            self.assertEqual((result["mode"], result["controller_action"]), ("blocked", "block"))
            self.assertEqual(
                result["blocking_reason"],
                {
                    "code": "repository_evidence_unavailable",
                    "message": "Authoritative repository evidence is unavailable for merge admission.",
                },
            )
            self.assertNotIn("private/path.py", responses[-1].text)
            self.assertEqual(api.file_reads, failure_attempt)
            self.assertEqual(
                (
                    controller_state.status,
                    controller_state.reconciliation_status,
                    controller_state.lease_owner,
                ),
                ("idle", "clean", ""),
            )
            self.assertEqual(controller_state.last_phase, "admit_pull_request")
