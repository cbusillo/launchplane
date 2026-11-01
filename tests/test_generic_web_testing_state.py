from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest

from httpx2 import Response

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.support.profiles import _generic_site_profile_payload


class GenericWebTestingStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.before = LaunchplaneProductProfileRecord.model_validate(
            _generic_site_profile_payload()
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
            "Idempotency-Key": "testing-state-review",
        }

    def payload(self, state: dict[str, str] | None) -> dict[str, Any]:
        result = self.before.model_dump(mode="json")
        lane = next(item for item in result["lanes"] if item["instance"] == "testing")
        lane["generic_web_testing_state"] = state
        return result

    async def post(self, payload: dict[str, Any]) -> Response:
        return await request(
            self.app, "POST", "/v1/product-profiles", headers=self.headers, payload=payload
        )

    async def test_director_records_only_the_reviewed_testing_state_and_replays(self) -> None:
        for state in ({"kind": "none"}, {"kind": "volume", "container_path": "/var/lib/app"}):
            with self.subTest(state=state):
                self.headers["If-Match"] = f'"{product_profile_record_sha256(self.before)}"'
                self.headers["Idempotency-Key"] = "testing-state-" + state["kind"]
                payload = self.payload(state)
                response = await self.post(payload)
                self.assertEqual(response.status_code, 202, response.text)
                expected = LaunchplaneProductProfileRecord.model_validate(payload)
                self.assertEqual(
                    self.store.read_product_profile_record(self.before.product), expected
                )
                replay = await self.post(payload)
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertTrue(replay.json()["replayed"])
                self.before = expected

    async def test_mixed_edits_removal_and_postgresql_never_change_authority(self) -> None:
        payload = self.payload({"kind": "none"})
        variants = []
        for field, value in (("display_name", "Other"), ("repository", "another/product")):
            variants.append({**deepcopy(payload), field: value})
        changed = deepcopy(payload)
        changed["lanes"][1]["base_url"] = "https://other.example.test"
        variants.append(changed)
        variants.extend((self.payload(None), self.payload({"kind": "postgresql"})))
        for variant in variants:
            with self.subTest(variant=variant):
                response = await self.post(variant)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    self.store.read_product_profile_record(self.before.product), self.before
                )
        self.before = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.before)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(self.before)}"'
        self.assertEqual((await self.post(self.payload(None))).status_code, 403)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_conditional_review_scoped_identity_and_current_profile_are_required(
        self,
    ) -> None:
        payload = self.payload({"kind": "none"})
        for missing in ("If-Match", "Idempotency-Key"):
            with self.subTest(missing=missing):
                value = self.headers.pop(missing)
                self.assertEqual((await self.post(payload)).status_code, 400)
                self.headers[missing] = value
        with scoped_agent_transport():
            self.assertEqual((await self.post(payload)).status_code, 403)
        current = self.before.model_copy(update={"display_name": "Concurrent change"})
        self.store.write_product_profile_record(current)
        self.assertEqual((await self.post(payload)).status_code, 412)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), current)
