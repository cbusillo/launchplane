from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchLandingPlan,
    build_merge_train_batch_landing_plan_record,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train_github import GitHubMergeTrainClient, MergeTrainGitHubError
from control_plane.merge_train_stack_disposition import DEFAULT_STACK_CHILD_DISPOSITION_LABEL
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.ship import utc_now_timestamp
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _FakeCollapsedRootStackedMergeTrainSnapshotReader,
    _FakeMergeTrainGitHubClient,
    _FakeStackedMergeTrainSnapshotReader,
    _merge_train_service_identity,
    _merge_train_service_policy,
)


class _LabelTransport:
    def __init__(self) -> None:
        self.labels: set[str] = set()
        self.applied: dict[int, set[str]] = {}
        self.preparation_unavailable = False
        self.creation_denied = False
        self.effects: list[str] = []

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method == "GET" and "/labels/" in path:
            if self.preparation_unavailable:
                raise MergeTrainGitHubError("Label read unavailable", status_code=503)
            label = path.rsplit("/", 1)[1]
            if label not in self.labels:
                raise MergeTrainGitHubError("Label missing", status_code=404)
            return {"name": label}
        if method == "POST" and "/issues/" in path and body is not None:
            labels = cast(list[str], body["labels"])
            if not set(labels) <= self.labels:
                raise MergeTrainGitHubError("Label missing", status_code=422)
            number = int(path.split("/issues/", 1)[1].split("/", 1)[0])
            self.applied.setdefault(number, set()).update(labels)
            self.effects.append("label")
            return [{"name": label} for label in labels]
        if method == "POST" and path.endswith("/labels") and body is not None:
            if self.creation_denied:
                raise MergeTrainGitHubError("Label creation denied", status_code=403)
            name = body["name"]
            assert isinstance(name, str)
            self.labels.add(name)
            self.effects.append("create-label")
            return body
        raise AssertionError(f"Unexpected provider request: {method} {path}")


class StackDispositionConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary.name))
        configured = build_test_merge_train_policy_record()
        payload = configured.model_dump(mode="json")
        payload["policy"]["policies"][0]["stack_child_disposition_label"] = ""
        payload["policy_sha256"] = ""
        self.policy = MergeTrainPolicyRecord.model_validate(payload)
        self.store.write_merge_train_policy_record(self.policy)
        self.transport = _LabelTransport()
        transport = self.transport

        class Client(_FakeMergeTrainGitHubClient):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.transport = transport
                self._effect_executor = None

            ensure_repository_label = GitHubMergeTrainClient.ensure_repository_label
            _create_repository_label = GitHubMergeTrainClient._create_repository_label
            add_pull_request_label = GitHubMergeTrainClient.add_pull_request_label

            def pull_request_has_label(
                self, *, pull_request_number: int, label: str, **_: Any
            ) -> bool:
                return label in transport.applied.get(pull_request_number, set())

            def find_pull_request_comment_url(self, **_: Any) -> str:
                return ""

            def comment_pull_request(self, **kwargs: Any) -> str:
                transport.effects.append("comment")
                return super().comment_pull_request(**kwargs)

            def merge_stack_child_into_parent(self, **kwargs: Any) -> str:
                transport.effects.append("collapse")
                return super().merge_stack_child_into_parent(**kwargs)

            def land_batch_candidate(self, **kwargs: Any) -> MergeTrainBatchLandingPlan:
                transport.effects.append("land")
                return super().land_batch_candidate(**kwargs)

        self.app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: self.store,
        )
        self.client_class = Client
        for name, value in (
            ("control_plane.http_app.resolve_merge_train_github_token", "token"),
            ("control_plane.merge_train_controller_run_once.GitHubMergeTrainClient", Client),
        ):
            patcher = (
                patch(name, return_value=value) if isinstance(value, str) else patch(name, value)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _run(self, *, collapsed: bool = False, mutate: bool = True) -> dict[str, Any]:
        reader = (
            _FakeCollapsedRootStackedMergeTrainSnapshotReader
            if collapsed
            else _FakeStackedMergeTrainSnapshotReader
        )
        with patch("control_plane.merge_train_github.GitHubMergeTrainSnapshotReader", reader):
            response = await _post_merge_train_controller_run_once(
                self.app,
                {"repository": self.policy.policy.policies[0].repository, "mutate": mutate},
            )
        self.assertEqual(response.status_code, 202, response.text)
        return cast(dict[str, Any], response.json()["result"])

    async def _prepare_landing(self) -> None:
        self.assertEqual((await self._run())["controller_action"], "plan_stack_collapse")
        self.assertEqual((await self._run())["controller_action"], "execute_stack_collapse")
        for _ in range(8):
            result = await self._run(collapsed=True)
            if result["controller_action"] == "plan_landing":
                return
        self.fail("Landing was not prepared")

    async def test_empty_label_repository_lands_stack_with_use_time_default(self) -> None:
        await self._prepare_landing()
        result = await self._run(collapsed=True)
        self.assertEqual(result["controller_action"], "land_batch")
        self.assertEqual(result["stack_collapse_plan"]["status"], "ready_for_train")
        self.assertEqual(self.transport.applied[2], {DEFAULT_STACK_CHILD_DISPOSITION_LABEL})
        self.assertLess(
            self.transport.effects.index("create-label"), self.transport.effects.index("collapse")
        )
        self.assertEqual(self.store.list_merge_train_policy_records()[0], self.policy)
        self.assertEqual(self.policy.policy.policies[0].stack_child_disposition_label, "")

    async def test_saved_stack_execution_bypasses_unavailable_preparation(self) -> None:
        await self._run()
        self.transport.preparation_unavailable = True
        result = await self._run()
        self.assertEqual(result["controller_action"], "execute_stack_collapse")
        self.assertIn("collapse", self.transport.effects)

    async def test_unlanded_stack_recovery_recreates_missing_label_at_disposition(self) -> None:
        await self._prepare_landing()
        self.transport.labels.clear()
        self.transport.effects.clear()
        with patch.object(
            self.client_class,
            "ensure_repository_label",
            side_effect=AssertionError("Recovery must bypass new-stack preparation"),
        ):
            result = await self._run(collapsed=True)
        self.assertEqual(result["stack_collapse_plan"]["status"], "ready_for_train")
        self.assertEqual(self.transport.applied[2], {DEFAULT_STACK_CHILD_DISPOSITION_LABEL})
        self.assertIn("create-label", self.transport.effects)
        self.assertNotIn("collapse", self.transport.effects)

    async def test_landed_stack_recovery_uses_default_without_new_preparation(self) -> None:
        await self._prepare_landing()
        landing = self.store.list_merge_train_batch_landing_plan_records()[0]
        completed = build_merge_train_batch_landing_plan_record(
            landing_plan=landing.landing_plan.model_copy(
                update={
                    "entries": tuple(
                        entry.model_copy(
                            update={
                                "status": "merged",
                                "merge_commit_sha": f"merge-{entry.pull_request_number}",
                            }
                        )
                        for entry in landing.landing_plan.entries
                    )
                }
            ),
            source="test:interrupted-landed-stack",
            updated_at=landing.updated_at,
        )
        self.store.write_merge_train_batch_landing_plan_record(completed)
        state = self.store.list_merge_train_controller_state_records()[0]
        self.store.write_merge_train_controller_state_record(
            state.model_copy(
                update={
                    "status": "reconcile_required",
                    "active_action": "land_batch",
                    "active_phase": "landing_entry_merged",
                    "active_record_id": landing.record_id,
                    "reconciliation_status": "required",
                }
            )
        )
        self.transport.preparation_unavailable = True
        self.transport.effects.clear()
        self.assertEqual(
            (await self._run(collapsed=True, mutate=False))["controller_action"],
            "resume_reconciliation",
        )
        result = await self._run(collapsed=True)
        self.assertEqual(result["stack_collapse_plan"]["status"], "ready_for_train")
        self.assertNotIn("land", self.transport.effects)
        self.assertNotIn("collapse", self.transport.effects)
        self.assertEqual(self.transport.applied[2], {DEFAULT_STACK_CHILD_DISPOSITION_LABEL})

    async def test_missing_prerequisite_refuses_before_any_stack_change(self) -> None:
        for failure in ("read", "create"):
            with self.subTest(failure=failure):
                self.transport.preparation_unavailable = failure == "read"
                self.transport.creation_denied = failure == "create"
                for _ in range(2):
                    result = await self._run()
                    self.assertEqual(result["reason_code"], "stack_disposition_preparation_failed")
                    self.assertEqual(result["controller_action"], "block")
                    self.assertEqual(self.transport.effects, [])
                    self.assertEqual(self.store.list_merge_train_stack_collapse_plan_records(), ())
                    self.assertEqual(self.store.list_merge_train_batch_candidate_records(), ())
                    self.assertEqual(self.store.list_merge_train_batch_landing_plan_records(), ())
                    self.assertEqual(
                        self.store.list_merge_train_controller_state_records()[0].status, "idle"
                    )
        self.transport.creation_denied = False
        self.assertEqual((await self._run())["controller_action"], "plan_stack_collapse")

    async def test_dry_run_missing_label_names_preparation_without_writes(self) -> None:
        result = await self._run(mutate=False)
        self.assertEqual(result["reason_code"], "stack_disposition_label_creation_required")
        self.assertEqual(self.transport.effects, [])
        self.assertEqual(self.store.list_merge_train_stack_collapse_plan_records(), ())

    async def test_default_label_collision_refuses_without_stack_effects(self) -> None:
        payload = self.policy.model_dump(mode="json")
        payload["record_id"] = "collision-policy"
        payload["updated_at"] = utc_now_timestamp()
        payload["policy"]["policies"][0]["blocked_label"] = DEFAULT_STACK_CHILD_DISPOSITION_LABEL
        payload["policy_sha256"] = ""
        self.policy = MergeTrainPolicyRecord.model_validate(payload)
        self.store.write_merge_train_policy_record(self.policy)
        result = await self._run()
        self.assertEqual(
            result["reason_code"], "stack_disposition_label_conflicts_with_train_label"
        )
        self.assertEqual(self.transport.effects, [])
        self.assertEqual(self.store.list_merge_train_stack_collapse_plan_records(), ())
