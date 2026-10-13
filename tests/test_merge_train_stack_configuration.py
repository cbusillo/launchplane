from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchLandingPlan,
    build_merge_train_batch_landing_plan_record,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.storage.filesystem import FilesystemRecordStore
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


class StackDispositionConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlanded_stack_reports_policy_recovery_and_retries_without_effects(self) -> None:
        await self._check_missing_configuration(landed=False)

    async def test_landed_stack_reports_policy_recovery_and_preserves_completion(self) -> None:
        await self._check_missing_configuration(landed=True)

    async def test_policy_replacement_cannot_retire_stack_without_no_effect_proof(self) -> None:
        await self._check_missing_configuration(landed=False, retirement_unavailable=True)

    async def _check_missing_configuration(
        self, *, landed: bool, retirement_unavailable: bool = False
    ) -> None:
        effects: list[str] = []

        class EffectRecordingClient(_FakeMergeTrainGitHubClient):
            def land_batch_candidate(self, **kwargs: Any) -> MergeTrainBatchLandingPlan:
                effects.append("merge")
                return super().land_batch_candidate(**kwargs)

            def merge_pull_request(self, **kwargs: Any) -> str:
                effects.append("merge")
                return super().merge_pull_request(**kwargs)

            def merge_stack_child_into_parent(self, **kwargs: Any) -> str:
                effects.append("collapse")
                return super().merge_stack_child_into_parent(**kwargs)

            def cleanup_batch_candidate_ref(self, **kwargs: Any) -> bool:
                effects.append("cleanup")
                return super().cleanup_batch_candidate_ref(**kwargs)

            def close_pull_request(self, **kwargs: Any) -> None:
                effects.append("close")
                super().close_pull_request(**kwargs)

            def comment_pull_request(self, **kwargs: Any) -> str:
                effects.append("comment")
                return super().comment_pull_request(**kwargs)

            def add_pull_request_label(self, **kwargs: Any) -> None:
                effects.append("label")
                super().add_pull_request_label(**kwargs)

            @staticmethod
            def verify_unlanded_batch(**kwargs: Any) -> tuple[str, str]:
                if retirement_unavailable:
                    raise MergeTrainGitHubStaleHeadError("Cannot prove the root is unlanded")
                plan = kwargs["landing_plan"]
                return plan.entries[0].expected_base_sha, "observed-base-tree"

            def read_pull_request_head(self, **kwargs: Any) -> tuple[str, str]:
                pr = self.read_pull_request_snapshot(**kwargs)
                return pr.head_sha, "closed"

        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            configured = build_test_merge_train_policy_record()
            policy = MergeTrainPolicyRecord.model_validate(
                {
                    **configured.model_dump(),
                    "policy_sha256": "",
                    "policy": {
                        **configured.policy.model_dump(),
                        "policies": [
                            {
                                **configured.policy.policies[0].model_dump(),
                                "stack_child_disposition_label": "",
                            }
                        ],
                    },
                }
            )
            store.write_merge_train_policy_record(policy)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            payload = {"repository": configured.policy.policies[0].repository, "mutate": True}
            with (
                patch(
                    "control_plane.http_app.resolve_merge_train_github_token", return_value="token"
                ),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                    EffectRecordingClient,
                ),
            ):
                with patch(
                    "control_plane.merge_train_github.GitHubMergeTrainSnapshotReader",
                    _FakeStackedMergeTrainSnapshotReader,
                ):
                    for _ in range(2):
                        response = await _post_merge_train_controller_run_once(app, payload)
                        self.assertEqual(response.status_code, 202, response.text)
                with patch(
                    "control_plane.merge_train_github.GitHubMergeTrainSnapshotReader",
                    _FakeCollapsedRootStackedMergeTrainSnapshotReader,
                ):
                    for _ in range(4):
                        response = await _post_merge_train_controller_run_once(app, payload)
                        self.assertEqual(response.status_code, 202, response.text)
                    self.assertEqual(response.json()["result"]["controller_action"], "plan_landing")
                    landing = store.list_merge_train_batch_landing_plan_records()[0]
                    if landed:
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
                        store.write_merge_train_batch_landing_plan_record(completed)
                        state = store.list_merge_train_controller_state_records()[0]
                        store.write_merge_train_controller_state_record(
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
                    history = store.list_merge_train_batch_landing_plan_records()
                    stack_history = store.list_merge_train_stack_collapse_plan_records()
                    effects.clear()
                    for mutate in (True, False, True):
                        response = await _post_merge_train_controller_run_once(
                            app, {**payload, "mutate": mutate}
                        )
                        self.assertEqual(response.status_code, 202, response.text)
                        result = response.json()["result"]
                        reason = (
                            "completed_landing_stack_disposition_not_configured"
                            if landed
                            else "landing_stack_disposition_not_configured"
                        )
                        self.assertEqual(result["reason_code"], reason)
                        self.assertEqual(result["controller_action"], "block")
                        self.assertEqual(result["controller_reconciliation_status"], "required")
                        self.assertTrue(result["details"]["stack_collapse_plan_record_ids"])
                        self.assertEqual(
                            result["details"]["recovery_action"], "privileged-policy-propose"
                        )
                        self.assertEqual(effects, [])
                        self.assertEqual(
                            store.list_merge_train_batch_landing_plan_records(), history
                        )
                        self.assertEqual(
                            store.list_merge_train_stack_collapse_plan_records(), stack_history
                        )
                        self.assertEqual(store.list_merge_admission_records(), ())
                    # Changing admission authority requires fresh work even when
                    # the same replacement fixes an annotation prerequisite.
                    configured.policy.policies[0].blocked_label = "changed-authority-hold"
                    configured = MergeTrainPolicyRecord.model_validate(
                        {**configured.model_dump(), "policy_sha256": ""}
                    )
                    configured = configured.model_copy(
                        update={
                            "record_id": "configured-disposition-policy",
                            "updated_at": store.list_merge_train_controller_state_records()[
                                0
                            ].updated_at,
                        }
                    )
                    store.write_merge_train_policy_record(configured)
                    actions = []
                    for _ in range(2):
                        response = await _post_merge_train_controller_run_once(app, payload)
                        if retirement_unavailable:
                            self.assertEqual(response.status_code, 409, response.text)
                            self.assertEqual(
                                store.list_merge_train_batch_landing_plan_records(), history
                            )
                            self.assertEqual(
                                store.list_merge_train_stack_collapse_plan_records(), stack_history
                            )
                            self.assertEqual(effects, [])
                            return
                        self.assertEqual(response.status_code, 202, response.text)
                        result = response.json()["result"]
                        actions.append(result["controller_action"])
                        if result.get("stack_collapse_plan", {}).get("status") == "ready_for_train":
                            break
                    if landed:
                        self.assertEqual(
                            result.get("stack_collapse_plan", {}).get("status"),
                            "ready_for_train",
                            actions,
                        )
                    else:
                        self.assertEqual(actions, ["retire_stale_landing", "block"])
                        self.assertEqual(
                            result["blocking_reason"]["code"],
                            "merge_train_stack_collapse_policy_changed",
                        )
                    self.assertNotIn("merge", effects)
