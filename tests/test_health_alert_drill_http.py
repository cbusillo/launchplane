import unittest
from unittest.mock import patch

from control_plane.health_alert_drill import DRILL_ROUTE
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.http_app_test_support import (
    _local_operator_bearer_config,
    _local_operator_product_environment_read_policy,
)
from tests.support.auth import _identity, _StubVerifier, scoped_agent_test
from tests.support.http import lifespan_client
from tests import test_health_alert_drill as drill_tests


class HealthAlertDrillHttpTests(unittest.IsolatedAsyncioTestCase):
    @scoped_agent_test
    async def test_read_and_default_dry_run_cannot_send_and_scoped_caller_is_denied(self) -> None:
        fixture = drill_tests.HealthAlertDrillTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write_policy()
        policy = _local_operator_product_environment_read_policy(context=fixture.scope.context)
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy,
            record_store_factory=lambda: fixture.store,
            bearer_identity_config=_local_operator_bearer_config(),
        )
        headers = {"Authorization": "Bearer local-operator-token"}
        async with lifespan_client(app) as client:
            read = await client.get(DRILL_ROUTE, params=fixture.scope.model_dump(), headers=headers)
            dry_run = await client.post(
                DRILL_ROUTE, json=fixture.scope.model_dump(), headers=headers
            )
            send = await client.post(
                DRILL_ROUTE,
                json={
                    **fixture.scope.model_dump(),
                    "mode": "send",
                    "window": read.json()["drill"]["window"],
                },
                headers=headers,
            )
            other = await client.get(
                DRILL_ROUTE,
                params={**fixture.scope.model_dump(), "product": "other"},
                headers=headers,
            )
        self.assertEqual(read.status_code, 200)
        self.assertEqual(dry_run.status_code, 200)
        self.assertEqual(dry_run.json()["drill"]["routing"], "enabled")
        self.assertIsNone(dry_run.json()["drill"]["receipt"])
        self.assertEqual(send.status_code, 403)
        self.assertEqual(other.status_code, 403)
        self.assertFalse(fixture.posts)

    @scoped_agent_test
    async def test_send_requires_window_replays_receipt_and_rejects_scope_injection(self) -> None:
        fixture = drill_tests.HealthAlertDrillTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write_policy()
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "local_operators": [
                    {
                        "subjects": ["local-owner-agent"],
                        "token_labels": ["local-owner-read"],
                        "products": [fixture.scope.product],
                        "contexts": [fixture.scope.context, "launchplane"],
                        "actions": ["product_environment.read", "public_ingress_monitor.run_once"],
                    }
                ]
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy,
            record_store_factory=lambda: fixture.store,
            bearer_identity_config=_local_operator_bearer_config(),
        )
        headers = {"Authorization": "Bearer local-operator-token"}
        with patch(
            "control_plane.health_alert_drill.public_ingress_notification_drivers",
            return_value=fixture.drivers,
        ):
            async with lifespan_client(app) as client:
                read = await client.get(
                    DRILL_ROUTE, params=fixture.scope.model_dump(), headers=headers
                )
                payload = {
                    **fixture.scope.model_dump(),
                    "mode": "send",
                    "window": read.json()["drill"]["window"],
                }
                missing_window = await client.post(
                    DRILL_ROUTE,
                    json={**fixture.scope.model_dump(), "mode": "send"},
                    headers=headers,
                )
                injected = await client.post(
                    DRILL_ROUTE,
                    json={**payload, "destination": "https://example.invalid"},
                    headers=headers,
                )
                first = await client.post(DRILL_ROUTE, json=payload, headers=headers)
                second = await client.post(DRILL_ROUTE, json=payload, headers=headers)
                receipt = await client.get(
                    DRILL_ROUTE,
                    params={**fixture.scope.model_dump(), "window": payload["window"]},
                    headers=headers,
                )
                stale = await client.post(
                    DRILL_ROUTE, json={**payload, "window": 0}, headers=headers
                )
                with patch.object(
                    fixture.store,
                    "read_idempotency_record",
                    side_effect=ValueError("private-value"),
                ):
                    unavailable = await client.get(
                        DRILL_ROUTE, params=fixture.scope.model_dump(), headers=headers
                    )
        monitor_only = policy.model_copy(
            update={
                "local_operators": tuple(
                    rule.model_copy(update={"actions": ("public_ingress_monitor.run_once",)})
                    for rule in policy.local_operators
                )
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=monitor_only,
            record_store_factory=lambda: fixture.store,
            bearer_identity_config=_local_operator_bearer_config(),
        )
        async with lifespan_client(app) as client:
            no_lane_read = await client.post(DRILL_ROUTE, json=payload, headers=headers)
        self.assertEqual(missing_window.status_code, 400)
        self.assertEqual(injected.status_code, 400)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["drill"]["receipt"]["status"], "delivered")
        self.assertEqual(second.json()["drill"]["receipt"], first.json()["drill"]["receipt"])
        self.assertEqual(receipt.json()["drill"]["receipt"], first.json()["drill"]["receipt"])
        self.assertEqual(len(fixture.posts), len(fixture.policy.destinations))
        self.assertEqual(stale.status_code, 400)
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("private-value", unavailable.text)
        self.assertEqual(no_lane_read.status_code, 403)
