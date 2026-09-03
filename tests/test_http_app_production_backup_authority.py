from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlencode

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.production_backup_authority import ProductionBackupAuthorityWriteEnvelope
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _StubVerifier, _identity
from tests.support.http import get as http_get
from tests.support.http import request as http_request
from tests.test_production_backup_authority import _dry_run_envelope


_WORKFLOW_REF = "example/example-product/.github/workflows/promote.yml@refs/heads/main"
_JOB_WORKFLOW_REF = (
    "example/launchplane/.github/workflows/reusable-promote.yml@"
    "0123456789abcdef0123456789abcdef01234567"
)


def _authz_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": [
                {
                    "managed_set_id": "test.production-backup-authority",
                    "managed_rule_id": "example-product.production-backup-authority",
                    "repository": "example/example-product",
                    "repository_id": "1001",
                    "repository_owner_id": "1000",
                    "workflow_refs": [_WORKFLOW_REF],
                    "job_workflow_refs": [_JOB_WORKFLOW_REF],
                    "event_names": ["workflow_dispatch"],
                    "products": ["example-product"],
                    "contexts": ["example-product"],
                    "instances": ["prod"],
                    "actions": [
                        "production_backup_authority.read",
                        "production_backup_authority.write",
                    ],
                }
            ],
        }
    )


class ProductionBackupAuthorityHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_dry_run_apply_replay_and_redaction(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            identity = _identity(
                repository="example/example-product",
                workflow_ref=_WORKFLOW_REF,
                job_workflow_ref=_JOB_WORKFLOW_REF,
                event_name="workflow_dispatch",
                environment="prod",
                repository_id="1001",
                repository_owner_id="1000",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_authz_policy(),
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer valid-token"}
            query = urlencode(
                {
                    "product": "example-product",
                    "context": "example-product",
                    "instance": "prod",
                    "promotion_action": "verireel_prod_promotion.execute",
                }
            )
            missing = await http_get(
                app,
                f"/v1/production-backup-authority?{query}",
                headers=headers,
            )
            self.assertEqual(missing.status_code, 200)
            self.assertEqual(missing.json()["authority"]["state"], "missing")

            dry_envelope = _dry_run_envelope()
            dry_response = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=headers,
                payload=dry_envelope.model_dump(mode="json"),
            )
            self.assertEqual(dry_response.status_code, 200)
            dry_payload = dry_response.json()
            self.assertEqual(dry_payload["result"]["status"], "would_apply")
            self.assertNotIn("proxmox.example.invalid", dry_response.text)
            self.assertNotIn("pbs-production", dry_response.text)

            apply_envelope = ProductionBackupAuthorityWriteEnvelope.model_validate(
                dry_envelope.model_dump(mode="json")
                | {
                    "mode": "apply",
                    "reviewed_authority_digest": dry_payload["result"]["authority_digest"],
                }
            )
            apply_headers = headers | {"Idempotency-Key": "issue-2306-example-apply"}
            applied = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=apply_headers,
                payload=apply_envelope.model_dump(mode="json"),
            )
            self.assertEqual(applied.status_code, 200)
            self.assertEqual(applied.json()["result"]["status"], "applied")
            replayed = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=apply_headers,
                payload=apply_envelope.model_dump(mode="json"),
            )
            self.assertEqual(replayed.status_code, 200)
            self.assertTrue(replayed.json()["replayed"])

            ready = await http_get(
                app,
                f"/v1/production-backup-authority?{query}",
                headers=headers,
            )
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["authority"]["state"], "ready")
            self.assertNotIn("proxmox.example.invalid", ready.text)
            self.assertNotIn("pbs-production", ready.text)
            store.close()

    async def test_openapi_exposes_bounded_authority_routes(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    _identity(
                        repository="example/example-product",
                        workflow_ref=_WORKFLOW_REF,
                        job_workflow_ref=_JOB_WORKFLOW_REF,
                        event_name="workflow_dispatch",
                        environment="prod",
                        repository_id="1001",
                        repository_owner_id="1000",
                    )
                ),
                authz_policy=_authz_policy(),
                record_store_factory=lambda: store,
            )
            paths = app.openapi()["paths"]
            self.assertIn("/v1/production-backup-authority", paths)
            self.assertIn("/v1/production-backup-authority/apply", paths)
            self.assertIn(
                "/v1/production-backup-authority/legacy-runtime-migration",
                paths,
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
