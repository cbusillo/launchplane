import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretClass,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
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
        "build_provenance": {
            "base_images": [
                {
                    "role": "runtime",
                    "image": {
                        "repository": "ghcr.io/cbusillo/odoo-runtime",
                        "digest": "sha256:runtime",
                        "tags": ["19.0-stable"],
                    },
                    "source_repository": "cbusillo/odoo-docker",
                    "source_ref": "1111111111111111111111111111111111111111",
                },
                {
                    "role": "devtools",
                    "image": {
                        "repository": "ghcr.io/cbusillo/odoo-devtools",
                        "digest": "sha256:devtools",
                    },
                    "source_repository": "cbusillo/odoo-docker",
                    "source_ref": "2222222222222222222222222222222222222222",
                },
            ],
            "build_tools": [
                {
                    "name": "odoo-devkit",
                    "source_repository": "cbusillo/odoo-devkit",
                    "source_ref": "3333333333333333333333333333333333333333",
                }
            ],
        },
        "image": {
            "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
            "digest": "sha256:005c291b63b61971ba6b1b29a3a49913cb5975a6005c291b63b6",
        },
    }


def _artifact_manifest(payload: dict[str, object] | None = None) -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest.model_validate(payload or _artifact_payload())


class OdooArtifactPublishWorkflowTests(unittest.TestCase):
    def _record_store(self) -> Mock:
        record_store = Mock()
        record_store.list_secret_bindings.return_value = ()
        record_store.list_runtime_key_safety_policy_records.return_value = ()
        return record_store

    def _write_secret_binding(
        self,
        record_store: Mock,
        *,
        secret_class: RuntimeSecretClass = "testing",
    ) -> None:
        binding_key = "ODOO_MASTER_PASSWORD"
        record_store.list_secret_bindings.return_value = (
            SecretBinding(
                binding_id="secret-odoo-master-password-binding",
                secret_id="secret-odoo-master-password",
                integration="runtime_environment",
                binding_key=binding_key,
                context="cm",
                instance="testing",
                status="configured",
                created_at="2026-05-05T23:00:00Z",
                updated_at="2026-05-05T23:00:00Z",
            ),
        )
        record_store.list_runtime_key_safety_policy_records.return_value = (
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-test",
                status="active",
                source="test",
                updated_at="2026-05-05T23:00:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key=binding_key,
                        secret_class=secret_class,
                        allowed_contexts=("cm",),
                        allowed_instances=("testing",),
                    ),
                ),
            ),
        )

    def test_artifact_manifest_keeps_build_provenance(self) -> None:
        manifest = _artifact_manifest()

        self.assertEqual(len(manifest.build_provenance.base_images), 2)
        self.assertEqual(manifest.build_provenance.base_images[0].role, "runtime")
        self.assertEqual(
            manifest.build_provenance.base_images[0].image.repository,
            "ghcr.io/cbusillo/odoo-runtime",
        )
        self.assertEqual(manifest.build_provenance.build_tools[0].name, "odoo-devkit")
        self.assertEqual(
            manifest.build_provenance.build_tools[0].source_repository,
            "cbusillo/odoo-devkit",
        )

    def test_artifact_manifest_accepts_legacy_payload_without_build_provenance(self) -> None:
        payload = _artifact_payload()
        payload.pop("build_provenance")

        manifest = ArtifactIdentityManifest.model_validate(payload)

        self.assertEqual(manifest.build_provenance.base_images, ())
        self.assertEqual(manifest.build_provenance.build_tools, ())

    def test_artifact_manifest_rejects_duplicate_base_image_roles(self) -> None:
        payload = _artifact_payload()
        build_provenance = payload["build_provenance"]
        assert isinstance(build_provenance, dict)
        base_images = build_provenance["base_images"]
        assert isinstance(base_images, list)
        duplicate_runtime = dict(base_images[0])
        duplicate_runtime["image"] = {
            "repository": "ghcr.io/cbusillo/other-runtime",
            "digest": "sha256:other",
        }
        base_images.append(duplicate_runtime)

        with self.assertRaisesRegex(ValidationError, "duplicate base image role"):
            ArtifactIdentityManifest.model_validate(payload)

    def test_artifact_manifest_rejects_incomplete_build_tool_provenance(self) -> None:
        payload = _artifact_payload()
        build_provenance = payload["build_provenance"]
        assert isinstance(build_provenance, dict)
        build_tools = build_provenance["build_tools"]
        assert isinstance(build_tools, list)
        build_tools[0] = {"name": "odoo-devkit"}

        with self.assertRaisesRegex(ValidationError, "requires version or source_repository"):
            ArtifactIdentityManifest.model_validate(payload)

    def test_publish_requests_accept_profile_owned_contexts(self) -> None:
        publish_request = OdooArtifactPublishRequest(
            context=" New-Site ",
            manifest_path=Path("/work/new-site/workspace.toml"),
            devkit_root=Path("/work/odoo-devkit"),
            image_repository="ghcr.io/cbusillo/odoo-tenant-new-site",
            image_tag="new-site-20260503",
        )
        evidence_payload = dict(_artifact_payload())
        evidence_payload["artifact_id"] = "artifact-new-site-005c291b63b6"
        evidence_request = OdooArtifactPublishEvidenceRequest(
            context="new-site",
            instance="testing",
            manifest=_artifact_manifest(evidence_payload),
        )
        inputs_request = OdooArtifactPublishInputsRequest(context="new-site", instance="testing")

        self.assertEqual(publish_request.context, "new-site")
        self.assertEqual(evidence_request.context, "new-site")
        self.assertEqual(inputs_request.context, "new-site")

    def test_publish_requests_reject_blank_contexts(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooArtifactPublishRequest(
                context=" ",
                manifest_path=Path("/work/new-site/workspace.toml"),
                devkit_root=Path("/work/odoo-devkit"),
                image_repository="ghcr.io/cbusillo/odoo-tenant-new-site",
                image_tag="new-site-20260503",
            )
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooArtifactPublishEvidenceRequest(
                context=" ",
                instance="testing",
                manifest=_artifact_manifest(),
            )
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooArtifactPublishInputsRequest(context=" ", instance="testing")

    def test_publish_resolves_launchplane_env_and_writes_artifact(self) -> None:
        record_store = self._record_store()
        self._write_secret_binding(record_store)
        captured_env: dict[str, str] = {}

        def fake_run(
            command: list[str], *, capture_output: bool, text: bool, env: dict[str, str]
        ) -> Mock:
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

    def test_publish_blocks_unsafe_managed_secret_payload(self) -> None:
        record_store = self._record_store()
        self._write_secret_binding(record_store, secret_class="prod_only")

        with (
            patch(
                "control_plane.workflows.odoo_artifact_publish.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_MASTER_PASSWORD": "managed-secret"},
            ),
            patch("control_plane.workflows.odoo_artifact_publish.subprocess.run") as run_mock,
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
        self.assertIn("Runtime key-safety gate failed", result.error_message)
        run_mock.assert_not_called()
        record_store.write_artifact_manifest.assert_not_called()

    def test_publish_rejects_wrong_context_artifact(self) -> None:
        record_store = self._record_store()

        def fake_run(
            command: list[str], *, capture_output: bool, text: bool, env: dict[str, str]
        ) -> Mock:
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
        record_store = self._record_store()

        result = ingest_odoo_artifact_publish_evidence(
            record_store=record_store,
            request=OdooArtifactPublishEvidenceRequest(
                context="cm",
                instance="testing",
                manifest=_artifact_manifest(),
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
