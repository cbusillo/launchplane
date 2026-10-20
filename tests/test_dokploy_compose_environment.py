"""Final-consumer proof; explicit commands come from the repository CI setup.

Ordinary unit discovery does not silently depend on host Node or Compose.
The qualification entrypoint runs this class with both provisioned commands.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from control_plane.dokploy import api


ROOT = Path(__file__).resolve().parents[1]
VALUES = {
    "PLAIN": "plain",
    "QUOTES": '"literal quotes"',
    "TOML": '[bridge]\nname = "example"\n[bridge.settings]\nenabled = true\n',
    "DOLLARS": "$UNSET ${UNSET:-expanded} $$ $(command)",
    "HASH": "before#after",
    "SPACE": "  padded  ",
    "BACKSLASH": "literal\\n and trailing\\",
    "APOSTROPHE": "it's literal",
    "UNICODE": "café ☃",
    "EMPTY": "",
    "CRLF": "first\r\nsecond\rthird",
    "TAB": "first\tsecond",
    "SHELL": "`command` && $(command) | > file; \\",
    "CONTROL": "back\bform\ffeed",
    "SEPARATORS": "before\u2028after\u2029end",
    "SLASH_DOLLAR": "\\$NAME \\${NAME} \\$(command)",
    "JSON": '{"message":"a # b"}',
    "QUOTED_SLASH": 'quote " and slash \\',
}


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_COMPOSE_COMMAND")
    and os.environ.get("LAUNCHPLANE_TEST_NODE_COMMAND"),
    "Run scripts/qualify-dokploy-compose-environment.sh with repository-provisioned tools",
)
class DokployComposeConsumerTests(unittest.TestCase):
    def test_provider_write_survives_generated_file_and_both_compose_consumers(self) -> None:
        previous_values = {"KEEP": "provider-only\n$NAME", "REMOVE": "old"}
        for previous in (
            api.serialize_dokploy_env_text(previous_values),
            api.serialize_dokploy_env_text(previous_values, target_type="compose"),
        ):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as directory:
                logical = api.render_dokploy_env_text_with_overrides(
                    previous, updates=VALUES, removals=("REMOVE",), target_type="compose"
                )
                with patch.object(api, "dokploy_request") as request:
                    api.update_dokploy_target_env(
                        host="https://provider.invalid",
                        token="synthetic",
                        target_type="compose",
                        target_id="synthetic",
                        target_payload={"env": previous},
                        env_text=logical,
                    )
                wire = request.call_args.kwargs["payload"]["env"]
                # Readback/retirement/credential callers need the logical values.
                self.assertEqual(
                    api.parse_dokploy_env_text(wire), {"KEEP": previous_values["KEEP"], **VALUES}
                )
                node_command = json.loads(os.environ["LAUNCHPLANE_TEST_NODE_COMMAND"])
                result = subprocess.run(
                    [*node_command, str(ROOT / "tests/fixtures/dokploy-compose/consume.mjs")],
                    input=json.dumps({"root": directory, "env": wire}),
                    text=True,
                    capture_output=True,
                    check=True,
                )
                consumer = json.loads(result.stdout)
                code = Path(directory) / "synthetic/code"
                expected = {"KEEP": previous_values["KEEP"], **VALUES}
                compose = {
                    "services": {
                        "interpolation": {
                            "image": "synthetic.invalid/example:latest",
                            "environment": {key: "${" + key + "}" for key in expected},
                        },
                        "file": {
                            "image": "synthetic.invalid/example:latest",
                            "env_file": [".env"],
                        },
                    },
                }
                (code / "compose.json").write_text(json.dumps(compose))
                compose_command = json.loads(os.environ["LAUNCHPLANE_TEST_COMPOSE_COMMAND"])
                configured = subprocess.run(
                    [
                        *compose_command,
                        "--project-directory",
                        str(code),
                        "-f",
                        str(code / "compose.json"),
                        "config",
                        "--format",
                        "json",
                    ],
                    env={"PATH": os.environ["PATH"]},
                    text=True,
                    capture_output=True,
                    check=True,
                )
                services = json.loads(configured.stdout)["services"]
                for name in ("interpolation", "file"):
                    actual = services[name]["environment"]
                    # `config` emits a reusable Compose document, escaping literal
                    # dollars once for that output document's interpolation pass.
                    self.assertEqual(
                        {key: actual.get(key) for key in expected},
                        {key: value.replace("$", "$$") for key, value in expected.items()},
                    )
                    self.assertNotIn("REMOVE", actual)
                self.assertTrue(consumer["generated"])
                interpolation = subprocess.run(
                    [
                        *compose_command,
                        "--project-directory",
                        str(code),
                        "-f",
                        str(code / "compose.json"),
                        "config",
                        "--environment",
                    ],
                    env={"PATH": os.environ["PATH"]},
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertIn("DOLLARS=" + VALUES["DOLLARS"], interpolation.stdout.splitlines())
