import unittest

from fastapi.routing import APIRoute

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.auth import _identity, _StubVerifier


class FastApiWriteRouteRegistrarTests(unittest.TestCase):
    def setUp(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=object,
        )
        self.api_routes = [route for route in app.routes if isinstance(route, APIRoute)]

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
            self.assertEqual(route.endpoint.__module__, "control_plane.http_routes.evidence")

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


if __name__ == "__main__":
    unittest.main()
