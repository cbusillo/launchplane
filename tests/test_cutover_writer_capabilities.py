import json
from pathlib import Path
from typing import Any
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from control_plane.contracts.cutover_host import OdooCronWriterBinding
from control_plane.dokploy.slot_compose import slot_startup_command
from control_plane.dokploy.cutover_writer_capabilities import (
    NativeCronWriterCapabilities,
    SUPPORTED_SERVER_WIDE_MODULES,
)
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

    def test_effective_executor_preflight_rejects_http_queue_even_when_cron_roles_exist(
        self,
    ) -> None:
        import configparser
        import os
        from contextlib import redirect_stdout
        import io
        import sys
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch
        from control_plane.dokploy.cutover_writer_capabilities import EXECUTOR_PREFLIGHT_PROGRAM

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifests = root / "addons"
            manifests.mkdir()
            for module in ("base", "rpc", "web", "queue_job"):
                (manifests / module).mkdir()
                (manifests / module / "__manifest__.py").write_text("{}")
            query = MagicMock()
            query.fetchone.return_value = (self.plan.old.runtime_identity.database_uuid,)
            installed = ["base", "web"]
            query.fetchall.side_effect = lambda: [(module,) for module in installed]
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.cursor.return_value.__enter__.return_value = query
            settings = SimpleNamespace(
                config_path=str(root / "generated.conf"),
                database_host="db",
                database_port=5432,
                database_user="inert",
                database_password="inert",
            )
            queue_section = False

            def configure(value: object) -> None:
                parser = configparser.ConfigParser()
                parser["options"] = {
                    "max_cron_threads": os.environ.get("ODOO_MAX_CRON_THREADS", "2"),
                    "addons_path": str(manifests),
                }
                if queue_section:
                    parser["queue_job"] = {"channels": "root:1"}
                with Path(settings.config_path).open("w") as stream:
                    parser.write(stream)

            def load_settings(args: object) -> SimpleNamespace:
                return SimpleNamespace(
                    **{
                        **vars(settings),
                        "database_name": os.environ.get(
                            "ODOO_DB_NAME", self.plan.old.binding.database
                        ),
                        "database_host": os.environ.get("ODOO_DB_HOST", settings.database_host),
                    },
                )

            native = SimpleNamespace(_load_settings=load_settings)
            startup = {"_load_settings": native._load_settings, "_write_runtime_config": configure}
            request = json.dumps(
                {
                    "database": self.plan.old.binding.database,
                    "database_uuid": self.plan.old.runtime_identity.database_uuid,
                    "planned_modules": [],
                    "environment": {"ODOO_MAX_CRON_THREADS": "0"},
                    "image": self.plan.old.image,
                    "supported_server_wide": sorted(SUPPORTED_SERVER_WIDE_MODULES),
                }
            )
            effective = MagicMock()
            effective.__getitem__.return_value = ["base", "rpc", "web"]
            modules = {
                "psycopg2": SimpleNamespace(connect=lambda **kwargs: connection),
                "odoo": SimpleNamespace(addons=SimpleNamespace(__path__=[str(manifests)])),
                "odoo.tools": SimpleNamespace(config=effective),
                "odoo.modules.module": SimpleNamespace(initialize_sys_path=lambda: None),
            }
            original_path = list(sys.path)
            self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))

            def probe(payload: str = request) -> str:
                output = io.StringIO()
                with (
                    patch.dict("os.environ", {}, clear=True),
                    patch.dict("sys.modules", modules),
                    patch("runpy.run_path", return_value=startup),
                    patch("sys.stdin", io.StringIO(payload)),
                    redirect_stdout(output),
                ):
                    exec(EXECUTOR_PREFLIGHT_PROGRAM, {})
                return output.getvalue()

            web: dict[str, Any] = {
                "Config": {
                    "Image": self.plan.old.image,
                    "Entrypoint": ["/bin/sh", "-c"],
                    "Cmd": ["exec " + slot_startup_command()],
                    "Env": ["ODOO_MAX_CRON_THREADS=0"],
                },
                "Mounts": [],
            }
            capability = NativeCronWriterCapabilities(
                self.plan,
                self.bindings,
                lambda argv, stdin: probe(stdin or ""),
                lambda slot: "runner",
                lambda slot: web,
            )
            self.assertTrue(capability.preflight(self.plan.old))
            web["Config"]["Env"] = ["ODOO_MAX_CRON_THREADS=1"]
            with self.assertRaisesRegex(ValueError, "web warming"):
                capability.preflight(self.plan.old)
            web["Config"]["Env"] = ["ODOO_MAX_CRON_THREADS=0"]
            web["Config"]["Env"] = ["ODOO_MAX_CRON_THREADS=0", "ODOO_DB_NAME=foreign"]
            with self.assertRaisesRegex(ValueError, "effective web database"):
                capability.preflight(self.plan.old)
            web["Config"]["Env"] = ["ODOO_MAX_CRON_THREADS=0", "ODOO_DB_HOST=foreign"]
            with self.assertRaisesRegex(ValueError, "effective web database"):
                capability.preflight(self.plan.old)
            web["Config"]["Env"] = ["ODOO_MAX_CRON_THREADS=0"]
            probe()
            installed.append("queue_job")
            with self.assertRaisesRegex(ValueError, "HTTP-dependent"):
                probe()
            installed.remove("queue_job")
            queue_section = True
            with self.assertRaisesRegex(ValueError, "HTTP-dependent"):
                probe()
