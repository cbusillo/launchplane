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

    def test_enqueue_label_and_its_trains_completions_wake_only_that_train(self) -> None:
        self.policy_reader.return_value = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=False)),
            ("cbusillo/beta", MergeTrainSchedulerPolicy(enabled=True, mutate=False)),
        )
        alpha = "merge-train-controller:cbusillo:alpha:main"
        into_main = [{"number": 7, "base": {"ref": "main"}}]
        candidate = "launchplane/train/cbusillo/alpha/main/merge-train-batch-1"
        wakes: tuple[tuple[str, dict[str, object]], ...] = (
            (
                "pull_request",
                {
                    "action": "labeled",
                    "pull_request": {"base": {"ref": "main"}},
                    "label": {"name": "ready-to-merge"},
                },
            ),
            (
                "pull_request",
                {
                    "action": "unlabeled",
                    "pull_request": {"base": {"ref": "main"}},
                    "label": {"name": "ready-to-merge"},
                },
            ),
            ("pull_request", {"action": "closed", "pull_request": {"base": {"ref": "main"}}}),
            # A PR into the base may be queued: labels are not in check payloads.
            ("check_run", {"action": "completed", "check_run": {"pull_requests": into_main}}),
            ("workflow_run", {"action": "completed", "workflow_run": {"pull_requests": into_main}}),
            # Candidate refs have no PR; the ref names the train.
            (
                "check_run",
                {
                    "action": "completed",
                    "check_run": {"pull_requests": [], "check_suite": {"head_branch": candidate}},
                },
            ),
            ("check_suite", {"action": "completed", "check_suite": {"head_branch": candidate}}),
            ("check_suite", {"action": "completed", "check_suite": {"head_branch": "main"}}),
            # A commit status names no PR, so a PR head branch may be queued.
            ("status", {"state": "success", "branches": [{"name": "feature"}]}),
        )
        for event, payload in wakes:
            with self.subTest(event=event, payload=payload):
                self.store.notify_merge_train.reset_mock()
                self.assertTrue(
                    wake_merge_train_for_event(
                        self.store, event, {"repository": {"id": 42}, **payload}
                    )
                )
                self.store.notify_merge_train.assert_called_once_with((alpha,))
        self.connect.assert_not_called()

    def test_completions_no_train_waits_on_do_not_notify(self) -> None:
        unrelated: tuple[tuple[str, dict[str, object]], ...] = (
            # A push to a branch with no PR, which is also a fork PR's shape.
            (
                "check_run",
                {
                    "action": "completed",
                    "check_run": {"pull_requests": [], "check_suite": {"head_branch": "feature"}},
                },
            ),
            ("check_suite", {"action": "completed", "check_suite": {"head_branch": "feature"}}),
            # Another train's candidate.
            (
                "check_suite",
                {
                    "action": "completed",
                    "check_suite": {
                        "head_branch": "launchplane/train/cbusillo/alpha/release/merge-train-batch-2"
                    },
                },
            ),
            (
                "status",
                {
                    "state": "success",
                    "branches": [
                        {"name": "launchplane/train/cbusillo/alpha/release/merge-train-batch-2"}
                    ],
                },
            ),
            ("check_run", {"action": "completed"}),
            ("status", {"state": "success", "branches": []}),
            (
                "pull_request",
                {
                    "action": "unlabeled",
                    "pull_request": {"base": {"ref": "main"}},
                    "label": {"name": "other"},
                },
            ),
        )
        for event, payload in unrelated:
            with self.subTest(event=event, payload=payload):
                self.assertFalse(
                    wake_merge_train_for_event(
                        self.store, event, {"repository": {"id": 42}, **payload}
                    )
                )
        self.store.notify_merge_train.assert_not_called()

    def test_a_pr_wakes_the_train_of_its_base_or_every_train_it_may_stack_on(self) -> None:
        policy = self.policy.policy
        release = policy.policies[0].model_copy(update={"base_branch": "release"})
        self.policy_reader.return_value = self.policy.model_copy(
            update={"policy": policy.model_copy(update={"policies": (*policy.policies, release)})}
        )
        main_train = "merge-train-controller:cbusillo:alpha:main"
        release_train = "merge-train-controller:cbusillo:alpha:release"
        for base, woken in (
            ("release", (release_train,)),
            ("main", (main_train,)),
            # A stack child's base is its parent's branch, which either train may root.
            ("parent-feature", (main_train, release_train)),
        ):
            with self.subTest(base=base):
                self.store.notify_merge_train.reset_mock()
                wake_merge_train_for_event(
                    self.store,
                    "check_run",
                    {
                        "repository": {"id": 42},
                        "action": "completed",
                        "check_run": {"pull_requests": [{"number": 8, "base": {"ref": base}}]},
                    },
                )
                self.store.notify_merge_train.assert_called_once_with(woken)

    def test_unmapped_disabled_or_unrelated_events_do_not_notify(self) -> None:
        payload: dict[str, object] = {
            "repository": {"id": 42},
            "action": "completed",
            "check_run": {"pull_requests": [{"number": 7, "base": {"ref": "main"}}]},
        }
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

    def _notifications(self, *payloads: str) -> list[SimpleNamespace]:
        pending = [SimpleNamespace(payload=payload) for payload in reversed(payloads)]

        # psycopg returns a generator with close() and honors stop_after.
        def notifications(
            *, timeout: float, stop_after: int | None = None
        ) -> Iterator[SimpleNamespace]:
            del timeout
            yielded = 0
            while pending and (stop_after is None or yielded < stop_after):
                yielded += 1
                yield pending.pop()

        self.connect.return_value.notifies.side_effect = notifications
        return pending

    def test_listener_folds_queued_hints_into_the_named_trains(self) -> None:
        connection = self.connect.return_value
        pending = self._notifications("alpha", "beta", "alpha")
        listener = MergeTrainEventListener("postgresql://test")
        # The first hint ends the wait; the two queued behind it join the same pass.
        self.assertEqual(listener.wait(300, Event()), frozenset({"alpha", "beta"}))
        connection.execute.assert_called_once()
        self.assertEqual(pending, [])
        connection.notifies.assert_any_call(timeout=0)
        listener.close()
        connection.close.assert_called_once()

    def test_a_hint_naming_no_train_or_a_due_sweep_wakes_every_train(self) -> None:
        listener = MergeTrainEventListener("postgresql://test")
        self._notifications("alpha", "")
        self.assertIsNone(listener.wait(300, Event()))
        # A due sweep drops waiting hints without blocking: it rereads every train.
        pending = self._notifications("alpha")
        self.assertIsNone(listener.wait(0, Event()))
        self.assertEqual(pending, [])
        self.connect.return_value.notifies.assert_called_with(timeout=0)

    def test_listener_failure_uses_sweep_and_reconnects_next_wait(self) -> None:
        self.connect.side_effect = RuntimeError("database unavailable")
        stop = MagicMock(spec=Event)
        listener = MergeTrainEventListener("postgresql://test")
        self.assertIsNone(listener.wait(300, stop, monotonic=lambda: 0))
        stop.wait.assert_called_once_with(timeout=300)
        self.assertIsNone(listener.connection)
        self.connect.side_effect = None
        self._notifications("alpha")
        self.assertEqual(listener.wait(300, Event()), frozenset({"alpha"}))
        self.assertEqual(self.connect.call_count, 2)
