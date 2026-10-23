from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from httpx2 import Response

from control_plane.artifact_scan import ArtifactScanResult
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
from tests.support.profiles import _odoo_profile_payload_with_prod_lane, product_profile_payload


class DirectorScanActivationTests(unittest.IsolatedAsyncioTestCase):
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
            "Idempotency-Key": "qualified-scan-activation",
        }
        self.after = self.before.model_dump(mode="json")
        self.after["image"]["scan_enabled"] = True
        self.scan = ArtifactScanResult(
            image_reference=self.before.image.repository + "@sha256:" + "a" * 64,
            status="pass",
            reason="clean",
            scanned_at=self.before.updated_at,
        )

    async def post(self, payload: dict[str, Any] | None = None) -> Response:
        return await request(
            self.app,
            "POST",
            "/v1/product-profiles",
            headers=self.headers,
            payload=self.after if payload is None else payload,
        )

    async def test_director_activates_only_a_qualified_scan_and_replays_without_scanning(
        self,
    ) -> None:
        with patch(
            "control_plane.artifact_scan.qualify_product_artifact", return_value=self.scan
        ) as scan:
            response = await self.post()
            self.assertEqual(response.status_code, 202, response.text)
            scan.assert_called_once_with(self.store, product=self.before.product)
            expected = LaunchplaneProductProfileRecord.model_validate(self.after)
            self.assertEqual(self.store.read_product_profile_record(self.before.product), expected)
            changed = expected.model_copy(update={"display_name": "Later name"})
            self.store.write_product_profile_record(changed)
            replay = await self.post()
            self.assertEqual(replay.status_code, 202, replay.text)
            scan.assert_called_once()
            self.assertEqual(self.store.read_product_profile_record(self.before.product), changed)

    async def test_failed_or_unavailable_scans_leave_the_gate_disabled(self) -> None:
        for status in ("fail", "unavailable"):
            with self.subTest(status=status):
                result = self.scan.model_copy(update={"status": status})
                with patch(
                    "control_plane.artifact_scan.qualify_product_artifact", return_value=result
                ):
                    response = await self.post()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], "artifact_scan_refused")
                self.assertEqual(
                    self.store.read_product_profile_record(self.before.product), self.before
                )

    async def test_generic_web_profiles_use_the_same_qualified_activation(self) -> None:
        before = LaunchplaneProductProfileRecord.model_validate(
            product_profile_payload("example-site")
        )
        self.store.write_product_profile_record(before)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(before)}"'
        after = before.model_dump(mode="json")
        after["image"]["scan_enabled"] = True
        with patch("control_plane.artifact_scan.qualify_product_artifact", return_value=self.scan):
            response = await self.post(after)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            self.store.read_product_profile_record(before.product),
            LaunchplaneProductProfileRecord.model_validate(after),
        )

    async def test_inactive_profiles_cannot_activate(self) -> None:
        before = self.before.model_copy(deep=True, update={"lifecycle_state": "retired"})
        before.preview.enabled = False
        self.store.write_product_profile_record(before)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(before)}"'
        after = before.model_dump(mode="json")
        after["image"]["scan_enabled"] = True
        with patch("control_plane.artifact_scan.qualify_product_artifact") as scan:
            response = await self.post(after)
        self.assertEqual(response.status_code, 403, response.text)
        scan.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(before.product), before)

    async def test_unrelated_profile_edits_are_not_activation_authority(self) -> None:
        variants = []
        for field, value in (("display_name", "Other"), ("repository", "example/other")):
            variants.append({**deepcopy(self.after), field: value})
        image = deepcopy(self.after)
        image["image"]["repository"] = "ghcr.io/example/other"
        variants.append(image)
        lane = deepcopy(self.after)
        lane["lanes"][0]["base_url"] = "https://other.example.test"
        variants.append(lane)
        preview = deepcopy(self.after)
        preview["preview"]["override_env"]["EXTRA_SETTING"] = "extra"
        variants.append(preview)
        with patch("control_plane.artifact_scan.qualify_product_artifact") as scan:
            for payload in variants:
                response = await self.post(payload)
                self.assertEqual(response.status_code, 403, response.text)
            scan.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_scoped_agents_cannot_activate_without_existing_write_authority(self) -> None:
        with (
            scoped_agent_transport(),
            patch("control_plane.artifact_scan.qualify_product_artifact") as scan,
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)
        scan.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_revision_and_idempotency_are_required_before_scanning(self) -> None:
        with patch("control_plane.artifact_scan.qualify_product_artifact") as scan:
            for name in ("If-Match", "Idempotency-Key"):
                old = self.headers.pop(name)
                response = await self.post()
                self.headers[name] = old
                self.assertEqual(response.status_code, 400, response.text)
            self.headers["If-Match"] = '"stale"'
            response = await self.post()
            self.assertEqual(response.status_code, 412, response.text)
            scan.assert_not_called()

    async def test_disabling_or_creating_a_gate_is_not_activation(self) -> None:
        enabled = LaunchplaneProductProfileRecord.model_validate(self.after)
        self.store.write_product_profile_record(enabled)
        self.headers["If-Match"] = f'"{product_profile_record_sha256(enabled)}"'
        with patch("control_plane.artifact_scan.qualify_product_artifact") as scan:
            response = await self.post(self.before.model_dump(mode="json"))
            self.assertEqual(response.status_code, 403, response.text)
            missing = deepcopy(self.after)
            missing["product"] = "absent-product"
            response = await self.post(missing)
            self.assertNotEqual(response.status_code, 202, response.text)
            scan.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.before.product), enabled)

    async def test_a_profile_edit_during_qualification_preserves_the_concurrent_record(
        self,
    ) -> None:
        concurrent = self.before.model_copy(update={"display_name": "Concurrent"})

        def qualify(*args: object, **kwargs: object) -> ArtifactScanResult:
            self.store.write_product_profile_record(concurrent)
            return self.scan

        with patch("control_plane.artifact_scan.qualify_product_artifact", side_effect=qualify):
            response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), concurrent)
