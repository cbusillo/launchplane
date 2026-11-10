from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_retention_lifecycle import CandidateRetentionLifecycle


class CandidateRetentionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(database_url=f"sqlite:///{self.root / 'state.db'}")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.custody: dict[str, Any] = {
            "namespace": "failed-candidate",
            "server": "fabricated-server",
            "target": "failed-target",
            "payload": {"container_ids": ["a" * 64], "container_snapshot": "b" * 64},
        }
        self.guard = Mock()
        self.enterContext(
            patch(
                "control_plane.testing_candidate_retention_lifecycle.source.read_dokploy_config",
                return_value=("fixture.invalid", "synthetic"),
            )
        )

    def test_uncertain_stop_requires_original_readback_and_does_not_repeat_stop(self) -> None:
        adapter = CandidateRetentionLifecycle(
            self.store, self.root, self.custody, "stop", self.guard
        )
        with (
            patch.object(adapter, "read", return_value={"stopped": False}) as read,
            patch(
                "control_plane.testing_candidate_retention_lifecycle.api.dokploy_request",
                side_effect=TimeoutError,
            ) as stop,
        ):
            for _ in range(2):
                with self.assertRaises(ProviderMutationUnknownError):
                    adapter.execute("cleanup-original", trace_id="trace", mutate=True)
            self.assertEqual(stop.call_count, 1)
            read.return_value = {"stopped": True}
            self.assertTrue(adapter.execute("cleanup-original", trace_id="readback", mutate=False))
            self.assertEqual(stop.call_count, 1)

    def test_dispatch_rechecks_authority_and_never_removes_an_unstarted_readback(self) -> None:
        adapter = CandidateRetentionLifecycle(
            self.store, self.root, self.custody, "remove", self.guard
        )
        with patch(
            "control_plane.testing_candidate_retention_lifecycle.retained_copy_operation"
        ) as remove:
            self.assertFalse(
                adapter.execute("unstarted-original", trace_id="readback", mutate=False)
            )
            remove.assert_not_called()
            self.guard.side_effect = ValueError("current candidate became selected")
            with self.assertRaisesRegex(ValueError, "became selected"):
                adapter.execute("selected-original", trace_id="trace", mutate=True)
            remove.assert_not_called()

    def test_container_removal_uses_dispatch_guard_and_original_absence_readback(self) -> None:
        adapter = CandidateRetentionLifecycle(
            self.store, self.root, self.custody, "remove", self.guard
        )
        effects = []

        def native(**kwargs: Any) -> dict[str, Any]:
            if kwargs["payload"]["mode"] == "remove":
                kwargs["dispatch_guard"](lambda: effects.append("removed"))
            return {"availability": "absent"}

        with patch(
            "control_plane.testing_candidate_retention_lifecycle.retained_copy_operation",
            side_effect=native,
        ):
            self.assertTrue(adapter.execute("cleanup-original", trace_id="trace", mutate=True))
            self.assertTrue(adapter.execute("cleanup-original", trace_id="replay", mutate=True))
            self.assertEqual(effects, ["removed"])
            self.assertGreaterEqual(self.guard.call_count, 2)
