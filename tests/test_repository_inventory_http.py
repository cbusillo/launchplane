from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.repository_inventory import (
    REPOSITORY_INVENTORY_READ_ACTION,
    REPOSITORY_INVENTORY_WRITE_ACTION,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy, TerminalAgentIdentity
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_get, _asgi_request
from tests.support.auth import StubVerifier, identity

REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/repository"


class _TestPostgresRecordStore(PostgresRecordStore):
    @property
    def database_dialect_name(self) -> str:
        return "postgresql"


def _postgres_store(root: Path) -> PostgresRecordStore:
    store = _TestPostgresRecordStore(
        database_url=f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"
    )
    store.ensure_schema()
    store.seed_authz_policy_if_absent(
        LaunchplaneAuthzPolicyRecord(
            record_id="test-repository-inventory-authz-policy",
            revision=1,
            status="active",
            source="test",
            updated_at="2026-08-26T00:00:00Z",
            policy=_authz_policy(
                actions=(
                    REPOSITORY_INVENTORY_READ_ACTION,
                    REPOSITORY_INVENTORY_WRITE_ACTION,
                )
            ),
        )
    )
    return store


def _authz_policy(*, actions: tuple[str, ...]) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": list(actions),
                }
            ],
        }
    )


def _payload(
    *,
    revision: int = 1,
    mode: str = "apply",
    state: str = "tracked",
    reason: str = "synthetic inventory evidence",
    expected_current_record_id: str = "",
    supersedes_record_id: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "inventory_state": state,
        "inventory_revision": revision,
        "recorded_at": f"2026-08-26T00:00:0{revision}Z",
        "source": "test",
        "reason": reason,
    }
    if supersedes_record_id is not None:
        record["supersedes_record_id"] = supersedes_record_id
    return {
        "schema_version": 1,
        "mode": mode,
        "expected_current_record_id": expected_current_record_id,
        "record": record,
    }


class RepositoryInventoryHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_openapi_documents_repository_inventory_error_contracts(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_authz_policy(
                    actions=(
                        REPOSITORY_INVENTORY_READ_ACTION,
                        REPOSITORY_INVENTORY_WRITE_ACTION,
                    )
                ),
                record_store_factory=lambda: store,
            )
            paths = app.openapi()["paths"]

        self.assertEqual(
            set(paths["/v1/repository-inventory"]["get"]["responses"]),
            {"200", "400", "401", "403", "409", "503"},
        )
        self.assertEqual(
            set(paths["/v1/repository-inventory/apply"]["post"]["responses"]),
            {"200", "400", "401", "403", "409", "503"},
        )

    async def test_dry_run_works_with_filesystem_rehearsal_store(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_authz_policy(actions=(REPOSITORY_INVENTORY_WRITE_ACTION,)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_payload(mode="dry_run"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["status"], "would_apply")

    async def test_apply_is_durable_and_idempotent(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(Path(temporary_directory_name))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_authz_policy(actions=(REPOSITORY_INVENTORY_WRITE_ACTION,)),
                record_store_factory=lambda: store,
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "repository-inventory-apply-1",
            }
            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers=headers,
                payload=_payload(),
            )
            replay_response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers=headers,
                payload=_payload(),
            )
            records = store.list_repository_inventory_records(repository_id=REPOSITORY_ID)
            store.close()

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()["result"]["status"], "applied")
        self.assertEqual(replay_response.status_code, 200)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(len(records), 1)

    async def test_idempotency_key_conflict_is_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(Path(temporary_directory_name))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_authz_policy(actions=(REPOSITORY_INVENTORY_WRITE_ACTION,)),
                record_store_factory=lambda: store,
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "repository-inventory-reused",
            }
            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers=headers,
                payload=_payload(),
            )
            conflict_response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers=headers,
                payload=_payload(reason="different payload"),
            )
            store.close()

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_same_record_with_different_key_conflicts(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(Path(temporary_directory_name))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_authz_policy(actions=(REPOSITORY_INVENTORY_WRITE_ACTION,)),
                record_store_factory=lambda: store,
            )
            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "repository-inventory-original",
                },
                payload=_payload(),
            )
            conflict_response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "repository-inventory-different",
                },
                payload=_payload(),
            )
            store.close()

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(
            conflict_response.json()["error"]["code"],
            "repository_inventory_conflict",
        )

    async def test_compare_and_write_rejects_stale_current_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(Path(temporary_directory_name))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_authz_policy(actions=(REPOSITORY_INVENTORY_WRITE_ACTION,)),
                record_store_factory=lambda: store,
            )
            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "repository-inventory-revision-1",
                },
                payload=_payload(),
            )
            record_id = first_response.json()["result"]["record_id"]
            conflict_response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "repository-inventory-revision-2",
                },
                payload=_payload(
                    revision=2,
                    state="retired",
                    expected_current_record_id="repository-inventory-1001-r0",
                    supersedes_record_id=record_id,
                ),
            )
            records = store.list_repository_inventory_records(repository_id=REPOSITORY_ID)
            store.close()

        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(len(records), 1)

    async def test_read_returns_current_inventory_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(Path(temporary_directory_name))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_authz_policy(
                    actions=(
                        REPOSITORY_INVENTORY_READ_ACTION,
                        REPOSITORY_INVENTORY_WRITE_ACTION,
                    )
                ),
                record_store_factory=lambda: store,
            )
            await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "repository-inventory-read-setup",
                },
                payload=_payload(),
            )
            response = await _asgi_get(
                app,
                f"/v1/repository-inventory?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )
            store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["read_model"]["status"], "available")
        self.assertEqual(
            response.json()["read_model"]["current_record"]["repository_id"],
            REPOSITORY_ID,
        )

    async def test_apply_requires_shared_postgresql_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=(
                    f"sqlite+pysqlite:///{Path(temporary_directory_name) / 'launchplane.sqlite3'}"
                )
            )
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=_authz_policy(actions=(REPOSITORY_INVENTORY_WRITE_ACTION,)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "repository-inventory-sqlite",
                },
                payload=_payload(),
            )
            store.close()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_terminal_agent_write_is_denied(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(
                    cast(
                        Any,
                        TerminalAgentIdentity(subject="agent", token_label="agent-token"),
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/repository-inventory/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_payload(mode="dry_run"),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
