from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any
from unittest.mock import patch

from httpx2 import Response

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class DirectorPreviewOverrideTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'test.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.before = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-site")
        )
        self.store.write_product_profile_record(self.before)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
        )
        self.headers = {
            "Authorization": "Bearer local-operator-token",
            "If-Match": f'"{product_profile_record_sha256(self.before)}"',
            "Idempotency-Key": "preview-setting-review",
        }
        self.after = self.before.model_dump(mode="json")
        self.after["preview"]["clean_testing_snapshot"] = True

    async def post(self, payload: dict[str, Any] | None = None) -> Response:
        return await request(
            self.app,
            "POST",
            "/v1/product-profiles",
            headers=self.headers,
            payload=payload if payload is not None else self.after,
        )

    async def test_retired_profile_settings_are_refused_without_writing(self) -> None:
        self.after["preview"]["override_env"] = {"COLOR": "preview"}
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    def prepare_testing_storage_update(self) -> None:
        self.after = self.before.model_dump(mode="json")
        lane = next(lane for lane in self.after["lanes"] if lane["instance"] == "testing")
        lane["odoo_testing_drill_storage"] = {
            "database_bytes": 10,
            "filestore_bytes": 20,
            "staging_bytes": 30,
            "reserve_bytes": 40,
        }

    async def test_director_records_testing_capacity_under_the_snapshot_hold(self) -> None:
        self.before.preview.clean_testing_snapshot = True
        self.store.write_product_profile_record(self.before)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(self.before)}"'
        self.prepare_testing_storage_update()
        accepted = await self.post()
        self.assertEqual(accepted.status_code, 202, accepted.text)
        expected = LaunchplaneProductProfileRecord.model_validate(self.after)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), expected)

    async def test_testing_capacity_update_preserves_existing_retention_bindings(self) -> None:
        self.prepare_snapshot_opt_in()
        self.prepare_testing_storage_update()
        budget = self.before.lanes[0].odoo_testing_drill_storage
        assert budget is not None
        self.after["lanes"][0]["odoo_testing_drill_storage"]["filestore_bytes"] += 1
        self.after["lanes"][0]["odoo_testing_drill_storage"]["retained_copy_bindings"] = list(
            budget.retained_copy_bindings
        )
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            self.store.read_product_profile_record(self.before.product),
            LaunchplaneProductProfileRecord.model_validate(self.after),
        )

    async def test_testing_capacity_refuses_retention_and_mixed_authority_changes(self) -> None:
        self.prepare_testing_storage_update()
        variants = []
        for field, value in (
            ("display_name", "Other"),
            ("repository", "example/other"),
            ("production_use", "live"),
        ):
            variants.append({**deepcopy(self.after), field: value})
        for lane_field, lane_value in (
            ("odoo_testing_drill_enabled", True),
            ("base_url", "https://other.example.test"),
        ):
            changed = deepcopy(self.after)
            changed["lanes"][0][lane_field] = lane_value
            variants.append(changed)
        retention = deepcopy(self.after)
        retention["lanes"][0]["odoo_testing_drill_storage"]["retained_copy_bindings"] = ["a" * 64]
        variants.append(retention)
        snapshot = deepcopy(self.after)
        snapshot["preview"]["clean_testing_snapshot"] = True
        variants.append(snapshot)
        for payload in variants:
            with self.subTest(payload=payload):
                refused = await self.post(payload)
                self.assertEqual(refused.status_code, 403, refused.text)
                self.assertEqual(
                    self.store.read_product_profile_record(self.before.product), self.before
                )
        self.prepare_snapshot_opt_in()
        self.prepare_testing_storage_update()
        refused = await self.post()
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_testing_capacity_refuses_inactive_or_non_odoo_products(self) -> None:
        original = self.before
        for change in (
            {
                "lifecycle_state": "retired",
                "preview": original.preview.model_copy(update={"enabled": False}),
            },
            {"driver_id": "generic-web"},
        ):
            with self.subTest(change=change):
                self.before = original.model_copy(update=change)
                self.store.write_product_profile_record(self.before)
                self.headers["If-Match"] = f'"{product_profile_record_sha256(self.before)}"'
                self.prepare_testing_storage_update()
                response = await self.post()
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    self.store.read_product_profile_record(self.before.product), self.before
                )

    def prepare_snapshot_opt_in(self) -> None:
        current = self.before.model_dump(mode="json")
        current["lanes"][0]["odoo_testing_drill_storage"] = {
            "database_bytes": 10,
            "filestore_bytes": 20,
            "staging_bytes": 30,
            "reserve_bytes": 40,
            "retained_copy_bindings": ["a" * 64],
        }
        self.before = LaunchplaneProductProfileRecord.model_validate(current)
        self.store.write_product_profile_record(self.before)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(self.before)}"'
        self.after = self.before.model_dump(mode="json")
        self.after["preview"]["clean_testing_snapshot"] = True

    async def test_director_opts_into_clean_snapshots_with_existing_budget(self) -> None:
        self.prepare_snapshot_opt_in()
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        expected = LaunchplaneProductProfileRecord.model_validate(self.after)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), expected)
        self.assertEqual(expected.lanes, self.before.lanes)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)

    async def test_snapshot_opt_in_cannot_install_a_missing_storage_budget(self) -> None:
        self.prepare_snapshot_opt_in()
        current = self.before.model_dump(mode="json")
        current["lanes"][0].pop("odoo_testing_drill_storage")
        self.before = LaunchplaneProductProfileRecord.model_validate(current)
        self.store.write_product_profile_record(self.before)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(self.before)}"'
        response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_snapshot_opt_in_can_hold_copies_before_budget_qualification(self) -> None:
        self.after = self.before.model_dump(mode="json")
        self.after["preview"]["clean_testing_snapshot"] = True
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        expected = LaunchplaneProductProfileRecord.model_validate(self.after)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), expected)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)

    async def test_snapshot_opt_in_preserves_budget_retention_drill_and_profile_authority(
        self,
    ) -> None:
        self.prepare_snapshot_opt_in()
        variants = []
        for budget_key, budget_value in (("filestore_bytes", 1), ("retained_copy_bindings", [])):
            changed = deepcopy(self.after)
            changed["lanes"][0]["odoo_testing_drill_storage"][budget_key] = budget_value
            variants.append(changed)
        drill = deepcopy(self.after)
        drill["lanes"][0]["odoo_testing_drill_enabled"] = True
        variants.append(drill)
        override = deepcopy(self.after)
        override["preview"]["copied_env_keys"] = ["OTHER_SETTING"]
        variants.append(override)
        for field, value in (("repository", "example/other"), ("display_name", "Other")):
            variants.append({**deepcopy(self.after), field: value})
        for payload in variants:
            with self.subTest(payload=payload):
                response = await self.post(payload)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    self.store.read_product_profile_record(self.before.product), self.before
                )

    async def test_scoped_agent_cannot_opt_in_and_director_cannot_opt_out(self) -> None:
        self.prepare_snapshot_opt_in()
        with scoped_agent_transport():
            refused = await self.post()
        self.assertEqual(refused.status_code, 403, refused.text)
        accepted = await self.post()
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.before = self.store.read_product_profile_record(self.before.product)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(self.before)}"'
        self.headers["Idempotency-Key"] = "snapshot-opt-out"
        self.after = self.before.model_dump(mode="json")
        self.after["preview"]["clean_testing_snapshot"] = False
        refused = await self.post()
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_conditional_review_and_idempotency_are_required(self) -> None:
        for name in ("If-Match", "Idempotency-Key"):
            with self.subTest(header=name):
                old = self.headers.pop(name)
                response = await self.post()
                self.headers[name] = old
                self.assertEqual(response.status_code, 400, response.text)
        self.headers["If-Match"] = '"stale"'
        response = await self.post()
        self.assertEqual(response.status_code, 412, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_scoped_agent_still_gets_denial_before_revision_or_profile_state(self) -> None:
        self.headers["If-Match"] = '"stale"'
        with scoped_agent_transport():
            response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)

    async def test_replay_survives_a_later_unrelated_profile_change(self) -> None:
        accepted = await self.post()
        self.assertEqual(accepted.status_code, 202, accepted.text)
        current = self.store.read_product_profile_record(self.before.product)
        changed = current.model_copy(update={"display_name": "Later name"})
        self.store.write_product_profile_record(changed)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), changed)

    async def test_cas_rejects_a_change_after_authorization(self) -> None:
        write = self.store.write_product_authority_bundle

        def concurrent_write(bundle: ProductAuthorityBundle) -> None:
            self.store.write_product_profile_record(
                self.before.model_copy(update={"display_name": "Concurrent"})
            )
            write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=concurrent_write
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        current = self.store.read_product_profile_record(self.before.product)
        self.assertEqual(current.preview, self.before.preview)
