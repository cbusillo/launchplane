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
    wire: str,
    keys: tuple[str, ...],
    *,
    project_env: str = "",
    provider: str = "legacy",
    compose_input: dict[str, object] | None = None,
) -> tuple[dict[str, dict[str, str]], str]:
    """Run the original provider file writer and Compose without a daemon."""
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(
            [
                *json.loads(os.environ["LAUNCHPLANE_TEST_NODE_COMMAND"]),
                str(ROOT / "tests/fixtures/dokploy-compose/consume.mjs"),
            ],
            input=json.dumps(
                {"root": directory, "env": wire, "projectEnv": project_env, "provider": provider}
            ),
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
        command = [
            *json.loads(os.environ["LAUNCHPLANE_TEST_COMPOSE_COMMAND"]),
            "--project-directory",
            str(code),
            "-f",
            "-",
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
                input=json.dumps(compose_input or compose),
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
        previous_values = {"KEEP": "provider-only", "REMOVE": "old"}
        for previous in (
            api.serialize_dokploy_env_text(previous_values),
            api.serialize_dokploy_env_text(previous_values, target_type="compose"),
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
                expected = {"KEEP": previous_values["KEEP"], **VALUES}
                self.assertEqual(api.parse_dokploy_env_text(wire), expected)
                actual, interpolation = consume_compose_environment(wire, tuple(expected))
                for service in actual.values():
                    # `config` emits reusable Compose source with escaped dollars.
                    self.assertEqual(
                        service, {key: value.replace("$", "$$") for key, value in expected.items()}
                    )
                self.assertIn("DOLLARS=" + VALUES["DOLLARS"], interpolation.splitlines())
                self.assertNotIn("REMOVE", api.parse_dokploy_env_text(wire))

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
        }
        previous = api.serialize_dokploy_env_text(legacy_values)
        before, _ = consume_compose_environment(
            previous, tuple(legacy_values), project_env="INPUT=project-value"
        )
        with (
            patch.object(api, "fetch_dokploy_target_payload", return_value={"env": previous}),
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
            api.parse_dokploy_env_text(wire)["ODOO_DB_PASSWORD"], legacy_values["ODOO_DB_PASSWORD"]
        )

    def test_odoo_identity_reaches_current_provider_consumers(self) -> None:
        import yaml
        from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
        from control_plane.dokploy.compose import render_odoo_raw_compose_file

        identity = RuntimeIdentity(
            product="example",
            context="example",
            instance="testing",
            deployment_record_id="deployment-current",
            artifact_id="artifact-current",
            source_git_ref="a" * 40,
            image_reference="synthetic.invalid/example@sha256:" + "a" * 64,
        )
        identity_values = runtime_identity_env(identity)
        compose = yaml.safe_load(
            render_odoo_raw_compose_file(
                image_reference=identity.image_reference,
                hold_web_until_integration_readback=False,
                runtime_identity=identity,
            )
        )
        services = {
            name: {
                "image": service["image"],
                "environment": service["environment"],
                "env_file": [".env"],
            }
            for name, service in compose["services"].items()
            if name in {"web", "script-runner"}
        }
        wire = api.serialize_dokploy_env_text(
            {
                **identity_values,
                "ODOO_DB_NAME": "synthetic",
                "ODOO_DB_USER": "synthetic",
                "ODOO_DB_PASSWORD": "synthetic",
                "ODOO_MASTER_PASSWORD": "synthetic",
            },
            target_type="compose",
        )
        actual, _ = consume_compose_environment(
            wire, tuple(identity_values), provider="current", compose_input={"services": services}
        )
        for service in actual.values():
            self.assertEqual(service, identity_values)
            self.assertEqual(
                json.loads(service["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"]),
                identity.model_dump(mode="json", exclude_none=True),
            )
