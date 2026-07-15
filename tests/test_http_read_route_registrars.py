import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.routing import APIRoute

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.openapi_export import write_canonical_openapi
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.auth import _identity, _StubVerifier


class FastApiReadRouteRegistrarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=object,
        )
        self.api_routes = [route for route in self.app.routes if isinstance(route, APIRoute)]

    def test_extracted_routes_preserve_names_operation_ids_schemas_and_ownership(self) -> None:
        expected_routes = (
            (
                "/v1/drivers/odoo/stable-bootstrap/operations/{operation_id}",
                "read_odoo_stable_bootstrap_operation_status",
                "read_odoo_stable_bootstrap_operation_status",
                "OdooStableBootstrapOperationStatusResponse",
                "control_plane.http_routes.drivers",
            ),
            (
                "/v1/drivers/odoo/target-replacement/operations/{operation_id}",
                "read_odoo_stable_target_replacement_operation_status",
                "read_odoo_target_replacement_operation_status",
                "OdooStableTargetReplacementOperationStatusResponse",
                "control_plane.http_routes.drivers",
            ),
            (
                "/v1/artifacts/protected",
                "read_protected_artifacts",
                "read_protected_artifacts",
                "ProtectedArtifactsResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/drivers",
                "read_driver_descriptors",
                "read_driver_descriptors",
                "DriverDescriptorsResponse",
                "control_plane.http_routes.drivers",
            ),
            (
                "/v1/drivers/{driver_id}",
                "read_driver_descriptor",
                "read_driver_descriptor",
                "DriverDescriptorResponse",
                "control_plane.http_routes.drivers",
            ),
            (
                "/v1/contexts/{context}/driver-view",
                "read_driver_context_view",
                "read_driver_context_view",
                "DriverContextViewResponse",
                "control_plane.http_routes.drivers",
            ),
            (
                "/v1/contexts/{context}/instances/{instance}/driver-view",
                "read_driver_instance_view",
                "read_driver_instance_view",
                "DriverContextViewResponse",
                "control_plane.http_routes.drivers",
            ),
            (
                "/v1/dokploy-targets/inspect",
                "read_dokploy_target_inspect",
                "read_dokploy_target_inspect",
                "DokployTargetInspectResponse",
                "control_plane.http_routes.drivers",
            ),
            (
                "/v1/contexts/{context}/instances/{instance}/logs",
                "read_tracked_target_logs",
                "read_tracked_target_logs",
                "TrackedTargetLogsResponse",
                "control_plane.http_routes.drivers",
            ),
            (
                "/v1/deployments/{record_id}",
                "read_deployment_record",
                "read_deployment_record",
                "DeploymentRecordResponse",
                "control_plane.http_routes.operational_records",
            ),
            (
                "/v1/promotions/{record_id}",
                "read_promotion_record",
                "read_promotion_record",
                "PromotionRecordResponse",
                "control_plane.http_routes.operational_records",
            ),
            (
                "/v1/previews/readiness",
                "read_preview_readiness",
                "read_preview_readiness",
                "PreviewReadinessResponse",
                "control_plane.http_routes.preview",
            ),
            (
                "/v1/previews/{preview_id}",
                "read_preview_record",
                "read_preview_record",
                "PreviewRecordResponse",
                "control_plane.http_routes.preview",
            ),
            (
                "/v1/previews/{preview_id}/history",
                "read_preview_history",
                "read_preview_history",
                "PreviewHistoryResponse",
                "control_plane.http_routes.preview",
            ),
            (
                "/v1/inventory/{context}/{instance}",
                "read_environment_inventory",
                "read_environment_inventory",
                "EnvironmentInventoryResponse",
                "control_plane.http_routes.operational_records",
            ),
            (
                "/v1/contexts/{context}/operations/recent",
                "read_recent_operations",
                "read_recent_operations",
                "RecentOperationsResponse",
                "control_plane.http_routes.operational_records",
            ),
            (
                "/v1/repo-product-mapping",
                "read_repo_product_mapping",
                "read_repo_product_mapping",
                "RepoProductMappingResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/agent/context",
                "read_agent_context",
                "read_agent_context",
                "AgentContextResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/work-graph/snapshot",
                "read_work_graph_snapshot",
                "read_work_graph_snapshot",
                "WorkGraphSnapshotResponse",
                "control_plane.http_routes.work_graph",
            ),
            (
                "/v1/work-graph/github/issues",
                "read_work_graph_issue_inbox",
                "read_work_graph_issue_inbox",
                "WorkGraphIssueInboxResponse",
                "control_plane.http_routes.work_graph",
            ),
            (
                "/v1/work-graph/merge-train/admission",
                "read_merge_train_admission",
                "read_merge_train_admission",
                "MergeTrainAdmissionResponse",
                "control_plane.http_routes.merge_train",
            ),
            (
                "/v1/work-graph/merge-train/controller/status",
                "read_merge_train_controller_status",
                "read_merge_train_controller_status",
                "MergeTrainControllerStatusResponse",
                "control_plane.http_routes.merge_train",
            ),
            (
                "/v1/work-graph/merge-train/policy-targets",
                "read_merge_train_policy_targets",
                "read_merge_train_policy_targets",
                "MergeTrainPolicyTargetsResponse",
                "control_plane.http_routes.merge_train",
            ),
            (
                "/v1/every-code/summary",
                "read_every_code_summary",
                "read_every_code_summary",
                "EveryCodeSummaryResponse",
                "control_plane.http_routes.every_code",
            ),
            (
                "/v1/every-code/work-requests",
                "list_every_code_work_requests",
                "list_every_code_work_requests",
                "EveryCodeWorkRequestRecordsResponse",
                "control_plane.http_routes.every_code",
            ),
            (
                "/v1/every-code/work-requests/{request_id}",
                "read_every_code_work_request",
                "read_every_code_work_request",
                "EveryCodeWorkRequestRecordResponse",
                "control_plane.http_routes.every_code",
            ),
            (
                "/v1/every-code/pr-feedback",
                "list_every_code_pr_feedback",
                "list_every_code_pr_feedback",
                "EveryCodePrFeedbackRecordsResponse",
                "control_plane.http_routes.every_code",
            ),
            (
                "/v1/every-code/preview-gates",
                "list_every_code_preview_gates",
                "list_every_code_preview_gates",
                "EveryCodePreviewGateRecordsResponse",
                "control_plane.http_routes.every_code",
            ),
            (
                "/v1/every-code/notification-attempts",
                "list_every_code_notification_attempts",
                "list_every_code_notification_attempts",
                "EveryCodeNotificationAttemptRecordsResponse",
                "control_plane.http_routes.every_code",
            ),
            (
                "/v1/previews/pr-feedback/notification-attempts",
                "list_preview_pr_feedback_notification_attempts",
                "list_preview_pr_feedback_notification_attempts",
                "PreviewPrFeedbackNotificationAttemptRecordsResponse",
                "control_plane.http_routes.preview",
            ),
            (
                "/v1/products",
                "list_product_environment_overviews",
                "list_products",
                "ProductEnvironmentListResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/products/{product}",
                "read_product_overview",
                "read_product",
                "ProductOverviewResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/products/{product}/activity",
                "read_product_activity",
                "read_product_activity",
                "ProductActivityResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/products/{product}/environments",
                "list_product_environments",
                "list_product_environments",
                "ProductEnvironmentsResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/products/{product}/environments/{environment}",
                "read_product_environment",
                "read_product_environment",
                "ProductEnvironmentResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/product-profiles",
                "list_product_profiles",
                "list_product_profiles",
                "ProductProfileListResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/product-profiles/{product}",
                "read_product_profile",
                "read_product_profile",
                "ProductProfileResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/product-profiles/{product}/context-cutover-audit",
                "read_product_context_cutover_audit",
                "read_product_context_cutover_audit",
                "ProductContextCutoverAuditResponse",
                "control_plane.http_routes.products",
            ),
            (
                "/v1/contexts/{context}/secrets",
                "list_context_secret_statuses",
                "list_context_secret_statuses",
                "SecretStatusListResponse",
                "control_plane.http_routes.operational_records",
            ),
            (
                "/v1/contexts/{context}/instances/{instance}/secrets",
                "list_instance_secret_statuses",
                "list_instance_secret_statuses",
                "SecretStatusListResponse",
                "control_plane.http_routes.operational_records",
            ),
            (
                "/v1/secrets/{secret_id}",
                "read_secret_status",
                "read_secret_status",
                "SecretStatusResponse",
                "control_plane.http_routes.operational_records",
            ),
            (
                "/v1/products/{product}/environments/{environment}/config-status",
                "read_product_environment_config_status",
                "read_product_environment_config_status",
                "ProductEnvironmentConfigStatusResponse",
                "control_plane.http_routes.products",
            ),
        )
        expected_keys = {(path, name) for path, name, _, _, _ in expected_routes}
        extracted_routes = [
            route
            for route in self.api_routes
            if route.methods == {"GET"} and (route.path, route.name) in expected_keys
        ]

        self.assertEqual(
            [(route.path, route.name) for route in extracted_routes],
            [(path, name) for path, name, _, _, _ in expected_routes],
        )
        for route, (_, name, operation_id, response_model_name, module_name) in zip(
            extracted_routes,
            expected_routes,
            strict=True,
        ):
            self.assertEqual(route.name, name)
            self.assertEqual(route.operation_id, operation_id)
            self.assertEqual(route.response_model.__name__, response_model_name)
            self.assertEqual(route.endpoint.__module__, module_name)

    def test_registrar_entrypoints_preserve_interleaved_route_order(self) -> None:
        route_keys = [(next(iter(route.methods or set())), route.path) for route in self.api_routes]

        def assert_slice(
            anchor: tuple[str, str],
            expected: list[tuple[str, str]],
        ) -> None:
            start = route_keys.index(anchor)
            self.assertEqual(route_keys[start : start + len(expected)], expected)

        assert_slice(
            ("GET", "/v1/drivers/odoo/target-replacement/operations/{operation_id}"),
            [
                ("GET", "/v1/drivers/odoo/target-replacement/operations/{operation_id}"),
                ("GET", "/v1/artifacts/protected"),
                ("GET", "/v1/drivers"),
            ],
        )
        assert_slice(
            ("GET", "/v1/ingress/route-audits/records/{record_id}"),
            [
                ("GET", "/v1/ingress/route-audits/records/{record_id}"),
                ("GET", "/v1/deployments/{record_id}"),
                ("GET", "/v1/promotions/{record_id}"),
                ("POST", "/v1/every-code/github-webhook"),
            ],
        )
        assert_slice(
            ("GET", "/v1/previews/{preview_id}/history"),
            [
                ("GET", "/v1/previews/{preview_id}/history"),
                ("GET", "/v1/inventory/{context}/{instance}"),
                ("GET", "/v1/contexts/{context}/operations/recent"),
                ("GET", "/v1/repo-product-mapping"),
                ("GET", "/v1/agent/context"),
                ("GET", "/v1/work-graph/snapshot"),
            ],
        )
        assert_slice(
            ("GET", "/v1/previews/pr-feedback/notification-attempts"),
            [
                ("GET", "/v1/previews/pr-feedback/notification-attempts"),
                ("GET", "/v1/products"),
                ("GET", "/v1/products/{product}"),
                ("GET", "/v1/products/{product}/activity"),
                ("GET", "/v1/products/{product}/environments"),
                ("GET", "/v1/products/{product}/environments/{environment}"),
                ("GET", "/v1/product-profiles"),
                ("POST", "/v1/product-profiles"),
            ],
        )
        assert_slice(
            ("POST", "/v1/product-profiles/preview-tls/apply"),
            [
                ("POST", "/v1/product-profiles/preview-tls/apply"),
                ("GET", "/v1/product-profiles/{product}"),
                ("POST", "/v1/product-config/apply"),
            ],
        )
        assert_slice(
            ("POST", "/v1/product-profiles/legacy-context-cleanup/apply"),
            [
                ("POST", "/v1/product-profiles/legacy-context-cleanup/apply"),
                ("GET", "/v1/product-profiles/{product}/context-cutover-audit"),
                ("GET", "/v1/contexts/{context}/secrets"),
                ("GET", "/v1/contexts/{context}/instances/{instance}/secrets"),
                ("GET", "/v1/secrets/{secret_id}"),
                ("POST", "/v1/products/public-ingress-monitor/run-once"),
            ],
        )
        self.assertEqual(
            route_keys[-6:],
            [
                ("GET", "/"),
                ("GET", "/ui"),
                ("GET", "/ui/{path:path}"),
                ("GET", "/v1/products/{product}/environments/{environment}/config-status"),
                (
                    "GET",
                    "/v1/products/{product}/environments/{environment}/promotion-status",
                ),
                (
                    "GET",
                    "/v1/products/{product}/environments/{environment}/promotion/workflow-deliveries/{delivery_id}",
                ),
            ],
        )

    def test_canonical_openapi_bytes_match_checked_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            output_path = Path(temporary_directory_name) / "openapi-canonical.json"
            write_canonical_openapi(output_path)

            self.assertEqual(
                output_path.read_bytes(),
                Path("frontend/generated/openapi-canonical.json").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
