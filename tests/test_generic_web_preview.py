import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.error import HTTPError

import click

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretClass,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding, SecretStatus
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
    resolve_generic_web_preview_url,
    _wait_for_preview_health,
)
from control_plane.workflows.preview_desired_state import render_preview_slug


class _GenericWebPreviewStore:
    def __init__(
        self,
        profile: LaunchplaneProductProfileRecord,
        *,
        runtime_key_safety_policies: tuple[RuntimeKeySafetyPolicyRecord, ...] = (),
        secret_bindings: tuple[SecretBinding, ...] = (),
    ) -> None:
        self.profile = profile
        self.runtime_key_safety_policies = runtime_key_safety_policies
        self.secret_bindings = secret_bindings

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        records = tuple(
            record
            for record in self.runtime_key_safety_policies
            if not status or record.status == status
        )
        if limit is not None:
            return records[:limit]
        return records

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        bindings = tuple(
            binding
            for binding in self.secret_bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        if limit is not None:
            return bindings[:limit]
        return bindings

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        records = (
            RuntimeEnvironmentRecord(
                scope="context",
                context=self.profile.preview.context,
                env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://syo-preview.example.test"},
                updated_at="2026-05-10T05:30:00Z",
                source_label="test",
            ),
        )
        return tuple(
            record
            for record in records
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )


def _profile(
    *, preview_enabled: bool = True, driver_id: str = "generic-web"
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id=driver_id,
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


def _odoo_compose_profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="odoo-tenant-cm",
        display_name="Odoo CM",
        repository="cbusillo/odoo-tenant-cm",
        driver_id="odoo",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="cm",
                base_url="https://testing.cm.example.test",
                health_url="https://testing.cm.example.test/web/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context="cm",
            enable_label="preview",
            slug_template="pr-{number}",
            app_name_prefix="cm-odoo-preview",
            template_instance="testing",
            override_env={"ODOO_INSTALL_MODULES": "cm_custom,cm_website"},
            preview_url_env_keys=("WEB_BASE_URL",),
            data_transport_mode="bootstrap",
        ),
        updated_at="2026-05-09T15:00:00Z",
        source="test",
    )


def _preview_runtime_policy(
    *, secret_class: RuntimeSecretClass = "preview"
) -> RuntimeKeySafetyPolicyRecord:
    return RuntimeKeySafetyPolicyRecord(
        record_id="runtime-key-safety-policy-test",
        status="active",
        source="test",
        updated_at="2026-05-05T20:00:00Z",
        rules=(
            RuntimeSecretSafetyRule(
                binding_key="SMTP_PASSWORD",
                secret_class=secret_class,
                allowed_contexts=("sellyouroutboard-testing",),
            ),
        ),
    )


def _runtime_secret_binding(*, status: SecretStatus = "configured") -> SecretBinding:
    return SecretBinding(
        binding_id="secret-smtp-password-binding-smtp-password",
        secret_id="secret-smtp-password",
        integration="runtime_environment",
        binding_key="SMTP_PASSWORD",
        context="sellyouroutboard-testing",
        instance="testing",
        status=status,
        created_at="2026-05-05T20:00:00Z",
        updated_at="2026-05-05T20:00:00Z",
    )


class GenericWebPreviewTests(unittest.TestCase):
    def test_resolve_generic_web_preview_profile_accepts_based_driver(self) -> None:
        profile = _profile(driver_id="odoo")

        resolved = resolve_generic_web_preview_profile(
            record_store=_GenericWebPreviewStore(profile),
            product="sellyouroutboard",
        )

        self.assertEqual(resolved.driver_id, "odoo")

    def test_resolve_generic_web_preview_profile_rejects_unbased_driver(self) -> None:
        profile = _profile(driver_id="missing-driver")

        with self.assertRaises(click.ClickException):
            resolve_generic_web_preview_profile(
                record_store=_GenericWebPreviewStore(profile),
                product="sellyouroutboard",
            )

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

    def test_execute_generic_web_preview_inventory_lists_odoo_compose_domains(self) -> None:
        store = _GenericWebPreviewStore(_odoo_compose_profile())
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            path = kwargs["path"]
            if path == "/api/domain.byComposeId":
                return [
                    {
                        "domainId": "domain-stable",
                        "host": "cm-testing.shinycomputers.com",
                    },
                    {
                        "domainId": "domain-preview",
                        "host": "pr-28.cm-preview.shinycomputers.com",
                    },
                ]
            return {}

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "composeId": "compose-cm-testing",
                    "name": "cm-testing",
                    "env": "ODOO_DB_NAME=cm\nODOO_DB_USER=odoo\nODOO_DB_PASSWORD=password\nODOO_DATA_VOLUME=data\nODOO_LOG_VOLUME=logs\nODOO_DB_VOLUME=db\nODOO_MASTER_PASSWORD=master\nODOO_ADMIN_PASSWORD=admin\n",
                },
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=DokploySourceOfTruth(
                    schema_version=1,
                    targets=(
                        DokployTargetDefinition(
                            context="cm",
                            instance="testing",
                            target_type="compose",
                            target_id="compose-cm-testing",
                            target_name="cm-testing",
                        ),
                    ),
                ),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
        ):
            result = execute_generic_web_preview_inventory(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewInventoryRequest(product="odoo-tenant-cm"),
            )

        self.assertEqual([item.previewSlug for item in result.previews], ["pr-28"])
        self.assertEqual(result.previews[0].providerType, "compose-domain")
        self.assertEqual(result.previews[0].domainId, "domain-preview")
        self.assertEqual([request["path"] for request in requests], ["/api/domain.byComposeId"])

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

    def test_evaluate_generic_web_preview_readiness_keeps_template_lane_when_target_id_missing(
        self,
    ) -> None:
        store = _GenericWebPreviewStore(_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="sellyouroutboard-testing",
                    instance="testing",
                    target_type="application",
                    target_id="",
                    target_name="sellyouroutboard-testing",
                ),
            ),
        )

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source,
            ) as source_of_truth,
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config"
            ) as read_dokploy_config,
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.fetch_dokploy_target_payload"
            ) as fetch_dokploy_target_payload,
        ):
            result = evaluate_generic_web_preview_readiness(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewReadinessRequest(product="sellyouroutboard"),
                checked_at="2026-04-30T21:00:00Z",
            )

        self.assertEqual(result.readiness_status, "blocked")
        self.assertEqual(result.template_context, "sellyouroutboard-testing")
        self.assertEqual(result.template_instance, "testing")
        self.assertEqual(result.template_target_id, "")
        self.assertEqual([check.check_id for check in result.checks], ["template_target"])
        self.assertIn("template lane to have a Dokploy target_id", result.checks[0].message)
        source_of_truth.assert_called_once_with(
            control_plane_root=Path("."),
            allow_incomplete_target_ids=True,
            allowed_incomplete_target_routes=(("sellyouroutboard-testing", "testing"),),
        )
        read_dokploy_config.assert_not_called()
        fetch_dokploy_target_payload.assert_not_called()

    def test_evaluate_generic_web_preview_readiness_allows_odoo_compose_stage_template(
        self,
    ) -> None:
        store = _GenericWebPreviewStore(_odoo_compose_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="cm",
                    instance="testing",
                    target_type="compose",
                    target_id="compose-cm-testing",
                    target_name="cm-testing",
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
                    "composeId": "compose-cm-testing",
                    "name": "cm-testing",
                    "environmentId": "env-1",
                    "env": (
                        "ODOO_DB_NAME=cm_preview\n"
                        "ODOO_DB_USER=odoo\n"
                        "ODOO_DB_PASSWORD=safe\n"
                        "ODOO_DATA_VOLUME=cm_data\n"
                        "ODOO_LOG_VOLUME=cm_logs\n"
                        "ODOO_DB_VOLUME=cm_db\n"
                        "ODOO_MASTER_PASSWORD=safe-master\n"
                        "ODOO_ADMIN_PASSWORD=safe-admin\n"
                    ),
                },
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
        ):
            result = evaluate_generic_web_preview_readiness(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewReadinessRequest(product="odoo-tenant-cm"),
                checked_at="2026-05-09T15:00:00Z",
            )

        self.assertEqual(result.readiness_status, "pass")
        self.assertEqual(result.template_target_type, "compose")
        self.assertEqual(result.template_target_id, "compose-cm-testing")
        self.assertEqual(result.transport.data_transport_mode, "bootstrap")
        self.assertEqual(result.missing_provider_fields, ())
        self.assertEqual(
            result.checks[1].message,
            "Staged Odoo compose preview uses Launchplane-rendered compose source.",
        )

    def test_evaluate_generic_web_preview_readiness_blocks_non_odoo_compose_template(
        self,
    ) -> None:
        store = _GenericWebPreviewStore(_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="sellyouroutboard-testing",
                    instance="testing",
                    target_type="compose",
                    target_id="compose-testing",
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
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config"
            ) as read_dokploy_config,
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.fetch_dokploy_target_payload"
            ) as fetch_dokploy_target_payload,
        ):
            result = evaluate_generic_web_preview_readiness(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewReadinessRequest(product="sellyouroutboard"),
                checked_at="2026-05-09T15:00:00Z",
            )

        self.assertEqual(result.readiness_status, "blocked")
        self.assertEqual(result.template_target_type, "compose")
        self.assertEqual(
            result.checks[0].message,
            "Generic web preview readiness requires the template lane to be a Dokploy application.",
        )
        read_dokploy_config.assert_not_called()
        fetch_dokploy_target_payload.assert_not_called()

    def test_evaluate_generic_web_preview_readiness_blocks_odoo_compose_missing_safe_env(
        self,
    ) -> None:
        store = _GenericWebPreviewStore(_odoo_compose_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="cm",
                    instance="testing",
                    target_type="compose",
                    target_id="compose-cm-testing",
                    target_name="cm-testing",
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
                    "composeId": "compose-cm-testing",
                    "name": "cm-testing",
                    "environmentId": "env-1",
                    "env": "ODOO_DB_NAME=cm_preview\nODOO_DB_USER=odoo\n",
                },
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
        ):
            result = evaluate_generic_web_preview_readiness(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewReadinessRequest(product="odoo-tenant-cm"),
                checked_at="2026-05-09T15:00:00Z",
            )

        self.assertEqual(result.readiness_status, "blocked")
        self.assertEqual(
            result.missing_template_env_keys,
            (
                "ODOO_DB_PASSWORD",
                "ODOO_DATA_VOLUME",
                "ODOO_LOG_VOLUME",
                "ODOO_DB_VOLUME",
                "ODOO_MASTER_PASSWORD",
                "ODOO_ADMIN_PASSWORD",
            ),
        )

    def test_evaluate_generic_web_preview_readiness_accepts_odoo_compose_runtime_env(
        self,
    ) -> None:
        store = _GenericWebPreviewStore(_odoo_compose_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="cm",
                    instance="testing",
                    target_type="compose",
                    target_id="compose-cm-testing",
                    target_name="cm-testing",
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
                    "composeId": "compose-cm-testing",
                    "name": "cm-testing",
                    "environmentId": "env-1",
                    "env": "ODOO_DB_NAME=cm_preview\nODOO_DB_USER=odoo\n",
                },
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={
                    "ODOO_DB_PASSWORD": "safe",
                    "ODOO_DATA_VOLUME": "cm_data",
                    "ODOO_LOG_VOLUME": "cm_logs",
                    "ODOO_DB_VOLUME": "cm_db",
                    "ODOO_MASTER_PASSWORD": "safe-master",
                    "ODOO_ADMIN_PASSWORD": "safe-admin",
                },
            ),
        ):
            result = evaluate_generic_web_preview_readiness(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewReadinessRequest(product="odoo-tenant-cm"),
                checked_at="2026-05-09T15:00:00Z",
            )

        self.assertEqual(result.readiness_status, "pass")
        self.assertEqual(result.missing_template_env_keys, ())

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

    def test_resolve_preview_url_derives_from_runtime_context_base_url(self) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(
                    update={"slug_template": "pr-{number}"}
                )
            }
        )
        store = _GenericWebPreviewStore(profile)

        with patch(
            "control_plane.workflows.generic_web_preview.control_plane_runtime_environments.load_runtime_environment_definition",
            return_value=control_plane_runtime_environments.build_runtime_environment_definition_from_records(
                tuple(store.list_runtime_environment_records())
            ),
        ):
            preview_url = resolve_generic_web_preview_url(
                control_plane_root=Path("."),
                profile=profile,
                request=GenericWebPreviewRefreshRequest(
                    product="sellyouroutboard",
                    preview_slug="pr-42",
                    preview_url="",
                    image_reference="ghcr.io/cbusillo/sellyouroutboard:sha",
                ),
            )

        self.assertEqual(preview_url, "https://pr-42.syo-preview.example.test")

    def test_resolve_preview_url_fails_closed_when_base_url_missing(self) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(
                    update={"slug_template": "pr-{number}"}
                )
            }
        )
        with patch(
            "control_plane.workflows.generic_web_preview.control_plane_runtime_environments.load_runtime_environment_definition",
            return_value=control_plane_runtime_environments.build_runtime_environment_definition_from_records(
                (
                    RuntimeEnvironmentRecord(
                        scope="context",
                        context="sellyouroutboard-testing",
                        env={"OTHER_KEY": "value"},
                        updated_at="2026-05-10T05:30:00Z",
                        source_label="test",
                    ),
                )
            ),
        ):
            with self.assertRaisesRegex(click.ClickException, "Missing LAUNCHPLANE_PREVIEW_BASE_URL"):
                resolve_generic_web_preview_url(
                    control_plane_root=Path("."),
                    profile=profile,
                    request=GenericWebPreviewRefreshRequest(
                        product="sellyouroutboard",
                        preview_slug="pr-42",
                        preview_url="",
                        image_reference="ghcr.io/cbusillo/sellyouroutboard:sha",
                    ),
                )

    def test_resolve_preview_url_rejects_non_root_base_url(self) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(
                    update={"slug_template": "pr-{number}"}
                )
            }
        )
        with patch(
            "control_plane.workflows.generic_web_preview.control_plane_runtime_environments.load_runtime_environment_definition",
            return_value=control_plane_runtime_environments.build_runtime_environment_definition_from_records(
                (
                    RuntimeEnvironmentRecord(
                        scope="context",
                        context="sellyouroutboard-testing",
                        env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://preview.example/path"},
                        updated_at="2026-05-10T05:30:00Z",
                        source_label="test",
                    ),
                )
            ),
        ):
            with self.assertRaisesRegex(click.ClickException, "root URL"):
                resolve_generic_web_preview_url(
                    control_plane_root=Path("."),
                    profile=profile,
                    request=GenericWebPreviewRefreshRequest(
                        product="sellyouroutboard",
                        preview_slug="pr-42",
                        preview_url="",
                        image_reference="ghcr.io/cbusillo/sellyouroutboard:sha",
                    ),
                )

    def test_resolve_preview_url_keeps_explicit_override_compatible(self) -> None:
        profile = _profile()
        preview_url = resolve_generic_web_preview_url(
            control_plane_root=Path("."),
            profile=profile,
            request=GenericWebPreviewRefreshRequest(
                product="sellyouroutboard",
                preview_slug="pr-42",
                preview_url="https://custom-preview.example.test",
                image_reference="ghcr.io/cbusillo/sellyouroutboard:sha",
            ),
        )

        self.assertEqual(preview_url, "https://custom-preview.example.test")

    def test_wait_for_preview_health_reports_dokploy_dead_host_as_ingress_failure(self) -> None:
        dead_host = HTTPError(
            url="https://preview-42.example.test/api/health",
            code=404,
            msg="Not Found",
            hdrs=Message(),
            fp=BytesIO(b"Dokploy Dead Host"),
        )

        with patch(
            "control_plane.workflows.generic_web_preview.urlopen",
            side_effect=dead_host,
        ):
            with self.assertRaisesRegex(click.ClickException, "Preview ingress"):
                _wait_for_preview_health(
                    preview_url="https://preview-42.example.test",
                    health_path="/api/health",
                    timeout_seconds=30,
                )

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

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
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

        def _fake_fetch(**kwargs: object) -> dict[str, object]:
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
            patch(
                "control_plane.workflows.generic_web_preview._wait_for_preview_health"
            ) as wait_health,
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
        update_application = [
            request for request in requests if request["path"] == "/api/application.update"
        ][0]
        update_application_payload = cast("dict[str, object]", update_application["payload"])
        self.assertEqual(update_application_payload["endpointSpecSwarm"], {"Mode": "dnsrr"})
        save_environment = [
            request for request in requests if request["path"] == "/api/application.saveEnvironment"
        ][0]
        save_environment_payload = cast("dict[str, object]", save_environment["payload"])
        env_text = str(save_environment_payload["env"])
        self.assertIn("SMTP_FROM=hello@example.com", env_text)
        self.assertIn("PUBLIC_URL=https://preview-42.example.test", env_text)
        self.assertIn("PUBLIC_DOMAIN=preview-42.example.test", env_text)
        self.assertNotIn("SMTP_HOST=", env_text)
        trigger_deployment.assert_called_once()
        wait_health.assert_called_once()

    def test_execute_generic_web_preview_refresh_blocks_unsafe_copied_secret_key(self) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(
                    update={
                        "required_template_env_keys": ("SMTP_HOST",),
                        "copied_env_keys": ("SMTP_PASSWORD",),
                    }
                )
            }
        )
        store = _GenericWebPreviewStore(
            profile,
            runtime_key_safety_policies=(_preview_runtime_policy(secret_class="prod_only"),),
            secret_bindings=(_runtime_secret_binding(),),
        )
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
                    "applicationId": "app-testing",
                    "env": "SMTP_HOST=smtp.example\nSMTP_PASSWORD=secret-value\n",
                    "dockerImage": "ghcr.io/cbusillo/sellyouroutboard:old",
                    "username": "github-actions",
                },
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

        self.assertEqual(result.refresh_status, "fail")
        self.assertIn("runtime key-safety gate failed", result.error_message)
        self.assertIn("secret_class_not_allowed", result.error_message)
        dokploy_request.assert_not_called()

    def test_execute_generic_web_preview_refresh_derives_odoo_compose_preview_url(
        self,
    ) -> None:
        store = _GenericWebPreviewStore(_odoo_compose_profile())
        source = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="cm",
                    instance="testing",
                    target_type="compose",
                    target_id="compose-cm-testing",
                    target_name="cm-testing",
                ),
            ),
        )
        env_updates: list[str] = []

        def _fake_fetch(**kwargs: object) -> dict[str, object]:
            self.assertEqual(kwargs["target_type"], "compose")
            self.assertEqual(kwargs["target_id"], "compose-cm-testing")
            return {
                "composeId": "compose-cm-testing",
                "name": "cm-testing",
                "environmentId": "env-1",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": "",
                "env": (
                    "ODOO_DB_NAME=cm_preview\n"
                    "ODOO_DB_USER=odoo\n"
                    "ODOO_DB_PASSWORD=safe\n"
                    "ODOO_DATA_VOLUME=cm_data\n"
                    "ODOO_LOG_VOLUME=cm_logs\n"
                    "ODOO_DB_VOLUME=cm_db\n"
                    "ODOO_MASTER_PASSWORD=safe-master\n"
                    "ODOO_ADMIN_PASSWORD=safe-admin\n"
                    "WEB_BASE_URL=https://testing.cm.example.test\n"
                ),
            }

        def _fake_update_env(**kwargs: object) -> None:
            env_updates.append(str(kwargs["env_text"]))

        domain_requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            domain_requests.append(dict(kwargs))
            path = kwargs["path"]
            if path == "/api/domain.byComposeId":
                return [
                    {
                        "domainId": "domain-testing",
                        "host": "cm-testing.shinycomputers.com",
                        "serviceName": "web",
                        "port": 8069,
                        "domainType": "compose",
                    }
                ]
            if path == "/api/domain.create":
                return {"domainId": "domain-preview"}
            return {}

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
                "control_plane.workflows.generic_web_preview.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_PROJECT_NAME": "cm-pr-28"},
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_runtime_environments.resolve_runtime_context_values",
                return_value={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://cm-preview.shinycomputers.com"},
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.sync_dokploy_compose_raw_source",
            ) as sync_raw_source,
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.update_dokploy_target_env",
                side_effect=_fake_update_env,
            ) as update_env,
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.trigger_deployment",
            ) as trigger_deployment,
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.wait_for_target_deployment",
            ) as wait_for_deployment,
            patch(
                "control_plane.workflows.generic_web_preview._wait_for_preview_health"
            ) as wait_health,
            patch(
                "control_plane.workflows.generic_web_preview.utc_now_timestamp",
                side_effect=["2026-05-09T15:00:00Z", "2026-05-09T15:00:05Z"],
            ),
        ):
            result = execute_generic_web_preview_refresh(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewRefreshRequest(
                    product="odoo-tenant-cm",
                    preview_slug="pr-28",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm:sha",
                    timeout_seconds=240,
                ),
            )

        self.assertEqual(result.refresh_status, "pass")
        self.assertEqual(result.application_id, "compose-cm-testing")
        sync_raw_source.assert_called_once()
        _, sync_kwargs = sync_raw_source.call_args
        self.assertEqual(sync_kwargs["compose_id"], "compose-cm-testing")
        self.assertIn(
            "image: ghcr.io/cbusillo/odoo-tenant-cm:sha",
            str(sync_kwargs["compose_file"]),
        )
        update_env.assert_called_once()
        self.assertEqual(len(env_updates), 1)
        self.assertIn("ODOO_DB_NAME=cm_preview", env_updates[0])
        self.assertIn("ODOO_PROJECT_NAME=cm-pr-28", env_updates[0])
        self.assertIn("ODOO_INSTALL_MODULES=cm_custom,cm_website", env_updates[0])
        self.assertIn(
            "WEB_BASE_URL=https://pr-28.cm-preview.shinycomputers.com", env_updates[0]
        )
        self.assertIn(
            "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/odoo-tenant-cm:sha", env_updates[0]
        )
        self.assertEqual(
            [request["path"] for request in domain_requests],
            ["/api/domain.byComposeId", "/api/domain.create"],
        )
        self.assertEqual(
            domain_requests[1]["payload"],
            {
                "host": "pr-28.cm-preview.shinycomputers.com",
                "path": "/",
                "internalPath": "/",
                "port": 8069,
                "https": True,
                "applicationId": None,
                "certificateType": "none",
                "customCertResolver": None,
                "composeId": "compose-cm-testing",
                "serviceName": "web",
                "domainType": "compose",
                "previewDeploymentId": None,
                "stripPath": False,
            },
        )
        trigger_deployment.assert_called_once_with(
            host="https://dokploy.example",
            token="token",
            target_type="compose",
            target_id="compose-cm-testing",
            no_cache=False,
        )
        wait_for_deployment.assert_called_once()
        _, wait_kwargs = wait_for_deployment.call_args
        self.assertEqual(wait_kwargs["timeout_seconds"], 120)
        wait_health.assert_called_once_with(
            preview_url="https://pr-28.cm-preview.shinycomputers.com",
            health_path="/web/health",
            timeout_seconds=120,
        )

    def test_execute_generic_web_preview_refresh_allows_preview_safe_copied_secret_key(
        self,
    ) -> None:
        profile = _profile().model_copy(
            update={
                "preview": _profile().preview.model_copy(
                    update={
                        "required_template_env_keys": ("SMTP_HOST",),
                        "copied_env_keys": ("SMTP_PASSWORD",),
                    }
                )
            }
        )
        store = _GenericWebPreviewStore(
            profile,
            runtime_key_safety_policies=(_preview_runtime_policy(),),
            secret_bindings=(_runtime_secret_binding(),),
        )
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

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
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

        def _fake_fetch(**kwargs: object) -> dict[str, object]:
            target_id = kwargs["target_id"]
            if target_id == "app-testing":
                return {
                    "applicationId": "app-testing",
                    "environmentId": "env-1",
                    "serverId": "server-1",
                    "env": "SMTP_HOST=smtp.example\nSMTP_PASSWORD=secret-value\n",
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
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.wait_for_target_deployment",
            ),
            patch("control_plane.workflows.generic_web_preview._wait_for_preview_health"),
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
        save_environment = [
            request for request in requests if request["path"] == "/api/application.saveEnvironment"
        ][0]
        save_environment_payload = cast("dict[str, object]", save_environment["payload"])
        self.assertIn("SMTP_PASSWORD=secret-value", str(save_environment_payload["env"]))

    def test_execute_generic_web_preview_destroy_deletes_domains_and_application(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
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

    def test_execute_generic_web_preview_destroy_deletes_odoo_compose_domain(self) -> None:
        store = _GenericWebPreviewStore(_odoo_compose_profile())
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            path = kwargs["path"]
            if path == "/api/domain.byComposeId":
                return [
                    {
                        "domainId": "domain-stable",
                        "host": "cm-testing.shinycomputers.com",
                    },
                    {
                        "domainId": "domain-pr-28",
                        "host": "pr-28.cm-preview.shinycomputers.com",
                    },
                    {
                        "domainId": "domain-pr-29",
                        "host": "pr-29.cm-preview.shinycomputers.com",
                    },
                ]
            return {}

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "composeId": "compose-cm-testing",
                    "name": "cm-testing",
                    "env": "ODOO_DB_NAME=cm\nODOO_DB_USER=odoo\nODOO_DB_PASSWORD=password\nODOO_DATA_VOLUME=data\nODOO_LOG_VOLUME=logs\nODOO_DB_VOLUME=db\nODOO_MASTER_PASSWORD=master\nODOO_ADMIN_PASSWORD=admin\n",
                },
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=DokploySourceOfTruth(
                    schema_version=1,
                    targets=(
                        DokployTargetDefinition(
                            context="cm",
                            instance="testing",
                            target_type="compose",
                            target_id="compose-cm-testing",
                            target_name="cm-testing",
                        ),
                    ),
                ),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.utc_now_timestamp",
                side_effect=["2026-05-09T22:00:00Z", "2026-05-09T22:00:01Z"],
            ),
        ):
            result = execute_generic_web_preview_destroy(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewDestroyRequest(
                    product="odoo-tenant-cm",
                    preview_slug="pr-28",
                    destroy_reason="test",
                ),
            )

        self.assertEqual(result.destroy_status, "pass")
        self.assertEqual(result.provider_type, "compose-domain")
        self.assertEqual(result.application_id, "compose-cm-testing")
        self.assertEqual(result.domain_ids, ("domain-pr-28",))
        delete_requests = [
            request for request in requests if request["path"] == "/api/domain.delete"
        ]
        self.assertEqual(len(delete_requests), 1)
        self.assertEqual(delete_requests[0]["payload"], {"domainId": "domain-pr-28"})


if __name__ == "__main__":
    unittest.main()
