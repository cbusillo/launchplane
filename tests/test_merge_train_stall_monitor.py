from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import runpy
from types import SimpleNamespace

from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy
from control_plane.contracts.merge_train_stall import (
    MergeTrainStallObservation,
    MergeTrainStallRecord,
    MergeTrainStallWorkflow,
)
from control_plane.merge_train_stall_monitor import (
    STALL_SECONDS,
    STALL_ISSUE_MARKER,
    GitHubStallReporter,
    observe_stall,
    run_stall_monitor_pass,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from tests.test_merge_train_scheduler import _policy_record


def _time(seconds: int) -> str:
    return (datetime(2026, 10, 12, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


class StallObservationTests(TestCase):
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
        client.read_merge_train_snapshot.return_value = snapshot
        reporter.publish.return_value = 91
        hold_reader = Mock(return_value=object())
        state = SimpleNamespace(
            last_action="observe",
            last_phase="checks",
            last_record_id="candidate",
            active_action="observe",
            active_phase="read_snapshot",
            active_record_id="candidate",
            lease_owner="",
            lease_expires_at="",
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
                        reporter_factory=lambda **_: reporter,
                    )

                run(0)
                run(STALL_SECONDS * 2)
                reporter.publish.assert_not_called()
                hold_reader.return_value = None
                run(STALL_SECONDS * 2 + 300)
                state.active_phase = "reconcile_candidate"
                run(STALL_SECONDS * 3 + 299)
                reporter.publish.assert_not_called()
                state.active_phase = "read_snapshot"
                run(STALL_SECONDS * 3 + 300)
                record = store.read_merge_train_stall_record(snapshot.repository)
                self.assertIn("controller_unchanged", record.observations["main"].reasons)
                self.assertIn("ready_change_not_moving", record.observations["main"].reasons)
                reporter.notify.assert_called_once()

    def test_reason_change_notification_survives_failed_issue_update(self) -> None:
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
        client.read_merge_train_snapshot.return_value = snapshot
        reporter.publish.return_value = 91
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
                    reporter_factory=lambda **_: reporter,
                )

            run(0)
            run(STALL_SECONDS)
            reporter.notify.assert_called_once()
            published = store.read_merge_train_stall_record(snapshot.repository)
            client.read_merge_train_snapshot.return_value = snapshot.model_copy(
                update={"base_sha": "new-base"}
            )
            reporter.publish.side_effect = RuntimeError("temporary delivery failure")
            run(STALL_SECONDS + 300)
            failed = store.read_merge_train_stall_record(snapshot.repository)
            self.assertEqual(failed.published_reason_sha256, published.published_reason_sha256)
            self.assertNotIn("controller_unchanged", failed.observations["main"].reasons)
            reporter.publish.side_effect = None
            run(STALL_SECONDS + 600)
            self.assertEqual(reporter.notify.call_count, 2)
            run(STALL_SECONDS + 900)
            self.assertEqual(reporter.notify.call_count, 2)

    def test_restart_updates_one_issue_and_closes_it_after_queue_clears(self) -> None:
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
        client.read_merge_train_snapshot.return_value = snapshot
        reporter = Mock()
        reporter.publish.return_value = 91
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
                store=store, root=root, now=lambda: _time(0), reporter_factory=lambda **_: reporter
            )
            reporter.publish.assert_not_called()
            restarted = FilesystemRecordStore(root)
            run_stall_monitor_pass(
                store=restarted,
                root=root,
                now=lambda: _time(STALL_SECONDS),
                reporter_factory=lambda **_: reporter,
            )
            stalled = restarted.read_merge_train_stall_record(snapshot.repository)
            self.assertEqual(stalled.issue_number, 91)
            self.assertEqual(stalled.issue_state, "open")
            reporter.notify.assert_called_once()
            run_stall_monitor_pass(
                store=restarted,
                root=root,
                now=lambda: _time(STALL_SECONDS + 300),
                reporter_factory=lambda **_: reporter,
            )
            reporter.notify.assert_called_once()
            client.read_merge_train_snapshot.return_value = snapshot.model_copy(
                update={"pull_requests": ()}
            )
            run_stall_monitor_pass(
                store=restarted,
                root=root,
                now=lambda: _time(STALL_SECONDS + 600),
                reporter_factory=lambda **_: reporter,
            )
            resolved = restarted.read_merge_train_stall_record(snapshot.repository)
            self.assertEqual(resolved.issue_number, stalled.issue_number)
            self.assertEqual(resolved.issue_state, "closed")
            self.assertFalse(reporter.publish.call_args.kwargs["stalled"])
            self.assertEqual(reporter.notify.call_count, 2)

    def test_issue_body_progress_does_not_repeat_discord_and_publish_failure_is_visible(
        self,
    ) -> None:
        from control_plane.merge_train_github import MergeTrainGitHubError

        snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
        )
        policy = _policy_record(
            (
                snapshot.repository,
                MergeTrainSchedulerPolicy(
                    enabled=True,
                    mutate=True,
                ),
            )
        )
        client, reporter = Mock(), Mock()
        reporter.publish.return_value = 91
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
            for seconds, base in (
                (0, "base-a"),
                (STALL_SECONDS, "base-b"),
                (STALL_SECONDS + 300, "base-c"),
            ):
                client.read_merge_train_snapshot.return_value = snapshot.model_copy(
                    update={"base_sha": base}
                )
                run_stall_monitor_pass(
                    store=store,
                    root=root,
                    now=lambda: _time(seconds),
                    reporter_factory=lambda **_: reporter,
                )
            reporter.notify.assert_called_once()
            self.assertEqual(reporter.publish.call_count, 2)
            reporter.publish.side_effect = MergeTrainGitHubError("issue missing", status_code=404)
            run_stall_monitor_pass(
                store=store,
                root=root,
                now=lambda: _time(STALL_SECONDS + 600),
                reporter_factory=lambda **_: reporter,
            )
            missing = store.read_merge_train_stall_record(snapshot.repository)
            self.assertIsNone(missing.issue_number)
            self.assertTrue(missing.delivery_error)
            reporter.publish.side_effect = None
            # A temporary missing response that reconciles the same issue does
            # not manufacture another Discord transition.
            run_stall_monitor_pass(
                store=store,
                root=root,
                now=lambda: _time(STALL_SECONDS + 700),
                reporter_factory=lambda **_: reporter,
            )
            reporter.notify.assert_called_once()
            reporter.publish.side_effect = MergeTrainGitHubError("issue gone", status_code=410)
            run_stall_monitor_pass(
                store=store,
                root=root,
                now=lambda: _time(STALL_SECONDS + 800),
                reporter_factory=lambda **_: reporter,
            )
            self.assertIsNone(store.read_merge_train_stall_record(snapshot.repository).issue_number)
            reporter.publish.side_effect = None
            reporter.publish.return_value = 92
            run_stall_monitor_pass(
                store=store,
                root=root,
                now=lambda: _time(STALL_SECONDS + 900),
                reporter_factory=lambda **_: reporter,
            )
            self.assertEqual(
                store.read_merge_train_stall_record(snapshot.repository).issue_number, 92
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
    def test_lost_create_response_reconciles_existing_issue_before_writing(self) -> None:
        reporter = object.__new__(GitHubStallReporter)
        reporter.repository = "example/repo"
        reporter.path = "/repos/example/repo"
        reporter.transport = Mock()
        body = STALL_ISSUE_MARKER + "\nA stalled train."
        issue = {"number": 6, "body": body, "state": "open", "user": {"type": "Bot"}}
        reporter.transport.request.side_effect = [{"total_count": 1, "items": [issue]}, issue]
        record = MergeTrainStallRecord(repository=reporter.repository, observed_at=_time(0))
        self.assertEqual(reporter.publish(record, body, stalled=True), issue["number"])
        self.assertTrue(
            all(
                call.kwargs["method"] == "GET" for call in reporter.transport.request.call_args_list
            )
        )

    def test_discord_payload_contains_only_identity_and_durable_link(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "STALL_REPOSITORY": "example/repo",
                    "STALL_ISSUE_NUMBER": "6",
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
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertIn(
                "https://github.com/example/repo/issues/6", payload["embeds"][0]["description"]
            )


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
