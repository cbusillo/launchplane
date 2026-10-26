from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.cutover_writer_observer import (
    NativeWriterObserver,
    process_supports_writer,
)
from control_plane.dokploy.slot_writers import SlotWriterContainer
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan


class NativeWriterObserverTests(unittest.TestCase):
    def test_postgres_network_suffix_does_not_turn_known_writers_into_unknowns(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = WebsiteFixture(Path(temporary))
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            for native, address in (
                ("10.0.0.2/32", "10.0.0.2"),
                ("2001:db8::2/128", "2001:db8::2"),
                ("::ffff:a00:2/128", "10.0.0.2"),
            ):
                with self.subTest(native=native):
                    observer = NativeWriterObserver(
                        plan,
                        execute=Mock(
                            return_value=json.dumps(
                                {
                                    "database_uuid": plan.old.runtime_identity.database_uuid,
                                    "prepared_transactions": 0,
                                    "observed_at": datetime.now(UTC).isoformat(),
                                    "sessions": [{"address": native}],
                                }
                            )
                        ),
                        runner=Mock(),
                        target=Mock(),
                        addresses=lambda slot, writer: {address} if slot == plan.old else set(),
                    )
                    self.assertEqual(observer.active_jobs(plan.new, plan.writers[0]), 0)
                    self.assertEqual(observer.active_jobs(plan.old, plan.writers[0]), 1)

    def test_missing_container_never_substitutes_for_surviving_database_sessions(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = WebsiteFixture(Path(temporary))
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            lookup = Mock(side_effect=ValueError("no such container"))
            observer = NativeWriterObserver(
                plan,
                execute=Mock(),
                runner=Mock(),
                target=lookup,
                addresses=lambda slot, writer: {"10.0.0.2"} if slot == plan.new else set(),
            )
            database = Mock(
                return_value={
                    "database_uuid": plan.old.runtime_identity.database_uuid,
                    "prepared_transactions": 0,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "sessions": [{"address": "10.0.0.1"}, {"address": "10.0.0.2"}],
                }
            )
            with patch.object(observer, "database", database):
                self.assertEqual(observer.active_jobs(plan.old, plan.writers[0]), 1)
            lookup.assert_not_called()
            self.assertTrue(observer.evidence[plan.old.slot_id, plan.writers[0].writer_id])

    def test_web_without_crons_cannot_cover_a_cron_family_role(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = WebsiteFixture(Path(temporary))
            self.addCleanup(fixture.engine.dispose)
            web = {
                "processes": [{"http": True, "cron_threads": 0}],
                "listening_ports": [8069],
                "http_ports": [8069, 8072],
            }
            scheduler = {
                "processes": [{"http": False, "cron_threads": 1}],
                "listening_ports": [42000],
                "http_ports": [8069, 8072],
            }
            for writer in fixture.plan.writers:
                self.assertEqual(process_supports_writer(web, writer), writer.kind == "web")
                self.assertEqual(process_supports_writer(scheduler, writer), writer.kind != "web")
            scheduler["listening_ports"] = [8069]
            cron = next(writer for writer in fixture.plan.writers if writer.kind == "cron")
            self.assertFalse(process_supports_writer(scheduler, cron))

    def test_docker_running_without_native_database_liveness_cannot_resume(self) -> None:
        with TemporaryDirectory() as temporary:
            fixture = WebsiteFixture(Path(temporary))
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            writer = next(writer for writer in plan.writers if writer.kind == "cron")
            target = SlotWriterContainer(writer, plan.new.slot_id, "scheduler", plan.new.image)

            def execute(argv: tuple[str, ...], stdin: str | None) -> str:
                if argv[1] == "inspect":
                    return json.dumps(
                        [
                            {
                                "Config": {"Image": plan.new.image},
                                "State": {
                                    "Running": True,
                                    "Restarting": False,
                                    "Paused": False,
                                    "StartedAt": datetime.now(UTC).isoformat(),
                                },
                                "RestartCount": 0,
                            }
                        ]
                    )
                return json.dumps(
                    {
                        "processes": [{"http": False, "cron_threads": 1}],
                        "listening_ports": [],
                        "http_ports": [8069, 8072],
                    }
                )

            observer = NativeWriterObserver(
                plan,
                execute=execute,
                runner=Mock(),
                target=lambda slot, writer: target,
                addresses=lambda slot, writer: {"10.0.0.2"},
                timeout_seconds=0,
                capability=lambda slot, writer: "registry-bound",
            )
            database = Mock(return_value={"sessions": []})
            with (
                patch.object(observer, "database", database),
                self.assertRaises((ValueError, TimeoutError)),
            ):
                observer.native_ready(target)
