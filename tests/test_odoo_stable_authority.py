from unittest import IsolatedAsyncioTestCase

from control_plane.action_effects import ACTION_DECLARATIONS
from control_plane.http_app import create_launchplane_fastapi_app
from tests.http_app_test_support import (
    _asgi_request,
    _driver_read_policy,
    _local_operator_bearer_config,
    _MissingProductReadStore,
    _RejectingVerifier,
)


class RetiredOdooTestingDeployRouteTests(IsolatedAsyncioTestCase):
    async def test_retired_testing_deploy_route_is_not_served_or_authorized(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_request(app, "POST", "/v1/drivers/odoo/testing-deploy", payload={})

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("odoo_testing_deploy.execute", ACTION_DECLARATIONS)
