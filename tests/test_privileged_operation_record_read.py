from __future__ import annotations

import base64
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from control_plane.contracts.privileged_operation import (
    ManagedSecretReencryptionPlanInput,
    PrivilegedOperationActor,
)
from control_plane.privileged_operation_service import create_typed_privileged_operation_plan
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.http import lifespan_client
from tests.test_authz_administration_read import _database_app


_READ_ACTIONS = (
    "privileged_secret_operation.read",
    "authz_policy_operation.read",
    "merge_train_policy_operation.read",
    "ordinary_agent_delivery_activation.read",
)
_DESCRIPTORS = (
    "managed-secret-reencryption",
    "managed-authz-policy-set",
    "managed-merge-train-policy-import",
    "ordinary-agent-delivery-activation",
)
_HEADERS = {"Authorization": "Bearer reader-token"}


def _reader_policy(
    *, actions: tuple[str, ...] = _READ_ACTIONS, product: str = "*"
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "local_operators": [
                {
                    "managed_set_id": "operator.record-reads",
                    "managed_rule_id": "contexts",
                    "subjects": ["record-reader"],
                    "token_labels": ["record-reader-label"],
                    "products": [product],
                    "contexts": ["*"],
                    "actions": actions,
                }
            ],
        }
    )


class PrivilegedOperationRecordReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_reads_each_descriptor_and_activation_options(self) -> None:
        with _database_app(_reader_policy()) as (_, _, app):
            async with lifespan_client(app) as client:
                for descriptor in _DESCRIPTORS:
                    with self.subTest(descriptor=descriptor):
                        response = await client.get(
                            "/v1/privileged-operations/plans",
                            params={"descriptor_id": descriptor},
                            headers=_HEADERS,
                        )
                        self.assertEqual(response.status_code, 200, response.text)
                response = await client.get(
                    "/v1/privileged-operations/ordinary-agent-delivery-activation/options",
                    headers=_HEADERS,
                )
                self.assertEqual(response.status_code, 200, response.text)

    async def test_operator_reads_expired_record_without_writes_or_transition_authority(
        self,
    ) -> None:
        with (
            _database_app(_reader_policy()) as (store, _, app),
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode()},
            ),
        ):
            record = create_typed_privileged_operation_plan(
                record_store=store,
                descriptor_id="managed-secret-reencryption",
                actor=PrivilegedOperationActor(
                    identity_type="github_human", github_id=123, login="operator"
                ),
                source_kind="browser_api",
                source_event_id="read-only-expired-plan",
                request=ManagedSecretReencryptionPlanInput(reason="Inspect existing plan"),
                expires_in_seconds=300,
                now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            ).record
            before_events = store.list_privileged_operation_event_records(
                operation_id=record.operation_id
            )
            path = f"/v1/privileged-operations/plans/{record.operation_id}"
            async with lifespan_client(app) as client:
                detail = await client.get(path, headers=_HEADERS)
                review = await client.get(f"{path}/review", headers=_HEADERS)
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["record"]["status"], "planned")
                self.assertEqual(review.status_code, 200, review.text)
                self.assertEqual(
                    review.json()["review"]["lifecycle"]["expiry_state"],
                    "past_expiry_unreconciled",
                )
                for transition in ("approve", "cancel", "revoke"):
                    with self.subTest(transition=transition):
                        response = await client.post(
                            f"{path}/{transition}",
                            headers=_HEADERS,
                            json={"source_event_id": "reader-write", "reason": "Must be denied"},
                        )
                        self.assertEqual(response.status_code, 403, response.text)
                response = await client.post(
                    "/v1/privileged-operations/plans",
                    headers=_HEADERS,
                    json={
                        "source_event_id": "reader-plan",
                        "request": {"reason": "Must be denied"},
                    },
                )
                self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(store.read_privileged_operation_record(record.operation_id), record)
            self.assertEqual(
                store.list_privileged_operation_event_records(operation_id=record.operation_id),
                before_events,
            )

    async def test_missing_or_wrong_product_read_grant_is_denied(self) -> None:
        for policy in (
            _reader_policy(actions=("authz_policy_operation.read",)),
            _reader_policy(product="another-product"),
        ):
            with self.subTest(policy=policy), _database_app(policy) as (_, _, app):
                async with lifespan_client(app) as client:
                    response = await client.get("/v1/privileged-operations/plans", headers=_HEADERS)
                self.assertEqual(response.status_code, 403, response.text)

    async def test_revoked_database_grant_is_denied_despite_runtime_grant(self) -> None:
        with _database_app(
            _reader_policy(actions=("authz_policy_operation.read",)),
            runtime_policy=_reader_policy(),
        ) as (_, _, app):
            async with lifespan_client(app) as client:
                response = await client.get("/v1/privileged-operations/plans", headers=_HEADERS)
            self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
