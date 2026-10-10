from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import cast
from unittest.mock import patch

from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    merge_train_policy_sha256,
)
from control_plane.contracts.merge_train_controller_state import (
    build_merge_train_controller_state_record,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import MergeTrainDryRunSnapshot, build_merge_train_dry_run_result
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerLeaseContext,
    MergeTrainControllerRunOnceEnvelope,
    _apply_queue_block,
)
from control_plane.ordinary_agent_merge_train_client import _NoAmbientTransport
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _FakeExpandedMergeTrainSnapshotReader,
    _FakeMergeTrainGitHubClient,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
)


class QueueBlockingTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicitly_enqueued_failure_is_blocked_then_green_pr_can_proceed(self) -> None:
        for failure_policy in ("pause_train", "continue_after_blocking_pr"):
            with self.subTest(failure_policy=failure_policy):
                await self._assert_queue_progress(failure_policy)

    async def test_failing_auto_admitted_update_does_not_gain_a_persistent_block(self) -> None:
        for failure_policy in ("pause_train", "continue_after_blocking_pr"):
            with self.subTest(failure_policy=failure_policy):
                await self._assert_queue_progress(failure_policy, automatically_admitted=True)

    async def _assert_queue_progress(
        self, failure_policy: str, *, automatically_admitted: bool = False
    ) -> None:
        labels: list[tuple[int, str]] = []

        class Reader(_FakeExpandedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(
                self, *, repository: str, base_branch: str
            ) -> MergeTrainDryRunSnapshot:
                snapshot = super().read_merge_train_snapshot(
                    repository=repository, base_branch=base_branch
                )
                failing, green = snapshot.pull_requests
                return snapshot.model_copy(
                    update={
                        "pull_requests": (
                            failing.model_copy(
                                update={
                                    "labels": (() if automatically_admitted else failing.labels)
                                    + tuple(label for number, label in labels if number == 1),
                                    "label_actors": (
                                        () if automatically_admitted else failing.label_actors
                                    ),
                                    "actor_id": 42,
                                    "actor_role": "trusted_automation",
                                    "dependency_update_class": "patch_or_minor",
                                    "required_checks_status": "fail",
                                }
                            ),
                            green,
                        )
                    }
                )

        class Client(_FakeMergeTrainGitHubClient):
            def add_pull_request_label(
                self, *, repository: str, pull_request_number: int, label: str
            ) -> None:
                labels.append((pull_request_number, label))

        with (
            TemporaryDirectory() as directory,
            patch("control_plane.http_app.resolve_merge_train_github_token", return_value="token"),
            patch("control_plane.merge_train_github.GitHubMergeTrainSnapshotReader", Reader),
            patch("control_plane.merge_train_controller_run_once.GitHubMergeTrainClient", Client),
        ):
            state_dir = Path(directory) / "state"
            record = build_test_merge_train_policy_record()
            repository_policy = record.policy.policies[0]
            repository_policy = repository_policy.model_copy(
                update={
                    "failure_policy": failure_policy,
                    "enqueue": repository_policy.enqueue.model_copy(
                        update={
                            "dependency_update_github_user_ids": (42,),
                            "trusted_automation_github_user_ids": (42,),
                        }
                    ),
                }
            )
            record = MergeTrainPolicyRecord(
                record_id=record.record_id,
                source=record.source,
                updated_at=record.updated_at,
                policy=record.policy.model_copy(update={"policies": (repository_policy,)}),
            )
            _seed_merge_train_policy(state_dir, policy=record)
            store = FilesystemRecordStore(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            request = {"repository": repository_policy.repository, "base_branch": "main"}
            dry = await _post_merge_train_controller_run_once(app, {**request, "mutate": False})
            self.assertEqual(dry.status_code, 202, dry.text)
            self.assertEqual(
                dry.json()["result"]["controller_action"],
                "plan_candidate" if automatically_admitted else "block",
            )
            self.assertEqual(labels, [])
            self.assertEqual(store.list_merge_train_batch_candidate_records(), ())
            expected_labels = (
                [] if automatically_admitted else [(1, repository_policy.blocked_label)]
            )
            if not automatically_admitted:
                blocked = await _post_merge_train_controller_run_once(
                    app, {**request, "mutate": True}
                )
                self.assertEqual(blocked.status_code, 202, blocked.text)
                result = blocked.json()["result"]
                self.assertEqual(result["mode"], "block")
                self.assertEqual(result["block_result"]["pull_request_number"], 1)
                self.assertTrue(result["block_result"]["train_should_continue"])
                feedback = build_feedback_payloads(response=blocked.json())
                self.assertEqual([entry["pull_request_number"] for entry in feedback], [1])
                self.assertIn("required checks failed", cast(str, feedback[0]["message"]))
                self.assertIn("remove", cast(str, feedback[0]["message"]))
                self.assertEqual(labels, expected_labels)
                self.assertEqual(store.list_merge_train_batch_candidate_records(), ())
            progressed = await _post_merge_train_controller_run_once(
                app, {**request, "mutate": True}
            )
            self.assertEqual(progressed.status_code, 202, progressed.text)
            self.assertEqual(progressed.json()["result"]["controller_action"], "plan_candidate")
            self.assertEqual(
                [
                    entry["pull_request_number"]
                    for entry in progressed.json()["result"]["candidate"]["entries"]
                ],
                [2],
            )
            self.assertEqual(labels, expected_labels)
            for action in ("build_candidate", "observe_candidate", "plan_landing", "land_batch"):
                response = await _post_merge_train_controller_run_once(
                    app, {**request, "mutate": True}
                )
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["result"]["controller_action"], action)
            landing = next(
                record.landing_plan
                for record in store.list_merge_train_batch_landing_plan_records()
                if record.record_id
                == response.json()["records"]["merge_train_batch_landing_plan_record_id"]
            )
            self.assertEqual(
                [(entry.pull_request_number, entry.status) for entry in landing.entries],
                [(2, "merged")],
            )
            self.assertEqual(labels, expected_labels)

    def test_bound_controller_reports_queue_block_without_ambient_provider_write(self) -> None:
        policy = build_test_merge_train_policy_record().policy
        snapshot = _FakeExpandedMergeTrainSnapshotReader(transport=None).read_merge_train_snapshot(
            repository=policy.policies[0].repository, base_branch="main"
        )
        failing, green = snapshot.pull_requests
        snapshot = snapshot.model_copy(
            update={
                "pull_requests": (
                    failing.model_copy(update={"required_checks_status": "fail"}),
                    green,
                ),
            }
        )
        intent = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
        self.assertEqual(intent.intended_next_action, "block")
        record = build_merge_train_controller_state_record(
            repository=snapshot.repository,
            base_branch=snapshot.base_branch,
            policy_key=policy.policies[0].policy_key,
            policy_sha256=merge_train_policy_sha256(policy),
            updated_at="2026-10-06T12:00:00Z",
        ).model_copy(
            update={
                "ordinary_job_binding": OrdinaryAgentJobBinding(
                    request_id="fixture-bound-job",
                    scope_sha256="0" * 64,
                    binding_revision=1,
                ),
            }
        )
        lease = MergeTrainControllerLeaseContext(record=record)
        result = _apply_queue_block(
            request=MergeTrainControllerRunOnceEnvelope(
                repository=snapshot.repository, mutate=True
            ),
            dry_run_result=intent,
            github_client=GitHubMergeTrainClient(transport=_NoAmbientTransport()),
            lease=lease,
        )
        self.assertEqual(result["controller_action"], "block")
        self.assertEqual(result["mode"], "dry-run")
        self.assertNotIn("block_result", result)


class BlockLabelTests(unittest.TestCase):
    def test_missing_label_is_created_before_applying_to_pull_request(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                MergeTrainGitHubError("missing label", status_code=422),
                MergeTrainGitHubError("not found", status_code=404),
                {},
                {},
            )
        )
        client = GitHubMergeTrainClient(transport=transport)
        client.add_pull_request_label(
            repository="example/repo", pull_request_number=1, label="held"
        )
        self.assertEqual(
            [(request.method, request.path) for request in transport.requests],
            [
                ("POST", "/repos/example/repo/issues/1/labels"),
                ("GET", "/repos/example/repo/labels/held"),
                ("POST", "/repos/example/repo/labels"),
                ("POST", "/repos/example/repo/issues/1/labels"),
            ],
        )

    def test_label_creation_race_reads_existing_label_before_retry(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                MergeTrainGitHubError("missing label", status_code=422),
                MergeTrainGitHubError("not found", status_code=404),
                MergeTrainGitHubError("already exists", status_code=422),
                {"name": "held"},
                {},
            )
        )
        GitHubMergeTrainClient(transport=transport).add_pull_request_label(
            repository="example/repo", pull_request_number=1, label="held"
        )
        self.assertEqual(transport.requests[-2].method, "GET")
        self.assertEqual(transport.requests[-1].path, "/repos/example/repo/issues/1/labels")

    def test_other_label_errors_do_not_create_or_retry(self) -> None:
        for responses in (
            (MergeTrainGitHubError("forbidden", status_code=403),),
            (MergeTrainGitHubError("invalid", status_code=422), {"name": "held"}),
            (
                MergeTrainGitHubError("invalid", status_code=422),
                MergeTrainGitHubError("unavailable", status_code=503),
            ),
        ):
            with self.subTest(responses=responses):
                transport = RecordingMergeTrainGitHubTransport(responses=responses)
                with self.assertRaises(MergeTrainGitHubError):
                    GitHubMergeTrainClient(transport=transport).add_pull_request_label(
                        repository="example/repo", pull_request_number=1, label="held"
                    )
                self.assertEqual(len(transport.requests), len(responses))
                self.assertFalse(
                    any(
                        request.path == "/repos/example/repo/labels"
                        for request in transport.requests
                    )
                )
