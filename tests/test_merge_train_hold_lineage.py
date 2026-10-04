import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import MergeTrainBatchHeldOutEntry
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train_controller_run_once import (
    _expose_persisted_conflict_holds,
    _surviving_held_out_entries,
)
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateHeadMergeOutcome,
    CandidateRefPrepareEffect,
    MergeTrainSemanticEffectExecutor,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubCandidateEntryConflictError,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import (
    _FakeExpandedMergeTrainSnapshotReader,
    _FakeMergeTrainGitHubClient,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
)
from tests.test_http_app_merge_train import (
    _PairConflictProbeExecutor,
)
from tests.support.auth import _StubVerifier
from tests.http_app_test_support import _asgi_get, _post_merge_train_controller_run_once


class HoldLineageTests(unittest.TestCase):
    def test_diagnostics_keep_old_holds_without_repeating_stopped_feedback(self) -> None:
        hold = MergeTrainBatchHeldOutEntry(pull_request_number=3, head_sha="held-head")
        for probe in (
            None,
            {"status": "will_run", "pull_request_numbers": [1, 2]},
            {"status": "ran", "pull_request_numbers": [1, 2], "held_out": []},
        ):
            with self.subTest(probe=probe):
                result: dict[str, Any] = {
                    "repository": "example/repo",
                    "base_branch": "main",
                    "controller_action": "candidate_failed",
                    "conflict_probe": probe,
                    "candidate": {
                        "status": "failed",
                        "entries": [],
                        "held_out": [hold.model_dump(mode="json")],
                    },
                }
                _expose_persisted_conflict_holds(result)
                self.assertEqual(result["conflict_probe"]["held_out"][0]["pull_request_number"], 3)
                self.assertEqual(
                    build_feedback_payloads(response={"result": result, "records": {}}), []
                )

    def test_changed_preceding_head_membership_or_order_invalidates_hold(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        first, second = snapshot.pull_requests
        hold = MergeTrainBatchHeldOutEntry(
            pull_request_number=second.number,
            head_sha=second.head_sha,
            probe_base_sha=snapshot.base_sha,
            conflicts_with=(first.number,),
            conflicts_with_head_shas=(first.head_sha,),
        )
        policy = build_test_merge_train_policy()
        self.assertEqual(
            _surviving_held_out_entries(policy=policy, snapshot=snapshot, held_out=(hold,)), (hold,)
        )
        for preceding in (
            first.model_copy(update={"head_sha": "new-head"}),
            first.model_copy(update={"labels": ()}),
            first.model_copy(update={"created_at": "2099-01-01T00:00:00Z"}),
        ):
            with self.subTest(preceding=preceding):
                changed = snapshot.model_copy(update={"pull_requests": (preceding, second)})
                self.assertEqual(
                    _surviving_held_out_entries(policy=policy, snapshot=changed, held_out=(hold,)),
                    (),
                )
        legacy = MergeTrainBatchHeldOutEntry(
            pull_request_number=second.number,
            head_sha=second.head_sha,
            conflicts_with=(first.number,),
        )
        self.assertEqual(
            _surviving_held_out_entries(policy=policy, snapshot=snapshot, held_out=(legacy,)), ()
        )


class HoldLineageControllerTests(unittest.IsolatedAsyncioTestCase):
    async def _exercise_base_change(self, *, remains_conflicting: bool) -> None:
        base = {"sha": "base-main"}

        class Reader(_FakeExpandedMergeTrainSnapshotReader):
            def read_merge_train_snapshot(self, **kwargs: Any) -> Any:
                return (
                    super()
                    .read_merge_train_snapshot(**kwargs)
                    .model_copy(update={"base_sha": base["sha"]})
                )

        class BaseConflictExecutor(_PairConflictProbeExecutor):
            def prepare_candidate_ref(self, effect: CandidateRefPrepareEffect) -> None:
                super().prepare_candidate_ref(effect)
                self.base_sha = effect.base_sha

            def merge_candidate_head(
                self, effect: CandidateHeadMergeEffect
            ) -> CandidateHeadMergeOutcome:
                if effect.pull_request_number == 1 and (
                    self.base_sha == "base-main" or remains_conflicting
                ):
                    raise MergeTrainGitHubCandidateEntryConflictError(
                        pull_request_number=effect.pull_request_number, head_sha=effect.head_sha
                    )
                return super().merge_candidate_head(effect)

        executor = BaseConflictExecutor(conflicting_pair=(99, 100))

        class Client(_FakeMergeTrainGitHubClient):
            def probe_batch_entry_conflicts(self, **kwargs: Any) -> Any:
                return GitHubMergeTrainClient(
                    transport=cast(Any, self.transport),
                    effect_executor=cast(MergeTrainSemanticEffectExecutor, executor),
                ).probe_batch_entry_conflicts(**kwargs)

        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"GH_TOKEN": "token"}, clear=True),
        ):
            state_dir = Path(directory) / "state"
            _seed_merge_train_policy(state_dir)

            def app() -> Any:
                return create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_merge_train_service_identity()),
                    authz_policy=_merge_train_service_policy(),
                    record_store_factory=lambda: FilesystemRecordStore(state_dir=state_dir),
                )

            payload = {
                "schema_version": 1,
                "repository": "cbusillo/sellyouroutboard",
                "base_branch": "main",
                "mutate": True,
            }
            with (
                patch("control_plane.merge_train_github.GitHubMergeTrainSnapshotReader", Reader),
                patch(
                    "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient", Client
                ),
            ):
                response = await _post_merge_train_controller_run_once(app(), payload)
                self.assertEqual(response.status_code, 202, response.text)
                planned = response.json()["result"]["candidate"]
                self.assertEqual(
                    [entry["pull_request_number"] for entry in planned["entries"]], [2]
                )
                self.assertEqual(planned["held_out"][0]["probe_base_sha"], base["sha"])
                self.assertEqual(planned["held_out"][0]["conflicts_with"], [])
                probe_events = list(executor.ref_events)
                # A new app/store instance proves persisted holds survive restart.
                for expected_action in ("build_candidate", "observe_candidate"):
                    dry = await _post_merge_train_controller_run_once(
                        app(), {**payload, "mutate": False}
                    )
                    result = dry.json()["result"]
                    self.assertEqual(result["controller_action"], expected_action)
                    self.assertEqual(result["conflict_probe"]["status"], "persisted")
                    self.assertEqual(
                        result["conflict_probe"]["held_out"][0]["pull_request_number"], 1
                    )
                    self.assertEqual(executor.ref_events, probe_events)
                    if expected_action == "build_candidate":
                        built = await _post_merge_train_controller_run_once(app(), payload)
                        self.assertEqual(
                            built.json()["result"]["controller_action"], expected_action
                        )
                status = await _asgi_get(
                    app(),
                    "/v1/work-graph/merge-train/controller/status?repository=cbusillo/sellyouroutboard&base_branch=main",
                    headers={"Authorization": "Bearer valid-token"},
                )
                self.assertEqual(status.status_code, 200, status.text)
                summaries = status.json()["controller_status"]["controller_records"]
                holds = next(
                    summary["held_out"]
                    for summary in summaries
                    if summary["record_type"] == "batch_candidate"
                )
                self.assertEqual(holds, result["conflict_probe"]["held_out"])
                base["sha"] = "new-base"
                response = await _post_merge_train_controller_run_once(app(), payload)
                self.assertEqual(response.status_code, 202, response.text)
                replacement = response.json()["result"]["candidate"]
                self.assertEqual(replacement["base_sha"], base["sha"])
                self.assertNotEqual(executor.ref_events, probe_events)
                expected = [2] if remains_conflicting else [1, 2]
                self.assertEqual(
                    [entry["pull_request_number"] for entry in replacement["entries"]], expected
                )
                if remains_conflicting:
                    self.assertEqual(replacement["held_out"][0]["probe_base_sha"], base["sha"])
                    self.assertEqual(
                        replacement["held_out"][0]["head_sha"], planned["held_out"][0]["head_sha"]
                    )
                else:
                    self.assertEqual(replacement["held_out"], [])

    async def test_base_conflict_becomes_clean_after_base_change(self) -> None:
        await self._exercise_base_change(remains_conflicting=False)

    async def test_continuing_conflict_is_held_again_on_new_base(self) -> None:
        await self._exercise_base_change(remains_conflicting=True)
