from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from control_plane.odoo_testing_drill_usage import (
    DRILL_RECENT_USE_SECONDS,
    read_drill_recent_signins,
)
from tests.test_odoo_testing_restore import generation


class DrillUsageReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gen = generation().model_copy(
            update={"completed_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}
        )
        self.selection = SimpleNamespace(
            identity=SimpleNamespace(
                context=self.gen.context, image_reference="registry.invalid/odoo@sha256:" + "a" * 64
            )
        )
        self.store = Mock()
        self.store.read_dokploy_target_id_record.return_value = SimpleNamespace(
            target_id=self.gen.target_id
        )
        self.store.read_dokploy_target_record.return_value = SimpleNamespace(
            target_name="fixture-testing", target_type="compose", env={}
        )
        self.store.list_runtime_environment_records.return_value = [
            SimpleNamespace(
                scope="instance", instance="testing", env={"ODOO_DB_NAME": "testing_database"}
            )
        ]
        module = "control_plane.odoo_testing_drill_usage."
        self.enterContext(patch(module + "select_stable_runtime", return_value=self.selection))
        self.enterContext(
            patch(
                module + "source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture-token"),
            )
        )
        self.enterContext(
            patch(
                module + "api.fetch_dokploy_target_payload",
                return_value={
                    "name": "fixture-testing",
                    "serverId": "remote-server",
                    "appName": self.gen.namespace,
                },
            )
        )
        self.current = self.enterContext(patch(module + "assert_selection_current"))
        self.inspect = self.enterContext(patch(module + "inspect_native_testing_receiver"))

    def test_complete_boolean_is_bound_to_the_selected_testing_runtime(self) -> None:
        now = datetime.now(timezone.utc)
        for used in (True, False):
            with self.subTest(used=used):
                self.inspect.side_effect = lambda **kw: kw["receive"](
                    {"type": "complete", "recent_signins": used}
                )
                self.assertEqual(
                    read_drill_recent_signins(self.store, self.gen, now.timestamp()), used
                )
                spec = self.inspect.call_args.kwargs["spec"]
                self.assertEqual(spec["generation"], self.gen.model_dump(mode="json"))
                self.assertEqual(spec["database_name"], "testing_database")
                self.assertEqual(
                    datetime.fromisoformat(spec["usage_since"]),
                    now - timedelta(seconds=DRILL_RECENT_USE_SECONDS),
                )

    def test_incomplete_or_malformed_reads_never_mean_idle(self) -> None:
        for frame in (
            None,
            {"type": "complete"},
            {"type": "complete", "recent_signins": 0},
            {"type": "failed"},
            {"type": "complete", "recent_signins": "false"},
        ):
            with self.subTest(frame=frame):
                self.inspect.side_effect = lambda **kw: kw["receive"](frame) if frame else None
                with self.assertRaises(ValueError):
                    read_drill_recent_signins(
                        self.store, self.gen, datetime.now(timezone.utc).timestamp()
                    )

    def test_production_history_is_excluded_and_reclaim_reads_original_served_copy(self) -> None:
        now = datetime.now(timezone.utc)
        history = self.gen.model_copy(
            update={"completed_at": (now - timedelta(hours=1)).isoformat()}
        )
        current = history.model_copy(update={"completed_at": "", "drill_database": "new_database"})
        self.store.list_runtime_environment_records.return_value[0].env["ODOO_DB_NAME"] = (
            current.drill_database
        )
        self.inspect.side_effect = lambda **kw: kw["receive"](
            {"type": "complete", "recent_signins": False}
        )
        self.assertFalse(
            read_drill_recent_signins(
                self.store,
                current,
                now.timestamp(),
                history_generation=history,
                history_database="original_database",
            )
        )
        spec = self.inspect.call_args.kwargs["spec"]
        self.assertEqual(spec["database_name"], "original_database")
        self.assertEqual(
            datetime.fromisoformat(spec["usage_since"]),
            datetime.fromisoformat(history.completed_at),
        )

    def test_target_or_runtime_change_refuses_the_history(self) -> None:
        self.store.read_dokploy_target_id_record.return_value.target_id = "foreign-target"
        with self.assertRaises(ValueError):
            read_drill_recent_signins(self.store, self.gen, datetime.now(timezone.utc).timestamp())
        self.inspect.assert_not_called()
        self.store.read_dokploy_target_id_record.return_value.target_id = self.gen.target_id
        self.inspect.side_effect = lambda **kw: kw["receive"](
            {"type": "complete", "recent_signins": False}
        )
        self.current.side_effect = ValueError("runtime changed")
        with self.assertRaises(ValueError):
            read_drill_recent_signins(self.store, self.gen, datetime.now(timezone.utc).timestamp())
