"""Execute the generated consumer through the legacy runtime's native shell API."""

import base64
import io
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane._odoo_website_settings_program import WEBSITE_SETTINGS_PROGRAM
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from tests.test_odoo_website_settings import intent


class NativeWebsite:
    _fields = {"google_analytics_key", "cookies_bar", "block_third_party_domains"}
    domain = "https://site.example"
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
