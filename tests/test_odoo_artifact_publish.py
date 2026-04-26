import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from control_plane.workflows.odoo_artifact_publish import (
    DEVKIT_RUNTIME_ENVIRONMENT_PAYLOAD_KEY,
    OdooArtifactPublishRequest,
    execute_odoo_artifact_publish,
)


def _artifact_payload() -> dict[str, object]:
    return {
        "artifact_id": "artifact-cm-005c291b63b6",
        "source_commit": "005c291b63b61971ba6b1b29a3a49913cb5975a6",
        "enterprise_base_digest": "sha256:enterprise",
        "image": {
            "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
            "digest": "sha256:005c291b63b61971ba6b1b29a3a49913cb5975a6005c291b63b6",
        },
    }


class OdooArtifactPublishWorkflowTests(unittest.TestCase):
    def test_publish_resolves_launchplane_env_and_writes_artifact(self) -> None:
        record_store = Mock()
        captured_env = {}

        def fake_run(command, *, capture_output, text, env):
            del capture_output, text
            captured_env.update(env)
            output_file = Path(command[command.index("--output-file") + 1])
            output_file.write_text(json.dumps(_artifact_payload()), encoding="utf-8")
            return Mock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "control_plane.workflows.odoo_artifact_publish.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={
                    "ODOO_MASTER_PASSWORD": "managed-secret",
                    "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/odoo-enterprise-docker:19.0-runtime",
                },
            ) as resolve_mock,
            patch(
                "control_plane.workflows.odoo_artifact_publish.subprocess.run", side_effect=fake_run
            ) as run_mock,
        ):
            result = execute_odoo_artifact_publish(
                control_plane_root=Path("/launchplane"),
                record_store=record_store,
                request=OdooArtifactPublishRequest(
                    context="cm",
                    manifest_path=Path("/work/cm/workspace.toml"),
                    devkit_root=Path("/work/odoo-devkit"),
                    image_repository="ghcr.io/cbusillo/odoo-tenant-cm",
                    image_tag="cm-20260426-005c291b",
                ),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.artifact_id, "artifact-cm-005c291b63b6")
        resolve_mock.assert_called_once_with(
            control_plane_root=Path("/launchplane"),
            context_name="cm",
            instance_name="testing",
        )
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:5], ["uv", "--directory", "/work/odoo-devkit", "run", "platform"])
        runtime_payload = json.loads(captured_env[DEVKIT_RUNTIME_ENVIRONMENT_PAYLOAD_KEY])
        self.assertEqual(runtime_payload["context"], "cm")
        self.assertEqual(runtime_payload["instance"], "testing")
        self.assertEqual(runtime_payload["environment"]["ODOO_MASTER_PASSWORD"], "managed-secret")
        written_manifest = record_store.write_artifact_manifest.call_args.args[0]
        self.assertEqual(written_manifest.artifact_id, "artifact-cm-005c291b63b6")

    def test_publish_rejects_wrong_context_artifact(self) -> None:
        record_store = Mock()

        def fake_run(command, *, capture_output, text, env):
            del capture_output, text, env
            payload = dict(_artifact_payload())
            payload["artifact_id"] = "artifact-opw-005c291b63b6"
            output_file = Path(command[command.index("--output-file") + 1])
            output_file.write_text(json.dumps(payload), encoding="utf-8")
            return Mock(returncode=0, stdout="", stderr="")

        with (
            patch(
                "control_plane.workflows.odoo_artifact_publish.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_MASTER_PASSWORD": "managed-secret"},
            ),
            patch(
                "control_plane.workflows.odoo_artifact_publish.subprocess.run", side_effect=fake_run
            ),
        ):
            result = execute_odoo_artifact_publish(
                control_plane_root=Path("/launchplane"),
                record_store=record_store,
                request=OdooArtifactPublishRequest(
                    context="cm",
                    manifest_path=Path("/work/cm/workspace.toml"),
                    devkit_root=Path("/work/odoo-devkit"),
                    image_repository="ghcr.io/cbusillo/odoo-tenant-cm",
                    image_tag="cm-20260426-005c291b",
                ),
            )

        self.assertEqual(result.status, "fail")
        self.assertIn("wrong context", result.error_message)
        record_store.write_artifact_manifest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
