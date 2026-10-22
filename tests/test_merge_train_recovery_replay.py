"""Reproduce failed batches through service passes with a restart between each.

Only the source-control provider is simulated. The HTTP route, controller,
leases, candidate history and landing checkpoints use the production code.
"""

import unittest
from control_plane.merge_train_github import MergeTrainBaseCheckObservation
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_controller_run_once import (
    _supersede_active_merge_train_batch_candidate_records,
    _persist_replacement_candidate,
)
from control_plane.storage.filesystem import FilesystemRecordStore, _utc_now_timestamp
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.support.auth import _StubVerifier
from tests.support.merge_train import (
    _FakeExpandedMergeTrainSnapshotReader,
    _FakeFailingMergeTrainGitHubClient,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _seed_merge_train_policy,
)


class RecoveryReplayTests(unittest.IsolatedAsyncioTestCase):
    async def _replay(
        self,
        *,
        interaction: bool = False,
        red_base: bool = False,
        late_repair: bool = False,
        replacement_crash: bool = False,
        equal_timestamps: bool = False,
        exhausted: bool = False,
    ) -> None:
        template = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        snapshot = template.model_copy(
            update={
                "pull_requests": tuple(
                    template.pull_requests[-1].model_copy(
                        update={
                            "number": number,
                            "head_sha": f"head-{number}",
                            "head_ref": f"feature-{number}",
                            "created_at": f"2026-10-10T12:0{number}:00Z",
                        }
                    )
                    for number in ((1, 3) if late_repair else range(1, 5))
                )
            }
        )
        landed: set[int] = set()
        blocked: list[int] = []
        builds: list[tuple[str, tuple[str, ...]]] = []
        results: list[dict[str, Any]] = []
        base_green = not (red_base or late_repair or exhausted)
        repair_added = False
        exhausted_build_count: int | None = None
        exhausted_polls = 0
        crashed = False
        timestamp = _utc_now_timestamp()

        def persist_replacement(**kwargs: Any) -> str:
            if equal_timestamps and not crashed and len(kwargs["candidate"].entries) == 1:
                parent = kwargs["active_candidate_record"]
                self.assertEqual(kwargs["recorded_at"], parent.updated_at)
                for nonce in range(1000):
                    trace = f"tied-replacement-{nonce}"
                    record = build_merge_train_batch_candidate_record(
                        candidate=kwargs["candidate"],
                        source=f"service:controller:candidate-reflow:{trace}",
                        updated_at=kwargs["recorded_at"],
                    )
                    if record.record_id < parent.record_id:
                        kwargs["trace_id"] = trace
                        break
                else:
                    self.fail("Could not exercise the parent-first record ordering")
            return _persist_replacement_candidate(**kwargs)

        class SimulatedRestart(BaseException):
            pass

        def retire_candidate(**kwargs: Any) -> None:
            nonlocal crashed
            store = kwargs["record_store"]
            replacement = next(
                (
                    record
                    for record in store.list_merge_train_batch_candidate_records()
                    if record.record_id == kwargs["replacement_record_id"]
                ),
                None,
            )
            if (
                replacement_crash
                and not crashed
                and replacement is not None
                and len(replacement.candidate.entries) == 1
            ):
                crashed = True
                raise SimulatedRestart()
            _supersede_active_merge_train_batch_candidate_records(**kwargs)

        class SnapshotReader:
            def __init__(self, **_: Any) -> None:
                pass

            @staticmethod
            def read_merge_train_snapshot(**_: Any) -> MergeTrainDryRunSnapshot:
                return snapshot

        class Client(_FakeFailingMergeTrainGitHubClient):
            def read_merge_train_snapshot(self, **_: Any) -> MergeTrainDryRunSnapshot:
                return snapshot

            def build_batch_candidate(self, **kwargs: Any) -> MergeTrainBatchCandidate:
                candidate = kwargs["candidate"]
                builds.append(
                    (candidate.base_sha, tuple(entry.head_sha for entry in candidate.entries))
                )
                built = super().build_batch_candidate(**kwargs)
                return built.model_copy(update={"candidate_sha": f"built-{len(builds)}"})

            def observe_batch_candidate_checks(
                self, *, candidate: MergeTrainBatchCandidate
            ) -> MergeTrainBatchCandidate:
                members = {entry.pull_request_number for entry in candidate.entries}
                fails = {1, 3} <= (members | landed) if interaction else 3 in members
                if late_repair:
                    fails = fails or 2 not in members | landed
                if exhausted:
                    fails = True
                return candidate.model_copy(
                    update={
                        "required_checks_status": "fail" if fails else "pass",
                        "status": "failed" if fails else "passed",
                    }
                )

            def read_base_check_observation(self, **_: Any) -> MergeTrainBaseCheckObservation:
                return MergeTrainBaseCheckObservation("pass" if base_green else "fail")

            def add_pull_request_label(self, *, pull_request_number: int, **_: Any) -> None:
                nonlocal snapshot
                if late_repair:
                    self_outer.assertTrue(base_green, "A red-base failure blamed a queued PR")
                blocked.append(pull_request_number)
                snapshot = snapshot.model_copy(
                    update={
                        "pull_requests": tuple(
                            pr.model_copy(update={"labels": (*pr.labels, "merge-blocked")})
                            if pr.number == pull_request_number
                            else pr
                            for pr in snapshot.pull_requests
                        )
                    }
                )

            def land_batch_candidate(self, **kwargs: Any) -> Any:
                nonlocal snapshot, base_green
                plan = super().land_batch_candidate(**kwargs)
                members = {entry.pull_request_number for entry in plan.entries}
                landed.update(members)
                if late_repair and 2 in landed:
                    base_green = True
                snapshot = snapshot.model_copy(
                    update={
                        "base_sha": f"base-after-{'-'.join(str(n) for n in sorted(landed))}",
                        "pull_requests": tuple(
                            pr for pr in snapshot.pull_requests if pr.number not in members
                        ),
                    }
                )
                return plan

        self_outer = self
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.http_app.resolve_merge_train_github_token", return_value="fixture"
            ),
            patch(
                "control_plane.merge_train_github.GitHubMergeTrainSnapshotReader", SnapshotReader
            ),
            patch("control_plane.merge_train_controller_run_once.GitHubMergeTrainClient", Client),
            patch(
                "control_plane.merge_train_controller_run_once._supersede_active_merge_train_batch_candidate_records",
                side_effect=retire_candidate,
            ),
            patch(
                "control_plane.merge_train_controller_run_once._persist_replacement_candidate",
                side_effect=persist_replacement,
            ),
            patch(
                "control_plane.storage.filesystem._utc_now_timestamp",
                side_effect=lambda: (
                    timestamp if equal_timestamps and not crashed else _utc_now_timestamp()
                ),
            ),
        ):
            state_dir = Path(directory) / "state"
            _seed_merge_train_policy(state_dir)
            for _ in range(40):
                # Reopen the actual persisted store and recreate the service each pass.
                store = FilesystemRecordStore(state_dir=state_dir)
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_merge_train_service_identity()),
                    authz_policy=_merge_train_service_policy(),
                    record_store_factory=lambda: store,
                )
                interrupted = False
                try:
                    response = await _post_merge_train_controller_run_once(
                        app,
                        {
                            "schema_version": 1,
                            "repository": snapshot.repository,
                            "base_branch": snapshot.base_branch,
                            "mutate": True,
                        },
                    )
                except SimulatedRestart:
                    interrupted = True
                except BaseExceptionGroup as error:
                    interruption, remaining = error.split(SimulatedRestart)
                    if interruption is None:
                        raise
                    if remaining is not None:
                        _, remaining = remaining.split(lambda item: isinstance(item, RuntimeError))
                    if remaining is not None:
                        raise remaining
                    interrupted = True
                if interrupted:
                    controller = store.list_merge_train_controller_state_records()[0]
                    store.write_merge_train_controller_state_record(
                        controller.model_copy(
                            update={"lease_expires_at": controller.lease_acquired_at}
                        )
                    )
                    continue
                self.assertEqual(response.status_code, 202, response.text)
                result = response.json()["result"]
                results.append(result)
                if exhausted and result.get("reason_code") == "base_failure_exploration_exhausted":
                    if exhausted_build_count is None:
                        exhausted_build_count = len(builds)
                    self.assertEqual(len(builds), exhausted_build_count)
                    self.assertEqual(landed, set())
                    self.assertEqual(blocked, [])
                    exhausted_polls += 1
                    if exhausted_polls == 5:
                        break
                    continue
                if (
                    red_base
                    and not base_green
                    and any(
                        record.candidate.status == "failed"
                        for record in store.list_merge_train_batch_candidate_records()
                    )
                ):
                    self.assertEqual(blocked, [])
                    self.assertEqual(landed, set())
                    self.assertEqual(len(builds), 1)
                    base_green = True
                if (
                    late_repair
                    and not repair_added
                    and any(
                        record.candidate.status == "failed"
                        for record in store.list_merge_train_batch_candidate_records()
                    )
                ):
                    repair = template.pull_requests[-1].model_copy(
                        update={
                            "number": 2,
                            "head_sha": "head-2",
                            "head_ref": "feature-2",
                            "created_at": "2026-10-10T12:02:00Z",
                        }
                    )
                    snapshot = snapshot.model_copy(
                        update={"pull_requests": (*snapshot.pull_requests, repair)}
                    )
                    repair_added = True
                if (
                    not (late_repair or exhausted)
                    and result["controller_action"] == "candidate_failed"
                    and result.get("reason_code") != "base_checks_not_green"
                ):
                    self.fail(str(result))
                if landed == ({1, 2} if late_repair else {1, 2, 4}) and blocked == [3]:
                    break
            else:
                self.fail(
                    f"Recovery did not settle: {[(r['controller_action'], r.get('reason_code')) for r in results]}"
                )

            if exhausted:
                self.assertEqual(len(builds), len(set(builds)))
                self.assertEqual(
                    {members for _, members in builds if len(members) == 1},
                    {(f"head-{number}",) for number in range(1, 5)},
                )
                return
            self.assertEqual(blocked, [3])
            self.assertEqual(crashed, replacement_crash)
            self.assertEqual(landed, {1, 2} if late_repair else {1, 2, 4})
            self.assertEqual(len(builds), len(set(builds)), "An unchanged failed batch was rebuilt")
            self.assertTrue(any(r.get("reason_code") == "failure_isolation" for r in results))
            culprit = next(
                r for r in results if r.get("reason_code") == "candidate_failure_isolated"
            )
            feedback = build_feedback_payloads(response={"result": culprit})
            self.assertTrue(
                any(p["pull_request_number"] == 3 and p["event"] == "blocked" for p in feedback)
            )
            failed_tests = culprit["details"]["failed_tests"]
            self.assertTrue(failed_tests)
            self.assertTrue(
                all(
                    any(test_id in cast(str, p["message"]) for p in feedback)
                    for test_id in failed_tests
                )
            )
            restarted = FilesystemRecordStore(state_dir=state_dir)
            records = restarted.list_merge_train_batch_landing_plan_records()
            recorded_members = {
                entry.pull_request_number
                for record in records
                for entry in record.landing_plan.entries
                if entry.status == "merged"
            }
            self.assertEqual(recorded_members, landed)
            controller = restarted.list_merge_train_controller_state_records(
                repository=snapshot.repository, base_branch=snapshot.base_branch, limit=1
            )[0]
            self.assertEqual(controller.status, "idle")

    async def test_single_culprit_recovers_and_lands_across_service_restarts(self) -> None:
        await self._replay()

    async def test_interaction_failure_isolated_against_the_newly_landed_base(self) -> None:
        await self._replay(interaction=True)

    async def test_red_base_waits_without_blame_then_recovers_after_restart(self) -> None:
        await self._replay(red_base=True)

    async def test_late_repair_lands_before_culprit_isolation_across_restarts(self) -> None:
        await self._replay(late_repair=True)

    async def test_interrupted_replacement_resumes_without_rebuilding_failed_inputs(self) -> None:
        await self._replay(late_repair=True, replacement_crash=True)

    async def test_interrupted_replacement_resumes_when_old_parent_sorts_first(self) -> None:
        await self._replay(late_repair=True, replacement_crash=True, equal_timestamps=True)

    async def test_exhausted_base_does_not_rebuild_or_blame_after_restarts(self) -> None:
        await self._replay(exhausted=True)
