from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest

from control_plane.contracts.tenant_merge_eligibility import (
    TenantRepositoryClassificationRecord,
    build_tenant_repository_classification_record_id,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy, TerminalAgentIdentity
from control_plane.storage.filesystem import FilesystemRecordStore
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


class _DummyStoreNoWrites:
    """Store that lacks write_tenant_repository_classification_record for 503 testing."""

    def list_tenant_repository_classification_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[TenantRepositoryClassificationRecord, ...]:
        return ()


class TenantAdmissionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_create_applies_revision_1(self) -> None:
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
            store = FilesystemRecordStore(Path(tmp_dir))
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
            self.assertEqual(dry_run_response.json()["result"]["status"], "applied")

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
            store = FilesystemRecordStore(Path(tmp_dir))
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
            store = FilesystemRecordStore(Path(tmp_dir))
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
            store = FilesystemRecordStore(Path(tmp_dir))
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
            store = FilesystemRecordStore(Path(tmp_dir))
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

    async def test_terminal_agent_denial(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
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
            store = FilesystemRecordStore(Path(tmp_dir))
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
            store = FilesystemRecordStore(Path(tmp_dir))
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
            store = FilesystemRecordStore(Path(tmp_dir))
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
            store = FilesystemRecordStore(Path(tmp_dir))
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
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
            record_store_factory=lambda: _DummyStoreNoWrites(),
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
                "contexts": ["launchplane"],
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


if __name__ == "__main__":
    unittest.main()
