from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click import ClickException

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_get, _record_read_policy
from tests.support.auth import identity, StubVerifier
from tests.support.stores import sqlite_database_url


class FastApiDokployRuntimeEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_dokploy_target_inspect_returns_runtime_proof(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with (
                patch(
                    "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_inspect.dokploy_api.fetch_dokploy_target_payload",
                    return_value={
                        "id": "compose-123",
                        "appName": "launchplane",
                        "serverId": "server-123",
                    },
                ),
                patch(
                    "control_plane.dokploy_target_inspect.dokploy_runtime_evidence.fetch_compose_service_runtime",
                    return_value={
                        "state": "running",
                        "status": "Up 5 minutes",
                        "running": True,
                        "configured_image": f"ghcr.io/example/launchplane@sha256:{'a' * 64}",
                        "image_id": f"sha256:{'b' * 64}",
                        "immutable_image_reference": (
                            f"ghcr.io/example/launchplane@sha256:{'a' * 64}"
                        ),
                        "image_reference_immutable": True,
                    },
                ),
                patch(
                    "control_plane.dokploy_target_inspect.dokploy_api.fetch_dokploy_compose_logs",
                    return_value=(
                        '{"event":"privileged_operation_worker_poll_succeeded",'
                        '"processed":0,"statuses":[]}',
                    ),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=StubVerifier(identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.inspect",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: store,
                    control_plane_root_path=root,
                )
                response = await _asgi_get(
                    app,
                    "/v1/dokploy-targets/inspect"
                    "?target_type=compose"
                    "&target_id=compose-123"
                    "&service=launchplane-privileged-operation-workers"
                    "&event=privileged_operation_worker_poll_succeeded"
                    f"&expected_image=ghcr.io/example/launchplane@sha256:{'a' * 64}",
                    headers={"Authorization": "Bearer valid-token"},
                )
            store.close()

        self.assertEqual(response.status_code, 200)
        runtime_evidence = response.json()["inspect"]["runtime_evidence"]
        self.assertTrue(runtime_evidence["proof_ready"])
        self.assertTrue(runtime_evidence["structured_event"]["observed"])

    async def test_dokploy_target_inspect_redacts_provider_runtime_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with (
                patch(
                    "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_inspect.dokploy_api.fetch_dokploy_target_payload",
                    return_value={"id": "compose-123", "appName": "launchplane"},
                ),
                patch(
                    "control_plane.dokploy_target_inspect.dokploy_runtime_evidence.fetch_compose_service_runtime",
                    side_effect=ClickException("provider TOKEN=secret"),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=StubVerifier(identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.inspect",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: store,
                    control_plane_root_path=root,
                )
                response = await _asgi_get(
                    app,
                    "/v1/dokploy-targets/inspect"
                    "?target_type=compose&target_id=compose-123&service=worker"
                    "&event=privileged_operation_worker_poll_succeeded"
                    f"&expected_image=example@sha256:{'a' * 64}",
                    headers={"Authorization": "Bearer valid-token"},
                )
            store.close()

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "dokploy_target_inspect_unavailable")
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("TOKEN", str(payload))


if __name__ == "__main__":
    unittest.main()
