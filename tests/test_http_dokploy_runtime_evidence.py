from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane.contracts.privileged_operation_worker_heartbeat import (
    PrivilegedOperationWorkerHeartbeatRecord,
    privileged_operation_worker_identity_sha256,
)
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
            recorded_at = datetime.now(timezone.utc)
            container_identity_sha256 = privileged_operation_worker_identity_sha256("a" * 12)
            store.write_privileged_operation_worker_heartbeat_record(
                PrivilegedOperationWorkerHeartbeatRecord(
                    worker_identity_sha256=container_identity_sha256,
                    image_reference=f"ghcr.io/example/launchplane@sha256:{'a' * 64}",
                    poll_interval_seconds=15,
                    last_poll_succeeded_at=recorded_at.isoformat(),
                ),
                prune_before=(recorded_at - timedelta(days=7)).isoformat(),
                prune_after=(recorded_at + timedelta(seconds=60)).isoformat(),
            )
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
                        "container_id": "a" * 64,
                        "container_identity_sha256": container_identity_sha256,
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
                    "control_plane.dokploy_target_inspect.dokploy_runtime_evidence.fetch_compose_container_logs",
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
        runtime_payload = response.json()["inspect"]["runtime_evidence"]
        self.assertTrue(runtime_payload["proof_ready"])
        self.assertEqual(runtime_payload["proof_source"], "worker_heartbeat_record")
        self.assertEqual(runtime_payload["worker_heartbeat"]["status"], "ready")
        self.assertTrue(runtime_payload["structured_event"]["observed"])
        self.assertNotIn(container_identity_sha256, str(runtime_payload))

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
                    "control_plane.dokploy.runtime_evidence.dokploy_api.dokploy_request",
                    side_effect=click.ClickException("provider TOKEN=secret"),
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
        self.assertIn("container-list", payload["error"]["message"])
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("TOKEN", str(payload))

    async def test_dokploy_target_inspect_identifies_provider_config_stage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch(
                "control_plane.http_routes.drivers.dokploy_source.read_dokploy_config",
                side_effect=click.ClickException("provider TOKEN=secret"),
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
                    "/v1/dokploy-targets/inspect?target_type=compose&target_id=compose-123",
                    headers={"Authorization": "Bearer valid-token"},
                )
            store.close()

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertIn("provider-config", payload["error"]["message"])
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("TOKEN", str(payload))


if __name__ == "__main__":
    unittest.main()
