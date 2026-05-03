import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.workflows.odoo_artifact_publish import (
    DEVKIT_RUNTIME_ENVIRONMENT_PAYLOAD_KEY,
    OdooArtifactPublishEvidenceRequest,
    OdooArtifactPublishInputsRequest,
    OdooArtifactPublishRequest,
    build_odoo_artifact_publish_inputs,
    execute_odoo_artifact_publish,
    ingest_odoo_artifact_publish_evidence,
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
    def test_publish_requests_accept_new_odoo_contexts(self) -> None:
        publish_request = OdooArtifactPublishRequest(
            context="  New-Site  ",
            manifest_path=Path("/work/new-site/workspace.toml"),
            devkit_root=Path("/work/odoo-devkit"),
            image_repository="ghcr.io/cbusillo/odoo-tenant-new-site",
            image_tag="new-site-20260503-005c291b",
        )
        evidence_request = OdooArtifactPublishEvidenceRequest.model_validate(
            {
                "context": "new-site",
                "manifest": {
                    **_artifact_payload(),
                    "artifact_id": "artifact-new-site-005c291b63b6",
                },
            }
        )
        inputs_request = OdooArtifactPublishInputsRequest(context="new-site")

        self.assertEqual(publish_request.context, "new-site")
        self.assertEqual(evidence_request.context, "new-site")
        self.assertEqual(inputs_request.context, "new-site")

    def test_publish_requests_reject_blank_contexts(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooArtifactPublishInputsRequest(context="   ")

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

    def test_ingest_publish_evidence_writes_artifact_manifest(self) -> None:
        record_store = Mock()

        result = ingest_odoo_artifact_publish_evidence(
            record_store=record_store,
            request=OdooArtifactPublishEvidenceRequest(
                context="cm",
                instance="testing",
                manifest=_artifact_payload(),
            ),
        )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.artifact_id, "artifact-cm-005c291b63b6")
        written_manifest = record_store.write_artifact_manifest.call_args.args[0]
        self.assertEqual(written_manifest.artifact_id, "artifact-cm-005c291b63b6")

    def test_publish_inputs_return_only_build_scoped_environment_keys(self) -> None:
        with patch(
            "control_plane.workflows.odoo_artifact_publish.control_plane_runtime_environments.resolve_runtime_environment_values",
            return_value={
                "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19",
                "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/cbusillo/devtools:19",
                "ODOO_DB_PASSWORD": "do-not-return",
                "GITHUB_TOKEN": "do-not-return",
            },
        ):
            payload = build_odoo_artifact_publish_inputs(
                control_plane_root=Path("/launchplane"),
                request=OdooArtifactPublishInputsRequest(context="cm", instance="testing"),
            )

        self.assertEqual(payload["context"], "cm")
        self.assertEqual(
            payload["environment"],
            {
                "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19",
                "ODOO_BASE_DEVTOOLS_IMAGE": "ghcr.io/cbusillo/devtools:19",
            },
        )


if __name__ == "__main__":
    unittest.main()
