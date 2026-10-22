from pathlib import Path
import re
from unittest import IsolatedAsyncioTestCase, TestCase

from control_plane.action_effects import ACTION_DECLARATIONS
from control_plane.http_app import create_launchplane_fastapi_app
from tests.http_app_test_support import (
    _asgi_request,
    _driver_read_policy,
    _local_operator_bearer_config,
    _MissingProductReadStore,
    _RejectingVerifier,
)


_REAL_TOPOLOGY_PATTERNS = (
    re.compile(r"^\s*default:\s*(?:sellyouroutboard|odoo-tenant-\S*|cm|opw)\s*$", re.MULTILINE),
    re.compile(r"sellyouroutboard", re.IGNORECASE),
    re.compile(r"shinycomputers\.com"),
    re.compile(r"odoo-tenant-(?:cm|opw)\b"),
    re.compile(r"allowed_targets="),
)


class OdooStableAuthorityTests(TestCase):
    def test_workflows_do_not_embed_real_product_topology(self) -> None:
        offenders: list[str] = []
        for workflow_path in sorted(Path(".github/workflows").glob("*.y*ml")):
            workflow_text = workflow_path.read_text(encoding="utf-8")
            for pattern in _REAL_TOPOLOGY_PATTERNS:
                if pattern.search(workflow_text):
                    offenders.append(f"{workflow_path.as_posix()}: {pattern.pattern}")

        self.assertEqual(offenders, [])


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
