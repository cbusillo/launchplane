from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_replacement_resources import ROUTE
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import prepare_held_rebuild
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class TestingReplacementResourceHttpTests(unittest.IsolatedAsyncioTestCase):
    database_url = ""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=self.database_url or f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.store.write_product_profile_record(self.profile)
        for lane in self.profile.lanes:
            target = DokployTargetRecord(
                context=lane.context,
                instance=lane.instance,
                target_name=f"example-{lane.instance}",
                updated_at=self.profile.updated_at,
            )
            target_id = DokployTargetIdRecord(
                context=lane.context,
                instance=lane.instance,
                target_id=f"compose-{lane.instance}",
                updated_at=self.profile.updated_at,
            )
            self.store.write_dokploy_target_record(target)
            self.store.write_dokploy_target_id_record(target_id)
            self.store.write_provider_target_record(
                ProviderTargetRecord.from_dokploy_records(
                    target_record=target,
                    target_id_record=target_id,
                )
            )
        self.context = next(
            lane.context for lane in self.profile.lanes if lane.instance == "testing"
        )
        self.operation = prepare_held_rebuild(
            self.store, self.profile.product, self.context, prepare=False
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    async def post(self, **changes: object) -> Any:
        return await request(
            self.app,
            "POST",
            ROUTE.format(product=self.profile.product),
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "resource-fixture",
            },
            payload={
                "context": self.context,
                "operation_id": self.operation.operation_id,
                "reason": "Prepare new isolated testing storage",
                **changes,
            },
        )

    async def test_plan_preserves_every_original_record_and_makes_no_provider_call(self) -> None:
        before = self.store.read_product_profile_record(self.profile.product)
        target = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        with patch(
            "control_plane.testing_replacement_resources.source.read_dokploy_config"
        ) as provider:
            response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(response.json()["result"]["applied"])
        provider.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), before)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ),
            target,
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.profile.product, self.context, "testing")
        )

    async def test_wrong_operation_production_and_client_cannot_prepare(self) -> None:
        with patch(
            "control_plane.testing_replacement_resources.source.read_dokploy_config"
        ) as provider:
            for changes in (
                {"instance": "prod"},
                {"context": "another-context"},
                {"operation_id": "missing"},
            ):
                with self.subTest(changes=changes):
                    self.assertGreaterEqual((await self.post(**changes)).status_code, 400)
            with scoped_agent_transport():
                self.assertEqual((await self.post()).status_code, 403)
        provider.assert_not_called()
        self.assertIsNone(
            self.store.read_disposable_replacement_preparation(self.operation.operation_id)
        )

    async def test_sqlite_rehearsal_cannot_publish_positive_resource_facts(self) -> None:
        plan = await self.post()
        with patch(
            "control_plane.testing_replacement_resources.source.read_dokploy_config"
        ) as provider:
            applied = await self.post(
                mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
            )
            readback = await self.post(mode="readback")
        self.assertEqual(applied.status_code, 503, applied.text)
        self.assertEqual(readback.status_code, 503, readback.text)
        provider.assert_not_called()
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.profile.product, self.context, "testing")
        )


if __name__ == "__main__":
    unittest.main()
