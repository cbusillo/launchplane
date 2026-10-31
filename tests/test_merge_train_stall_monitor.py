from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import runpy
from control_plane.contracts.merge_train_controller_state import (
    build_merge_train_controller_state_record,
)

from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy
from control_plane.contracts.merge_train_batch import (
    MergeTrainCandidateBlockIntent,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_github import MergeTrainGitHubPolicyUnavailableError
from control_plane.contracts.merge_train_stall import (
    MergeTrainStallObservation,
    MergeTrainStallRecord,
    MergeTrainStallWorkflow,
)
from control_plane.merge_train_stall_monitor import (
    STALL_SECONDS,
    DiscordStallNotifier,
    observe_stall,
    run_stall_monitor_pass,
)
from control_plane.storage.filesystem import (
    FilesystemRecordStore,
    _merge_train_controller_storage_id,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from tests.test_merge_train_scheduler import _policy_record


def _time(seconds: int) -> str:
    return (datetime(2026, 10, 12, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


class StallObservationTests(TestCase):
    def test_idle_time_never_counts_toward_new_work_stalling(self) -> None:
        old = self.observe(MergeTrainStallObservation(), 0)
        idle = self.observe(old, STALL_SECONDS * 2, ready=(), busy=False)
        self.assertFalse(idle.reasons)
        for ready in ((), ("7:head",)):
            with self.subTest(ready=ready):
                started = self.observe(idle, STALL_SECONDS * 4, ready=ready)
                self.assertFalse(started.reasons)
                before = self.observe(started, STALL_SECONDS * 5 - 1, ready=ready)
                self.assertFalse(before.reasons)
                stalled = self.observe(before, STALL_SECONDS * 5, ready=ready)
                self.assertIn("controller_unchanged", stalled.reasons)

    def test_stalled_check_recovery_names_the_controller_wait(self) -> None:
        first = self.observe(MergeTrainStallObservation(), 0)
        stalled = self.observe(first, STALL_SECONDS, wait_reason="candidate_checks_disagree")
        self.assertIn("candidate_checks_disagree", stalled.reasons)

    def test_candidate_block_clock_survives_unrelated_progress_and_clears_with_block(self) -> None:
        initial = self.observe(
            MergeTrainStallObservation(), 0, blocked=("7:head",), ready=(), busy=False
        )
        moved = self.observe(
            initial,
            STALL_SECONDS,
            blocked=("7:head",),
            ready=(),
            busy=False,
            progress_key="different-base-and-candidate",
        )
        self.assertIn("blocked_change_not_moving", moved.reasons)
        cleared = self.observe(moved, STALL_SECONDS + 300, blocked=(), ready=(), busy=False)
        self.assertNotIn("blocked_change_not_moving", cleared.reasons)
        self.assertFalse(cleared.blocked_since)

    @staticmethod
    def observe(
        previous: MergeTrainStallObservation, seconds: int, **kwargs: object
    ) -> MergeTrainStallObservation:
        defaults = {"progress_key": "observe:record:head", "ready": ("7:head",), "busy": True}
        return observe_stall(previous=previous, now=_time(seconds), **{**defaults, **kwargs})  # type: ignore[arg-type]

    def test_threshold_survives_repeated_heartbeats_and_unrelated_arrivals(self) -> None:
        initial = self.observe(MergeTrainStallObservation(), 0)
        early = self.observe(initial, STALL_SECONDS - 1, ready=("7:head", "8:new"))
        self.assertFalse(early.reasons)
        stalled = self.observe(early, STALL_SECONDS, ready=("7:head", "8:new"))
        self.assertIn("controller_unchanged", stalled.reasons)
        self.assertIn("ready_change_not_moving", stalled.reasons)
        self.assertEqual(stalled.ready_since["8:new"], early.ready_since["8:new"])

    def test_progress_does_not_hide_a_different_ready_change_wait(self) -> None:
        initial = self.observe(MergeTrainStallObservation(), 0)
        moved = self.observe(initial, STALL_SECONDS, progress_key="new-candidate")
        self.assertNotIn("controller_unchanged", moved.reasons)
        self.assertIn("ready_change_not_moving", moved.reasons)
        idle = self.observe(moved, STALL_SECONDS + 1, ready=(), busy=False)
        self.assertFalse(idle.reasons)

    def test_transient_lease_and_recovery_signals_confirm_on_the_next_pass(self) -> None:
        result = self.observe(
            MergeTrainStallObservation(), 0, orphaned_lease=True, recovered_failure=True
        )
        self.assertFalse(result.reasons)
        transient = self.observe(result, 300)
        self.assertFalse(transient.reasons)
        result = self.observe(result, 300, orphaned_lease=True, recovered_failure=True)
        self.assertIn("lease_without_live_heartbeat", result.reasons)
        self.assertIn("candidate_failed_after_passing_rerun", result.reasons)


class StallMonitorTests(TestCase):
    def test_recorded_candidate_block_is_reported_without_an_eligible_queue(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="example/repo", base_branch="main")
        policy = _policy_record(
            (snapshot.repository, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        candidate = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(
                policy=policy.policy, snapshot=snapshot, batch_landing=True
            ),
            base_sha=snapshot.base_sha,
            policy_sha256=policy.policy_sha256,
            created_at=_time(0),
        ).model_copy(
            update={
                "status": "failed",
                "required_checks_status": "fail",
                "candidate_sha": "candidate-sha",
            }
        )
        entry = candidate.entries[0]
        snapshot = snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pr.model_copy(update={"labels": ("merge-blocked",)})
                    for pr in snapshot.pull_requests
                    if pr.number == entry.pull_request_number
                )
            }
        )
        client, reporter = Mock(), Mock()
        client.target_setting_gaps.return_value = ()
        client.read_merge_train_snapshot.return_value = snapshot
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_policy_record",
                return_value=policy,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.GitHubMergeTrainClient",
                return_value=client,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_github_token",
                return_value="test-token",
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_stall_workflow",
                return_value=MergeTrainStallWorkflow(repository="example/control", ref="main"),
            ),
        ):
            root = Path(directory)
            store = FilesystemRecordStore(root)
            record = build_merge_train_batch_candidate_record(
                candidate=candidate,
                source="service:controller:candidate-block",
                updated_at=_time(0),
                block_intent=MergeTrainCandidateBlockIntent(
                    label="merge-blocked",
                    prior_label_event_id=0,
                    source_failed_record_id="failed-record",
                    label_write_succeeded=True,
                ),
            ).model_copy(update={"status": "superseded"})
            store.write_merge_train_batch_candidate_record(record)
            for seconds in (0, STALL_SECONDS):
                run_stall_monitor_pass(
                    store=store,
                    root=root,
                    now=lambda: _time(seconds),
                    notifier_factory=lambda **_: reporter,
                )
            observation = store.read_merge_train_stall_record(snapshot.repository).observations[
                "main"
            ]
            self.assertIn("blocked_change_not_moving", observation.reasons)
            self.assertIn(
                f"{entry.pull_request_number}:{entry.head_sha}", observation.blocked_since
            )
            reporter.notify.assert_called_once()
            client.read_merge_train_snapshot.return_value = snapshot.model_copy(
                update={"pull_requests": ()}
            )
            run_stall_monitor_pass(
                store=store,
                root=root,
                now=lambda: _time(STALL_SECONDS + 300),
                notifier_factory=lambda **_: reporter,
            )
            self.assertEqual(
                store.read_merge_train_stall_record(snapshot.repository).issue_state, "closed"
            )

    def test_missing_target_setting_alerts_at_once_and_resolves_when_added(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="example/repo", base_branch="main")
        policy = _policy_record(
            (snapshot.repository, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        failed = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(
                policy=policy.policy, snapshot=snapshot, batch_landing=True
            ),
            base_sha=snapshot.base_sha,
            policy_sha256=policy.policy_sha256,
            created_at=_time(0),
        ).model_copy(
            update={
                "status": "failed",
                "required_checks_status": "fail",
                "candidate_sha": "candidate-sha",
            }
        )
        client, reporter = Mock(), Mock()
        client.target_setting_gaps.return_value = ("Require status checks on main.",)
        client.read_merge_train_snapshot.return_value = snapshot.model_copy(
            update={"pull_requests": ()}
        )
        client.observe_batch_candidate_checks.side_effect = MergeTrainGitHubPolicyUnavailableError(
            "no required checks"
        )
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_policy_record",
                return_value=policy,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.GitHubMergeTrainClient",
                return_value=client,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_github_token",
                return_value="test-token",
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_stall_workflow",
                return_value=MergeTrainStallWorkflow(repository="example/control", ref="main"),
            ),
        ):
            root = Path(directory)
            store = FilesystemRecordStore(root)
            store.write_merge_train_batch_candidate_record(
                build_merge_train_batch_candidate_record(
                    candidate=failed, source="test", updated_at=_time(0)
                )
            )
            run_stall_monitor_pass(
                store=store, root=root, now=lambda: _time(0), notifier_factory=lambda **_: reporter
            )
            observation = store.read_merge_train_stall_record(snapshot.repository).observations[
                "main"
            ]
            self.assertIn("target_not_ready", observation.reasons)
            self.assertEqual(observation.setting_gaps, client.target_setting_gaps.return_value)
            reporter.notify.assert_called_once()
            client.target_setting_gaps.return_value = ()
            client.observe_batch_candidate_checks.side_effect = None
            client.observe_batch_candidate_checks.return_value = failed
            for seconds in (300, STALL_SECONDS):
                run_stall_monitor_pass(
                    store=store,
                    root=root,
                    now=lambda: _time(seconds),
                    notifier_factory=lambda **_: reporter,
                )
                self.assertEqual(
                    bool(
                        store.read_merge_train_stall_record(snapshot.repository)
                        .observations["main"]
                        .reasons
                    ),
                    seconds == STALL_SECONDS,
                )
            self.assertIn(
                "controller_unchanged",
                store.read_merge_train_stall_record(snapshot.repository)
                .observations["main"]
                .reasons,
            )

    def test_empty_repository_ignores_leases_but_reports_unresolved_aliases(self) -> None:
        snapshot = (
            _FakeExpandedMergeTrainSnapshotReader(transport=object())
            .read_merge_train_snapshot(repository="example/repo", base_branch="main")
            .model_copy(update={"pull_requests": ()})
        )
        policy = _policy_record(
            (snapshot.repository, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        client, reporter = Mock(), Mock()
        client.target_setting_gaps.return_value = ()
        client.read_merge_train_snapshot.return_value = snapshot
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_policy_record",
                return_value=policy,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_github_token",
                return_value="test-token",
            ),
            patch(
                "control_plane.merge_train_stall_monitor.GitHubMergeTrainClient",
                return_value=client,
            ),
        ):
            store = FilesystemRecordStore(Path(directory))
            repository = snapshot.repository.upper()
            alias = build_merge_train_controller_state_record(
                repository=repository,
                base_branch="main",
                policy_key=f"{snapshot.repository}:main",
                policy_sha256=policy.policy_sha256,
                updated_at=_time(0),
            ).model_copy(
                update={
                    "repository": repository,
                    "controller_key": f"merge-train-controller:{repository.replace('/', ':')}:main",
                    "active_record_id": "retained-provider-effect",
                }
            )
            state = alias.model_copy(
                update={
                    "repository": snapshot.repository,
                    "controller_key": alias.controller_key.lower(),
                    "active_record_id": "",
                }
            )
            for seconds, owner, expires in (
                (0, "", 0),
                (STALL_SECONDS * 2, "controller", STALL_SECONDS * 3),
                (STALL_SECONDS * 4, "controller", STALL_SECONDS * 3),
                (STALL_SECONDS * 5, "controller", STALL_SECONDS * 3),
                (STALL_SECONDS * 6, "", 0),
            ):
                with self.subTest(seconds=seconds, owner=owner):
                    state.status = "running" if owner else "idle"
                    state.lease_owner = owner
                    state.lease_acquired_at = _time(0) if owner else ""
                    state.heartbeat_at = _time(0) if owner else ""
                    state.lease_expires_at = _time(expires) if owner else ""
                    store.write_merge_train_controller_state_record(state)
                    run_stall_monitor_pass(
                        store=store,
                        root=Path(directory),
                        now=lambda: _time(seconds),
                        notifier_factory=lambda **_: reporter,
                    )
                    observation = store.read_merge_train_stall_record(
                        snapshot.repository
                    ).observations["main"]
                    self.assertFalse(observation.reasons)
                    reporter.notify.assert_not_called()
            path = store._write_model(
                "launchplane_merge_train_controller_states",
                _merge_train_controller_storage_id(alias.controller_key),
                alias,
            )
            before = path.read_bytes()
            run_stall_monitor_pass(
                store=store,
                root=Path(directory),
                now=lambda: _time(STALL_SECONDS * 6),
                notifier_factory=lambda **_: reporter,
            )
            record = store.read_merge_train_stall_record(snapshot.repository)
            self.assertIn(
                "controller_alias_requires_reconciliation", record.observations["main"].reasons
            )
            self.assertEqual(path.read_bytes(), before)

    def test_release_holds_pause_clocks_and_midpass_activity_does_not_reset_them(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="example/repo", base_branch="main")
        policy = _policy_record(
            (
                snapshot.repository,
                MergeTrainSchedulerPolicy(enabled=True, mutate=True),
            )
        )
        client, reporter = Mock(), Mock()
        client.target_setting_gaps.return_value = ()
        client.read_merge_train_snapshot.return_value = snapshot
        hold_reader = Mock(return_value=object())
        state = build_merge_train_controller_state_record(
            repository=snapshot.repository,
            base_branch="main",
            policy_key=f"{snapshot.repository}:main",
            policy_sha256=policy.policy_sha256,
            updated_at=_time(0),
        ).model_copy(
            update={
                "last_action": "observe",
                "last_phase": "checks",
                "last_record_id": "candidate",
                "active_action": "observe",
                "active_phase": "read_snapshot",
                "active_record_id": "candidate",
            }
        )
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_policy_record",
                return_value=policy,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.GitHubMergeTrainClient",
                return_value=client,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_github_token",
                return_value="test-token",
            ),
            patch(
                "control_plane.merge_train_stall_monitor.merge_train_release_hold_reader",
                return_value=hold_reader,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_stall_workflow",
                return_value=MergeTrainStallWorkflow(repository="example/control", ref="main"),
            ),
        ):
            root = Path(directory)
            store = FilesystemRecordStore(root)
            with patch.object(
                store, "list_merge_train_controller_state_records", return_value=(state,)
            ):

                def run(seconds: int) -> None:
                    run_stall_monitor_pass(
                        store=store,
                        root=root,
                        now=lambda: _time(seconds),
                        notifier_factory=lambda **_: reporter,
                    )

                run(0)
                run(STALL_SECONDS * 2)
                reporter.notify.assert_not_called()
                hold_reader.return_value = None
                run(STALL_SECONDS * 2 + 300)
                state.active_phase = "reconcile_candidate"
                run(STALL_SECONDS * 3 + 299)
                reporter.notify.assert_not_called()
                state.active_phase = "read_snapshot"
                run(STALL_SECONDS * 3 + 300)
                record = store.read_merge_train_stall_record(snapshot.repository)
                self.assertIn("controller_unchanged", record.observations["main"].reasons)
                self.assertIn("ready_change_not_moving", record.observations["main"].reasons)
                reporter.notify.assert_called_once()

    def test_reason_change_notification_survives_failed_dispatch(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="example/repo", base_branch="main")
        policy = _policy_record(
            (
                snapshot.repository,
                MergeTrainSchedulerPolicy(enabled=True, mutate=True),
            )
        )
        client, reporter = Mock(), Mock()
        client.target_setting_gaps.return_value = ()
        client.read_merge_train_snapshot.return_value = snapshot
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_policy_record",
                return_value=policy,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.GitHubMergeTrainClient",
                return_value=client,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_github_token",
                return_value="test-token",
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_stall_workflow",
                return_value=MergeTrainStallWorkflow(repository="example/control", ref="main"),
            ),
        ):
            root = Path(directory)
            store = FilesystemRecordStore(root)

            def run(seconds: int) -> None:
                run_stall_monitor_pass(
                    store=store,
                    root=root,
                    now=lambda: _time(seconds),
                    notifier_factory=lambda **_: reporter,
                )

            run(0)
            run(STALL_SECONDS)
            reporter.notify.assert_called_once()
            published = store.read_merge_train_stall_record(snapshot.repository)
            client.read_merge_train_snapshot.return_value = snapshot.model_copy(
                update={"base_sha": "new-base"}
            )
            reporter.notify.side_effect = RuntimeError("temporary delivery failure")
            run(STALL_SECONDS + 300)
            failed = store.read_merge_train_stall_record(snapshot.repository)
            self.assertNotEqual(failed.notification_key, published.notification_key)
            self.assertNotEqual(failed.notification_key, failed.notified_key)
            self.assertEqual(failed.delivery_error, "RuntimeError")
            self.assertNotIn("controller_unchanged", failed.observations["main"].reasons)
            reporter.notify.side_effect = None
            run(STALL_SECONDS + 600)
            self.assertEqual(reporter.notify.call_count, 3)
            self.assertEqual(
                store.read_merge_train_stall_record(snapshot.repository).notified_key,
                failed.notification_key,
            )
            run(STALL_SECONDS + 900)
            self.assertEqual(reporter.notify.call_count, 3)

    def test_restart_preserves_stall_and_sends_resolution_after_queue_clears(self) -> None:
        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
        )
        policy = _policy_record(
            (
                snapshot.repository,
                MergeTrainSchedulerPolicy(enabled=True, mutate=True),
            )
        )
        client = Mock()
        client.target_setting_gaps.return_value = ()
        client.read_merge_train_snapshot.return_value = snapshot
        reporter = Mock()
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_stall_monitor.GitHubMergeTrainClient",
                return_value=client,
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_github_token",
                return_value="test-token",
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_stall_workflow",
                return_value=MergeTrainStallWorkflow(repository="example/control", ref="main"),
            ),
            patch(
                "control_plane.merge_train_stall_monitor.resolve_merge_train_policy_record",
                return_value=policy,
            ),
        ):
            root = Path(directory)
            store = FilesystemRecordStore(root)
            run_stall_monitor_pass(
                store=store, root=root, now=lambda: _time(0), notifier_factory=lambda **_: reporter
            )
            reporter.notify.assert_not_called()
            restarted = FilesystemRecordStore(root)
            run_stall_monitor_pass(
                store=restarted,
                root=root,
                now=lambda: _time(STALL_SECONDS),
                notifier_factory=lambda **_: reporter,
            )
            stalled = restarted.read_merge_train_stall_record(snapshot.repository)
            self.assertEqual(stalled.issue_state, "open")
            reporter.notify.assert_called_once()
            run_stall_monitor_pass(
                store=restarted,
                root=root,
                now=lambda: _time(STALL_SECONDS + 300),
                notifier_factory=lambda **_: reporter,
            )
            reporter.notify.assert_called_once()
            client.read_merge_train_snapshot.return_value = snapshot.model_copy(
                update={"pull_requests": ()}
            )
            run_stall_monitor_pass(
                store=restarted,
                root=root,
                now=lambda: _time(STALL_SECONDS + 600),
                notifier_factory=lambda **_: reporter,
            )
            resolved = restarted.read_merge_train_stall_record(snapshot.repository)
            self.assertEqual(resolved.issue_state, "closed")
            self.assertEqual(
                store.read_merge_train_stall_record(snapshot.repository).issue_state, "closed"
            )
            self.assertEqual(reporter.notify.call_count, 2)

    def test_deliberate_dry_run_and_level1_targets_do_not_start_alert_clocks(self) -> None:
        for scheduler in (
            MergeTrainSchedulerPolicy(enabled=True),
            MergeTrainSchedulerPolicy(enabled=True, mutate=True, runner_mode="level1"),
        ):
            with self.subTest(scheduler=scheduler), TemporaryDirectory() as directory:
                root = Path(directory)
                store = FilesystemRecordStore(root)
                policy = _policy_record(("example/repo", scheduler))
                with (
                    patch(
                        "control_plane.merge_train_stall_monitor.resolve_merge_train_policy_record",
                        return_value=policy,
                    ),
                    patch(
                        "control_plane.merge_train_stall_monitor.resolve_merge_train_github_token"
                    ) as resolve,
                ):
                    run_stall_monitor_pass(store=store, root=root)
                resolve.assert_not_called()
                with self.assertRaises(FileNotFoundError):
                    store.read_merge_train_stall_record("example/repo")

    def test_unreadable_evidence_cannot_close_existing_incident(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = FilesystemRecordStore(root)
            record = MergeTrainStallRecord(
                repository="example/repo", observed_at=_time(0), issue_number=5, issue_state="open"
            )
            store.write_merge_train_stall_record(record)
            policy = _policy_record(
                (
                    record.repository,
                    MergeTrainSchedulerPolicy(enabled=True, mutate=True),
                )
            )
            with (
                patch(
                    "control_plane.merge_train_stall_monitor.resolve_merge_train_policy_record",
                    return_value=policy,
                ),
                patch(
                    "control_plane.merge_train_stall_monitor.resolve_merge_train_github_token",
                    side_effect=ValueError("unavailable"),
                ),
            ):
                run_stall_monitor_pass(store=store, root=root)
            self.assertEqual(store.read_merge_train_stall_record(record.repository), record)

    def test_database_storage_survives_restart(self) -> None:
        with TemporaryDirectory() as directory:
            url = _sqlite_database_url(Path(directory) / "state.db")
            store = PostgresRecordStore(database_url=url)
            store.ensure_schema()
            record = MergeTrainStallRecord(
                repository="example/repo",
                observed_at=_time(0),
                observations={
                    "main": observe_stall(
                        previous=MergeTrainStallObservation(),
                        now=_time(0),
                        progress_key="a",
                        ready=("1:h",),
                        busy=True,
                    )
                },
            )
            with store.merge_train_stall_lock(repository=record.repository):
                store.write_merge_train_stall_record(record)
            store.close()
            reopened = PostgresRecordStore(database_url=url)
            try:
                self.assertEqual(reopened.read_merge_train_stall_record(record.repository), record)
            finally:
                reopened.close()


class StallReporterTests(TestCase):
    def test_dispatch_requires_only_workflow_delivery_and_no_product_issue(self) -> None:
        record = MergeTrainStallRecord(repository="example/repo", observed_at=_time(0))
        workflow = MergeTrainStallWorkflow(repository="example/control", ref="main")
        with (
            patch(
                "control_plane.merge_train_stall_monitor.resolve_delivery_github_token",
                return_value="test-token",
            ) as resolve,
            patch(
                "control_plane.merge_train_stall_monitor.UrllibMergeTrainGitHubTransport"
            ) as transport,
        ):
            DiscordStallNotifier(root=Path(".")).notify(record, workflow)
        resolve.assert_called_once_with(
            control_plane_root=Path("."),
            context_name="launchplane",
            repository=workflow.repository,
            purpose="workflow_dispatch",
        )
        request = transport.return_value.request
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["method"], "POST")
        self.assertNotIn("/issues", request.call_args.kwargs["path"])
        self.assertNotIn("issue_number", request.call_args.kwargs["body"]["inputs"])

    def test_discord_payload_points_to_launchplane_without_product_issue(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "STALL_REPOSITORY": "example/repo",
                    "STALL_STATE": "open",
                    "STALL_NOTIFICATION_KEY": "a" * 64,
                },
            ),
            patch(
                "control_plane.deploy_failure_alert.validate_sender",
                return_value="fake-existing-sender",
            ),
            patch("control_plane.notifications.post_discord_webhook") as send,
        ):
            module = runpy.run_path(
                str(Path(__file__).parents[1] / "scripts/notify-merge-train-stall.py")
            )
            module["send_stall_alert"]()
            payload = send.call_args.args[1]
            self.assertNotIn("github.com", payload["embeds"][0]["description"])
            self.assertEqual(payload["allowed_mentions"], {"parse": []})


class StallMonitorPostgresTests(TestCase):
    def test_replica_lock_keeps_issue_identity_and_clock_after_wait(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from tests.test_postgres_integration import _store_for_fresh_head_database

        with _store_for_fresh_head_database() as store:
            attempting, entered = Event(), Event()
            record = MergeTrainStallRecord(
                repository="example/replica",
                observed_at=_time(0),
                issue_number=7,
                issue_state="open",
            )

            def read_after_lock() -> MergeTrainStallRecord:
                attempting.set()
                with store.merge_train_stall_lock(repository=record.repository):
                    entered.set()
                    return store.read_merge_train_stall_record(record.repository)

            with ThreadPoolExecutor(max_workers=1) as executor:
                with store.merge_train_stall_lock(repository=record.repository):
                    future = executor.submit(read_after_lock)
                    self.assertTrue(attempting.wait(5))
                    self.assertFalse(entered.wait(0.2))
                    store.write_merge_train_stall_record(record)
                self.assertEqual(future.result(timeout=5), record)
