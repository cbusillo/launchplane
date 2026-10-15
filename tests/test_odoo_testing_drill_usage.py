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
        self.gen = generation()
        self.selection = SimpleNamespace(
            identity=SimpleNamespace(
                context=self.gen.context, image_reference="registry.invalid/odoo@sha256:" + "a" * 64
            )
        )
        self.connection = SimpleNamespace(
            evidence=SimpleNamespace(target_id=self.gen.target_id, container_id="web"),
            host="https://provider.invalid",
            token="fixture-private-token",
            server_id="remote-server",
            app_name=self.gen.namespace,
            credentials={"ODOO_DB_NAME": "testing_database"},
        )
        module = "control_plane.odoo_testing_drill_usage."
        self.enterContext(patch(module + "select_stable_runtime", return_value=self.selection))
        self.enterContext(patch(module + "connect_runtime", return_value=self.connection))
        self.enterContext(patch(module + "inspect_runtime_container", return_value={}))
        self.current = self.enterContext(patch(module + "assert_selection_current"))
        self.inspect = self.enterContext(patch(module + "inspect_native_testing_receiver"))

    def test_complete_boolean_is_bound_to_the_selected_testing_runtime(self) -> None:
        now = datetime.now(timezone.utc)
        for used in (True, False):
            with self.subTest(used=used):
                self.inspect.side_effect = lambda **kw: kw["receive"](
                    {"type": "complete", "recent_signins": used}
                )
                self.assertEqual(read_drill_recent_signins(Mock(), self.gen, now.timestamp()), used)
                spec = self.inspect.call_args.kwargs["spec"]
                self.assertEqual(spec["generation"], self.gen.model_dump(mode="json"))
                self.assertEqual(spec["database_name"], self.connection.credentials["ODOO_DB_NAME"])
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
                        Mock(), self.gen, datetime.now(timezone.utc).timestamp()
                    )

    def test_target_or_runtime_change_refuses_the_history(self) -> None:
        self.connection.evidence.target_id = "foreign-target"
        with self.assertRaises(ValueError):
            read_drill_recent_signins(Mock(), self.gen, datetime.now(timezone.utc).timestamp())
        self.inspect.assert_not_called()
        self.connection.evidence.target_id = self.gen.target_id
        self.inspect.side_effect = lambda **kw: kw["receive"](
            {"type": "complete", "recent_signins": False}
        )
        self.current.side_effect = ValueError("runtime changed")
        with self.assertRaises(ValueError):
            read_drill_recent_signins(Mock(), self.gen, datetime.now(timezone.utc).timestamp())
