from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.read_coverage import ReadCoverage
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.monitoring_authority_history import MonitoringAuthorityHistory
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _product_profile_read_policy
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import request
from tests.support.stores import _sqlite_database_url


class MonitoringAuthorityHistoryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_is_authorized_before_reading_even_without_a_current_profile(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(temporary) / "records.sqlite")
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="retired-site"),
            record_store_factory=lambda: store,
        )
        history = MonitoringAuthorityHistory(
            product="retired-site",
            read_at=datetime.now(timezone.utc),
            cluster_id="fixture",
            settled_before_transaction=1,
            epoch=None,
            confirmed_since=None,
            capture_errors=(),
            coverage=ReadCoverage(
                population="fixture", source="complete", status="scoped", authorization="scoped"
            ),
            cursor=None,
            next_cursor=None,
            revisions=(),
        )
        with patch.object(
            store, "read_monitoring_authority_history", return_value=history
        ) as reader:
            for query in ("", "?product=another-site"):
                denied = await request(
                    app,
                    "GET",
                    "/v1/monitoring-authority-history" + query,
                    headers={"Authorization": "Bearer valid-token"},
                )
                self.assertEqual(denied.status_code, 403, denied.text)
                reader.assert_not_called()
            allowed = await request(
                app,
                "GET",
                "/v1/monitoring-authority-history?product=retired-site",
                headers={"Authorization": "Bearer valid-token"},
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertEqual(allowed.headers["Cache-Control"], "no-store")
            reader.assert_called_once_with(product="retired-site", limit=200, cursor=None)
        unavailable = await request(
            app,
            "GET",
            "/v1/monitoring-authority-history?product=retired-site",
            headers={"Authorization": "Bearer valid-token"},
        )
        self.assertEqual(unavailable.status_code, 503, unavailable.text)

    async def test_existing_service_read_can_read_the_whole_roster(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(temporary) / "records.sqlite")
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="launchplane"),
            record_store_factory=lambda: store,
        )
        response = await request(
            app,
            "GET",
            "/v1/monitoring-authority-history",
            headers={"Authorization": "Bearer valid-token"},
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "history_unavailable")
