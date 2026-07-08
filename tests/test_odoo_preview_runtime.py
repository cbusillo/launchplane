import unittest
from email.message import Message
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, Literal, cast
from unittest.mock import ANY, patch
from urllib.error import HTTPError, URLError

import click

from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
from control_plane.contracts.odoo_preview_runtime_plan import (
    OdooPreviewProviderCapabilities,
    OdooPreviewRuntimeBindingEvidence,
    OdooPreviewRuntimePlan,
    OdooPreviewRuntimePlanRequest,
    OdooPreviewRuntimeTargetEvidence,
    plan_odoo_preview_runtime,
)
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewDokployApplyRequest,
    OdooPreviewDokployDryRunRequest,
    OdooPreviewDokployEndpointSpec,
    OdooPreviewApplyInputsRequest,
    OdooPreviewWorkflowRequestFacts,
    build_odoo_preview_dokploy_dry_run,
    build_odoo_preview_apply_inputs,
    build_odoo_preview_apply_inputs_workflow_request,
    build_odoo_preview_apply_workflow_request,
    build_odoo_preview_artifact_publish_inputs_workflow_request,
    execute_odoo_preview_dokploy_apply,
    _wait_for_smoke_check,
)


def _capabilities() -> OdooPreviewProviderCapabilities:
    return OdooPreviewProviderCapabilities(
        can_create_compose=True,
        can_update_compose_env=True,
        can_deploy_compose=True,
        can_bind_domain=True,
        can_delete_compose=True,
        can_delete_domain=True,
    )


def _endpoint_spec() -> OdooPreviewDokployEndpointSpec:
    return OdooPreviewDokployEndpointSpec()


def _bindings() -> tuple[OdooPreviewRuntimeBindingEvidence, ...]:
    return (
        OdooPreviewRuntimeBindingEvidence(
            key="WEB_BASE_URL",
            source="runtime_environment",
        ),
        OdooPreviewRuntimeBindingEvidence(
            key="ODOO_ADMIN_PASSWORD",
            source="managed_secret",
        ),
    )


def _target() -> OdooPreviewRuntimeTargetEvidence:
    return OdooPreviewRuntimeTargetEvidence(
        target_id="compose-cm-pr-45",
        target_name="cm-pr-45",
        context="cm-preview",
        instance="pr-45",
        environment_kind="preview",
        domain="pr-45.cm-preview.example.test",
    )


def _runtime_plan(
    *,
    operation: Literal["refresh", "destroy"] = "refresh",
    target: OdooPreviewRuntimeTargetEvidence | None = None,
) -> OdooPreviewRuntimePlan:
    return plan_odoo_preview_runtime(
        request=OdooPreviewRuntimePlanRequest(
            operation=operation,
            product="odoo-tenant-cm",
            repository="cbusillo/odoo-tenant-cm",
            pr_number=45,
            preview_slug="pr-45",
            preview_url="https://pr-45.cm-preview.example.test",
            strategy="isolated_dokploy_compose",
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
            source_git_ref="abc123",
            target=target,
            provider_capabilities=_capabilities(),
            runtime_bindings=_bindings(),
            required_runtime_keys=("WEB_BASE_URL", "ODOO_ADMIN_PASSWORD"),
        )
    )


def _environment_values() -> dict[str, str]:
    return {
        "ODOO_DB_NAME": "cm_pr_45",
        "ODOO_DB_USER": "odoo",
        "ODOO_DB_PASSWORD": "safe-db",
        "ODOO_DATA_VOLUME": "cm_pr_45_data",
        "ODOO_LOG_VOLUME": "cm_pr_45_logs",
        "ODOO_DB_VOLUME": "cm_pr_45_db",
        "ODOO_MASTER_PASSWORD": "safe-master",
        "ODOO_ADMIN_PASSWORD": "safe-admin",
    }


class _OdooApplyInputsStore:
    @staticmethod
    def list_runtime_environment_records(
        *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        records = (
            RuntimeEnvironmentRecord(
                scope="context",
                context="cm-preview",
                env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://cm-preview.example.test"},
                updated_at="2026-05-10T05:30:00Z",
                source_label="test",
            ),
            RuntimeEnvironmentRecord(
                scope="instance",
                context="cm-preview",
                instance="testing",
                env={
                    "ODOO_DB_USER": "odoo",
                    "DOKPLOY_ENVIRONMENT_ID": "env-cm-preview",
                },
                updated_at="2026-05-10T05:31:00Z",
                source_label="test",
            ),
        )
        return tuple(
            record
            for record in records
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )

    @staticmethod
    def list_secret_bindings(
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        bindings = tuple(
            SecretBinding(
                binding_id=f"secret-{key.lower()}-binding",
                secret_id=f"secret-{key.lower()}",
                integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                context="cm-preview",
                instance="testing",
                binding_key=key,
                created_at="2026-05-10T05:32:00Z",
                updated_at="2026-05-10T05:32:00Z",
            )
            for key in (
                "ODOO_DB_PASSWORD",
                "ODOO_MASTER_PASSWORD",
                "ODOO_ADMIN_PASSWORD",
            )
        )
        filtered = tuple(
            binding
            for binding in bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        if limit is not None:
            return filtered[:limit]
        return filtered


def _workflow_facts(
    *, operation: Literal["refresh", "destroy"] = "refresh"
) -> OdooPreviewWorkflowRequestFacts:
    return OdooPreviewWorkflowRequestFacts(
        product="odoo-tenant-cm-website",
        context="cm_website",
        operation=operation,
        pr_number=42,
        preview_slug="pr-42",
        source_git_ref="abc123",
        run_id="456",
        run_attempt="2",
    )


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="odoo-tenant-cm",
        display_name="CM Odoo",
        repository="cbusillo/odoo-tenant-cm",
        driver_id="odoo",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="cm-preview",
                base_url="https://cm-testing.example.test",
                health_url="https://cm-testing.example.test/web/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context="cm-preview",
            app_name_prefix="cm-odoo-preview",
        ),
        updated_at="2026-05-10T05:00:00Z",
        source="test",
    )


def _source_of_truth() -> DokploySourceOfTruth:
    return DokploySourceOfTruth(
        schema_version=1,
        targets=(
            DokployTargetDefinition(
                context="cm-preview",
                instance="testing",
                target_id="compose-template",
                target_name="cm-template",
            ),
        ),
    )


def _preview_project_payload(
    *, compose_id: str = "compose-cm-pr-45", compose_name: str = "cm-odoo-preview-pr-45"
) -> list[dict[str, object]]:
    return [
        {
            "environments": [
                {
                    "composes": [
                        {
                            "composeId": compose_id,
                            "name": compose_name,
                        }
                    ]
                }
            ]
        }
    ]


def _preview_domain_payload(
    *,
    domain_id: str = "domain-cm-pr-45",
    domain_host: str = "pr-45.cm-preview.example.test",
) -> list[dict[str, object]]:
    return [{"domainId": domain_id, "host": domain_host}]


@contextmanager
def _patched_apply_inputs_runtime(*, dokploy_side_effect: object) -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=dokploy_side_effect,
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            )
        )
        stack.enter_context(
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"DOKPLOY_ENVIRONMENT_ID": "env-cm-preview"},
            )
        )
        yield


class OdooPreviewDokployDryRunTests(unittest.TestCase):
    def test_builds_artifact_publish_inputs_workflow_request_shape(self) -> None:
        request = build_odoo_preview_artifact_publish_inputs_workflow_request(
            facts=_workflow_facts()
        )

        self.assertEqual(request.route_path, "/v1/drivers/odoo/artifact-publish-inputs")
        self.assertEqual(
            request.payload,
            {
                "schema_version": 1,
                "product": "odoo-tenant-cm-website",
                "inputs": {
                    "schema_version": 1,
                    "context": "cm_website",
                    "instance": "testing",
                    "pr_number": 42,
                    "isolated": True,
                    "source_git_ref": "abc123",
                },
            },
        )
        self.assertEqual(
            request.idempotency_key,
            "odoo-artifact-publish-inputs:odoo-tenant-cm-website:cm_website:testing:preview-42-run-456-attempt-2",
        )
        self.assertEqual(request.fail_result_paths, ("result.input_status",))
        self.assertEqual(request.response_output_path, "result")

    def test_artifact_publish_inputs_builder_requires_refresh_operation(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires refresh operation"):
            build_odoo_preview_artifact_publish_inputs_workflow_request(
                facts=_workflow_facts(operation="destroy")
            )

    def test_builds_preview_apply_inputs_workflow_request_shape(self) -> None:
        request = build_odoo_preview_apply_inputs_workflow_request(
            facts=_workflow_facts(),
            manifest_file="/tmp/preview-artifact.json",
        )

        self.assertEqual(request.route_path, "/v1/drivers/odoo/preview-apply-inputs")
        self.assertEqual(
            request.payload,
            {
                "schema_version": 1,
                "product": "odoo-tenant-cm-website",
                "inputs": {
                    "schema_version": 1,
                    "product": "odoo-tenant-cm-website",
                    "operation": "refresh",
                    "pr_number": 42,
                    "source_git_ref": "abc123",
                    "source": "odoo-tenant-cm-website-preview-apply-inputs",
                    "timeout_seconds": 300,
                    "no_cache": False,
                },
            },
        )
        self.assertEqual(
            request.payload_json_files, {"inputs.manifest": "/tmp/preview-artifact.json"}
        )
        self.assertEqual(
            request.idempotency_key,
            "odoo-preview-apply-inputs:odoo-tenant-cm-website:pr-42:abc123:inputs-run-456-attempt-2",
        )
        self.assertEqual(request.fail_result_paths, ("result.status",))
        self.assertEqual(request.response_output_path, "result.dry_run_plan")

    def test_apply_inputs_builder_allows_destroy_without_manifest(self) -> None:
        request = build_odoo_preview_apply_inputs_workflow_request(
            facts=_workflow_facts(operation="destroy"),
        )

        self.assertEqual(request.payload_json_files, {})
        inputs = cast(dict[str, object], request.payload["inputs"])
        self.assertIsInstance(inputs, dict)
        self.assertEqual(inputs["operation"], "destroy")
        self.assertNotIn("preview_slug", inputs)
        self.assertEqual(
            request.idempotency_key,
            "odoo-preview-apply-inputs:odoo-tenant-cm-website:pr-42:destroy:inputs-run-456-attempt-2",
        )

    def test_apply_inputs_destroy_without_source_ref_uses_destroy_idempotency_token(self) -> None:
        facts = OdooPreviewWorkflowRequestFacts(
            product="odoo-tenant-cm-website",
            context="cm_website",
            operation="destroy",
            pr_number=42,
            preview_slug="pr-42",
            source_git_ref="",
            run_id="456",
            run_attempt="2",
        )

        request = build_odoo_preview_apply_inputs_workflow_request(facts=facts)

        self.assertEqual(
            request.idempotency_key,
            "odoo-preview-apply-inputs:odoo-tenant-cm-website:pr-42:destroy:inputs-run-456-attempt-2",
        )

    def test_apply_inputs_destroy_uses_stable_idempotency_token_with_source_ref(self) -> None:
        request = build_odoo_preview_apply_inputs_workflow_request(
            facts=_workflow_facts(operation="destroy")
        )

        self.assertEqual(
            request.idempotency_key,
            "odoo-preview-apply-inputs:odoo-tenant-cm-website:pr-42:destroy:inputs-run-456-attempt-2",
        )

    def test_apply_inputs_builder_requires_manifest_for_refresh(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires manifest_file"):
            build_odoo_preview_apply_inputs_workflow_request(facts=_workflow_facts())

    def test_builds_preview_apply_workflow_request_shape(self) -> None:
        request = build_odoo_preview_apply_workflow_request(
            facts=_workflow_facts(),
            dry_run_plan_file="/tmp/dry-run.json",
            manifest_file="/tmp/preview-artifact.json",
        )

        self.assertEqual(request.route_path, "/v1/drivers/odoo/preview-apply")
        self.assertEqual(
            request.payload,
            {
                "schema_version": 1,
                "product": "odoo-tenant-cm-website",
                "apply": {
                    "timeout_seconds": 600,
                    "wait_for_deploy": True,
                },
            },
        )
        self.assertEqual(
            request.payload_json_files,
            {
                "apply.dry_run_plan": "/tmp/dry-run.json",
                "apply.manifest": "/tmp/preview-artifact.json",
            },
        )
        self.assertEqual(
            request.idempotency_key,
            "odoo-preview-apply:odoo-tenant-cm-website:pr-42:refresh:run-456-attempt-2",
        )

    def test_preview_apply_workflow_request_accepts_manual_apply_overrides(self) -> None:
        request = build_odoo_preview_apply_workflow_request(
            facts=_workflow_facts(),
            dry_run_plan_file="/tmp/dry-run.json",
            manifest_file="/tmp/preview-artifact.json",
            wait_for_deploy=False,
            smoke_check=True,
        )

        self.assertEqual(
            request.payload["apply"],
            {"timeout_seconds": 600, "wait_for_deploy": False, "smoke_check": True},
        )

    def test_preview_apply_workflow_request_accepts_refresh_smoke_disabled(self) -> None:
        request = build_odoo_preview_apply_workflow_request(
            facts=_workflow_facts(),
            dry_run_plan_file="/tmp/dry-run.json",
            manifest_file="/tmp/preview-artifact.json",
            smoke_check=False,
        )

        apply = cast(dict[str, object], request.payload["apply"])
        self.assertIsInstance(apply, dict)
        self.assertEqual(apply["smoke_check"], False)

    def test_destroy_preview_apply_request_skips_manifest_and_smoke(self) -> None:
        request = build_odoo_preview_apply_workflow_request(
            facts=_workflow_facts(operation="destroy"),
            dry_run_plan_file="/tmp/destroy-dry-run.json",
        )

        self.assertEqual(
            request.payload_json_files, {"apply.dry_run_plan": "/tmp/destroy-dry-run.json"}
        )
        apply = cast(dict[str, object], request.payload["apply"])
        self.assertIsInstance(apply, dict)
        self.assertNotIn("smoke_check", apply)
        self.assertEqual(
            request.idempotency_key,
            "odoo-preview-apply:odoo-tenant-cm-website:pr-42:destroy:run-456-attempt-2",
        )

    def test_refresh_preview_apply_request_requires_manifest(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires manifest_file"):
            build_odoo_preview_apply_workflow_request(
                facts=_workflow_facts(),
                dry_run_plan_file="/tmp/dry-run.json",
            )

    def test_preview_apply_workflow_request_requires_dry_run_plan_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires dry_run_plan_file"):
            build_odoo_preview_apply_workflow_request(
                facts=_workflow_facts(),
                dry_run_plan_file=" ",
                manifest_file="/tmp/preview-artifact.json",
            )

    def test_builder_accepts_product_profile_preview_slug_token_without_payload_authority(
        self,
    ) -> None:
        facts = OdooPreviewWorkflowRequestFacts(
            product="odoo-tenant-cm",
            context="cm",
            pr_number=42,
            preview_slug="preview-42",
            source_git_ref="abc123",
            run_id="456",
            run_attempt="2",
        )

        request = build_odoo_preview_apply_inputs_workflow_request(
            facts=facts,
            manifest_file="/tmp/preview-artifact.json",
        )

        inputs = cast(dict[str, object], request.payload["inputs"])
        self.assertIsInstance(inputs, dict)
        self.assertNotIn("preview_slug", inputs)
        self.assertIn(":preview-42:", request.idempotency_key)

    def test_apply_inputs_reuses_discovered_preview_compose_for_refresh(self) -> None:
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            path = kwargs["path"]
            if path == "/api/project.all":
                return _preview_project_payload()
            if path == "/api/domain.byComposeId":
                return _preview_domain_payload()
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "ready")
        self.assertIsNotNone(result.runtime_plan.target)
        assert result.runtime_plan.target is not None
        self.assertEqual(result.runtime_plan.target.target_id, "compose-cm-pr-45")
        self.assertEqual(result.dry_run_plan.compose_ref, "compose-cm-pr-45")
        self.assertEqual(result.dry_run_plan.template_compose_id, "compose-template")
        self.assertNotIn(
            "compose_create",
            {operation.name for operation in result.dry_run_plan.operations},
        )
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/project.all", "/api/domain.byComposeId"],
        )

    def test_apply_inputs_builds_destroy_plan_for_discovered_preview_compose(self) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return _preview_project_payload()
            if path == "/api/domain.byComposeId":
                return _preview_domain_payload()
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    operation="destroy",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                ),
            )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.runtime_plan.operation, "destroy")
        self.assertIsNotNone(result.runtime_plan.target)
        assert result.runtime_plan.target is not None
        self.assertEqual(result.runtime_plan.target.target_id, "compose-cm-pr-45")
        self.assertEqual(result.dry_run_plan.compose_ref, "compose-cm-pr-45")
        self.assertEqual(
            [operation.name for operation in result.dry_run_plan.operations],
            ["domain_lookup", "domain_delete", "compose_delete"],
        )

    def test_apply_inputs_builds_destroy_plan_from_compose_search_match(self) -> None:
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            path = kwargs["path"]
            if path == "/api/project.all":
                return [{"environments": [{"composes": []}]}]
            if path == "/api/compose.search":
                return [
                    {
                        "composeId": "compose-cm-pr-45",
                        "name": "cm-odoo-preview-pr-45",
                        "appName": "cm-odoo-preview-pr-45-lu6502",
                    }
                ]
            if path == "/api/domain.byComposeId":
                return _preview_domain_payload()
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    operation="destroy",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                ),
            )

        self.assertEqual(result.status, "ready")
        self.assertIsNotNone(result.runtime_plan.target)
        assert result.runtime_plan.target is not None
        self.assertEqual(result.runtime_plan.target.target_id, "compose-cm-pr-45")
        self.assertEqual(result.dry_run_plan.compose_ref, "compose-cm-pr-45")
        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/api/project.all",
                "/api/compose.search",
                "/api/domain.byComposeId",
            ],
        )
        self.assertEqual(requests[1]["query"], {"q": "cm-odoo-preview-pr-45", "limit": 25})

    def test_apply_inputs_ignores_compose_search_name_mismatch(self) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return [{"environments": [{"composes": []}]}]
            if path == "/api/compose.search":
                return [
                    {
                        "composeId": "compose-cm-pr-450",
                        "name": "cm-odoo-preview-pr-450",
                    }
                ]
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    operation="destroy",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "destroy_target_missing", {blocker.code for blocker in result.runtime_plan.blockers}
        )

    def test_apply_inputs_destroy_blocks_without_matching_preview_compose(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                return_value=[{"environments": [{"composes": []}]}],
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"DOKPLOY_ENVIRONMENT_ID": "env-cm-preview"},
            ),
        ):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    operation="destroy",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "destroy_target_missing", {blocker.code for blocker in result.runtime_plan.blockers}
        )

    def test_apply_inputs_destroy_does_not_require_runtime_environment_id(self) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return _preview_project_payload()
            if path == "/api/domain.byComposeId":
                return _preview_domain_payload()
            raise AssertionError(path)

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_environment_values",
                side_effect=click.ClickException(
                    "Missing DB-backed Launchplane runtime environment records."
                ),
            ),
        ):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    operation="destroy",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "ready")
        self.assertNotIn(
            "environment_id_missing", {blocker.code for blocker in result.dry_run_plan.blockers}
        )
        self.assertEqual(result.dry_run_plan.operation, "destroy")

    def test_apply_inputs_refresh_create_ignores_inventory_connection_failure(self) -> None:
        def _fake_dokploy_request(**_kwargs: object) -> object:
            raise click.ClickException("Dokploy inventory unavailable")

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "ready")
        self.assertIsNone(result.runtime_plan.target)
        self.assertNotIn(
            "runtime_target_discovery_failed",
            {blocker.code for blocker in result.runtime_plan.blockers},
        )

    def test_apply_inputs_blocks_when_preview_url_authority_is_missing(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                side_effect=click.ClickException("Dokploy inventory unavailable"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_context_values",
                side_effect=click.ClickException(
                    "Missing LAUNCHPLANE_PREVIEW_BASE_URL in Launchplane runtime-environment records for cm-preview."
                ),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"DOKPLOY_ENVIRONMENT_ID": "env-cm-preview"},
            ),
        ):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "preview_url_missing", {blocker.code for blocker in result.runtime_plan.blockers}
        )
        self.assertEqual(result.preview_url, "")
        self.assertIn("LAUNCHPLANE_PREVIEW_BASE_URL", result.error_message)

    def test_apply_inputs_blocks_when_runtime_environment_authority_is_missing(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                side_effect=click.ClickException("Dokploy inventory unavailable"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_environment_values",
                side_effect=click.ClickException(
                    "Missing DB-backed Launchplane runtime environment records."
                ),
            ),
        ):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "environment_id_missing", {blocker.code for blocker in result.dry_run_plan.blockers}
        )
        self.assertIn("Missing DB-backed Launchplane runtime environment", result.error_message)

    def test_apply_inputs_reuses_existing_target_when_runtime_environment_authority_is_missing(
        self,
    ) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return _preview_project_payload()
            if path == "/api/domain.byComposeId":
                return _preview_domain_payload()
            raise AssertionError(path)

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_environment_values",
                side_effect=click.ClickException(
                    "Missing DB-backed Launchplane runtime environment records."
                ),
            ),
        ):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "ready")
        self.assertNotIn(
            "environment_id_missing", {blocker.code for blocker in result.dry_run_plan.blockers}
        )
        self.assertTrue(result.dry_run_plan.operations)
        self.assertEqual(result.dry_run_plan.rollback_operations, ())
        self.assertEqual(result.error_message, "")

    def test_apply_inputs_uses_wrapped_compose_search_result_envelope(self) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return [{"environments": [{"composes": []}]}]
            if path == "/api/compose.search":
                compose = {
                    "composeId": "compose-cm-pr-45",
                    "name": "cm-odoo-preview-pr-45",
                }
                return {
                    "items": [],
                    "results": [compose],
                    "data": [compose],
                }
            if path == "/api/domain.byComposeId":
                return _preview_domain_payload()
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "ready")
        self.assertIsNotNone(result.runtime_plan.target)
        target = result.runtime_plan.target
        assert target is not None
        self.assertEqual(target.target_id, "compose-cm-pr-45")
        self.assertEqual(result.error_message, "")

    def test_apply_inputs_refresh_reuses_compose_from_search_fallback(self) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return [{"environments": [{"composes": []}]}]
            if path == "/api/compose.search":
                return {
                    "results": [
                        {
                            "composeId": "compose-cm-pr-45",
                            "name": "cm-odoo-preview-pr-45",
                        }
                    ]
                }
            if path == "/api/domain.byComposeId":
                return _preview_domain_payload()
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "ready")
        self.assertIsNotNone(result.runtime_plan.target)
        target = result.runtime_plan.target
        assert target is not None
        self.assertEqual(target.target_id, "compose-cm-pr-45")
        self.assertNotIn(
            "compose_create", [operation.name for operation in result.dry_run_plan.operations]
        )
        self.assertEqual(result.dry_run_plan.rollback_operations, ())

    def test_apply_inputs_blocks_on_ambiguous_wrapped_compose_search_envelopes(
        self,
    ) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return [{"environments": [{"composes": []}]}]
            if path == "/api/compose.search":
                return {
                    "items": [
                        {
                            "composeId": "compose-cm-pr-45-a",
                            "name": "cm-odoo-preview-pr-45",
                        }
                    ],
                    "results": [
                        {
                            "composeId": "compose-cm-pr-45-b",
                            "name": "cm-odoo-preview-pr-45",
                        }
                    ],
                }
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.runtime_plan.target)
        self.assertIn(
            "runtime_target_discovery_failed",
            {blocker.code for blocker in result.runtime_plan.blockers},
        )
        self.assertIn("ambiguous result envelopes", result.error_message)

    def test_apply_inputs_destroy_blocks_on_ambiguous_wrapped_compose_search_envelopes(
        self,
    ) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return [{"environments": [{"composes": []}]}]
            if path == "/api/compose.search":
                return {
                    "items": [
                        {
                            "composeId": "compose-cm-pr-45-a",
                            "name": "cm-odoo-preview-pr-45",
                        }
                    ],
                    "results": [
                        {
                            "composeId": "compose-cm-pr-45-b",
                            "name": "cm-odoo-preview-pr-45",
                        }
                    ],
                }
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    operation="destroy",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.runtime_plan.target)
        self.assertIn(
            "runtime_target_discovery_failed",
            {blocker.code for blocker in result.runtime_plan.blockers},
        )
        self.assertIn("ambiguous result envelopes", result.error_message)

    def test_apply_inputs_ambiguous_discovery_does_not_add_environment_blocker(
        self,
    ) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return [{"environments": [{"composes": []}]}]
            if path == "/api/compose.search":
                return {
                    "items": [
                        {
                            "composeId": "compose-cm-pr-45-a",
                            "name": "cm-odoo-preview-pr-45",
                        }
                    ],
                    "results": [
                        {
                            "composeId": "compose-cm-pr-45-b",
                            "name": "cm-odoo-preview-pr-45",
                        }
                    ],
                }
            raise AssertionError(path)

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_environment_values",
                side_effect=click.ClickException(
                    "Missing DB-backed Launchplane runtime environment records."
                ),
            ),
        ):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "runtime_target_discovery_failed",
            {blocker.code for blocker in result.runtime_plan.blockers},
        )
        self.assertNotIn(
            "environment_id_missing", {blocker.code for blocker in result.dry_run_plan.blockers}
        )
        self.assertNotIn("Missing DB-backed Launchplane runtime environment", result.error_message)

    def test_apply_inputs_blocks_on_duplicate_discovered_preview_composes(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=(
                    [
                        {
                            "environments": [
                                {
                                    "composes": [
                                        {
                                            "composeId": "compose-cm-pr-45-a",
                                            "name": "cm-odoo-preview-pr-45",
                                        },
                                        {
                                            "composeId": "compose-cm-pr-45-b",
                                            "name": "cm-odoo-preview-pr-45",
                                        },
                                    ]
                                }
                            ]
                        }
                    ],
                    [
                        {
                            "domainId": "domain-cm-pr-45-a",
                            "host": "pr-45.cm-preview.example.test",
                        }
                    ],
                    [
                        {
                            "domainId": "domain-cm-pr-45-b",
                            "host": "pr-45.cm-preview.example.test",
                        }
                    ],
                ),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"DOKPLOY_ENVIRONMENT_ID": "env-cm-preview"},
            ),
        ):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    operation="destroy",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "runtime_target_discovery_failed",
            {blocker.code for blocker in result.runtime_plan.blockers},
        )

    def test_apply_inputs_refresh_blocks_on_duplicate_discovered_preview_composes(
        self,
    ) -> None:
        def _fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return [
                    {
                        "environments": [
                            {
                                "composes": [
                                    {
                                        "composeId": "compose-cm-pr-45-a",
                                        "name": "cm-odoo-preview-pr-45",
                                    },
                                    {
                                        "composeId": "compose-cm-pr-45-b",
                                        "name": "cm-odoo-preview-pr-45",
                                    },
                                ]
                            }
                        ]
                    }
                ]
            if path == "/api/domain.byComposeId":
                return _preview_domain_payload()
            raise AssertionError(path)

        with _patched_apply_inputs_runtime(dokploy_side_effect=_fake_dokploy_request):
            result = build_odoo_preview_apply_inputs(
                control_plane_root=Path("."),
                record_store=_OdooApplyInputsStore(),
                profile=_profile(),
                request=OdooPreviewApplyInputsRequest(
                    product="odoo-tenant-cm",
                    pr_number=45,
                    preview_url="https://pr-45.cm-preview.example.test",
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    source_git_ref="abc123",
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "runtime_target_discovery_failed",
            {blocker.code for blocker in result.runtime_plan.blockers},
        )
        self.assertIn("multiple Odoo preview composes", result.error_message)

    def test_refresh_create_dry_run_blocks_missing_create_and_delete_paths(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=OdooPreviewDokployEndpointSpec(
                    compose_create_path="",
                    compose_delete_path="",
                ),
            )
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("endpoint_path_missing", {blocker.code for blocker in plan.blockers})
        self.assertEqual(plan.operations, ())

    def test_refresh_create_dry_run_blocks_missing_environment_id(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
            )
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("environment_id_missing", {blocker.code for blocker in plan.blockers})

    def test_refresh_create_dry_run_blocks_missing_template_compose_id(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
            )
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("template_compose_id_missing", {blocker.code for blocker in plan.blockers})

    def test_refresh_create_dry_run_renders_ordered_provider_operations(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
                template_compose_id="compose-template",
            )
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.domain_host, "pr-45.cm-preview.example.test")
        self.assertEqual(plan.template_compose_id, "compose-template")
        self.assertEqual(plan.operations[0].path, "/api/compose.create")
        self.assertEqual(
            plan.operations[0].payload_keys,
            ("name", "appName", "environmentId", "serverId", "composeType"),
        )
        self.assertEqual(
            [operation.name for operation in plan.operations],
            [
                "compose_create",
                "compose_update_raw_source",
                "compose_update_env",
                "domain_lookup",
                "domain_create_or_update",
                "compose_deploy",
                "smoke_check",
            ],
        )
        domain_operation = next(
            operation
            for operation in plan.operations
            if operation.name == "domain_create_or_update"
        )
        self.assertEqual(domain_operation.path, "/api/domain.create")
        self.assertEqual(domain_operation.alternate_paths, ("/api/domain.update",))
        self.assertEqual(
            [operation.name for operation in plan.rollback_operations],
            ["domain_delete", "compose_delete"],
        )
        compose_delete = plan.rollback_operations[-1]
        self.assertEqual(compose_delete.path, "/api/compose.delete")
        self.assertIn("deleteVolumes", compose_delete.payload_keys)
        self.assertTrue(
            any(
                operation.secret_payload
                for operation in plan.operations
                if operation.name == "compose_update_env"
            )
        )

    def test_refresh_dry_run_omits_smoke_check_when_disabled(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
                template_compose_id="compose-template",
                smoke_check=False,
            )
        )

        self.assertEqual(plan.status, "ready")
        self.assertNotIn("smoke_check", [operation.name for operation in plan.operations])

    def test_apply_inherits_smoke_check_disabled_from_dry_run_plan(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
                template_compose_id="compose-template",
                smoke_check=False,
            )
        )

        request = OdooPreviewDokployApplyRequest(
            dry_run_plan=dry_run,
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
            environment_values=_environment_values(),
        )

        self.assertFalse(request.smoke_check)

    def test_apply_allows_explicit_smoke_check_override(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
                template_compose_id="compose-template",
                smoke_check=False,
            )
        )

        request = OdooPreviewDokployApplyRequest(
            dry_run_plan=dry_run,
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
            environment_values=_environment_values(),
            smoke_check=True,
        )

        self.assertTrue(request.smoke_check)

    def test_refresh_existing_runtime_does_not_plan_delete_rollback(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(target=_target()),
                endpoint_spec=OdooPreviewDokployEndpointSpec(),
            )
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.compose_ref, "compose-cm-pr-45")
        self.assertEqual(plan.operations[0].name, "compose_update_raw_source")
        self.assertEqual(plan.rollback_operations, ())

    def test_destroy_dry_run_renders_domain_then_compose_delete(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(operation="destroy", target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(
            [operation.name for operation in plan.operations],
            ["domain_lookup", "domain_delete", "compose_delete"],
        )
        self.assertIn("deleteVolumes", plan.operations[-1].payload_keys)
        self.assertEqual(plan.rollback_operations, ())

    def test_blocked_runtime_plan_blocks_provider_dry_run(self) -> None:
        runtime_plan = plan_odoo_preview_runtime(
            request=OdooPreviewRuntimePlanRequest(
                operation="refresh",
                product="odoo-tenant-cm",
                repository="cbusillo/odoo-tenant-cm",
                pr_number=45,
                preview_slug="pr-45",
                strategy="staged_compose_mvp",
                provider_capabilities=_capabilities(),
            )
        )

        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=runtime_plan,
                endpoint_spec=_endpoint_spec(),
            )
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("runtime_plan_not_ready", {blocker.code for blocker in plan.blockers})

    def test_no_cache_refresh_uses_redeploy_endpoint(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(target=_target()),
                endpoint_spec=OdooPreviewDokployEndpointSpec(),
                no_cache=True,
            )
        )

        deploy_operations = tuple(
            operation for operation in plan.operations if operation.name == "compose_deploy"
        )
        self.assertEqual(len(deploy_operations), 1)
        self.assertEqual(deploy_operations[0].path, "/api/compose.redeploy")

    def test_apply_refresh_creates_updates_deploys_and_smokes_compose(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
                template_compose_id="compose-template",
            )
        )
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/compose.create":
                return {"composeId": "compose-cm-pr-45"}
            return {}

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=(
                    {
                        "composeId": "compose-template",
                        "environmentId": "env-cm-preview",
                        "serverId": "server-nonprod",
                    },
                    {
                        "composeId": "compose-cm-pr-45",
                        "environmentId": "env-cm-preview",
                        "serverId": "server-nonprod",
                    },
                ),
            ) as fetch_target_payload,
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.sync_dokploy_compose_raw_source",
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.update_dokploy_target_env",
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.ensure_compose_web_domain_route",
                return_value="domain-cm-pr-45",
            ) as ensure_domain,
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.trigger_deployment",
            ) as trigger_deployment,
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.wait_for_target_deployment",
            ) as wait_deploy,
            patch(
                "control_plane.workflows.odoo_preview_runtime._wait_for_smoke_check",
            ) as smoke_check,
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.compose_id, "compose-cm-pr-45")
        self.assertTrue(result.created_compose)
        self.assertEqual([request["path"] for request in requests], ["/api/compose.create"])
        fetch_targets = [call.kwargs["target_id"] for call in fetch_target_payload.call_args_list]
        self.assertEqual(fetch_targets, ["compose-template", "compose-cm-pr-45"])
        create_payload = requests[0]["payload"]
        self.assertIsInstance(create_payload, dict)
        assert isinstance(create_payload, dict)
        self.assertEqual(create_payload["environmentId"], "env-cm-preview")
        self.assertEqual(create_payload["serverId"], "server-nonprod")
        sync_source.assert_called_once()
        _, sync_kwargs = sync_source.call_args
        self.assertNotIn("ODOO_WEB_HOST_PORT", sync_kwargs["compose_file"])
        self.assertNotIn("ODOO_LONGPOLL_HOST_PORT", sync_kwargs["compose_file"])
        self.assertIn("traefik.enable=true", sync_kwargs["compose_file"])
        update_env.assert_called_once()
        trigger_deployment.assert_called_once_with(
            host="https://dokploy.example",
            token="token",
            target_type="compose",
            target_id="compose-cm-pr-45",
            no_cache=False,
        )
        ensure_domain.assert_called_once_with(
            host="https://dokploy.example",
            token="token",
            compose_id="compose-cm-pr-45",
            domain_host="pr-45.cm-preview.example.test",
            runtime_port=8069,
        )
        wait_deploy.assert_called_once()
        smoke_check.assert_called_once()

    def test_apply_create_blocks_missing_template_compose_id_before_provider_fetch(
        self,
    ) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
                template_compose_id="compose-template",
            )
        ).model_copy(update={"template_compose_id": ""})

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.fetch_dokploy_target_payload",
            ) as fetch_target_payload,
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "fail")
        self.assertIn("template_compose_id", result.error_message)
        fetch_target_payload.assert_not_called()

    def test_apply_blocks_missing_runtime_env_before_provider_calls(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )

        with patch(
            "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config"
        ) as read_dokploy_config:
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values={"ODOO_DB_NAME": "cm_pr_45"},
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn("ODOO_DB_USER", result.error_message)
        read_dokploy_config.assert_not_called()

    def test_apply_existing_refresh_smoke_failure_preserves_existing_domain(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "composeId": "compose-cm-pr-45",
                    "environmentId": "env-cm-preview",
                    "serverId": "server-nonprod",
                },
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.sync_dokploy_compose_raw_source",
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.update_dokploy_target_env",
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.ensure_compose_web_domain_route",
                return_value="domain-cm-pr-45",
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.trigger_deployment",
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.wait_for_target_deployment",
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime._wait_for_smoke_check",
                side_effect=click.ClickException("smoke failed"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime._delete_domain",
            ) as delete_domain,
            patch(
                "control_plane.workflows.odoo_preview_runtime._delete_compose",
            ) as delete_compose,
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.compose_id, "compose-cm-pr-45")
        self.assertEqual(result.domain_id, "domain-cm-pr-45")
        self.assertFalse(result.created_compose)
        self.assertNotIn("smoke_check", [step.name for step in result.steps])
        delete_domain.assert_not_called()
        delete_compose.assert_not_called()

    def test_apply_refresh_blocks_create_without_template_server_id(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
                template_compose_id="compose-template",
            )
        )

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={"composeId": "compose-template", "environmentId": "env-cm-preview"},
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
            ) as dokploy_request,
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "fail")
        self.assertIn("serverId", result.error_message)
        dokploy_request.assert_not_called()

    def test_apply_destroy_deletes_domain_then_compose_with_volumes(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(operation="destroy", target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/domain.byComposeId":
                return [
                    {
                        "domainId": "domain-cm-pr-45",
                        "host": "pr-45.cm-preview.example.test",
                    }
                ]
            return {}

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/domain.byComposeId", "/api/domain.delete", "/api/compose.delete"],
        )
        delete_payload = requests[-1]["payload"]
        self.assertIsInstance(delete_payload, dict)
        assert isinstance(delete_payload, dict)
        self.assertEqual(delete_payload["composeId"], "compose-cm-pr-45")
        self.assertTrue(delete_payload["deleteVolumes"])

    def test_apply_destroy_treats_missing_compose_without_domains_as_clean(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(operation="destroy", target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/domain.byComposeId":
                return []
            if kwargs["path"] == "/api/compose.delete":
                raise click.ClickException(
                    "Dokploy API POST /api/compose.delete failed (404): Not Found"
                )
            return {}

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.error_message, "")
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/domain.byComposeId", "/api/compose.delete"],
        )

    def test_apply_destroy_treats_missing_compose_after_domain_delete_as_clean(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(operation="destroy", target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/domain.byComposeId":
                return [
                    {
                        "domainId": "domain-pr-45",
                        "host": "pr-45.cm-preview.example.test",
                    }
                ]
            if kwargs["path"] == "/api/compose.delete":
                raise click.ClickException(
                    "Dokploy API POST /api/compose.delete failed (404): Not Found"
                )
            return {}

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.domain_id, "domain-pr-45")
        self.assertEqual(result.error_message, "")
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/domain.byComposeId", "/api/domain.delete", "/api/compose.delete"],
        )

    def test_smoke_check_retries_transient_http_404(self) -> None:
        clock = _FakeClock(start=0)
        responses: list[HTTPError | _SmokeResponse] = [
            HTTPError(
                "https://pr-45.cm-preview.example.test/launchplane/health",
                404,
                "Not Found",
                hdrs=Message(),
                fp=None,
            ),
            _SmokeResponse(status=200),
        ]

        def _fake_urlopen(*_args: object, **_kwargs: object) -> object:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.urlopen", side_effect=_fake_urlopen
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.time.monotonic",
                side_effect=clock.monotonic,
            ),
            patch("control_plane.workflows.odoo_preview_runtime.time.sleep") as sleep,
        ):
            sleep.side_effect = clock.sleep
            _wait_for_smoke_check(
                preview_url="https://pr-45.cm-preview.example.test/",
                health_path="/launchplane/health",
                timeout_seconds=10,
            )

        sleep.assert_called_once_with(5)

    def test_smoke_check_uses_status_before_response_closes(self) -> None:
        clock = _FakeClock(start=0)
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.urlopen",
                return_value=_SmokeResponse(status=204, clear_status_on_exit=True),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.time.monotonic",
                side_effect=clock.monotonic,
            ),
            patch("control_plane.workflows.odoo_preview_runtime.time.sleep") as sleep,
        ):
            _wait_for_smoke_check(
                preview_url="https://pr-45.cm-preview.example.test/",
                health_path="/launchplane/health",
                timeout_seconds=10,
            )

        sleep.assert_not_called()

    def test_smoke_check_raises_timeout_when_attempts_are_exhausted(self) -> None:
        clock = _FakeClock(start=0)
        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.urlopen",
                side_effect=URLError("not ready"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.time.monotonic",
                side_effect=clock.monotonic,
            ),
            patch("control_plane.workflows.odoo_preview_runtime.time.sleep") as sleep,
            self.assertRaisesRegex(click.ClickException, "Timed out waiting"),
        ):
            sleep.side_effect = clock.sleep
            _wait_for_smoke_check(
                preview_url="https://pr-45.cm-preview.example.test/",
                health_path="/launchplane/health",
                timeout_seconds=2,
            )


class _FakeClock:
    def __init__(self, *, start: float = 0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _SmokeResponse:
    def __init__(self, *, status: int, clear_status_on_exit: bool = False) -> None:
        self.status = status
        self._clear_status_on_exit = clear_status_on_exit

    def __enter__(self) -> "_SmokeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        if self._clear_status_on_exit:
            self.status = 0
        return None

    @staticmethod
    def read() -> bytes:
        return b"ok"


if __name__ == "__main__":
    unittest.main()
