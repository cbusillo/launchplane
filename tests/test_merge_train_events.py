import json
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
import click

from control_plane import merge_train_events
from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy
from control_plane.github_app_webhook import (
    GitHubAppWebhookDependencies,
    handle_github_app_webhook_request,
)
from control_plane.merge_train_events import MergeTrainEventListener, wake_merge_train_for_event
from tests.test_merge_train_scheduler import _policy_record


class MergeTrainEventTests(TestCase):
    def setUp(self) -> None:
        self.store = SimpleNamespace(notify_merge_train=MagicMock())
        self.policy = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=False))
        )
        self.patchers = (
            patch.object(
                merge_train_events, "resolve_merge_train_policy_record", return_value=self.policy
            ),
            patch.object(
                merge_train_events,
                "get_repository_inventory_read_model",
                return_value=SimpleNamespace(
                    current_record=SimpleNamespace(
                        inventory_state="tracked", repository="cbusillo/alpha"
                    )
                ),
            ),
            patch.object(merge_train_events, "_connect"),
        )
        self.policy_reader, self.inventory_reader, self.connect = (
            patcher.start() for patcher in self.patchers
        )
        for patcher in self.patchers:
            self.addCleanup(patcher.stop)

    def test_enqueue_label_and_completed_checks_wake_enabled_train(self) -> None:
        payload: dict[str, object] = {
            "repository": {"id": 42},
            "action": "labeled",
            "pull_request": {"base": {"ref": "main"}},
            "label": {"name": self.policy.policy.policies[0].enqueue_label},
        }
        self.assertTrue(wake_merge_train_for_event(self.store, "pull_request", payload))
        for event in ("check_run", "check_suite", "workflow_run"):
            self.assertTrue(
                wake_merge_train_for_event(
                    self.store, event, {"repository": {"id": 42}, "action": "completed"}
                )
            )
        self.assertTrue(
            wake_merge_train_for_event(
                self.store, "status", {"repository": {"id": 42}, "state": "success"}
            )
        )
        self.store.notify_merge_train.assert_called()
        self.connect.assert_not_called()

    def test_unmapped_disabled_or_unrelated_events_do_not_notify(self) -> None:
        payload: dict[str, object] = {"repository": {"id": 42}, "action": "completed"}
        self.inventory_reader.return_value.current_record = None
        self.assertFalse(wake_merge_train_for_event(self.store, "check_run", payload))
        self.inventory_reader.return_value.current_record = SimpleNamespace(
            inventory_state="tracked", repository="cbusillo/other"
        )
        self.assertFalse(wake_merge_train_for_event(self.store, "check_run", payload))
        self.inventory_reader.return_value.current_record.repository = "cbusillo/alpha"
        self.policy_reader.return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=False))
        )
        self.assertFalse(wake_merge_train_for_event(self.store, "check_run", payload))
        self.assertFalse(
            wake_merge_train_for_event(self.store, "check_run", {**payload, "action": "created"})
        )
        self.connect.assert_not_called()

    def test_other_label_or_base_does_not_notify(self) -> None:
        for base, label in (("other", "ready-to-merge"), ("main", "other")):
            self.assertFalse(
                wake_merge_train_for_event(
                    self.store,
                    "pull_request",
                    {
                        "repository": {"id": 42},
                        "action": "labeled",
                        "pull_request": {"base": {"ref": base}},
                        "label": {"name": label},
                    },
                )
            )
        self.connect.assert_not_called()

    def test_receiver_wakes_only_after_signature_and_json_validation(self) -> None:
        wake = MagicMock(return_value=True)
        dependencies = GitHubAppWebhookDependencies(
            webhook_secret=lambda: "secret",
            verify_signature=lambda **_: None,
            wake_merge_train=wake,
        )
        status, body = handle_github_app_webhook_request(
            json.dumps({"action": "completed", "repository": {"id": 42}}).encode(),
            "check_run",
            "delivery",
            "sig",
            object(),
            Path("."),
            "trace",
            dependencies=dependencies,
        )
        self.assertEqual(status, 202)
        self.assertEqual(
            body["result"], {"status": "ignored", "reason": "merge_train_woken", "target_keys": []}
        )
        wake.assert_called_once()
        wake.reset_mock()
        status, _ = handle_github_app_webhook_request(
            b"invalid-json",
            "check_run",
            "delivery",
            "sig",
            object(),
            Path("."),
            "trace",
            dependencies=dependencies,
        )
        self.assertEqual(status, 400)
        wake.assert_not_called()

    def test_invalid_signature_never_wakes_the_train(self) -> None:
        wake = MagicMock()
        status, _ = handle_github_app_webhook_request(
            b"{}",
            "check_run",
            "delivery",
            "invalid",
            object(),
            Path("."),
            "trace",
            dependencies=GitHubAppWebhookDependencies(
                webhook_secret=lambda: "secret",
                verify_signature=MagicMock(side_effect=click.ClickException("invalid signature")),
                wake_merge_train=wake,
            ),
        )
        self.assertEqual(status, 401)
        wake.assert_not_called()

    def test_listener_returns_on_notification_and_closes_connection(self) -> None:
        connection = self.connect.return_value

        # psycopg returns a generator with close().
        pending = [object(), object(), object()]

        def notifications(**_: object) -> Iterator[object]:
            while pending:
                yield pending.pop()

        connection.notifies.side_effect = notifications
        listener = MergeTrainEventListener("postgresql://test")
        listener.wait(300, Event())
        connection.execute.assert_called_once()
        self.assertEqual(pending, [])
        connection.notifies.assert_any_call(timeout=0)
        listener.close()
        connection.close.assert_called_once()

    def test_listener_failure_uses_sweep_and_reconnects_next_wait(self) -> None:
        self.connect.side_effect = RuntimeError("database unavailable")
        stop = MagicMock(spec=Event)
        listener = MergeTrainEventListener("postgresql://test")
        listener.wait(300, stop, monotonic=lambda: 0)
        stop.wait.assert_called_once_with(timeout=300)
        self.assertIsNone(listener.connection)
