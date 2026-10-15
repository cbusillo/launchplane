"""Execute the generated consumer through the legacy runtime's native shell API."""

import base64
import hashlib
import json
import io
import configparser
from pathlib import Path
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane._odoo_website_settings_program import (
    WEBSITE_SETTINGS_PROGRAM,
    WEBSITE_SETTINGS_SOURCE_REVISION,
    WEBSITE_INDEXING_PARAMETER,
)
from scripts.generate_odoo_website_settings_program import render
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from tests.test_odoo_website_settings import intent


class NativeWebsite:
    _fields = {"google_analytics_key", "cookies_bar", "block_third_party_domains"}
    domain = intent().settings.public_origin
    google_analytics_key = "G-COPIED"
    cookies_bar = False
    block_third_party_domains = False

    def __len__(self) -> int:
        return 1

    def exists(self) -> "NativeWebsite":
        return self

    def write(self, values: dict[str, object]) -> None:
        for field, value in values.items():
            setattr(self, field, value)

    def flush_recordset(self, fields: list[str]) -> None:
        pass

    def invalidate_recordset(self, fields: list[str]) -> None:
        pass


class NativeEnvironment:
    registry = {"website"}

    def __init__(self) -> None:
        self.site = NativeWebsite()
        self.cr = SimpleNamespace(commit=Mock())
        self.parameter = "0"
        self.parameters = SimpleNamespace(
            set_param=self.set_parameter, get_param=lambda key: self.parameter
        )
        self.parameters.sudo = lambda: self.parameters
        self.websites = SimpleNamespace(
            browse=lambda record_id: self.site, search=lambda domain: [self.site]
        )
        self.websites.sudo = lambda: self.websites

    def set_parameter(self, key: str, value: str) -> None:
        self.parameter = value

    def __getitem__(self, model: str) -> object:
        return self.websites if model == "website" else self.parameters


def run_legacy_shell(
    env: NativeEnvironment, *, instance: str = "prod", output: io.StringIO | None = None
) -> str:
    startup = types.ModuleType("run_odoo_startup")
    startup._load_settings = lambda arguments: arguments  # type: ignore[attr-defined]
    startup._write_runtime_config = lambda settings: None  # type: ignore[attr-defined]

    def shell(settings: object, program: str, *, label: str) -> None:
        exec(program, {"env": env})

    startup._run_odoo_shell = shell  # type: ignore[attr-defined]
    payload = OdooPostDeployPayload(
        context="example-site", instance="prod", website_settings=intent().settings
    )
    encoded = base64.b64encode(payload.to_wire_json_bytes()).decode()
    output = output if output is not None else io.StringIO()
    with (
        patch.dict(sys.modules, {"run_odoo_startup": startup, "odoo_website_settings": None}),
        patch.object(sys, "argv", ["program", "fixture-db", instance]),
        patch.object(sys, "path", list(sys.path)),
        patch.dict(os.environ, {"ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": encoded}),
        redirect_stdout(output),
    ):
        exec(WEBSITE_SETTINGS_PROGRAM, {})
    return output.getvalue()


class GeneratedWebsiteConsumerTests(unittest.TestCase):
    def test_immutable_native_artifacts_and_generated_consumer_agree(self) -> None:
        root = Path(__file__).parent / "fixtures/website-settings"
        records = json.loads((root / "artifacts.json").read_text())
        for record in records.values():
            data = (root / record["artifact_path"]).read_bytes()
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            self.assertEqual(blob, record["git_blob"])
        consumer = records["consumer"]
        namespace: dict[str, object] = {}
        exec(
            render(
                commit=consumer["revision"], source=(root / consumer["artifact_path"]).read_text()
            ),
            namespace,
        )
        self.assertEqual(namespace["WEBSITE_SETTINGS_PROGRAM"], WEBSITE_SETTINGS_PROGRAM)
        self.assertEqual(
            namespace["WEBSITE_SETTINGS_SOURCE_REVISION"], WEBSITE_SETTINGS_SOURCE_REVISION
        )
        self.assertEqual(namespace["WEBSITE_INDEXING_PARAMETER"], WEBSITE_INDEXING_PARAMETER)

    def test_old_runtime_without_consumer_applies_and_commits_the_typed_payload(self) -> None:
        env = NativeEnvironment()
        output = run_legacy_shell(env)
        self.assertEqual(env.site.google_analytics_key, intent().settings.google_analytics_key)
        self.assertEqual(env.parameter, "1")
        env.cr.commit.assert_called_once()
        self.assertEqual(
            dict(line.split("=", 1) for line in output.splitlines())["website_settings_sha256"],
            intent().settings.digest,
        )

    def test_nonproduction_old_runtime_clears_the_copied_native_values(self) -> None:
        env = NativeEnvironment()
        env.parameter = "1"
        run_legacy_shell(env, instance="testing")
        self.assertFalse(env.site.google_analytics_key)
        self.assertEqual(env.parameter, "0")
        env.cr.commit.assert_called_once()

    def test_commit_failure_cannot_emit_a_success_digest(self) -> None:
        env = NativeEnvironment()
        env.cr.commit.side_effect = RuntimeError("database commit failure")
        output = io.StringIO()
        with self.assertRaises(RuntimeError):
            run_legacy_shell(env, output=output)
        self.assertNotIn(intent().settings.digest, output.getvalue())

    def test_native_shell_uses_the_legacy_runtime_config_and_addons_contract(self) -> None:
        source = Path(__file__).parent / "fixtures/website-settings/legacy_startup.txt"
        startup = types.ModuleType("legacy_startup_fixture")
        admin = types.ModuleType("odoo_admin_password")
        admin.validate_admin_password = lambda value: None  # type: ignore[attr-defined]
        fake_psycopg = types.ModuleType("psycopg2")
        env = NativeEnvironment()
        payload = OdooPostDeployPayload(
            context="example-site", instance="prod", website_settings=intent().settings
        )
        encoded = base64.b64encode(payload.to_wire_json_bytes()).decode()

        def shell(command: list[str], **kwargs: object) -> None:
            config_file = command[command.index("-c") + 1]
            configuration = configparser.ConfigParser(interpolation=None)
            configuration.read(config_file)
            self.assertIn("/opt/fixture/addons", configuration["options"]["addons_path"].split(","))
            self.assertEqual(configuration["options"]["db_name"], "fixture-db")
            self.assertEqual(configuration["options"]["admin_passwd"], "fixture-master")
            program = kwargs["input"]
            assert isinstance(program, bytes)
            exec(program.decode(), {"env": env})

        with (
            patch.dict(
                sys.modules,
                {
                    "psycopg2": fake_psycopg,
                    "legacy_startup_fixture": startup,
                    "odoo_admin_password": admin,
                },
            ),
            patch.object(sys, "path", list(sys.path)),
            patch.object(sys, "argv", ["program", "fixture-db", "prod"]),
            patch.dict(
                os.environ,
                {
                    "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": encoded,
                    "ODOO_MASTER_PASSWORD": "fixture-master",
                    "ODOO_ADDONS_PATH": "/opt/fixture/addons",
                    "PLATFORM_INSTANCE": "prod",
                    "ODOO_ADMIN_PASSWORD": "fixture-admin",
                },
            ),
            patch("subprocess.run", side_effect=shell),
            redirect_stdout(io.StringIO()),
        ):
            exec(compile(source.read_text(), str(source), "exec"), startup.__dict__)
            with patch.dict(sys.modules, {"run_odoo_startup": startup}):
                exec(WEBSITE_SETTINGS_PROGRAM, {})
        self.assertEqual(env.site.google_analytics_key, intent().settings.google_analytics_key)
