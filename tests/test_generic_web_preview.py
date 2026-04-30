import unittest
from pathlib import Path
from unittest.mock import patch

import click

from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDesiredStateRequest,
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewInventoryRequest,
    GenericWebPreviewReadinessRequest,
    GenericWebPreviewRefreshRequest,
    discover_generic_web_preview_desired_state,
    evaluate_generic_web_preview_readiness,
    execute_generic_web_preview_destroy,
    execute_generic_web_preview_inventory,
    execute_generic_web_preview_refresh,
    preview_pr_number_from_slug,
    resolve_generic_web_preview_profile,
)
from control_plane.workflows.preview_desired_state import render_preview_slug


class _GenericWebPreviewStore:
    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile


def _profile(*, preview_enabled: bool = True) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="sellyouroutboard-testing",
                base_url="https://testing.sellyouroutboard.com",
                health_url="https://testing.sellyouroutboard.com/api/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=preview_enabled,
            context="sellyouroutboard-testing" if preview_enabled else "",
            slug_template="preview-{number}-site",
            app_name_prefix="syo-preview",
            required_template_env_keys=("SMTP_HOST",),
            copied_env_keys=("SMTP_FROM",),
            omitted_env_keys=("PUBLIC_URL",),
            override_env={"NODE_ENV": "production"},
            preview_url_env_keys=("PUBLIC_URL",),
            preview_domain_env_keys=("PUBLIC_DOMAIN",),
            required_provider_fields=("dockerImage", "username"),
        ),
        updated_at="2026-04-30T21:00:00Z",
        source="test",
    )


class GenericWebPreviewTests(unittest.TestCase):
    def test_render_preview_slug_uses_template_when_present(self) -> None:
        self.assertEqual(
            render_preview_slug(
                anchor_pr_number=123,
                preview_slug_prefix="pr-",
                preview_slug_template="preview-{number}-site",
            ),
            "preview-123-site",
        )

    def test_discover_generic_web_preview_desired_state_uses_profile_contract(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        record = PreviewDesiredStateRecord(
            desired_state_id="preview-desired-state-syo-testing-1",
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            source="generic-web-preview",
            discovered_at="2026-04-30T21:00:00Z",
            repository="cbusillo/sellyouroutboard",
            label="preview",
            anchor_repo="sellyouroutboard",
            preview_slug_prefix="preview-",
            status="pass",
            desired_count=0,
        )

        with patch(
            "control_plane.workflows.generic_web_preview.discover_github_preview_desired_state",
            return_value=record,
        ) as discover:
            result = discover_generic_web_preview_desired_state(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewDesiredStateRequest(product="sellyouroutboard"),
                discovered_at="2026-04-30T21:00:00Z",
            )

        self.assertEqual(result, record)
        discover.assert_called_once()
        _, kwargs = discover.call_args
        self.assertEqual(kwargs["product"], "sellyouroutboard")
        self.assertEqual(kwargs["context"], "sellyouroutboard-testing")
        self.assertEqual(kwargs["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(kwargs["anchor_repo"], "sellyouroutboard")
        self.assertEqual(kwargs["preview_slug_prefix"], "preview-")
        self.assertEqual(kwargs["preview_slug_template"], "preview-{number}-site")

    def test_resolve_generic_web_preview_profile_rejects_disabled_preview(self) -> None:
        store = _GenericWebPreviewStore(_profile(preview_enabled=False))

        with self.assertRaises(click.ClickException):
            resolve_generic_web_preview_profile(
                record_store=store,
                product="sellyouroutboard",
            )

    def test_preview_profile_rejects_copy_omit_overlap(self) -> None:
        with self.assertRaises(ValueError):
            ProductPreviewProfile(
                enabled=True,
                context="sellyouroutboard-testing",
                copied_env_keys=("SMTP_HOST",),
                omitted_env_keys=("SMTP_HOST",),
            )

    def test_preview_pr_number_from_slug_uses_template(self) -> None:
        self.assertEqual(
            preview_pr_number_from_slug(
                preview_slug="preview-42-site",
                slug_template="preview-{number}-site",
            ),
            42,
        )
        self.assertIsNone(
            preview_pr_number_from_slug(
                preview_slug="not-preview-42-site",
                slug_template="preview-{number}-site",
            )
        )

    def test_execute_generic_web_preview_inventory_filters_by_app_prefix(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        raw_projects = [
            {
                "environments": [
                    {
                        "applications": [
                            {"applicationId": "app-1", "name": "syo-preview-preview-42-site"},
                            {"applicationId": "app-2", "name": "other-preview-pr-1"},
                        ]
                    }
                ]
            }
        ]

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request",
                return_value=raw_projects,
            ),
        ):
            result = execute_generic_web_preview_inventory(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewInventoryRequest(product="sellyouroutboard"),
            )

        self.assertEqual(result.context, "sellyouroutboard-testing")
        self.assertEqual(result.app_name_prefix, "syo-preview")
        self.assertEqual([item.previewSlug for item in result.previews], ["preview-42-site"])

    def test_evaluate_generic_web_preview_readiness_passes_with_template_contract(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="sellyouroutboard-testing",
                    instance="testing",
                    target_type="application",
                    target_id="app-testing",
                    target_name="sellyouroutboard-testing",
                ),
            ),
        )

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "env": "SMTP_HOST=smtp.example\nSMTP_FROM=hello@example.com\n",
                    "dockerImage": "ghcr.io/cbusillo/sellyouroutboard:sha",
                    "username": "github-actions",
                },
            ),
        ):
            result = evaluate_generic_web_preview_readiness(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewReadinessRequest(product="sellyouroutboard"),
                checked_at="2026-04-30T21:00:00Z",
            )

        self.assertEqual(result.readiness_status, "pass")
        self.assertEqual(result.template_instance, "testing")
        self.assertEqual(result.template_target_id, "app-testing")
        self.assertEqual(result.missing_template_env_keys, ())
        self.assertEqual(result.missing_provider_fields, ())
        self.assertEqual(result.transport.copied_env_keys, ("SMTP_FROM",))
        self.assertEqual(result.transport.preview_url_env_keys, ("PUBLIC_URL",))

    def test_evaluate_generic_web_preview_readiness_blocks_missing_template_inputs(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="sellyouroutboard-testing",
                    instance="testing",
                    target_type="application",
                    target_id="app-testing",
                ),
            ),
        )

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "env": "SMTP_HOST=\n",
                    "dockerImage": "",
                    "registry": {},
                },
            ),
        ):
            result = evaluate_generic_web_preview_readiness(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewReadinessRequest(product="sellyouroutboard"),
                checked_at="2026-04-30T21:00:00Z",
            )

        self.assertEqual(result.readiness_status, "blocked")
        self.assertEqual(result.missing_template_env_keys, ("SMTP_HOST", "SMTP_FROM"))
        self.assertEqual(result.missing_provider_fields, ("dockerImage", "username"))
        self.assertEqual(
            [check.check_id for check in result.checks],
            ["template_env", "template_provider_fields", "transport_policy"],
        )

    def test_execute_generic_web_preview_refresh_blocks_before_provider_mutation(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="sellyouroutboard-testing",
                    instance="testing",
                    target_type="application",
                    target_id="app-testing",
                ),
            ),
        )
        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={"env": "", "dockerImage": "", "registry": {}},
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request"
            ) as dokploy_request,
        ):
            result = execute_generic_web_preview_refresh(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewRefreshRequest(
                    product="sellyouroutboard",
                    preview_slug="preview-42-site",
                    preview_url="https://preview-42.example.test",
                    image_reference="ghcr.io/cbusillo/sellyouroutboard:sha",
                ),
            )

        self.assertEqual(result.refresh_status, "blocked")
        dokploy_request.assert_not_called()

    def test_execute_generic_web_preview_refresh_creates_application_from_template(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="sellyouroutboard-testing",
                    instance="testing",
                    target_type="application",
                    target_id="app-testing",
                    target_name="sellyouroutboard-testing",
                ),
            ),
        )
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs):
            requests.append(kwargs)
            path = kwargs["path"]
            if path == "/api/project.all":
                return [{"environments": [{"applications": []}]}]
            if path == "/api/application.create":
                return {"applicationId": "app-preview"}
            if path == "/api/domain.byApplicationId":
                return []
            if path == "/api/domain.create":
                return {"domainId": "domain-preview"}
            return {}

        def _fake_fetch(**kwargs):
            target_id = kwargs["target_id"]
            if target_id == "app-testing":
                return {
                    "applicationId": "app-testing",
                    "environmentId": "env-1",
                    "serverId": "server-1",
                    "env": "SMTP_HOST=smtp.example\nSMTP_FROM=hello@example.com\nPUBLIC_URL=https://testing.example\n",
                    "dockerImage": "ghcr.io/cbusillo/sellyouroutboard:old",
                    "username": "github-actions",
                    "password": "registry-token",
                    "registryUrl": "ghcr.io",
                    "buildType": "dockerfile",
                }
            if target_id == "app-preview":
                return {"applicationId": "app-preview", "description": ""}
            raise AssertionError(target_id)

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=_fake_fetch,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.latest_deployment_for_target",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.trigger_deployment",
            ) as trigger_deployment,
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.wait_for_target_deployment",
            ),
            patch("control_plane.workflows.generic_web_preview._wait_for_preview_health") as wait_health,
            patch(
                "control_plane.workflows.generic_web_preview.utc_now_timestamp",
                side_effect=["2026-04-30T21:00:00Z", "2026-04-30T21:00:05Z"],
            ),
        ):
            result = execute_generic_web_preview_refresh(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewRefreshRequest(
                    product="sellyouroutboard",
                    preview_slug="preview-42-site",
                    preview_url="https://preview-42.example.test",
                    image_reference="ghcr.io/cbusillo/sellyouroutboard:sha",
                ),
            )

        self.assertEqual(result.refresh_status, "pass")
        self.assertEqual(result.application_id, "app-preview")
        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/api/project.all",
                "/api/application.create",
                "/api/application.update",
                "/api/application.saveBuildType",
                "/api/application.saveDockerProvider",
                "/api/application.saveEnvironment",
                "/api/domain.byApplicationId",
                "/api/domain.create",
            ],
        )
        save_environment = [
            request for request in requests if request["path"] == "/api/application.saveEnvironment"
        ][0]
        env_text = str(save_environment["payload"]["env"])
        self.assertIn("SMTP_FROM=hello@example.com", env_text)
        self.assertIn("PUBLIC_URL=https://preview-42.example.test", env_text)
        self.assertIn("PUBLIC_DOMAIN=preview-42.example.test", env_text)
        self.assertNotIn("SMTP_HOST=", env_text)
        trigger_deployment.assert_called_once()
        wait_health.assert_called_once()

    def test_execute_generic_web_preview_destroy_deletes_domains_and_application(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs):
            requests.append(kwargs)
            path = kwargs["path"]
            if path == "/api/project.all":
                return [
                    {
                        "environments": [
                            {
                                "applications": [
                                    {
                                        "applicationId": "app-1",
                                        "name": "syo-preview-preview-42-site",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            if path == "/api/domain.byApplicationId":
                return [{"domainId": "domain-1"}]
            return {}

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.utc_now_timestamp",
                side_effect=["2026-04-30T21:00:00Z", "2026-04-30T21:00:02Z"],
            ),
        ):
            result = execute_generic_web_preview_destroy(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewDestroyRequest(
                    product="sellyouroutboard",
                    preview_slug="preview-42-site",
                    destroy_reason="test",
                ),
            )

        self.assertEqual(result.destroy_status, "pass")
        self.assertEqual(result.application_id, "app-1")
        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/api/project.all",
                "/api/domain.byApplicationId",
                "/api/domain.delete",
                "/api/application.delete",
            ],
        )


if __name__ == "__main__":
    unittest.main()
