import unittest
from pathlib import Path
from unittest.mock import patch

import click

from control_plane.contracts.artifact_publish_inputs import (
    GenericArtifactPublishInputsRequest,
    GenericArtifactPublishInputsResult,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.workflows.odoo_artifact_publish import (
    OdooArtifactPublishInputsRequest,
    build_odoo_artifact_publish_inputs,
)


def _profile_payload(product: str = "odoo-tenant-cm", context: str = "cm") -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "Odoo Tenant",
        "repository": f"cbusillo/{product}",
        "driver_id": "odoo",
        "image": {"repository": f"ghcr.io/cbusillo/{product}"},
        "runtime_port": 8069,
        "health_path": "/web/health",
        "lanes": (
            {
                "instance": "testing",
                "context": context,
                "base_url": f"https://{context}-testing.example.com",
                "health_url": f"https://{context}-testing.example.com/web/health",
            },
        ),
        "preview": {
            "enabled": True,
            "context": context,
            "slug_template": "preview-{number}",
        },
        "updated_at": "2026-05-27T12:00:00Z",
        "source": "test",
    }


class OdooArtifactPublishInputsTests(unittest.TestCase):
    def test_publish_inputs_include_profile_derived_preview_metadata(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_profile_payload())
        with patch(
            "control_plane.workflows.odoo_artifact_publish.control_plane_runtime_environments.resolve_runtime_environment_values",
            return_value={
                "ODOO_BASE_RUNTIME_IMAGE": "ghcr.io/cbusillo/runtime:19",
                "ODOO_DEVKIT_REPOSITORY": "every/odoo-devkit",
                "ODOO_SHARED_ADDONS_REPOSITORY": "every/odoo-shared-addons",
            },
        ):
            payload = build_odoo_artifact_publish_inputs(
                control_plane_root=Path("/launchplane"),
                request=OdooArtifactPublishInputsRequest(
                    context="cm",
                    pr_number=28,
                    source_git_ref="abcdef1234567890",
                ),
                product_profile=profile,
            )

        self.assertEqual(payload["product"], "odoo-tenant-cm")
        self.assertEqual(payload["repository"], "cbusillo/odoo-tenant-cm")
        self.assertEqual(payload["image_repository"], "ghcr.io/cbusillo/odoo-tenant-cm")
        self.assertEqual(payload["devkit_repository"], "every/odoo-devkit")
        self.assertEqual(payload["shared_addons_repository"], "every/odoo-shared-addons")
        self.assertEqual(payload["preview_slug"], "preview-28")
        self.assertEqual(payload["source_git_ref"], "abcdef1234567890")
        self.assertEqual(payload["image_tag"], "cm-preview-28-abcdef12-amd64")
        GenericArtifactPublishInputsResult.model_validate(payload)

    def test_publish_inputs_request_extends_generic_contract(self) -> None:
        request = OdooArtifactPublishInputsRequest(context=" CM ", instance=" TESTING ")

        self.assertIsInstance(request, GenericArtifactPublishInputsRequest)
        self.assertEqual(request.context, "cm")
        self.assertEqual(request.instance, "testing")

    def test_publish_inputs_result_rejects_invalid_product_repository(self) -> None:
        with self.assertRaisesRegex(ValueError, "repository must be formatted as owner/name"):
            GenericArtifactPublishInputsResult(
                context="cm",
                instance="testing",
                repository="not-a-repository",
            )

    def test_publish_inputs_can_derive_isolated_preview_image_tag(self) -> None:
        profile_payload = _profile_payload()
        profile_payload["preview"] = {
            "enabled": True,
            "context": "cm",
            "slug_template": "pr-{number}",
        }
        with patch(
            "control_plane.workflows.odoo_artifact_publish.control_plane_runtime_environments.resolve_runtime_environment_values",
            return_value={
                "ODOO_DEVKIT_REPOSITORY": "every/odoo-devkit",
                "ODOO_SHARED_ADDONS_REPOSITORY": "every/odoo-shared-addons",
            },
        ):
            payload = build_odoo_artifact_publish_inputs(
                control_plane_root=Path("/launchplane"),
                request=OdooArtifactPublishInputsRequest(
                    context="cm",
                    pr_number=28,
                    source_git_ref="abcdef1234567890",
                    isolated=True,
                ),
                product_profile=LaunchplaneProductProfileRecord.model_validate(profile_payload),
            )

        self.assertEqual(payload["preview_slug"], "pr-28")
        self.assertEqual(payload["image_tag"], "cm-pr-28-abcdef12-isolated-amd64")

    def test_publish_inputs_require_dependency_repositories_for_profile_metadata(self) -> None:
        with patch(
            "control_plane.workflows.odoo_artifact_publish.control_plane_runtime_environments.resolve_runtime_environment_values",
            return_value={"ODOO_DEVKIT_REPOSITORY": "every/odoo-devkit"},
        ):
            with self.assertRaisesRegex(
                click.ClickException,
                "ODOO_SHARED_ADDONS_REPOSITORY",
            ):
                build_odoo_artifact_publish_inputs(
                    control_plane_root=Path("/launchplane"),
                    request=OdooArtifactPublishInputsRequest(
                        context="cm",
                        source_git_ref="abcdef1234567890",
                    ),
                    product_profile=LaunchplaneProductProfileRecord.model_validate(
                        _profile_payload()
                    ),
                )

    def test_publish_inputs_require_product_profile_for_preview_metadata(self) -> None:
        with self.assertRaises(click.ClickException):
            build_odoo_artifact_publish_inputs(
                control_plane_root=Path("/launchplane"),
                request=OdooArtifactPublishInputsRequest(
                    context="cm",
                    pr_number=28,
                    source_git_ref="abcdef1234567890",
                ),
            )

    def test_authz_policy_allows_product_specific_publish_inputs_route(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/tenant-opw",
                        "workflow_refs": [
                            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["odoo-tenant-opw"],
                        "contexts": ["opw"],
                        "actions": ["odoo_artifact_publish_inputs.read"],
                    }
                ]
            }
        )
        from tests.support.auth import _identity

        self.assertTrue(
            policy.allows(
                identity=_identity(
                    repository="every/tenant-opw",
                    workflow_ref=(
                        "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                    ),
                    event_name="workflow_dispatch",
                ),
                action="odoo_artifact_publish_inputs.read",
                product="odoo-tenant-opw",
                context="opw",
            )
        )


if __name__ == "__main__":
    unittest.main()
