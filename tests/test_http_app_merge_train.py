import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from click import ClickException

from control_plane.contracts.merge_train_controller_state import (
    build_merge_train_controller_state_record,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    parse_merge_train_policy_toml,
)
from control_plane.contracts.merge_train_stack_collapse import (
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import (
    _asgi_get,
    _BatchLandingWithoutLandingPlanStore,
    _CountingBatchCandidateMergeTrainSnapshotReader,
    _CountingMergeTrainSnapshotReader,
    _MergeTrainPolicyOnlyStore,
    _MissingProductReadStore,
    _post_merge_train_batch_candidate_run_once,
    _post_merge_train_batch_landing_run_once,
    _post_merge_train_controller_run_once,
    _post_merge_train_pr_feedback,
    _post_merge_train_run_once,
    _post_merge_train_stack_collapse_run_once,
    _RejectingVerifier,
    _StackCollapseWithoutBatchCandidateStore,
    _StaleMergeTrainSnapshotReader,
    _terminal_agent_merge_train_pr_feedback_policy,
    _terminal_agent_merge_train_run_once_policy,
    _UnavailableBatchCandidateMergeTrainGitHubClient,
    _UnavailableMergeTrainSnapshotReader,
    _UnavailableWorkerMergeTrainGitHubClient,
    _UnexpectedBatchCandidateMergeTrainGitHubClient,
    _UnsupportedStackMergeTrainSnapshotReader,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills
from tests.support.auth import _identity, _local_operator_policy, _StubVerifier
from tests.support.merge_train import (
    _CleanupFailingMergeTrainGitHubClient,
    _FakeCollapsedRootStackedMergeTrainSnapshotReader,
    _FakeExpandedMergeTrainSnapshotReader,
    _FakeFailingMergeTrainGitHubClient,
    _FakeMergeTrainGitHubClient,
    _FakeMergeTrainSnapshotReader,
    _FakeMovedRootStackedMergeTrainSnapshotReader,
    _FakeStackedMergeTrainSnapshotReader,
    _UnavailableLandingMergeTrainGitHubClient,
    _mark_merge_train_batch_candidate_record_passed,
    _merge_train_policy_table,
    _merge_train_run_record,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_admitted_merge_train_stack_collapse_candidate,
    _seed_executed_merge_train_stack_collapse_plan_record,
    _seed_merge_train_batch_candidate_record,
    _seed_merge_train_policy,
    _seed_merge_train_stack_collapse_plan_record,
    _StackCollapseWriteFailingFilesystemRecordStore,
    _StaleLandingMergeTrainGitHubClient,
)


class FastApiMergeTrainReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_reads_store_decision(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/admission?repository=cbusillo/sellyouroutboard&base_branch=main",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["admission"]["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(payload["admission"]["base_branch"], "main")
        self.assertEqual(payload["admission"]["status"], "admitted")
        self.assertEqual(payload["admission"]["reason_code"], "no_prior_run")

    async def test_controller_status_reads_stored_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            run_record = _merge_train_run_record(recorded_at="2026-05-20T12:00:00Z")
            store.write_merge_train_run_record(run_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/controller/status?repository=cbusillo/sellyouroutboard&base_branch=main",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        controller_status = response.json()["controller_status"]
        self.assertEqual(controller_status["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(controller_status["base_branch"], "main")
        self.assertEqual(controller_status["latest_run"]["run_id"], run_record.run_id)
        self.assertEqual(controller_status["latest_dry_run"]["queue_count"], 1)
        self.assertEqual(controller_status["latest_dry_run"]["selected_pr_number"], 1)

    async def test_policy_targets_lists_authorized_policy_targets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            policy_record = _seed_merge_train_policy(
                state_dir,
                policy=MergeTrainPolicyRecord(
                    record_id="merge-train-policy-targets-fastapi-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-13T21:00:00Z",
                    policy=parse_merge_train_policy_toml(
                        "\n\n".join(
                            (
                                "schema_version = 1",
                                _merge_train_policy_table("cbusillo/sellyouroutboard", "release"),
                                _merge_train_policy_table(
                                    "cbusillo/codex-skills",
                                    "main",
                                    scheduler_enabled=True,
                                    scheduler_runner_mode="level1",
                                    scheduler_mutate=True,
                                ),
                            )
                        )
                    ),
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/policy-targets",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["policy"]["record_id"], policy_record.record_id)
        self.assertEqual(payload["policy"]["policy_sha256"], policy_record.policy_sha256)
        self.assertEqual(
            [(target["repository"], target["base_branch"]) for target in payload["targets"]],
            [("cbusillo/codex-skills", "main"), ("cbusillo/sellyouroutboard", "release")],
        )
        self.assertEqual(payload["targets"][0]["scheduler"]["runner_mode"], "level1")

    async def test_policy_targets_allows_local_operator_visibility(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(
                state_dir,
                policy=MergeTrainPolicyRecord(
                    record_id="merge-train-policy-targets-local-operator-fastapi-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-13T21:00:00Z",
                    policy=build_test_merge_train_policy_with_codex_skills(),
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("merge_train.policy_targets",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                ),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/policy-targets",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [
                (target["repository"], target["base_branch"])
                for target in response.json()["targets"]
            ],
            [("cbusillo/codex-skills", "main"), ("cbusillo/sellyouroutboard", "main")],
        )

    async def test_admission_rejects_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: store,
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/admission?repository=cbusillo/sellyouroutboard&base_branch=main",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_openapi_includes_merge_train_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        expected_routes = {
            "/v1/work-graph/merge-train/admission": "MergeTrainAdmissionResponse",
            "/v1/work-graph/merge-train/controller/status": "MergeTrainControllerStatusResponse",
            "/v1/work-graph/merge-train/policy-targets": "MergeTrainPolicyTargetsResponse",
        }
        for path, response_model_name in expected_routes.items():
            route = openapi["paths"][path]["get"]
            success_schema = route["responses"]["200"]["content"]["application/json"]["schema"]
            self.assertEqual(success_schema["$ref"], f"#/components/schemas/{response_model_name}")
            self.assertTrue(set(route["responses"]) >= {"200", "401", "403", "503"})
            self.assertEqual(
                openapi["components"]["schemas"][response_model_name]["additionalProperties"],
                False,
            )


class FastApiMergeTrainBatchLandingRunOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_plans_from_passed_candidate(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            passed_candidate_record = _seed_merge_train_batch_candidate_record(
                state_dir,
                status="passed",
                required_checks_status="pass",
                candidate_sha="candidate-built",
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_merge_train_batch_landing_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mode": "plan",
                    "candidate_record_id": passed_candidate_record.record_id,
                },
            )
            payload = response.json()
            listed_records = store.list_merge_train_batch_landing_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "plan")
        self.assertEqual(payload["result"]["landing_plan"]["entries"][0]["status"], "planned")
        self.assertEqual(
            listed_records[0].record_id,
            payload["records"]["merge_train_batch_landing_plan_record_id"],
        )

    async def test_lands_existing_plan_and_cleans_candidate_ref(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            passed_candidate_record = _seed_merge_train_batch_candidate_record(
                state_dir,
                status="passed",
                required_checks_status="pass",
                candidate_sha="candidate-built",
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                plan_response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                        "candidate_record_id": passed_candidate_record.record_id,
                    },
                )
                response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "land",
                        "landing_plan_record_id": plan_response.json()["records"][
                            "merge_train_batch_landing_plan_record_id"
                        ],
                    },
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["mode"], "land")
        self.assertEqual(response.json()["result"]["candidate_ref_cleanup_status"], "deleted")
        self.assertEqual(
            response.json()["result"]["landing_plan"]["entries"][0]["status"], "merged"
        )
        self.assertEqual(
            response.json()["result"]["landing_plan"]["entries"][0]["merge_commit_sha"],
            "merge-1",
        )

    async def test_records_stale_plan_before_returning_stale_github_state(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            passed_candidate_record = _seed_merge_train_batch_candidate_record(
                state_dir,
                status="passed",
                required_checks_status="pass",
                candidate_sha="candidate-built",
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _StaleLandingMergeTrainGitHubClient,
            ):
                plan_response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                        "candidate_record_id": passed_candidate_record.record_id,
                    },
                )
                response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "land",
                        "landing_plan_record_id": plan_response.json()["records"][
                            "merge_train_batch_landing_plan_record_id"
                        ],
                    },
                )
            landing_records = store.list_merge_train_batch_landing_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "merge_train_github_stale_state")
        self.assertTrue(
            any(record.landing_plan.entries[0].status == "stale" for record in landing_records)
        )

    async def test_persists_land_before_cleanup_failure(self) -> None:
        _CleanupFailingMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls = 0
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            passed_candidate_record = _seed_merge_train_batch_candidate_record(
                state_dir,
                status="passed",
                required_checks_status="pass",
                candidate_sha="candidate-built",
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _CleanupFailingMergeTrainGitHubClient,
            ):
                plan_response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                        "candidate_record_id": passed_candidate_record.record_id,
                    },
                )
                response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "land",
                        "landing_plan_record_id": plan_response.json()["records"][
                            "merge_train_batch_landing_plan_record_id"
                        ],
                    },
                )
            landing_records = store.list_merge_train_batch_landing_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["candidate_ref_cleanup_status"], "failed")
        self.assertEqual(
            response.json()["result"]["candidate_ref_cleanup_message"],
            "candidate ref cleanup unavailable",
        )
        self.assertEqual(response.json()["result"]["candidate_ref_cleanup_github_status_code"], 503)
        self.assertEqual(_CleanupFailingMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls, 1)
        self.assertTrue(
            any(
                record.landing_plan.entries[0].status == "merged"
                and record.landing_plan.entries[0].merge_commit_sha == "merge-1"
                for record in landing_records
            )
        )

    async def test_closes_stack_children_after_root_lands(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            executed_record_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            admitted_candidate_record = _seed_admitted_merge_train_stack_collapse_candidate(
                state_dir,
                executed_record_id=executed_record_id,
            )
            passed_candidate_record = _mark_merge_train_batch_candidate_record_passed(
                state_dir,
                record_id=admitted_candidate_record.record_id,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                plan_response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                        "candidate_record_id": passed_candidate_record.record_id,
                    },
                )
                response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "land",
                        "landing_plan_record_id": plan_response.json()["records"][
                            "merge_train_batch_landing_plan_record_id"
                        ],
                        "stack_collapse_plan_record_id": executed_record_id,
                    },
                )
            disposition_record_id = response.json()["records"][
                "merge_train_stack_collapse_plan_record_id"
            ]
            stack_records = store.list_merge_train_stack_collapse_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )
            disposition_record = next(
                record for record in stack_records if record.record_id == disposition_record_id
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["result"]["stack_collapse_plan"]["status"], "ready_for_train"
        )
        self.assertEqual(
            response.json()["result"]["stack_collapse_plan"]["child_dispositions"][0]["status"],
            "closed",
        )
        self.assertEqual(disposition_record.plan.child_dispositions[0].status, "closed")
        self.assertEqual(
            disposition_record.plan.child_dispositions[0].comment_url,
            "https://github.com/cbusillo/sellyouroutboard/pull/2#issuecomment-1",
        )

    async def test_persists_root_merge_before_child_record_failure(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            executed_record_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            admitted_candidate_record = _seed_admitted_merge_train_stack_collapse_candidate(
                state_dir,
                executed_record_id=executed_record_id,
            )
            passed_candidate_record = _mark_merge_train_batch_candidate_record_passed(
                state_dir,
                record_id=admitted_candidate_record.record_id,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                plan_response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                        "candidate_record_id": passed_candidate_record.record_id,
                    },
                )
            failing_store = _StackCollapseWriteFailingFilesystemRecordStore(state_dir)
            failing_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: failing_store,
            )
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                response = await _post_merge_train_batch_landing_run_once(
                    failing_app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "land",
                        "landing_plan_record_id": plan_response.json()["records"][
                            "merge_train_batch_landing_plan_record_id"
                        ],
                        "stack_collapse_plan_record_id": executed_record_id,
                    },
                    capture_server_error_response=True,
                )
            landing_records = store.list_merge_train_batch_landing_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 500)
        self.assertTrue(
            any(
                record.landing_plan.entries[0].status == "merged"
                and record.landing_plan.entries[0].merge_commit_sha == "merge-1"
                for record in landing_records
            )
        )

    async def test_validates_stack_before_root_merge(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            stale_stack_record_id = _seed_merge_train_stack_collapse_plan_record(state_dir)
            executed_record_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            admitted_candidate_record = _seed_admitted_merge_train_stack_collapse_candidate(
                state_dir,
                executed_record_id=executed_record_id,
            )
            passed_candidate_record = _mark_merge_train_batch_candidate_record_passed(
                state_dir,
                record_id=admitted_candidate_record.record_id,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            _FakeMergeTrainGitHubClient.land_batch_candidate_calls = 0
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                plan_response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                        "candidate_record_id": passed_candidate_record.record_id,
                    },
                )
                response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "land",
                        "landing_plan_record_id": plan_response.json()["records"][
                            "merge_train_batch_landing_plan_record_id"
                        ],
                        "stack_collapse_plan_record_id": stale_stack_record_id,
                    },
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(_FakeMergeTrainGitHubClient.land_batch_candidate_calls, 0)

    async def test_rejects_policy_digest_mismatch_before_root_merge(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            executed_record_id = _seed_executed_merge_train_stack_collapse_plan_record(state_dir)
            admitted_candidate_record = _seed_admitted_merge_train_stack_collapse_candidate(
                state_dir,
                executed_record_id=executed_record_id,
            )
            passed_candidate_record = _mark_merge_train_batch_candidate_record_passed(
                state_dir,
                record_id=admitted_candidate_record.record_id,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                plan_response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                        "candidate_record_id": passed_candidate_record.record_id,
                    },
                )
            _seed_merge_train_policy(
                state_dir,
                policy=MergeTrainPolicyRecord(
                    record_id="merge-train-policy-20260513T220000Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-13T22:00:00Z",
                    policy=build_test_merge_train_policy_with_codex_skills(),
                ),
            )
            _FakeMergeTrainGitHubClient.land_batch_candidate_calls = 0
            with patch(
                "control_plane.merge_train_batch_landing.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                response = await _post_merge_train_batch_landing_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "land",
                        "landing_plan_record_id": plan_response.json()["records"][
                            "merge_train_batch_landing_plan_record_id"
                        ],
                        "stack_collapse_plan_record_id": executed_record_id,
                    },
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(_FakeMergeTrainGitHubClient.land_batch_candidate_calls, 0)

    async def test_replays_idempotent_plan_without_rewriting_landing_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            passed_candidate_record = _seed_merge_train_batch_candidate_record(
                state_dir,
                status="passed",
                required_checks_status="pass",
                candidate_sha="candidate-built",
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            request_payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mode": "plan",
                "candidate_record_id": passed_candidate_record.record_id,
            }
            first_response = await _post_merge_train_batch_landing_run_once(
                app,
                request_payload,
                idempotency_key="merge-train-batch-landing-plan",
            )
            replay_response = await _post_merge_train_batch_landing_run_once(
                app,
                request_payload,
                idempotency_key="merge-train-batch-landing-plan",
            )
            landing_records = store.list_merge_train_batch_landing_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(len(landing_records), 1)

    async def test_rejects_missing_landing_storage(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            passed_candidate_record = _seed_merge_train_batch_candidate_record(
                state_dir,
                status="passed",
                required_checks_status="pass",
                candidate_sha="candidate-built",
            )
            store = _BatchLandingWithoutLandingPlanStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            response = await _post_merge_train_batch_landing_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mode": "plan",
                    "candidate_record_id": passed_candidate_record.record_id,
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_openapi_includes_batch_landing_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/work-graph/merge-train/batch-landing/run-once"][
            "post"
        ]
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        self.assertEqual(request_schema["title"], "MergeTrainBatchLandingRunOnceEnvelope")
        self.assertTrue(set(route["responses"]) >= {"400", "401", "403", "409", "502", "503"})


class FastApiMergeTrainStackCollapseRunOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_executes_existing_plan_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            plan_record_id = _seed_merge_train_stack_collapse_plan_record(state_dir)
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "stack_collapse_plan_record_id": plan_record_id,
                    },
                )
            payload = response.json()
            executed_record_id = payload["records"]["merge_train_stack_collapse_plan_record_id"]
            executed_records = store.list_merge_train_stack_collapse_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )
            executed_record = next(
                record for record in executed_records if record.record_id == executed_record_id
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "execute")
        self.assertEqual(
            payload["result"]["stack_collapse_plan"]["status"], "waiting_for_root_checks"
        )
        self.assertEqual(
            payload["result"]["stack_collapse_plan"]["mutations"][0]["status"], "mutated"
        )
        self.assertEqual(
            payload["result"]["stack_collapse_plan"]["mutations"][0]["merge_commit_sha"],
            "stack-merge-2-into-1",
        )
        self.assertEqual(executed_record.plan.status, "waiting_for_root_checks")

    async def test_admits_executed_root_only(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            plan_record_id = _seed_merge_train_stack_collapse_plan_record(state_dir)
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                execute_response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "execute",
                        "stack_collapse_plan_record_id": plan_record_id,
                    },
                )
            executed_record_id = execute_response.json()["records"][
                "merge_train_stack_collapse_plan_record_id"
            ]
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainSnapshotReader",
                _FakeCollapsedRootStackedMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "admit",
                        "stack_collapse_plan_record_id": executed_record_id,
                    },
                )
            payload = response.json()
            candidate_record_id = payload["records"]["merge_train_batch_candidate_record_id"]
            candidate_records = store.list_merge_train_batch_candidate_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "admit")
        self.assertEqual(payload["result"]["candidate"]["entries"][0]["pull_request_number"], 1)
        self.assertEqual(len(payload["result"]["candidate"]["entries"]), 1)
        candidate_record = next(
            record for record in candidate_records if record.record_id == candidate_record_id
        )
        self.assertEqual(candidate_record.candidate.entries[0].pull_request_number, 1)

    async def test_rejects_admit_when_root_head_moves(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            plan_record_id = _seed_merge_train_stack_collapse_plan_record(state_dir)
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                execute_response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "execute",
                        "stack_collapse_plan_record_id": plan_record_id,
                    },
                )
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainSnapshotReader",
                _FakeMovedRootStackedMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "admit",
                        "stack_collapse_plan_record_id": execute_response.json()["records"][
                            "merge_train_stack_collapse_plan_record_id"
                        ],
                    },
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(response.json()["error"]["message"], "Request could not be completed.")

    async def test_rejects_admit_when_policy_digest_changes(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            plan_record_id = _seed_merge_train_stack_collapse_plan_record(state_dir)
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                execute_response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "execute",
                        "stack_collapse_plan_record_id": plan_record_id,
                    },
                )
            _seed_merge_train_policy(
                state_dir,
                policy=MergeTrainPolicyRecord(
                    record_id="merge-train-policy-20260513T220000Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-13T22:00:00Z",
                    policy=build_test_merge_train_policy_with_codex_skills(),
                ),
            )
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainSnapshotReader",
                _FakeStackedMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "admit",
                        "stack_collapse_plan_record_id": execute_response.json()["records"][
                            "merge_train_stack_collapse_plan_record_id"
                        ],
                    },
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(response.json()["error"]["message"], "Request could not be completed.")

    async def test_replays_idempotent_execute_without_reexecuting_stack(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            plan_record_id = _seed_merge_train_stack_collapse_plan_record(state_dir)
            request_payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mode": "execute",
                "stack_collapse_plan_record_id": plan_record_id,
            }
            with patch(
                "control_plane.merge_train_stack_collapse.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                first_response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    request_payload,
                    idempotency_key="merge-train-stack-collapse-execute",
                )
                replay_response = await _post_merge_train_stack_collapse_run_once(
                    app,
                    request_payload,
                    idempotency_key="merge-train-stack-collapse-execute",
                )
            stack_records = store.list_merge_train_stack_collapse_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(len(stack_records), 2)

    async def test_rejects_missing_stack_collapse_storage(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = _MergeTrainPolicyOnlyStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            response = await _post_merge_train_stack_collapse_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mode": "execute",
                    "stack_collapse_plan_record_id": "missing-stack-collapse-plan",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_rejects_missing_batch_candidate_storage_for_admit(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            plan_record_id = _seed_merge_train_stack_collapse_plan_record(state_dir)
            full_store = FilesystemRecordStore(state_dir=state_dir)
            executed_record = execute_merge_train_stack_collapse_plan(
                plan=next(
                    record
                    for record in full_store.list_merge_train_stack_collapse_plan_records(
                        repository="cbusillo/sellyouroutboard", base_branch="main"
                    )
                    if record.record_id == plan_record_id
                ).plan,
                branch_client=_FakeMergeTrainGitHubClient(transport=object()),
                updated_at="2026-05-13T21:02:00Z",
            )
            executed_plan_record = build_merge_train_stack_collapse_plan_record(
                plan=executed_record,
                source="test:execute",
                updated_at="2026-05-13T21:02:00Z",
            )
            full_store.write_merge_train_stack_collapse_plan_record(executed_plan_record)
            store = _StackCollapseWithoutBatchCandidateStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            response = await _post_merge_train_stack_collapse_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mode": "admit",
                    "stack_collapse_plan_record_id": executed_plan_record.record_id,
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")
        self.assertEqual(
            response.json()["error"]["message"],
            "Merge train stack collapse admission requires database-backed candidate records.",
        )

    async def test_openapi_includes_stack_collapse_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/work-graph/merge-train/stack-collapse/run-once"][
            "post"
        ]
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        self.assertEqual(request_schema["title"], "MergeTrainStackCollapseRunOnceEnvelope")
        self.assertTrue(set(route["responses"]) >= {"400", "401", "403", "409", "502", "503"})


class FastApiMergeTrainRunOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_dry_run_from_policy_and_records_run(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            policy_record = _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch("control_plane.merge_train_run_once.GitHubMergeTrainClient") as github_client,
            ):
                response = await _post_merge_train_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                    },
                )
            payload = response.json()
            run_id = payload["records"]["merge_train_run_id"]
            loaded_record = FilesystemRecordStore(state_dir).read_merge_train_run_record(run_id)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertEqual(payload["result"]["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(payload["result"]["base_branch"], "main")
        self.assertEqual(payload["result"]["dry_run_result"]["intended_next_action"], "merge")
        self.assertEqual(loaded_record.mode, "dry_run")
        self.assertEqual(loaded_record.status, "merge")
        self.assertEqual(loaded_record.selected_pr_number, 1)
        self.assertEqual(loaded_record.selected_head_sha, "head-1")
        self.assertEqual(loaded_record.policy_key, "cbusillo/sellyouroutboard:main")
        self.assertEqual(loaded_record.policy_sha256, policy_record.policy_sha256)
        self.assertEqual(loaded_record.worker_step_result, None)
        github_client.assert_not_called()

    async def test_mutates_one_worker_step_and_records_worker_result(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                response = await _post_merge_train_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mutate": True,
                    },
                )
            payload = response.json()
            run_id = payload["records"]["merge_train_run_id"]
            loaded_record = FilesystemRecordStore(state_dir).read_merge_train_run_record(run_id)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["status"], "merged")
        self.assertEqual(payload["result"]["intended_next_action"], "merge")
        self.assertEqual(payload["result"]["selected_pr_number"], 1)
        self.assertEqual(loaded_record.mode, "mutate")
        self.assertEqual(loaded_record.status, "merged")
        self.assertTrue(loaded_record.reread_required)
        self.assertFalse(loaded_record.poll_required)
        self.assertIsNotNone(loaded_record.worker_step_result)

    async def test_replays_idempotent_request_without_reexecuting_github_read(self) -> None:
        request_payload = {
            "schema_version": 1,
            "repository": "cbusillo/sellyouroutboard",
            "base_branch": "main",
        }
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            _CountingMergeTrainSnapshotReader.read_calls = 0
            with patch(
                "control_plane.merge_train_run_once.GitHubMergeTrainSnapshotReader",
                _CountingMergeTrainSnapshotReader,
            ):
                first_response = await _post_merge_train_run_once(
                    app,
                    request_payload,
                    idempotency_key="merge-train-run-once-dry-run",
                )
                replay_response = await _post_merge_train_run_once(
                    app,
                    request_payload,
                    idempotency_key="merge-train-run-once-dry-run",
                )
            records = FilesystemRecordStore(state_dir).list_merge_train_run_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(
            replay_response.json()["records"]["merge_train_run_id"],
            first_response.json()["records"]["merge_train_run_id"],
        )
        self.assertEqual(_CountingMergeTrainSnapshotReader.read_calls, 1)
        self.assertEqual(len(records), 1)

    async def test_rejects_reused_idempotency_key_with_different_payload(self) -> None:
        request_payload = {
            "schema_version": 1,
            "repository": "cbusillo/sellyouroutboard",
            "base_branch": "main",
        }
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_run_once.GitHubMergeTrainSnapshotReader",
                _FakeMergeTrainSnapshotReader,
            ):
                first_response = await _post_merge_train_run_once(
                    app,
                    request_payload,
                    idempotency_key="merge-train-run-once-conflict",
                )
                conflict_response = await _post_merge_train_run_once(
                    app,
                    {**request_payload, "base_branch": "release"},
                    idempotency_key="merge-train-run-once-conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_rejects_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: store,
            )
            with patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True):
                response = await _post_merge_train_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                    },
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_rejects_terminal_agent_bearer_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_merge_train_run_once_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )
            response = await _post_merge_train_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                },
                authorization="Bearer terminal-read-token",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_rejects_missing_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch.dict("os.environ", {}, clear=True):
                response = await _post_merge_train_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                    },
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "github_token_not_configured")

    async def test_rejects_unsupported_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            response = await _post_merge_train_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/other",
                    "base_branch": "main",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_rejects_invalid_payload_as_launchplane_invalid_request(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_merge_train_run_once(
            app,
            {"schema_version": 1, "repository": "cbusillo", "base_branch": "main"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_rejects_missing_policy_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            response = await _post_merge_train_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "merge_train_policy_not_configured")

    async def test_uses_configured_codex_skills_policy(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(
                state_dir,
                policy=MergeTrainPolicyRecord(
                    record_id="merge-train-policy-codex-skills-fastapi-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-13T21:00:00Z",
                    policy=build_test_merge_train_policy_with_codex_skills(),
                ),
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_run_once.GitHubMergeTrainSnapshotReader",
                _FakeMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/codex-skills",
                        "base_branch": "main",
                    },
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["repository"], "cbusillo/codex-skills")
        self.assertEqual(response.json()["result"]["base_branch"], "main")
        self.assertEqual(
            response.json()["result"]["dry_run_result"]["policy_key"],
            "cbusillo/codex-skills:main",
        )

    async def test_maps_stale_github_state(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_run_once.GitHubMergeTrainSnapshotReader",
                _StaleMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                    },
                )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "merge_train_github_stale_state")
        self.assertEqual(payload["details"]["github_status_code"], 409)

    async def test_maps_github_request_failure(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_run_once.GitHubMergeTrainSnapshotReader",
                _UnavailableMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                    },
                )

        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "github_request_failed")
        self.assertEqual(payload["details"]["github_status_code"], 503)

    async def test_maps_mutating_worker_github_request_failure(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_run_once.GitHubMergeTrainClient",
                    _UnavailableWorkerMergeTrainGitHubClient,
                ),
            ):
                response = await _post_merge_train_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mutate": True,
                    },
                )
            records = FilesystemRecordStore(state_dir).list_merge_train_run_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "github_request_failed")
        self.assertEqual(payload["details"]["github_status_code"], 503)
        self.assertEqual(records, ())

    async def test_openapi_includes_run_once_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/work-graph/merge-train/run-once"]["post"]
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        self.assertEqual(request_schema["title"], "MergeTrainRunOnceEnvelope")
        self.assertTrue(set(route["responses"]) >= {"400", "401", "403", "409", "502", "503"})


class FastApiMergeTrainControllerRunOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_advances_unstacked_batch_flow(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
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
            payloads = [response.json() for response in responses]
            landing_records = store.list_merge_train_batch_landing_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertTrue(all(response.status_code == 202 for response in responses))
        self.assertEqual(payloads[0]["result"]["controller_action"], "plan_candidate")
        self.assertEqual(payloads[1]["result"]["controller_action"], "build_candidate")
        self.assertEqual(payloads[1]["result"]["candidate"]["status"], "ready_for_checks")
        self.assertEqual(payloads[2]["result"]["controller_action"], "observe_candidate")
        self.assertEqual(payloads[2]["result"]["candidate"]["status"], "passed")
        self.assertEqual(payloads[3]["result"]["controller_action"], "plan_landing")
        self.assertEqual(payloads[4]["result"]["controller_action"], "land_batch")
        self.assertEqual(payloads[4]["result"]["candidate_ref_cleanup_status"], "deleted")
        self.assertEqual(payloads[4]["result"]["landing_plan"]["entries"][0]["status"], "merged")
        landed_record = next(
            record
            for record in landing_records
            if record.record_id
            == payloads[4]["records"]["merge_train_batch_landing_plan_record_id"]
        )
        self.assertTrue(landed_record.source.startswith("service:controller:land:"))
        self.assertTrue(
            any(
                record.source.startswith("service:controller:landing-progress:")
                for record in landing_records
            )
        )

    async def test_stale_landing_returns_accepted_result_and_replays_idempotently(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            request_payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mutate": True,
            }
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                for _ in range(4):
                    await _post_merge_train_controller_run_once(app, request_payload)
            with patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _StaleLandingMergeTrainGitHubClient,
            ):
                stale_response = await _post_merge_train_controller_run_once(
                    app, request_payload, idempotency_key="controller-stale-landing"
                )
                replay_response = await _post_merge_train_controller_run_once(
                    app, request_payload, idempotency_key="controller-stale-landing"
                )
            stale_payload = stale_response.json()
            replay_payload = replay_response.json()
            landing_records = store.list_merge_train_batch_landing_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(stale_response.status_code, 202)
        self.assertEqual(stale_payload["result"]["mode"], "stale_landing")
        self.assertEqual(stale_payload["result"]["error"]["code"], "merge_train_github_stale_state")
        self.assertEqual(stale_payload["result"]["details"]["github_status_code"], 409)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(replay_payload["records"], stale_payload["records"])
        stale_record = next(
            record
            for record in landing_records
            if record.record_id
            == stale_payload["records"]["merge_train_batch_landing_plan_record_id"]
        )
        self.assertEqual(stale_record.landing_plan.entries[0].status, "stale")
        self.assertTrue(stale_record.source.startswith("service:controller:stale-landing:"))

    async def test_reflows_failed_candidate_after_queue_changes(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            request_payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mutate": True,
            }
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                await _post_merge_train_controller_run_once(app, request_payload)
                await _post_merge_train_controller_run_once(app, request_payload)
            with patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _FakeFailingMergeTrainGitHubClient,
            ):
                await _post_merge_train_controller_run_once(app, request_payload)
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeExpandedMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                reflow_response = await _post_merge_train_controller_run_once(app, request_payload)
                build_response = await _post_merge_train_controller_run_once(app, request_payload)

        reflow_payload = reflow_response.json()
        build_payload = build_response.json()
        self.assertEqual(reflow_response.status_code, 202)
        self.assertEqual(reflow_payload["result"]["controller_action"], "plan_candidate")
        self.assertEqual(
            [
                entry["pull_request_number"]
                for entry in reflow_payload["result"]["candidate"]["entries"]
            ],
            [1, 2],
        )
        self.assertIn("superseded_merge_train_batch_candidate_record_id", reflow_payload["result"])
        self.assertEqual(build_response.status_code, 202)
        self.assertEqual(build_payload["result"]["controller_action"], "build_candidate")
        self.assertEqual(
            [
                entry["pull_request_number"]
                for entry in build_payload["result"]["candidate"]["entries"]
            ],
            [1, 2],
        )

    async def test_advances_stacked_batch_flow(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            request_payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mutate": True,
            }
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeStackedMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                plan_response = await _post_merge_train_controller_run_once(app, request_payload)
                execute_response = await _post_merge_train_controller_run_once(app, request_payload)
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeCollapsedRootStackedMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                admit_response = await _post_merge_train_controller_run_once(app, request_payload)
                build_response = await _post_merge_train_controller_run_once(app, request_payload)
                observe_response = await _post_merge_train_controller_run_once(app, request_payload)
                landing_plan_response = await _post_merge_train_controller_run_once(
                    app, request_payload
                )
                land_response = await _post_merge_train_controller_run_once(app, request_payload)

        plan_payload = plan_response.json()
        execute_payload = execute_response.json()
        admit_payload = admit_response.json()
        build_payload = build_response.json()
        observe_payload = observe_response.json()
        landing_plan_payload = landing_plan_response.json()
        land_payload = land_response.json()
        self.assertEqual(plan_response.status_code, 202)
        self.assertEqual(plan_payload["result"]["controller_action"], "plan_stack_collapse")
        self.assertEqual(execute_response.status_code, 202)
        self.assertEqual(execute_payload["result"]["controller_action"], "execute_stack_collapse")
        self.assertEqual(admit_response.status_code, 202)
        self.assertEqual(admit_payload["result"]["controller_action"], "admit_collapsed_root")
        self.assertEqual(
            admit_payload["result"]["candidate"]["entries"][0]["pull_request_number"], 1
        )
        self.assertEqual(build_response.status_code, 202)
        self.assertEqual(build_payload["result"]["controller_action"], "build_candidate")
        self.assertEqual(observe_response.status_code, 202)
        self.assertEqual(observe_payload["result"]["candidate"]["status"], "passed")
        self.assertEqual(landing_plan_response.status_code, 202)
        self.assertEqual(landing_plan_payload["result"]["controller_action"], "plan_landing")
        self.assertEqual(land_response.status_code, 202)
        self.assertEqual(land_payload["result"]["controller_action"], "land_batch")
        self.assertEqual(land_payload["result"]["stack_collapse_plan"]["status"], "ready_for_train")
        self.assertEqual(
            land_payload["result"]["stack_collapse_plan"]["child_dispositions"][0]["status"],
            "closed",
        )

    async def test_cleanup_failure_after_landing_is_reported_without_rollback(self) -> None:
        _CleanupFailingMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls = 0
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _CleanupFailingMergeTrainGitHubClient,
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
            payload = responses[-1].json()
            landing_records = store.list_merge_train_batch_landing_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(responses[-1].status_code, 202)
        self.assertEqual(payload["result"]["controller_action"], "land_batch")
        self.assertEqual(payload["result"]["candidate_ref_cleanup_status"], "failed")
        self.assertEqual(payload["result"]["candidate_ref_cleanup_github_status_code"], 503)
        self.assertEqual(_CleanupFailingMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls, 1)
        landed_record = next(
            record
            for record in landing_records
            if record.record_id == payload["records"]["merge_train_batch_landing_plan_record_id"]
        )
        self.assertEqual(landed_record.landing_plan.entries[0].status, "merged")

    async def test_cleanup_failure_is_resumed_from_controller_state(self) -> None:
        _CleanupFailingMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls = 0
        _FakeMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls = 0
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            request_payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mutate": True,
            }
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                for _ in range(4):
                    await _post_merge_train_controller_run_once(app, request_payload)
            with patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _CleanupFailingMergeTrainGitHubClient,
            ):
                failed_response = await _post_merge_train_controller_run_once(
                    app,
                    request_payload,
                    idempotency_key="controller-cleanup-failed",
                )
            failed_state = store.list_merge_train_controller_state_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                limit=1,
            )[0]
            with patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                dry_run_response = await _post_merge_train_controller_run_once(
                    app,
                    {**request_payload, "mutate": False},
                    idempotency_key="controller-cleanup-dry-run",
                )
            dry_run_state = store.list_merge_train_controller_state_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                limit=1,
            )[0]
            dry_run_cleanup_calls = _FakeMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls
            with patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                recovered_response = await _post_merge_train_controller_run_once(
                    app,
                    request_payload,
                    idempotency_key="controller-cleanup-recovered",
                )
            recovered_state = store.list_merge_train_controller_state_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                limit=1,
            )[0]

        self.assertEqual(failed_response.status_code, 202)
        self.assertEqual(failed_state.status, "reconcile_required")
        self.assertEqual(failed_state.active_phase, "cleanup_candidate_ref")
        self.assertEqual(dry_run_response.status_code, 202)
        self.assertEqual(
            dry_run_response.json()["result"]["controller_action"],
            "resume_reconciliation",
        )
        self.assertEqual(dry_run_state.status, "reconcile_required")
        self.assertEqual(
            dry_run_state.reconciliation_detail,
            "retryable:candidate_ref_cleanup_failed",
        )
        self.assertEqual(dry_run_cleanup_calls, 0)
        self.assertEqual(recovered_response.status_code, 202)
        self.assertEqual(
            recovered_response.json()["result"]["candidate_ref_cleanup_status"],
            "deleted",
        )
        self.assertEqual(_CleanupFailingMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls, 1)
        self.assertEqual(_FakeMergeTrainGitHubClient.cleanup_batch_candidate_ref_calls, 1)
        self.assertEqual(recovered_state.status, "idle")
        self.assertEqual(recovered_state.reconciliation_status, "clean")
        self.assertEqual(recovered_state.last_phase, "cleanup_candidate_ref")

    async def test_active_controller_lease_returns_conflict(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_merge_train_controller_state_record(
                build_merge_train_controller_state_record(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    policy_key="cbusillo/sellyouroutboard:main",
                    policy_sha256="policy-sha",
                    updated_at="2099-01-01T00:00:00Z",
                ).model_copy(
                    update={
                        "status": "running",
                        "lease_owner": "controller-a",
                        "lease_acquired_at": "2099-01-01T00:00:00Z",
                        "lease_expires_at": "2099-01-01T00:05:00Z",
                        "heartbeat_at": "2099-01-01T00:00:00Z",
                    }
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_merge_train_controller_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mutate": True,
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "merge_train_controller_lease_held",
        )

    async def test_release_failure_does_not_mask_github_failure(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            request_payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mutate": True,
            }
            with (
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                for _ in range(4):
                    await _post_merge_train_controller_run_once(app, request_payload)

            original_compare_and_set = store.compare_and_set_merge_train_controller_state_record

            def fail_reconciliation_release(**kwargs: object) -> object:
                record = kwargs["record"]
                if getattr(record, "status", "") == "reconcile_required":
                    raise RuntimeError("controller release persistence unavailable")
                return original_compare_and_set(**kwargs)  # type: ignore[arg-type]

            with (
                patch.object(
                    store,
                    "compare_and_set_merge_train_controller_state_record",
                    side_effect=fail_reconciliation_release,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    _UnavailableLandingMergeTrainGitHubClient,
                ),
            ):
                response = await _post_merge_train_controller_run_once(
                    app,
                    request_payload,
                    idempotency_key="controller-release-failure",
                )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "github_request_failed")

    async def test_failure_before_first_action_preserves_valid_reconciliation_state(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                _UnavailableMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_controller_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mutate": True,
                    },
                )
            controller_state = store.list_merge_train_controller_state_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                limit=1,
            )[0]

        self.assertEqual(response.status_code, 502)
        self.assertEqual(controller_state.status, "reconcile_required")
        self.assertEqual(controller_state.active_action, "controller_run_once")
        self.assertEqual(controller_state.active_phase, "select_next_action")
        self.assertEqual(controller_state.reconciliation_status, "required")

    async def test_invalid_controller_payload_uses_invalid_state_envelope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=state_dir),
            )
            response = await _post_merge_train_controller_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "not-a-repository",
                    "base_branch": "main",
                    "mutate": False,
                },
            )

        payload = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "merge_train_controller_invalid_state")
        self.assertIn("merge train repository must be owner/name", payload["error"]["message"])

    async def test_openapi_includes_controller_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("/tmp/unused")),
        )
        route = app.openapi()["paths"]["/v1/work-graph/merge-train/controller/run-once"]["post"]
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]

        self.assertEqual(route["operationId"], "write_merge_train_controller_run_once")
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        self.assertEqual(request_schema["title"], "MergeTrainControllerRunOnceEnvelope")
        self.assertTrue(set(route["responses"]) >= {"400", "401", "403", "409", "502", "503"})


class FastApiMergeTrainMutationFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_controller_lease_fences_all_legacy_mutation_routes(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            policy_record = _seed_merge_train_policy(state_dir)
            repository_policy = policy_record.policy.find_repository_policy(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
            )
            store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key=repository_policy.policy_key,
                policy_sha256=policy_record.policy_sha256,
                lease_owner="controller-active",
                lease_seconds=300,
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            route_requests = (
                (
                    _post_merge_train_batch_candidate_run_once,
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                ),
                (
                    _post_merge_train_batch_landing_run_once,
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                        "candidate_record_id": "candidate-record",
                    },
                ),
                (
                    _post_merge_train_stack_collapse_run_once,
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "execute",
                        "stack_collapse_plan_record_id": "collapse-record",
                    },
                ),
                (
                    _post_merge_train_run_once,
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mutate": True,
                    },
                ),
            )

            responses = [
                await post_request(app, cast(dict[str, object], payload))
                for post_request, payload in route_requests
            ]

        self.assertEqual([response.status_code for response in responses], [409] * 4)
        self.assertEqual(
            [response.json()["error"]["code"] for response in responses],
            ["merge_train_controller_lease_held"] * 4,
        )

    async def test_legacy_candidate_mutation_heartbeats_and_records_idempotency_before_release(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            candidate_record = _seed_merge_train_batch_candidate_record(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            observed_phases: list[str] = []
            idempotency_controller_statuses: list[str] = []
            original_compare_and_set = store.compare_and_set_merge_train_controller_state_record
            original_write_idempotency = store.write_idempotency_record

            def capture_checkpoint(**kwargs: object) -> object:
                record = kwargs["record"]
                observed_phases.append(str(getattr(record, "active_phase", "")))
                return original_compare_and_set(**kwargs)  # type: ignore[arg-type]

            def capture_idempotency(record: object) -> object:
                controller_state = store.list_merge_train_controller_state_records(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    limit=1,
                )[0]
                idempotency_controller_statuses.append(controller_state.status)
                return original_write_idempotency(record)  # type: ignore[arg-type]

            with (
                patch.object(
                    store,
                    "compare_and_set_merge_train_controller_state_record",
                    side_effect=capture_checkpoint,
                ),
                patch.object(
                    store,
                    "write_idempotency_record",
                    side_effect=capture_idempotency,
                ),
                patch(
                    "control_plane.merge_train_batch_candidate.GitHubMergeTrainClient",
                    _FakeMergeTrainGitHubClient,
                ),
            ):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "build",
                        "candidate_record_id": candidate_record.record_id,
                    },
                    idempotency_key="legacy-candidate-heartbeat",
                )
            final_state = store.list_merge_train_controller_state_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                limit=1,
            )[0]

        self.assertEqual(response.status_code, 202)
        self.assertIn("merge_candidate_entry", observed_phases)
        self.assertIn("candidate_entry_merged", observed_phases)
        self.assertEqual(idempotency_controller_statuses, ["running"])
        self.assertEqual(final_state.status, "idle")

    async def test_controller_records_idempotency_before_releasing_lease(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            controller_statuses: list[str] = []
            original_write_idempotency = store.write_idempotency_record

            def capture_idempotency(record: object) -> object:
                controller_state = store.list_merge_train_controller_state_records(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    limit=1,
                )[0]
                controller_statuses.append(controller_state.status)
                return original_write_idempotency(record)  # type: ignore[arg-type]

            with (
                patch.object(
                    store,
                    "write_idempotency_record",
                    side_effect=capture_idempotency,
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
            ):
                response = await _post_merge_train_controller_run_once(
                    app,
                    {
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mutate": True,
                    },
                    idempotency_key="controller-idempotency-before-release",
                )
            final_state = store.list_merge_train_controller_state_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                limit=1,
            )[0]

        self.assertEqual(response.status_code, 202)
        self.assertEqual(controller_statuses, ["running"])
        self.assertEqual(final_state.status, "idle")


class FastApiMergeTrainBatchCandidateRunOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_plans_candidate_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            policy_record = _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                    _FakeMergeTrainSnapshotReader,
                ),
                patch(
                    "control_plane.merge_train_batch_candidate.GitHubMergeTrainClient",
                    _UnexpectedBatchCandidateMergeTrainGitHubClient,
                ),
            ):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )
            payload = response.json()
            record_id = payload["records"]["merge_train_batch_candidate_record_id"]
            listed_records = FilesystemRecordStore(
                state_dir
            ).list_merge_train_batch_candidate_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "plan")
        self.assertEqual(payload["result"]["candidate"]["status"], "planned")
        self.assertEqual(payload["result"]["candidate"]["base_sha"], "current-base-main")
        self.assertEqual(payload["result"]["candidate"]["entries"][0]["pull_request_number"], 1)
        self.assertEqual(listed_records[0].record_id, record_id)
        self.assertEqual(listed_records[0].candidate.policy_key, "cbusillo/sellyouroutboard:main")
        self.assertEqual(listed_records[0].candidate.policy_sha256, policy_record.policy_sha256)

    async def test_plans_stack_collapse_first_without_candidate_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                _FakeStackedMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )
            payload = response.json()
            record_id = payload["records"]["merge_train_stack_collapse_plan_record_id"]
            stack_records = store.list_merge_train_stack_collapse_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )
            candidate_records = store.list_merge_train_batch_candidate_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "plan")
        self.assertNotIn("candidate", payload["result"])
        self.assertEqual(payload["result"]["stack_collapse_plan"]["status"], "planned")
        self.assertEqual(
            [
                entry["pull_request_number"]
                for entry in payload["result"]["stack_collapse_plan"]["entries"]
            ],
            [1, 2],
        )
        self.assertEqual(stack_records[0].record_id, record_id)
        self.assertEqual(candidate_records, ())

    async def test_reports_unsupported_stack_without_writing_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                _UnsupportedStackMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )
            candidate_records = store.list_merge_train_batch_candidate_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )
            stack_records = store.list_merge_train_stack_collapse_plan_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {})
        self.assertEqual(payload["result"]["next_action"], "stack_unsupported")
        self.assertEqual(payload["result"]["stack_discovery"]["status"], "unsupported")
        self.assertEqual(candidate_records, ())
        self.assertEqual(stack_records, ())

    async def test_builds_existing_candidate_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                _FakeMergeTrainSnapshotReader,
            ):
                plan_response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "build",
                        "candidate_record_id": plan_response.json()["records"][
                            "merge_train_batch_candidate_record_id"
                        ],
                    },
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["mode"], "build")
        self.assertEqual(response.json()["result"]["candidate"]["status"], "ready_for_checks")
        self.assertEqual(response.json()["result"]["candidate"]["candidate_sha"], "candidate-built")

    async def test_observes_existing_candidate_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                _FakeMergeTrainSnapshotReader,
            ):
                plan_response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainClient",
                _FakeMergeTrainGitHubClient,
            ):
                build_response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "build",
                        "candidate_record_id": plan_response.json()["records"][
                            "merge_train_batch_candidate_record_id"
                        ],
                    },
                )
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "observe",
                        "candidate_record_id": build_response.json()["records"][
                            "merge_train_batch_candidate_record_id"
                        ],
                    },
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["mode"], "observe")
        self.assertEqual(response.json()["result"]["candidate"]["status"], "passed")
        self.assertEqual(response.json()["result"]["candidate"]["required_checks_status"], "pass")

    async def test_replays_idempotent_plan_without_rereading_github(self) -> None:
        request_payload = {
            "schema_version": 1,
            "repository": "cbusillo/sellyouroutboard",
            "base_branch": "main",
            "mode": "plan",
        }
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            _CountingBatchCandidateMergeTrainSnapshotReader.read_calls = 0
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                _CountingBatchCandidateMergeTrainSnapshotReader,
            ):
                first_response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    request_payload,
                    idempotency_key="merge-train-batch-candidate-plan",
                )
                replay_response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    request_payload,
                    idempotency_key="merge-train-batch-candidate-plan",
                )
            records = store.list_merge_train_batch_candidate_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(_CountingBatchCandidateMergeTrainSnapshotReader.read_calls, 1)
        self.assertEqual(len(records), 1)

    async def test_rejects_reused_idempotency_key_with_different_payload(self) -> None:
        request_payload = {
            "schema_version": 1,
            "repository": "cbusillo/sellyouroutboard",
            "base_branch": "main",
            "mode": "plan",
        }
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                _FakeMergeTrainSnapshotReader,
            ):
                first_response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    request_payload,
                    idempotency_key="merge-train-batch-candidate-conflict",
                )
                conflict_response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {**request_payload, "base_branch": "release"},
                    idempotency_key="merge-train-batch-candidate-conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_rejects_invalid_payload_as_launchplane_invalid_request(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_merge_train_batch_candidate_run_once(
            app,
            {"schema_version": 1, "repository": "cbusillo", "base_branch": "main"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_build_requires_candidate_record_id(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_merge_train_batch_candidate_run_once(
            app,
            {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mode": "build",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_build_rejects_unknown_candidate_record_without_leaking_detail(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_merge_train_batch_candidate_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mode": "build",
                    "candidate_record_id": "missing-candidate-record",
                },
            )
            records = store.list_merge_train_batch_candidate_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(response.json()["error"]["message"], "Request could not be completed.")
        self.assertEqual(records, ())

    async def test_rejects_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: store,
            )
            with patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_rejects_terminal_agent_bearer_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_merge_train_run_once_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )
            response = await _post_merge_train_batch_candidate_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mode": "plan",
                },
                authorization="Bearer terminal-read-token",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_rejects_missing_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch.dict("os.environ", {}, clear=True):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "github_token_not_configured")

    async def test_rejects_missing_policy_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            response = await _post_merge_train_batch_candidate_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mode": "plan",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "merge_train_policy_not_configured")

    async def test_maps_stale_github_state_without_writing_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                _StaleMergeTrainSnapshotReader,
            ):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )
            records = store.list_merge_train_batch_candidate_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "merge_train_github_stale_state")
        self.assertEqual(records, ())

    async def test_maps_build_github_request_failure_without_writing_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainSnapshotReader",
                _FakeMergeTrainSnapshotReader,
            ):
                plan_response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "plan",
                    },
                )
            with patch(
                "control_plane.merge_train_batch_candidate.GitHubMergeTrainClient",
                _UnavailableBatchCandidateMergeTrainGitHubClient,
            ):
                response = await _post_merge_train_batch_candidate_run_once(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "mode": "build",
                        "candidate_record_id": plan_response.json()["records"][
                            "merge_train_batch_candidate_record_id"
                        ],
                    },
                )
            records = store.list_merge_train_batch_candidate_records(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "github_request_failed")
        self.assertEqual(len(records), 1)

    async def test_rejects_missing_batch_candidate_storage(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = _MergeTrainPolicyOnlyStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            response = await _post_merge_train_batch_candidate_run_once(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "mode": "plan",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")
        self.assertEqual(
            response.json()["error"]["message"],
            "Merge train batch candidate storage requires database-backed records.",
        )

    async def test_openapi_includes_batch_candidate_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/work-graph/merge-train/batch-candidate/run-once"][
            "post"
        ]
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        self.assertEqual(request_schema["title"], "MergeTrainBatchCandidateRunOnceEnvelope")
        self.assertTrue(set(route["responses"]) >= {"400", "401", "403", "409", "502", "503"})


class FastApiMergeTrainPrFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_managed_comment_and_records_evidence(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            policy_record = _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_pr_feedback.find_github_issue_comment_by_marker",
                    return_value=None,
                ) as find_comment,
                patch(
                    "control_plane.merge_train_pr_feedback.create_github_issue_comment",
                    return_value={
                        "id": 123,
                        "html_url": "https://github.com/cbusillo/sellyouroutboard/pull/7#issuecomment-123",
                    },
                ) as create_comment,
            ):
                response = await _post_merge_train_pr_feedback(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_request_number": 7,
                        "event": "waiting",
                        "controller_action": "observe_candidate",
                        "controller_record_id": "candidate-1",
                        "message": "Waiting for required checks to settle.",
                    },
                )
            records = FilesystemRecordStore(state_dir).list_merge_train_pr_feedback_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pr_number=7,
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        feedback = payload["result"]["feedback"]
        self.assertEqual(feedback["delivery_status"], "delivered")
        self.assertEqual(feedback["delivery_action"], "created_comment")
        self.assertEqual(feedback["comment_id"], 123)
        self.assertEqual(feedback["policy_sha256"], policy_record.policy_sha256)
        self.assertIn("launchplane-merge-train", feedback["marker"])
        self.assertIn("Waiting for required checks", feedback["comment_markdown"])
        self.assertEqual(payload["records"]["merge_train_pr_feedback_id"], feedback["feedback_id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].comment_id, 123)
        self.assertEqual(records[0].delivery_action, "created_comment")
        find_comment.assert_called_once()
        create_comment.assert_called_once()

    async def test_updates_managed_comment_and_records_evidence(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_pr_feedback.find_github_issue_comment_by_marker",
                    return_value={"id": 456, "body": "old body"},
                ) as find_comment,
                patch(
                    "control_plane.merge_train_pr_feedback.update_github_issue_comment",
                    return_value={
                        "id": 456,
                        "html_url": "https://github.com/cbusillo/sellyouroutboard/pull/7#issuecomment-456",
                    },
                ) as update_comment,
                patch(
                    "control_plane.merge_train_pr_feedback.create_github_issue_comment"
                ) as create_comment,
            ):
                response = await _post_merge_train_pr_feedback(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_request_number": 7,
                        "event": "completed",
                    },
                )
            records = FilesystemRecordStore(state_dir).list_merge_train_pr_feedback_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pr_number=7,
            )

        self.assertEqual(response.status_code, 202)
        feedback = response.json()["result"]["feedback"]
        self.assertEqual(feedback["delivery_status"], "delivered")
        self.assertEqual(feedback["delivery_action"], "updated_comment")
        self.assertEqual(feedback["comment_id"], 456)
        self.assertEqual(records[0].delivery_action, "updated_comment")
        find_comment.assert_called_once()
        update_comment.assert_called_once()
        create_comment.assert_not_called()

    async def test_rejects_comment_delivery_failure_but_records_evidence(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_pr_feedback.find_github_issue_comment_by_marker",
                    return_value=None,
                ),
                patch(
                    "control_plane.merge_train_pr_feedback.create_github_issue_comment",
                    side_effect=ClickException("GitHub API returned 502"),
                ) as create_comment,
            ):
                response = await _post_merge_train_pr_feedback(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_request_number": 7,
                        "event": "blocked",
                    },
                )
            records = FilesystemRecordStore(state_dir).list_merge_train_pr_feedback_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pr_number=7,
            )

        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "github_comment_delivery_failed")
        self.assertIn("GitHub API returned 502", payload["error"]["message"])
        feedback = payload["result"]["feedback"]
        self.assertEqual(feedback["delivery_status"], "failed")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].delivery_status, "failed")
        create_comment.assert_called_once()

    async def test_replays_idempotent_request(self) -> None:
        request_payload = {
            "schema_version": 1,
            "repository": "cbusillo/sellyouroutboard",
            "base_branch": "main",
            "pull_request_number": 7,
            "event": "waiting",
            "controller_action": "observe_candidate",
            "controller_record_id": "candidate-1",
            "message": "Waiting for required checks to settle.",
        }
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_pr_feedback.find_github_issue_comment_by_marker",
                    return_value=None,
                ) as find_comment,
                patch(
                    "control_plane.merge_train_pr_feedback.create_github_issue_comment",
                    return_value={
                        "id": 123,
                        "html_url": "https://github.com/cbusillo/sellyouroutboard/pull/7#issuecomment-123",
                    },
                ) as create_comment,
            ):
                first_response = await _post_merge_train_pr_feedback(
                    app,
                    request_payload,
                    idempotency_key="merge-train-pr-feedback-7-waiting",
                )
                replay_response = await _post_merge_train_pr_feedback(
                    app,
                    request_payload,
                    idempotency_key="merge-train-pr-feedback-7-waiting",
                )
            records = FilesystemRecordStore(state_dir).list_merge_train_pr_feedback_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pr_number=7,
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        first_payload = first_response.json()
        replay_payload = replay_response.json()
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(
            replay_payload["result"]["feedback"]["feedback_id"],
            first_payload["result"]["feedback"]["feedback_id"],
        )
        self.assertEqual(len(records), 1)
        find_comment.assert_called_once()
        create_comment.assert_called_once()

    async def test_rejects_reused_idempotency_key_with_different_payload(self) -> None:
        request_payload = {
            "schema_version": 1,
            "repository": "cbusillo/sellyouroutboard",
            "base_branch": "main",
            "pull_request_number": 7,
            "event": "waiting",
        }
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_pr_feedback.find_github_issue_comment_by_marker",
                    return_value=None,
                ),
                patch(
                    "control_plane.merge_train_pr_feedback.create_github_issue_comment",
                    return_value={
                        "id": 123,
                        "html_url": "https://github.com/cbusillo/sellyouroutboard/pull/7#issuecomment-123",
                    },
                ),
            ):
                first_response = await _post_merge_train_pr_feedback(
                    app,
                    request_payload,
                    idempotency_key="merge-train-pr-feedback-7-conflict",
                )
                conflict_response = await _post_merge_train_pr_feedback(
                    app,
                    {**request_payload, "event": "blocked"},
                    idempotency_key="merge-train-pr-feedback-7-conflict",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_preserves_same_second_updates(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with (
                patch(
                    "control_plane.merge_train_pr_feedback.find_github_issue_comment_by_marker",
                    return_value={"id": 456, "body": "old body"},
                ),
                patch(
                    "control_plane.merge_train_pr_feedback.update_github_issue_comment",
                    return_value={
                        "id": 456,
                        "html_url": "https://github.com/cbusillo/sellyouroutboard/pull/7#issuecomment-456",
                    },
                ),
                patch(
                    "control_plane.http_app.utc_now_timestamp",
                    return_value="2026-05-20T15:00:00Z",
                ),
            ):
                first_response = await _post_merge_train_pr_feedback(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_request_number": 7,
                        "event": "waiting",
                    },
                )
                second_response = await _post_merge_train_pr_feedback(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_request_number": 7,
                        "event": "waiting",
                        "message": "Still waiting after a rerun.",
                    },
                )
            records = FilesystemRecordStore(state_dir).list_merge_train_pr_feedback_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                pr_number=7,
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertNotEqual(
            first_response.json()["result"]["feedback"]["feedback_id"],
            second_response.json()["result"]["feedback"]["feedback_id"],
        )
        self.assertEqual(len(records), 2)

    async def test_rejects_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: store,
            )
            with patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True):
                response = await _post_merge_train_pr_feedback(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_request_number": 7,
                        "event": "blocked",
                    },
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_rejects_missing_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            with patch.dict("os.environ", {}, clear=True):
                response = await _post_merge_train_pr_feedback(
                    app,
                    {
                        "schema_version": 1,
                        "repository": "cbusillo/sellyouroutboard",
                        "base_branch": "main",
                        "pull_request_number": 7,
                        "event": "blocked",
                    },
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "github_token_not_configured")

    async def test_rejects_terminal_agent_bearer_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(
                state_dir,
                policy=MergeTrainPolicyRecord(
                    record_id="merge-train-policy-pr-feedback-terminal-agent-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-13T21:00:00Z",
                    policy=parse_merge_train_policy_toml(
                        _merge_train_policy_table("cbusillo/sellyouroutboard").replace(
                            'action = "merge_train.run_once"',
                            'action = "merge_train.pr_feedback"',
                        )
                    ),
                ),
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_merge_train_pr_feedback_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )
            response = await _post_merge_train_pr_feedback(
                app,
                {
                    "schema_version": 1,
                    "repository": "cbusillo/sellyouroutboard",
                    "base_branch": "main",
                    "pull_request_number": 7,
                    "event": "blocked",
                },
                authorization="Bearer terminal-read-token",
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_openapi_includes_pr_feedback_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/work-graph/merge-train/pr-feedback"]["post"]
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        self.assertTrue(set(route["responses"]) >= {"400", "401", "403", "409", "502", "503"})
