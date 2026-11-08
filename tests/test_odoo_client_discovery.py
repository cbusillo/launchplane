import io
import json
import os
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest import TestCase
from unittest.mock import Mock, patch

from control_plane.dokploy.odoo_client_signin import (
    CLIENT_SIGNIN_LIST_PROGRAM,
    CLIENT_USER_MANIFEST_KEY,
)
from tests.test_odoo_client_signup import NativeClientListConnection

FIXTURES = Path(__file__).parent / "fixtures" / "odoo-client-signin"


class NativeAddonDiscoveryTests(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch("logging.disable"))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        expanduser = os.path.expanduser

        def fixture_home(path: str) -> str:
            if path == "~":
                return str(self.root)
            if path.startswith("~/"):
                return str(self.root / path[2:])
            return expanduser(path)

        self.enterContext(patch("os.path.expanduser", side_effect=fixture_home))
        os.environ.update(
            ODOO_DB_NAME="fixture",
            ODOO_DB_HOST="database",
            ODOO_DB_USER="fixture",
            ODOO_DB_PASSWORD="fixture",
            ODOO_MASTER_PASSWORD="fixture-master",
            ODOO_ADMIN_PASSWORD="fixture-password",
            PLATFORM_INSTANCE="testing",
            ODOO_ADDONS_PATH=str(self.root / "explicit"),
            ODOO_CONFIG=str(self.root / "base.conf"),
        )
        (self.root / "explicit").mkdir()
        (self.root / "base.conf").write_text("[options]\naddons_path=/unused-base-only\n")
        self.enterContext(patch.dict(sys.modules))
        self.enterContext(patch.object(sys, "path", list(sys.path)))
        self.enterContext(patch.object(sys, "meta_path", list(sys.meta_path)))
        odoo = self.module("odoo")
        tools = self.module("odoo.tools")
        release = self.load("odoo.release", "release.py.txt")
        self.series = release.series
        self.module(
            "odoo.tools.appdirs",
            **{
                name: lambda *_args: str(self.root / "defaults")
                for name in ("user_data_dir", "site_data_dir", "user_config_dir", "site_config_dir")
            },
        )
        self.load("odoo.tools.func", "func.py.txt")
        config_module = self.load("odoo.tools.config", "config.py.txt")
        self.config = config_module.config
        self.config.root_path = str(self.root / "odoo")
        tools.config = self.config
        tools.file_open = lambda path, **_kwargs: open(path)
        namespace_path = type("NamespacePath", (list,), {})
        self.module("odoo.addons", __path__=namespace_path([self.config.addons_base_dir]))
        self.module("odoo.upgrade", __path__=namespace_path())
        modules = self.module("odoo.modules")
        self.module("odoo.tools.translate", get_po_paths=None, get_translations_for_references=None)
        native = self.load("odoo.modules.module", "module.py.txt")
        self.native = native
        modules.module = native
        self.module("odoo.netsvc")
        odoo.modules = modules
        self.load("odoo_addon_paths", "odoo_addon_paths.py.txt")
        admin = self.load("odoo_admin_password", "odoo_admin_password.py.txt")
        startup = self.module(
            "run_odoo_startup", validate_admin_password=admin.validate_admin_password
        )
        exec((FIXTURES / "startup.py.txt").read_text(), vars(startup))
        self.startup = startup
        load_settings = startup._load_settings
        self.enterContext(
            patch.object(
                startup,
                "_load_settings",
                side_effect=lambda args: replace(
                    load_settings(args), data_dir=str(self.root / "data")
                ),
            )
        )

    def module(self, name: str, **attributes: Any) -> Any:
        module = ModuleType(name)
        vars(module).update(attributes)
        module.__file__ = str(FIXTURES / (name.rsplit(".", 1)[-1] + ".py.txt"))
        module.__package__ = name.rpartition(".")[0]
        sys.modules[name] = module
        if "." in name:
            parent, _, child = name.rpartition(".")
            setattr(sys.modules[parent], child, module)
        return module

    def load(self, name: str, fixture: str) -> Any:
        module = self.module(name)
        exec(compile((FIXTURES / fixture).read_text(), module.__file__, "exec"), vars(module))
        return module

    def addon(self, root: Path, name: str, declaration: object) -> None:
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        addon = root / name
        addon.mkdir(parents=True)
        (addon / "__init__.py").touch()
        (addon / "__manifest__.py").write_text(
            repr(
                {
                    "name": name,
                    "author": "Fixture",
                    "license": "LGPL-3",
                    CLIENT_USER_MANIFEST_KEY: declaration,
                }
            )
        )

    def read(self) -> dict[str, Any]:
        connection = NativeClientListConnection()
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=lambda **_: connection)}),
            patch(
                "sys.stdin",
                io.StringIO(json.dumps({"database": "fixture", "forbidden_groups": []})),
            ),
            redirect_stdout(output),
        ):
            exec(CLIENT_SIGNIN_LIST_PROGRAM, {})
        return cast(dict[str, Any], json.loads(output.getvalue()))

    def test_effective_data_directory_and_explicit_paths_find_native_custom_metadata(self) -> None:
        self.addon(self.root / "data" / "addons" / self.series, "fixture", ["fixture.user_3"])
        self.addon(self.root / "explicit", "uninstalled", ["uninstalled.user_client"])
        result = self.read()
        self.assertEqual([user["xmlid"] for user in result["users"]], ["fixture.user_3"])
        self.assertEqual(self.config["data_dir"], str(self.root / "data"))

    def test_startup_addon_prefix_survives_native_environment_precedence(self) -> None:
        self.startup.LAUNCHPLANE_ADDONS_PATH = str(self.root / "managed")
        self.addon(self.root / "managed", "fixture", ["fixture.user_3"])
        self.addon(self.root / "explicit", "other", [])
        self.assertEqual([user["xmlid"] for user in self.read()["users"]], ["fixture.user_3"])

    def test_executor_preflight_uses_the_same_effective_startup_environment(self) -> None:
        from control_plane.dokploy.cutover_writer_capabilities import (
            EXECUTOR_PREFLIGHT_PROGRAM,
            SUPPORTED_SERVER_WIDE_MODULES,
        )

        startup = self.startup
        startup.LAUNCHPLANE_ADDONS_PATH = str(self.root / "managed")
        for name in {"fixture", *SUPPORTED_SERVER_WIDE_MODULES}:
            self.addon(self.root / "managed", name, [])
        self.addon(self.root / "explicit", "other", [])
        connection = NativeClientListConnection()
        self.addCleanup(connection.close)
        connection.database.executescript(
            "CREATE TABLE ir_config_parameter(key TEXT,value TEXT); INSERT INTO ir_config_parameter VALUES ('database.uuid','fixture-uuid');"
        )
        os.environ["ODOO_MAX_CRON_THREADS"] = "0"
        request = {
            "environment": dict(os.environ),
            "database": "fixture",
            "database_uuid": "fixture-uuid",
            "planned_modules": [],
            "supported_server_wide": sorted(SUPPORTED_SERVER_WIDE_MODULES),
            "image": "fixture",
        }
        setattr(sys.modules["odoo.netsvc"], "init_logger", Mock())
        output = io.StringIO()
        with (
            patch("runpy.run_path", return_value=vars(startup)),
            patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=lambda **_: connection)}),
            patch("sys.stdin", io.StringIO(json.dumps(request))),
            redirect_stdout(output),
        ):
            exec(EXECUTOR_PREFLIGHT_PROGRAM, {})
        self.assertIn("fixture", json.loads(output.getvalue())["modules"])

    def test_native_explicit_path_retains_optional_metadata_and_rejects_malformed_values(
        self,
    ) -> None:
        self.addon(self.root / "explicit", "fixture", [])
        self.assertEqual(self.read()["users"], [])
        manifest = self.root / "explicit" / "fixture" / "__manifest__.py"
        manifest.write_text(repr({CLIENT_USER_MANIFEST_KEY: "fixture.user_3"}))
        self.native.Manifest._get_manifest_from_addons.cache_clear()
        with self.assertRaises(RuntimeError):
            self.read()

    def test_missing_or_shadowed_installed_addon_refuses_instead_of_selecting_a_declaration(
        self,
    ) -> None:
        with self.assertRaises(RuntimeError):
            self.read()
        self.addon(self.root / "explicit", "fixture", ["fixture.user_3"])
        self.addon(self.root / "data" / "addons" / self.series, "fixture", ["fixture.user_4"])
        self.native.Manifest._get_manifest_from_addons.cache_clear()
        with self.assertRaises(RuntimeError):
            self.read()
