from __future__ import annotations

from typing import cast
import unittest
from unittest.mock import Mock

from fastapi import FastAPI
from pydantic import BaseModel

from control_plane.http_routes.ordinary_agent import (
    OrdinaryAgentRouteDependencies,
    register_ordinary_agent_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.ordinary_agent_authentication import (
    OrdinaryAgentToken,
    generate_receiver_claim_secret,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client


class OrdinaryAgentPrivateClaimHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = Mock(spec=PostgresRecordStore)
        self.legacy_identity = Mock(side_effect=AssertionError("Legacy auth must not run"))
        self.app = FastAPI()
        register_ordinary_agent_routes(
            cast(ApiRouteRegistrar, self.app),
            dependencies=OrdinaryAgentRouteDependencies(
                common=ReadRouteDependencies(
                    read_identity=self.legacy_identity,
                    get_record_store=lambda: self.store,
                    next_trace_id=lambda: "private-claim-test",
                    authorization_allows=Mock(),
                    http_error=Mock(),
                    error_response_model=BaseModel,
                )
            ),
        )
        self.path = "/v1/agent/ordinary-agent-enrollments/test-operation/claim"

    async def test_private_delivery_uses_only_receiver_proof_and_does_not_echo_failed_input(
        self,
    ) -> None:
        claim = generate_receiver_claim_secret()
        private_token = "test-private-issued-credential"
        self.store.claim_ordinary_agent_credential.return_value = OrdinaryAgentToken(private_token)
        async with lifespan_client(self.app) as client:
            denied = await client.post(
                self.path, headers={"Authorization": "Bearer malformed-private-claim"}
            )
            self.assertEqual(denied.status_code, 401)
            self.assertNotIn("malformed-private-claim", denied.text)
            self.store.claim_ordinary_agent_credential.assert_not_called()

            delivered = await client.post(
                self.path, headers={"Authorization": f"Bearer {claim.value}"}
            )
            self.assertEqual(delivered.status_code, 200)
            self.assertEqual(delivered.json()["credential"], private_token)
            self.assertEqual(delivered.headers["cache-control"], "no-store")
            self.assertNotIn(claim.value, delivered.text)
            self.store.claim_ordinary_agent_credential.assert_called_once_with(
                operation_id="test-operation", claim_secret=claim
            )
            self.legacy_identity.assert_not_called()

    async def test_unknown_delivery_and_private_failure_never_escape_through_errors(self) -> None:
        claim = generate_receiver_claim_secret()
        async with lifespan_client(self.app) as client:
            self.store.claim_ordinary_agent_credential.return_value = None
            denied = await client.post(
                self.path, headers={"Authorization": f"Bearer {claim.value}"}
            )
            self.assertEqual(denied.status_code, 401)
            self.store.claim_ordinary_agent_credential.side_effect = RuntimeError(
                f"private persistence context {claim.value} secret-key-material"
            )
            failed = await client.post(
                self.path, headers={"Authorization": f"Bearer {claim.value}"}
            )
            self.assertEqual(failed.status_code, 503)
            self.assertNotIn(claim.value, failed.text)
            self.assertNotIn("secret-key-material", failed.text)
            self.assertEqual(failed.headers["cache-control"], "no-store")

            rejected_body = await client.post(
                self.path,
                headers={"Authorization": f"Bearer {claim.value}"},
                json={"misplaced_secret": claim.value},
            )
            self.assertEqual(rejected_body.status_code, 400)
            self.assertNotIn(claim.value, rejected_body.text)
