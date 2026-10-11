from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.health_monitor_scheduler import HealthMonitorScheduler
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _driver_read_policy,
    _local_operator_bearer_config,
    _local_operator_launchplane_service_read_policy,
    _MissingProductReadStore,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url


class HealthMonitorStatusHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipts_are_bounded_and_do_not_expose_stored_payload_or_keys(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            self.addCleanup(store.close)
            store.ensure_schema()
            now = datetime.now(timezone.utc).timestamp()
            scheduler = HealthMonitorScheduler(store, clock=lambda: now, run_monitor=lambda: None)
            scheduler._write_completion("enabled", now)
            scheduler.run_once()
            record = scheduler._read("enabled")
            assert record is not None
            store.write_idempotency_record(
                record.model_copy(
                    update={
                        "response_payload": {"private": "fixture-secret-value"},
                        "lease_owner": "fixture-private-lease",
                    }
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=_local_operator_bearer_config(),
            )
            async with lifespan_client(app) as client:
                response = await client.get(
                    "/v1/service/health-monitor/status?slots=1",
                    headers={"Authorization": "Bearer local-operator-token"},
                )
                oversized = await client.get(
                    "/v1/service/health-monitor/status?slots=49",
                    headers={"Authorization": "Bearer local-operator-token"},
                )
            self.assertEqual(response.status_code, 200)
            monitor = response.json()["monitor"]
            self.assertEqual(len(monitor["slots"]), 1)
            self.assertIsNotNone(monitor["slots"][0]["completion"])
            self.assertNotIn("fixture-secret-value", response.text)
            self.assertNotIn("fixture-private-lease", response.text)
            self.assertNotIn("idempotency_key", response.text)
            self.assertEqual(oversized.status_code, 400)

    async def test_requires_authentication_service_read_and_database_storage(self) -> None:
        for authorized, token, expected in (
            (True, False, 401),
            (False, True, 403),
            (True, True, 503),
        ):
            with self.subTest(authorized=authorized, token=token):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=(
                        _local_operator_launchplane_service_read_policy()
                        if authorized
                        else _driver_read_policy()
                    ),
                    record_store_factory=lambda: _MissingProductReadStore(),
                    bearer_identity_config=_local_operator_bearer_config(),
                )
                async with lifespan_client(app) as client:
                    response = await client.get(
                        "/v1/service/health-monitor/status",
                        headers={"Authorization": "Bearer local-operator-token"} if token else {},
                    )
                self.assertEqual(response.status_code, expected)
