import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from control_plane.contracts.cutover_host import OdooCronWriterBinding
from control_plane.dokploy.cutover_writer_capabilities import NativeCronWriterCapabilities
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan


class NativeCronWriterCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = WebsiteFixture(Path(temporary.name))
        self.addCleanup(fixture.engine.dispose)
        self.plan = release_plan(fixture)
        self.bindings = tuple(
            OdooCronWriterBinding(
                writer_id=writer.writer_id, cron_xmlids=("fixture.tick_" + writer.kind,)
            )
            for writer in self.plan.writers
            if writer.kind != "web"
        )
        self.execute = Mock()

    def test_missing_logical_role_refuses_before_any_provider_read(self) -> None:
        with self.assertRaises(ValueError):
            NativeCronWriterCapabilities(
                self.plan, self.bindings[:-1], self.execute, lambda slot: "runner"
            )
        self.execute.assert_not_called()

    def test_every_role_requires_its_own_bound_native_readback(self) -> None:
        capability = NativeCronWriterCapabilities(
            self.plan, self.bindings, self.execute, lambda slot: "runner"
        )
        for writer in self.plan.writers:
            if writer.kind == "web":
                continue
            binding = next(item for item in self.bindings if item.writer_id == writer.writer_id)
            self.execute.return_value = json.dumps(
                {
                    "writer_id": writer.writer_id,
                    "backend": "odoo_cron",
                    "jobs": binding.cron_xmlids,
                    "registry_sha256": "a" * 64,
                }
            )
            self.assertTrue(capability(self.plan.new, writer))
            requested = json.loads(self.execute.call_args.args[1])
            self.assertEqual(requested["jobs"], list(binding.cron_xmlids))
            self.execute.return_value = json.dumps(
                {
                    "writer_id": "another-role",
                    "backend": "odoo_cron",
                    "jobs": binding.cron_xmlids,
                    "registry_sha256": "a" * 64,
                }
            )
            with self.assertRaises(ValueError):
                capability(self.plan.new, writer)
