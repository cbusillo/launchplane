"""Real PostgreSQL override, custody and original-key transaction behavior."""

from tests import test_testing_override_sanitization as scenarios
from tests.test_postgres_integration import _head_postgres_database
from tests.support.http import request
from control_plane.testing_override_sanitization import ROUTE


class RealPostgresTestingOverrideSanitizationTests(scenarios.TestingOverrideSanitizationTests):
    def setUp(self) -> None:
        self.database_url = self.enterContext(_head_postgres_database())
        super().setUp()


class RealPostgresTestingOverrideSanitizationHttpTests(
    scenarios.TestingOverrideSanitizationHttpTests
):
    def setUp(self) -> None:
        from control_plane.contracts.authz_policy_record import (
            LaunchplaneAuthzPolicyRecord,
            authz_policy_sha256,
            build_authz_policy_record_id,
        )
        from control_plane.service_auth import LaunchplaneAuthzPolicy

        self.database_url = self.enterContext(_head_postgres_database())
        super().setUp()
        policy = LaunchplaneAuthzPolicy()
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=1, policy_sha256=authz_policy_sha256(policy)
                ),
                revision=1,
                status="active",
                source="test:override-sanitization",
                updated_at=self.profile.updated_at,
                policy=policy,
            )
        )

    async def test_review_apply_original_key_replay_and_independent_readback(self) -> None:
        path = ROUTE.format(product=self.profile.product)
        headers = {"Authorization": "Bearer local-operator-token"}
        review = await request(
            self.fixture.app, "POST", path, headers=headers, payload=self.sanitize_payload
        )
        self.assertEqual(review.status_code, 202, review.text)
        body = {
            **self.sanitize_payload,
            "mode": "apply",
            "reviewed_plan_sha256": review.json()["result"]["plan_sha256"],
        }
        headers["Idempotency-Key"] = "original-sanitization-key"
        applied = await request(self.fixture.app, "POST", path, headers=headers, payload=body)
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertTrue(applied.json()["result"]["applied"])
        replay = await request(self.fixture.app, "POST", path, headers=headers, payload=body)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["result"], applied.json()["result"])
        readback = await request(
            self.fixture.app, "POST", path, headers=headers, payload={**body, "mode": "readback"}
        )
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertEqual(readback.json()["result"], applied.json()["result"])
        changed = await request(
            self.fixture.app,
            "POST",
            path,
            headers=headers,
            payload={**body, "reason": "A changed request cannot reuse the original key"},
        )
        self.assertEqual(changed.status_code, 409, changed.text)
        for item in self.original_overrides.addon_settings:
            if item.value.secret_binding_id:
                self.assertNotIn(
                    item.value.secret_binding_id, applied.text + replay.text + readback.text
                )

    async def test_preflight_drift_returning_to_review_cannot_commit_another_receipt(self) -> None:
        from unittest.mock import patch

        from control_plane.contracts.odoo_instance_override_record import (
            OdooConfigParameterOverride,
            OdooOverrideValue,
        )
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.testing_override_sanitization import (
            TestingOverrideSanitizationRequest,
            TestingOverrideSanitizationSnapshot,
            read_sanitization_audit,
            sanitization_snapshot,
        )

        path = ROUTE.format(product=self.profile.product)
        headers = {"Authorization": "Bearer local-operator-token"}
        review = await request(
            self.fixture.app, "POST", path, headers=headers, payload=self.sanitize_payload
        )
        self.assertEqual(review.status_code, 202, review.text)
        body = TestingOverrideSanitizationRequest.model_validate(
            {
                **self.sanitize_payload,
                "mode": "apply",
                "reviewed_plan_sha256": review.json()["result"]["plan_sha256"],
            }
        )
        self.store.write_odoo_instance_override_record(
            self.original_overrides.model_copy(
                update={
                    "config_parameters": (
                        *self.original_overrides.config_parameters,
                        OdooConfigParameterOverride(
                            key="fixture.changed",
                            value=OdooOverrideValue(source="literal", value=True),
                        ),
                    )
                }
            )
        )

        def snapshot_then_restore(
            store: PostgresRecordStore,
            product: str,
            request: TestingOverrideSanitizationRequest,
        ) -> TestingOverrideSanitizationSnapshot:
            observed = sanitization_snapshot(store, product, request)
            self.store.write_odoo_instance_override_record(self.original_overrides)
            return observed

        key = "review-aba-sanitization-key"
        with patch(
            "control_plane.http_routes.testing_override_sanitization.sanitization_snapshot",
            side_effect=snapshot_then_restore,
        ):
            response = await request(
                self.fixture.app,
                "POST",
                path,
                headers={**headers, "Idempotency-Key": key},
                payload=body.model_dump(mode="json"),
            )
        audit = read_sanitization_audit(self.store, self.profile.product, body)
        if response.status_code == 409:
            self.assertEqual(
                response.json()["error"]["code"], "testing_override_sanitization_unverified"
            )
            self.assertIsNone(audit)
            self.assertEqual(
                self.store.read_odoo_instance_override_record(
                    context_name=self.fixture.context, instance_name="testing"
                ),
                self.original_overrides,
            )
        else:
            self.assertEqual(response.status_code, 202, response.text)
            assert audit is not None
            self.assertEqual(
                response.json()["result"],
                audit.plan.model_copy(update={"applied": True}).model_dump(mode="json"),
            )
