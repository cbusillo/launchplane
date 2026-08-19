import unittest

from fastapi.routing import APIRoute

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.auth import identity, StubVerifier


class FastApiWriteRouteRegistrarTests(unittest.TestCase):
    def setUp(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=object,
        )
        self.app = app
        self.api_routes = [route for route in app.routes if isinstance(route, APIRoute)]

    def test_generic_web_deploy_recovery_openapi_contract(self) -> None:
        route = self.app.openapi()["paths"]["/v1/admin/generic-web/deploy-recovery/dry-run"]["post"]

        self.assertEqual(route["operationId"], "dry_run_generic_web_deploy_recovery")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/GenericWebDeployRecoveryDryRunRequest",
        )
        self.assertEqual(
            route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/GenericWebDeployRecoveryDryRunResponse",
        )
        idempotency_header = next(
            parameter
            for parameter in route["parameters"]
            if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
        )
        self.assertTrue(idempotency_header["required"])
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(status_code, route["responses"])

    def test_generic_web_deploy_recovery_apply_openapi_contract(self) -> None:
        route = self.app.openapi()["paths"]["/v1/admin/generic-web/deploy-recovery/apply"]["post"]

        self.assertEqual(route["operationId"], "apply_generic_web_deploy_recovery")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/GenericWebDeployRecoveryApplyRequest",
        )
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/GenericWebDeployRecoveryApplyResponse",
        )
        idempotency_header = next(
            parameter
            for parameter in route["parameters"]
            if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
        )
        self.assertTrue(idempotency_header["required"])
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(status_code, route["responses"])

    def test_generic_web_deploy_recovery_provider_evidence_openapi_contract(self) -> None:
        route = self.app.openapi()["paths"][
            "/v1/admin/generic-web/deploy-recovery/provider-evidence"
        ]["post"]

        self.assertEqual(
            route["operationId"],
            "inspect_generic_web_deploy_recovery_provider_evidence",
        )
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/GenericWebDeployRecoveryDryRunRequest",
        )
        self.assertEqual(
            route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/GenericWebDeployRecoveryProviderEvidenceResponse",
        )
        idempotency_header = next(
            parameter
            for parameter in route["parameters"]
            if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
        )
        self.assertTrue(idempotency_header["required"])
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(status_code, route["responses"])

    def test_evidence_write_routes_preserve_contracts_and_ownership(self) -> None:
        expected_routes = (
            ("/v1/evidence/backup-gates", "write_backup_gate_evidence"),
            ("/v1/evidence/promotions", "write_promotion_evidence"),
            ("/v1/evidence/previews/generations", "write_preview_generation_evidence"),
            ("/v1/evidence/previews/destroyed", "write_preview_destroyed_evidence"),
            (
                "/v1/evidence/runner-host-hygiene/audits",
                "write_runner_host_hygiene_audit_evidence",
            ),
            (
                "/v1/evidence/runner-lane-registration/audits",
                "write_runner_lane_registration_audit_evidence",
            ),
            ("/v1/evidence/deployments", "write_deployment_evidence"),
        )
        expected_keys = set(expected_routes)
        extracted_routes = [
            route
            for route in self.api_routes
            if route.methods == {"POST"} and (route.path, route.name) in expected_keys
        ]

        self.assertEqual(
            [(route.path, route.name) for route in extracted_routes],
            list(expected_routes),
        )
        for route, (_, operation_id) in zip(extracted_routes, expected_routes, strict=True):
            self.assertEqual(route.operation_id, operation_id)
            self.assertEqual(route.response_model.__name__, "AcceptedEvidenceResponse")
            self.assertEqual(
                getattr(route.endpoint, "__module__", ""),
                "control_plane.http_routes.evidence",
            )

    def test_evidence_write_routes_preserve_interleaved_route_order(self) -> None:
        route_keys = [(next(iter(route.methods or set())), route.path) for route in self.api_routes]
        expected = [
            ("POST", "/v1/products/public-ingress-monitor/run-once"),
            ("POST", "/v1/evidence/backup-gates"),
            ("POST", "/v1/evidence/promotions"),
            ("POST", "/v1/evidence/previews/generations"),
            ("POST", "/v1/evidence/previews/destroyed"),
            ("POST", "/v1/evidence/runner-host-hygiene/audits"),
            ("POST", "/v1/evidence/runner-lane-registration/audits"),
            ("POST", "/v1/evidence/deployments"),
        ]

        start = route_keys.index(expected[0])
        self.assertEqual(route_keys[start : start + len(expected)], expected)

    def test_generic_web_write_routes_preserve_contracts_and_ownership(self) -> None:
        expected_routes = (
            (
                "/v1/drivers/generic-web/preview-desired-state",
                "apply_generic_web_preview_desired_state",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/drivers/generic-web/preview-inventory",
                "apply_generic_web_preview_inventory",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/drivers/generic-web/preview-readiness",
                "apply_generic_web_preview_readiness",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/drivers/generic-web/preview-refresh",
                "apply_generic_web_preview_refresh",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/drivers/generic-web/preview-destroy",
                "apply_generic_web_preview_destroy",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/drivers/generic-web/deploy",
                "apply_generic_web_deploy",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/admin/generic-web/deploy-recovery/dry-run",
                "dry_run_generic_web_deploy_recovery",
                "GenericWebDeployRecoveryDryRunResponse",
            ),
            (
                "/v1/admin/generic-web/deploy-recovery/apply",
                "apply_generic_web_deploy_recovery",
                "GenericWebDeployRecoveryApplyResponse",
            ),
            (
                "/v1/drivers/generic-web/prod-promotion",
                "apply_generic_web_prod_promotion",
                "GenericWebProdPromotionResponse",
            ),
            (
                "/v1/drivers/generic-web/prod-promotion-workflow",
                "dispatch_generic_web_prod_promotion_workflow",
                "GenericWebPromotionWorkflowResponse",
            ),
            (
                "/v1/drivers/generic-web/stable-verification",
                "apply_generic_web_stable_verification",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/drivers/generic-web/preview-verification",
                "apply_generic_web_preview_verification",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/admin/generic-web/deploy-recovery/provider-evidence",
                "inspect_generic_web_deploy_recovery_provider_evidence",
                "GenericWebDeployRecoveryProviderEvidenceResponse",
            ),
            (
                "/v1/drivers/generic-web/prod-rollback-plan",
                "write_generic_web_rollback_plan",
                "AcceptedEvidenceResponse",
            ),
            (
                "/v1/drivers/generic-web/prod-rollback",
                "write_generic_web_rollback",
                "AcceptedEvidenceResponse",
            ),
        )
        expected_paths = {path for path, _, _ in expected_routes}
        extracted_routes = [
            route
            for route in self.api_routes
            if route.methods == {"POST"} and route.path in expected_paths
        ]

        self.assertEqual(
            [(route.path, route.name) for route in extracted_routes],
            [(path, operation_id) for path, operation_id, _ in expected_routes],
        )
        for route, (_, operation_id, response_model) in zip(
            extracted_routes, expected_routes, strict=True
        ):
            self.assertEqual(route.operation_id, operation_id)
            self.assertEqual(route.response_model.__name__, response_model)
            expected_module = (
                "control_plane.generic_web_deploy_recovery_http"
                if route.path.startswith("/v1/admin/generic-web/deploy-recovery/")
                else "control_plane.http_routes.generic_web"
            )
            self.assertEqual(getattr(route.endpoint, "__module__", ""), expected_module)

    def test_generic_web_write_routes_preserve_interleaved_route_order(self) -> None:
        route_keys = [(next(iter(route.methods or set())), route.path) for route in self.api_routes]
        expected_primary = [
            ("POST", "/v1/previews/lifecycle-sweep"),
            ("POST", "/v1/drivers/generic-web/preview-desired-state"),
            ("POST", "/v1/drivers/generic-web/preview-inventory"),
            ("POST", "/v1/drivers/generic-web/preview-readiness"),
            ("POST", "/v1/drivers/generic-web/preview-refresh"),
            ("POST", "/v1/drivers/generic-web/preview-destroy"),
            ("POST", "/v1/drivers/generic-web/deploy"),
            ("POST", "/v1/admin/generic-web/deploy-recovery/dry-run"),
            ("POST", "/v1/admin/generic-web/deploy-recovery/apply"),
            ("POST", "/v1/drivers/generic-web/prod-promotion"),
            ("POST", "/v1/drivers/generic-web/prod-promotion-workflow"),
            ("POST", "/v1/drivers/generic-web/stable-verification"),
            ("POST", "/v1/drivers/generic-web/preview-verification"),
            ("POST", "/v1/admin/generic-web/deploy-recovery/provider-evidence"),
            ("POST", "/v1/drivers/verireel/testing-deploy"),
        ]
        expected_rollback = [
            ("POST", "/v1/drivers/odoo/prod-backup-gate"),
            ("POST", "/v1/drivers/odoo/prod-backup-verification"),
            ("POST", "/v1/drivers/generic-web/prod-rollback-plan"),
            ("POST", "/v1/drivers/generic-web/prod-rollback"),
            ("POST", "/v1/drivers/odoo/prod-rollback"),
        ]

        primary_start = route_keys.index(expected_primary[0])
        rollback_start = route_keys.index(expected_rollback[0])
        self.assertEqual(
            route_keys[primary_start : primary_start + len(expected_primary)],
            expected_primary,
        )
        self.assertEqual(
            route_keys[rollback_start : rollback_start + len(expected_rollback)],
            expected_rollback,
        )


if __name__ == "__main__":
    unittest.main()
