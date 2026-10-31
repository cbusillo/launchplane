"""Final-consumer proof with commands provisioned by the repository CI setup."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from control_plane.dokploy import api
from control_plane.workflows.dokploy_deploy import update_dokploy_target_artifact

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
    "SEPARATORS": "before\u2028after\u2029end",
    "SLASH_DOLLAR": "\\$NAME \\${NAME} \\$(command)",
    "JSON": '{"message":"a # b"}',
    "ESCAPE_NAME": "${LAUNCHPLANE_DOTENV_DOLLAR}",
    "LAUNCHPLANE_DOTENV_DOLLAR": "application-value",
    "QUOTED_SLASH": 'quote " and slash \\',
    "MIXED_QUOTES": "a' b\" c` #\nnext",
    "PROVIDER_LITERAL": "${{project.INPUT}} ${{environment.INPUT}} ${{SELF}}",
    "OCTAL_TEXT": r"\0047 \0044 \\0047",
    **{f"ASCII_{code}": "before" + chr(code) + "after" for code in range(1, 128)},
}


def consumer_commands_available() -> bool:
    return bool(
        os.environ.get("LAUNCHPLANE_TEST_COMPOSE_COMMAND")
        and os.environ.get("LAUNCHPLANE_TEST_NODE_COMMAND")
    )


def consume_compose_environment(
    wire: str, keys: tuple[str, ...], *, project_env: str = ""
) -> tuple[dict[str, dict[str, str]], str]:
    """Run the original provider file writer and Compose without a daemon."""
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(
            [
                *json.loads(os.environ["LAUNCHPLANE_TEST_NODE_COMMAND"]),
                str(ROOT / "tests/fixtures/dokploy-compose/consume.mjs"),
            ],
            input=json.dumps({"root": directory, "env": wire, "projectEnv": project_env}),
            text=True,
            capture_output=True,
            check=True,
        )
        code = Path(directory) / "synthetic/code"
        compose = {
            "services": {
                "interpolation": {
                    "image": "synthetic.invalid/example:latest",
                    "environment": {key: "${" + key + "}" for key in keys},
                },
                "file": {"image": "synthetic.invalid/example:latest", "env_file": [".env"]},
            }
        }
        (code / "compose.json").write_text(json.dumps(compose))
        command = [
            *json.loads(os.environ["LAUNCHPLANE_TEST_COMPOSE_COMMAND"]),
            "--project-directory",
            str(code),
            "-f",
            str(code / "compose.json"),
            "config",
        ]

        def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [*command, *arguments],
                env={
                    "PATH": os.environ["PATH"],
                    **(
                        {"DOCKER_CONFIG": os.environ["DOCKER_CONFIG"]}
                        if "DOCKER_CONFIG" in os.environ
                        else {}
                    ),
                },
                text=True,
                capture_output=True,
                check=True,
            )

        configured = run(["--format", "json"])
        interpolation = run(["--environment"])
        return {
            name: {key: service["environment"][key] for key in keys}
            for name, service in json.loads(configured.stdout)["services"].items()
        }, interpolation.stdout


@unittest.skipUnless(
    consumer_commands_available(),
    "Run scripts/qualify-dokploy-compose-environment.sh with provisioned tools",
)
class DokployComposeConsumerTests(unittest.TestCase):
    def test_provider_write_survives_generated_file_and_both_compose_consumers(self) -> None:
        previous_values = {
            "KEEP": "provider-only",
            "REMOVE": "old",
            "JSON_KEPT": '{"deployment_record_id":"example"}',
        }
        for previous in (
            api.serialize_dokploy_env_text(previous_values),
            api.serialize_dokploy_env_text(previous_values, target_type="compose"),
            (ROOT / "tests/fixtures/dokploy-compose/legacy-literal.env").read_text(),
        ):
            with self.subTest(previous=previous):
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
                expected = {
                    **{key: value for key, value in previous_values.items() if key != "REMOVE"},
                    **VALUES,
                }
                self.assertEqual(api.parse_dokploy_env_text(wire), expected)
                before, _ = consume_compose_environment(previous, ("JSON_KEPT",))
                actual, interpolation = consume_compose_environment(wire, tuple(expected))
                for name, service in actual.items():
                    # `config` emits reusable Compose source with escaped dollars.
                    self.assertEqual(
                        service,
                        {
                            **{key: value.replace("$", "$$") for key, value in expected.items()},
                            "JSON_KEPT": before[name]["JSON_KEPT"],
                        },
                    )
                self.assertIn("DOLLARS=" + VALUES["DOLLARS"], interpolation.splitlines())
                self.assertNotIn("REMOVE", api.parse_dokploy_env_text(wire))
                qualified = api.serialize_dokploy_env_text(
                    expected,
                    target_type="compose",
                    current_env_text=wire,
                    literal_keys=("JSON_KEPT",),
                )
                qualified_actual, _ = consume_compose_environment(qualified, ("JSON_KEPT",))
                for service in qualified_actual.values():
                    self.assertEqual(service["JSON_KEPT"], expected["JSON_KEPT"])

    def test_unrelated_image_write_preserves_legacy_effective_credentials_and_references(
        self,
    ) -> None:
        legacy_values = {
            "ODOO_DB_PASSWORD": '"password"',
            "DOLLAR_PASSWORD": "password$MISSING",
            "PADDED": "password  ",
            "REFERENCE": "${APP_NAME}",
            "PROJECT": "${{project.INPUT}}",
            "OLD_TOML": '[legacy]\nname = "example"',
            "JSON_KEPT": "${APP_NAME}",
        }
        unmarked = api.serialize_dokploy_env_text(legacy_values)
        saved = (ROOT / "tests/fixtures/dokploy-compose/legacy-literal.env").read_text()
        credential = (ROOT / "tests/fixtures/dokploy-compose/legacy-credential.env").read_text()
        references = api.serialize_dokploy_env_text(
            {
                key: value
                for key, value in legacy_values.items()
                if key not in {"ODOO_DB_PASSWORD", "JSON_KEPT"}
            }
        )
        edited = api.serialize_dokploy_env_text(
            legacy_values | {"ODOO_DB_PASSWORD": "retained$value"}, target_type="compose"
        ).replace("ODOO_DB_PASSWORD=", "ODOO_DB_PASSWORD=provider's ")
        for previous in (unmarked, saved + "\n" + unmarked, credential + "\n" + references, edited):
            with self.subTest(previous=previous):
                before, _ = consume_compose_environment(
                    previous, tuple(legacy_values), project_env="INPUT=project-value"
                )
                with (
                    patch.object(
                        api, "fetch_dokploy_target_payload", return_value={"env": previous}
                    ),
                    patch.object(api, "dokploy_request") as request,
                ):
                    update_dokploy_target_artifact(
                        host="https://provider.invalid",
                        token="synthetic",
                        target_type="compose",
                        target_id="synthetic",
                        artifact_id="synthetic.invalid/example:next",
                    )
                wire = request.call_args.kwargs["payload"]["env"]
                after, _ = consume_compose_environment(
                    wire, tuple(legacy_values), project_env="INPUT=project-value"
                )
                self.assertEqual(after, before)
                self.assertEqual(
                    api.parse_dokploy_env_text(wire)["ODOO_DB_PASSWORD"],
                    api.parse_dokploy_env_text(previous)["ODOO_DB_PASSWORD"],
                )
                again = api.serialize_dokploy_env_text(
                    api.parse_dokploy_env_text(wire), target_type="compose", current_env_text=wire
                )
                self.assertEqual(again, wire)
                self.assertEqual(
                    api.compose_unqualified_keys(wire, ("ODOO_DB_PASSWORD", "JSON_KEPT")),
                    api.compose_unqualified_keys(previous, ("ODOO_DB_PASSWORD", "JSON_KEPT")),
                )
