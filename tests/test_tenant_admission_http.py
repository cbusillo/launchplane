from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest

from control_plane.contracts.repository_human_admission import (
    build_repository_human_role_policy_record_id,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.tenant_merge_eligibility import (
    build_tenant_repository_classification_record_id,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy, TerminalAgentIdentity
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_get, _asgi_request
from tests.support.auth import _StubVerifier, _identity

PRODUCT = "launchplane"
CONTEXT = "production"
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/tenant-site"
CLASSIFIED_AT = "2026-07-31T11:00:00Z"
SOURCE = "operator"
REASON = "initial classification"


class _TestPostgresRecordStore(PostgresRecordStore):
    @property
    def database_dialect_name(self) -> str:
        return "postgresql"


def _postgres_store(
    root: Path,
    *,
    actions: tuple[str, ...] = (
        "tenant_repository_classification.read",
        "tenant_repository_classification.write",
        "repository_human_role_policy.read",
        "repository_human_role_policy.write",
    ),
) -> PostgresRecordStore:
    root.mkdir(parents=True, exist_ok=True)
    store = _TestPostgresRecordStore(
        database_url=f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"
    )
    store.ensure_schema()
    store.seed_authz_policy_if_absent(
        LaunchplaneAuthzPolicyRecord(
            record_id="test-tenant-admission-authz-policy",
            revision=1,
            status="active",
            source="test",
            updated_at="2026-07-31T00:00:00Z",
            policy=_authz_policy(actions=actions),
        )
    )
    return store


class TenantAdmissionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_create_applies_revision_1(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-create-1",
                },
                payload=_apply_payload(revision=1),
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["result"]["status"], "applied")
        self.assertEqual(data["result"]["mode"], "apply")
        self.assertEqual(data["result"]["repository_id"], REPOSITORY_ID)
        self.assertEqual(data["result"]["classification_revision"], 1)
        expected_record_id = build_tenant_repository_classification_record_id(
            repository_id=REPOSITORY_ID, classification_revision=1
        )
        self.assertEqual(data["result"]["record_id"], expected_record_id)
        self.assertIsNone(data["result"]["supersedes_record_id"])

    async def test_dry_run_no_write(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "tenant_repository_classification.read",
                        "tenant_repository_classification.write",
                    )
                ),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_apply_payload(revision=1, mode="dry_run"),
            )
            self.assertEqual(dry_run_response.status_code, 200)
            self.assertEqual(dry_run_response.json()["result"]["mode"], "dry_run")
            self.assertEqual(dry_run_response.json()["result"]["status"], "would_apply")

            read_response = await _asgi_get(
                app,
                f"/v1/work-graph/tenant-admission/repository-classification?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(read_response.status_code, 200)
        read_data = read_response.json()
        self.assertEqual(read_data["read_model"]["status"], "missing")
        self.assertIsNone(read_data["read_model"]["current_record"])
        self.assertEqual(read_data["read_model"]["history_count"], 0)

    async def test_revision_update(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "tenant_repository_classification.read",
                        "tenant_repository_classification.write",
                    )
                ),
                record_store_factory=lambda: store,
            )

            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-rev1",
                },
                payload=_apply_payload(revision=1),
            )
            self.assertEqual(res1.status_code, 200)
            rev1_record_id = res1.json()["result"]["record_id"]

            res2 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-rev2",
                },
                payload=_apply_payload(
                    revision=2,
                    kind="engineering",
                    supersedes_record_id=rev1_record_id,
                    expected_current_record_id=rev1_record_id,
                ),
            )
            self.assertEqual(res2.status_code, 200)
            res2_data = res2.json()
            self.assertEqual(res2_data["result"]["status"], "applied")
            self.assertEqual(res2_data["result"]["classification_revision"], 2)
            self.assertEqual(res2_data["result"]["supersedes_record_id"], rev1_record_id)

            read_res = await _asgi_get(
                app,
                f"/v1/work-graph/tenant-admission/repository-classification?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(read_res.status_code, 200)
        read_data = read_res.json()["read_model"]
        self.assertEqual(read_data["status"], "available")
        self.assertEqual(read_data["history_count"], 2)
        self.assertEqual(read_data["current_record"]["classification_revision"], 2)
        self.assertEqual(read_data["current_record"]["classification_kind"], "engineering")

    async def test_stale_expected_current_conflict(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-1",
                },
                payload=_apply_payload(revision=1),
            )
            rev1_id = res1.json()["result"]["record_id"]

            res2 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-2",
                },
                payload=_apply_payload(
                    revision=2,
                    supersedes_record_id=rev1_id,
                    expected_current_record_id=rev1_id,
                ),
            )
            self.assertEqual(res2.status_code, 200)
            rev2_id = res2.json()["result"]["record_id"]

            conflict_res = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-3",
                },
                payload=_apply_payload(
                    revision=3,
                    supersedes_record_id=rev2_id,
                    expected_current_record_id=rev1_id,
                ),
            )

        self.assertEqual(conflict_res.status_code, 409)
        self.assertEqual(conflict_res.json()["error"]["code"], "classification_conflict")

    async def test_skipped_revision_and_supersedes_mismatch_rejection(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-rev1",
                },
                payload=_apply_payload(revision=1),
            )
            rev1_id = res1.json()["result"]["record_id"]

            skipped_res = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-skipped",
                },
                payload=_apply_payload(
                    revision=3,
                    supersedes_record_id=rev1_id,
                    expected_current_record_id=rev1_id,
                ),
            )
            self.assertEqual(skipped_res.status_code, 400)
            self.assertEqual(skipped_res.json()["error"]["code"], "invalid_sequence")

            mismatch_res = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-mismatch",
                },
                payload=_apply_payload(
                    revision=2,
                    supersedes_record_id="wrong-rev-id",
                    expected_current_record_id=rev1_id,
                ),
            )

        self.assertEqual(mismatch_res.status_code, 400)
        self.assertEqual(mismatch_res.json()["error"]["code"], "invalid_sequence")

    async def test_idempotent_replay(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            payload = _apply_payload(revision=1)
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "key-replay-100",
            }

            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers=headers,
                payload=payload,
            )
            self.assertEqual(res1.status_code, 200)

            res2 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers=headers,
                payload=payload,
            )

        self.assertEqual(res2.status_code, 200)
        data = res2.json()
        self.assertTrue(data.get("replayed"))

    async def test_identical_payload_with_different_idempotency_key_conflicts(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            payload = _apply_payload(revision=1)
            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-replay-original",
                },
                payload=payload,
            )
            second_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-replay-different",
                },
                payload=payload,
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(
            second_response.json()["error"]["code"],
            "classification_conflict",
        )

    async def test_same_idempotency_key_with_different_payload_conflicts(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "key-reused-different-payload",
            }
            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers=headers,
                payload=_apply_payload(revision=1),
            )
            second_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers=headers,
                payload=_apply_payload(revision=1, kind="engineering"),
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(
            second_response.json()["error"]["code"],
            "idempotency_key_reused",
        )

    async def test_terminal_agent_denial(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=cast(
                    Any,
                    _StubVerifier(
                        cast(
                            Any,
                            TerminalAgentIdentity(
                                subject="local-owner-agent",
                                token_label="local-owner-token",
                            ),
                        )
                    ),
                ),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-agent",
                },
                payload=_apply_payload(revision=1),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_authz_denial(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir), actions=())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=()),
                record_store_factory=lambda: store,
            )
            read_res = await _asgi_get(
                app,
                f"/v1/work-graph/tenant-admission/repository-classification?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )
            write_res = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-no-auth",
                },
                payload=_apply_payload(revision=1),
            )

        self.assertEqual(read_res.status_code, 403)
        self.assertEqual(write_res.status_code, 403)

    async def test_missing_classification_read(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.read",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_get(
                app,
                "/v1/work-graph/tenant-admission/repository-classification?repository_id=9999",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["read_model"]
        self.assertEqual(data["status"], "missing")
        self.assertEqual(data["repository_id"], "9999")
        self.assertIsNone(data["current_record"])
        self.assertEqual(data["history_count"], 0)

    async def test_immutable_repository_id_lookup(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "tenant_repository_classification.read",
                        "tenant_repository_classification.write",
                    )
                ),
                record_store_factory=lambda: store,
            )
            await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-lookup",
                },
                payload=_apply_payload(revision=1),
            )

            read_res = await _asgi_get(
                app,
                f"/v1/work-graph/tenant-admission/repository-classification?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(read_res.status_code, 200)
        data = read_res.json()["read_model"]
        self.assertEqual(data["status"], "available")
        self.assertEqual(data["repository_id"], REPOSITORY_ID)
        self.assertEqual(data["history_count"], 1)
        self.assertEqual(data["current_record"]["repository_id"], REPOSITORY_ID)
        self.assertEqual(data["current_record"]["classification_revision"], 1)

    async def test_no_evaluate_or_evidence_ingress_route(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "tenant_repository_classification.read",
                        "tenant_repository_classification.write",
                    )
                ),
                record_store_factory=lambda: store,
            )
            routes = [
                ("GET", "/v1/tenant-admission/evaluate"),
                ("POST", "/v1/tenant-admission/evaluate"),
                ("POST", "/v1/evidence/tenant-admission"),
                ("GET", "/v1/work-graph/tenant-admission/evaluate"),
            ]
            results = []
            for method, path in routes:
                res = await _asgi_request(
                    app,
                    method,
                    path,
                    headers={"Authorization": "Bearer valid-token"},
                )
                results.append(res.status_code)

        for status_code in results:
            self.assertEqual(status_code, 404)

    async def test_missing_db_capability_503(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-503",
                },
                payload=_apply_payload(revision=1),
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_sqlite_backed_postgres_store_is_not_shared_authority(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(tmp_dir) / 'launchplane.sqlite3'}"
            )
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-sqlite-503",
                },
                payload=_apply_payload(revision=1),
            )
            store.close()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_role_policy_dry_run_works_with_filesystem_rehearsal_store(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_role_policy_payload(revision=1, mode="dry_run"),
            )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["result"]["status"], "would_apply")
        self.assertEqual(data["result"]["mode"], "dry_run")
        self.assertEqual(
            store.list_repository_human_role_policy_records(repository_id=REPOSITORY_ID),
            (),
        )

    async def test_role_policy_apply_revision_and_read_model(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "repository_human_role_policy.read",
                        "repository_human_role_policy.write",
                    )
                ),
                record_store_factory=lambda: store,
            )

            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-rev1",
                },
                payload=_role_policy_payload(revision=1),
            )
            self.assertEqual(res1.status_code, 202)
            rev1 = res1.json()["result"]

            res2 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-rev2",
                },
                payload=_role_policy_payload(
                    revision=2,
                    repository_owner_github_ids=(302,),
                    expected_current_record_id=rev1["record_id"],
                    expected_current_role_policy_digest=rev1["role_policy_digest"],
                    supersedes_record_id=rev1["record_id"],
                ),
            )
            self.assertEqual(res2.status_code, 202)
            read_res = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/repository-human-role-policy"
                    f"?repository_id={REPOSITORY_ID}&product={PRODUCT}&context={CONTEXT}"
                ),
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(res2.json()["result"]["status"], "applied")
        self.assertEqual(res2.json()["result"]["role_policy_revision"], 2)
        self.assertEqual(read_res.status_code, 200)
        read_model = read_res.json()["read_model"]
        self.assertEqual(read_model["status"], "available")
        self.assertEqual(read_model["history_count"], 2)
        self.assertEqual(read_model["current_record"]["role_policy_revision"], 2)
        self.assertEqual(read_model["current_record"]["repository_owner_github_ids"], [302])

    async def test_role_policy_missing_read_requires_product_context_authz(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.read",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/repository-human-role-policy"
                    "?repository_id=9999&product=launchplane&context=production"
                ),
                headers={"Authorization": "Bearer valid-token"},
            )

            denied = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/repository-human-role-policy"
                    "?repository_id=9999&product=launchplane&context=unauthorized"
                ),
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["read_model"]["status"], "missing")
        self.assertEqual(denied.status_code, 403)

    async def test_role_policy_apply_requires_postgres_and_idempotency_key(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            filesystem_store = FilesystemRecordStore(Path(tmp_dir) / "fs")
            filesystem_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: filesystem_store,
            )
            sqlite_store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(tmp_dir) / 'launchplane.sqlite3'}"
            )
            sqlite_store.ensure_schema()
            sqlite_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: sqlite_store,
            )

            missing_key = await _asgi_request(
                filesystem_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_role_policy_payload(revision=1),
            )
            filesystem_response = await _asgi_request(
                filesystem_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-fs",
                },
                payload=_role_policy_payload(revision=1),
            )
            sqlite_response = await _asgi_request(
                sqlite_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-sqlite",
                },
                payload=_role_policy_payload(revision=1),
            )
            sqlite_store.close()

        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_key_required")
        self.assertEqual(filesystem_response.status_code, 503)
        self.assertEqual(
            filesystem_response.json()["error"]["code"],
            "database_storage_required",
        )
        self.assertEqual(sqlite_response.status_code, 503)
        self.assertEqual(sqlite_response.json()["error"]["code"], "database_storage_required")

    async def test_role_policy_apply_rejects_terminal_agent_and_wrong_context_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            terminal_store = _postgres_store(Path(tmp_dir) / "terminal")
            terminal_app = create_launchplane_fastapi_app(
                verifier=cast(
                    Any,
                    _StubVerifier(
                        cast(
                            Any,
                            TerminalAgentIdentity(
                                subject="local-owner-agent",
                                token_label="local-owner-token",
                            ),
                        )
                    ),
                ),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: terminal_store,
            )
            denied_store = _postgres_store(Path(tmp_dir) / "denied", actions=())
            denied_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=()),
                record_store_factory=lambda: denied_store,
            )
            terminal_response = await _asgi_request(
                terminal_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-terminal",
                },
                payload=_role_policy_payload(revision=1),
            )
            denied_response = await _asgi_request(
                denied_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-denied",
                },
                payload=_role_policy_payload(revision=1),
            )

        self.assertEqual(terminal_response.status_code, 403)
        self.assertEqual(denied_response.status_code, 403)

    async def test_role_policy_idempotency_replay_conflict_and_exact_replay(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: store,
            )
            payload = _role_policy_payload(revision=1)
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "role-policy-replay",
            }
            first = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers=headers,
                payload=payload,
            )
            same_key = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers=headers,
                payload=payload,
            )
            changed_payload = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers=headers,
                payload=_role_policy_payload(revision=1, reason="changed payload"),
            )
            exact_replay = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-exact-replay-new-key",
                },
                payload=payload,
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(same_key.status_code, 202)
        self.assertTrue(same_key.json().get("replayed"))
        self.assertEqual(changed_payload.status_code, 409)
        self.assertEqual(changed_payload.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(exact_replay.status_code, 202)
        self.assertIsNone(exact_replay.json().get("replayed"))
        self.assertEqual(exact_replay.json()["result"]["status"], "replayed")

    async def test_role_policy_revision_two_exact_replay_with_new_key(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: store,
            )
            revision_1_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-revision-1",
                },
                payload=_role_policy_payload(revision=1),
            )
            revision_1 = revision_1_response.json()["result"]
            revision_2_payload = _role_policy_payload(
                revision=2,
                repository_owner_github_ids=(302,),
                expected_current_record_id=revision_1["record_id"],
                expected_current_role_policy_digest=revision_1["role_policy_digest"],
                supersedes_record_id=revision_1["record_id"],
            )
            revision_2_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-revision-2",
                },
                payload=revision_2_payload,
            )
            replay_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-revision-2-replay",
                },
                payload=revision_2_payload,
            )

        self.assertEqual(revision_1_response.status_code, 202)
        self.assertEqual(revision_2_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertEqual(replay_response.json()["result"]["status"], "replayed")

    async def test_role_policy_rejects_missing_stale_or_digest_drift_expected_tip(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: store,
            )
            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-current-1",
                },
                payload=_role_policy_payload(revision=1),
            )
            rev1 = res1.json()["result"]
            missing_digest = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-missing-digest",
                },
                payload=_role_policy_payload(
                    revision=2,
                    expected_current_record_id=rev1["record_id"],
                    supersedes_record_id=rev1["record_id"],
                ),
            )
            stale_id = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-stale-id",
                },
                payload=_role_policy_payload(
                    revision=2,
                    expected_current_record_id="wrong-record-id",
                    expected_current_role_policy_digest=rev1["role_policy_digest"],
                    supersedes_record_id=rev1["record_id"],
                ),
            )
            digest_drift = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-digest-drift",
                },
                payload=_role_policy_payload(
                    revision=2,
                    expected_current_record_id=rev1["record_id"],
                    expected_current_role_policy_digest="f" * 64,
                    supersedes_record_id=rev1["record_id"],
                ),
            )

        self.assertEqual(missing_digest.status_code, 400)
        self.assertEqual(missing_digest.json()["error"]["code"], "invalid_request")
        self.assertEqual(stale_id.status_code, 409)
        self.assertEqual(stale_id.json()["error"]["code"], "role_policy_conflict")
        self.assertEqual(digest_drift.status_code, 409)
        self.assertEqual(digest_drift.json()["error"]["code"], "role_policy_conflict")

    async def test_no_role_policy_waiver_status_or_controller_routes(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "repository_human_role_policy.read",
                        "repository_human_role_policy.write",
                    )
                ),
                record_store_factory=lambda: store,
            )
            routes = [
                ("POST", "/v1/tenant-admission/technical-human-waivers/apply"),
                ("GET", "/v1/work-graph/tenant-admission/status"),
                ("POST", "/v1/work-graph/tenant-admission/controller/run-once"),
                ("GET", "/v1/work-graph/tenant-admission/trusted-maintenance"),
            ]
            results = []
            for method, path in routes:
                res = await _asgi_request(
                    app,
                    method,
                    path,
                    headers={"Authorization": "Bearer valid-token"},
                )
                results.append(res.status_code)

        for status_code in results:
            self.assertEqual(status_code, 404)


def _authz_policy(*, actions: tuple[str, ...]) -> LaunchplaneAuthzPolicy:
    rules = []
    if actions:
        rules.append(
            {
                "repository": "every/verireel",
                "workflow_refs": [
                    "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                ],
                "event_names": ["pull_request"],
                "products": ["launchplane"],
                "contexts": ["launchplane", CONTEXT],
                "actions": list(actions),
            }
        )
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": rules,
        }
    )


def _apply_payload(
    *,
    revision: int,
    kind: str = "tenant_ui",
    mode: str = "apply",
    expected_current_record_id: str = "",
    supersedes_record_id: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "product": PRODUCT,
        "context": CONTEXT,
        "classification_kind": kind,
        "classification_revision": revision,
        "classified_at": CLASSIFIED_AT,
        "source": SOURCE,
        "reason": REASON,
    }
    if supersedes_record_id is not None:
        record["supersedes_record_id"] = supersedes_record_id

    return {
        "schema_version": 1,
        "mode": mode,
        "expected_current_record_id": expected_current_record_id,
        "record": record,
    }


def _role_policy_payload(
    *,
    revision: int,
    mode: str = "apply",
    repository_owner_github_ids: tuple[int, ...] = (301,),
    manager_primary_github_ids: tuple[int, ...] = (501,),
    expected_current_record_id: str = "",
    expected_current_role_policy_digest: str = "",
    supersedes_record_id: str | None = None,
    effective_at: str = CLASSIFIED_AT,
    reason: str = "initial role policy",
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "record_id": build_repository_human_role_policy_record_id(
            repository_id=REPOSITORY_ID,
            product=PRODUCT,
            context=CONTEXT,
            role_policy_revision=revision,
        ),
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "product": PRODUCT,
        "context": CONTEXT,
        "status": "active",
        "role_policy_revision": revision,
        "repository_owner_github_ids": repository_owner_github_ids,
        "manager_primary_github_ids": manager_primary_github_ids,
        "manager_backup_github_ids": [],
        "manager_delegations": [],
        "effective_at": effective_at,
        "source": SOURCE,
        "reason": reason,
    }
    if supersedes_record_id is not None:
        record["supersedes_record_id"] = supersedes_record_id

    return {
        "schema_version": 1,
        "mode": mode,
        "expected_current_record_id": expected_current_record_id,
        "expected_current_role_policy_digest": expected_current_role_policy_digest,
        "record": record,
    }


if __name__ == "__main__":
    unittest.main()
