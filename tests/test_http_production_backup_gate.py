from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import os
from urllib.parse import urlencode

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorPolicyRule
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.verireel_prod_backup_gate import VeriReelProdBackupGateResult
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.http import get, request
from tests.test_production_backup_provider import _binding
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from tests.support.auth import _identity, _StubVerifier
from tests.test_http_app_production_backup_authority import (
    _authz_policy,
    _WORKFLOW_REF,
    _JOB_WORKFLOW_REF,
)
from tests.test_postgres_integration import _store_for_fresh_head_database


class ProductionBackupGateHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_denial_missing_key_and_non_postgres_fail_before_enqueue(self) -> None:
        for actions, key, status, code in (
            (("production_backup_authority.read",), "key", 403, "authorization_denied"),
            (("production_backup_gate.execute",), "", 400, "missing_idempotency_key"),
            (("production_backup_gate.execute",), "key", 503, "database_storage_required"),
        ):
            with self.subTest(code=code), TemporaryDirectory() as directory:
                store = PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{Path(directory) / 'state.sqlite3'}"
                )
                store.ensure_schema()
                try:
                    app = create_launchplane_fastapi_app(
                        verifier=_RejectingVerifier(),
                        bearer_identity_config=_local_operator_bearer_config(),
                        authz_policy=LaunchplaneAuthzPolicy(
                            schema_version=2,
                            local_operators=(
                                LocalOperatorPolicyRule(
                                    subjects=("local-owner-agent",),
                                    token_labels=("local-owner-read",),
                                    actions=actions,
                                    products=("example-product",),
                                    contexts=("example-product",),
                                    instances=("prod",),
                                ),
                            ),
                        ),
                        record_store_factory=lambda: store,
                    )
                    response = await request(
                        app,
                        "POST",
                        "/v1/production-backup-gates",
                        headers={
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": key,
                        },
                        payload=_binding().request.model_dump(mode="json"),
                    )
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.json()["error"]["code"], code)
                    self.assertEqual(store.list_verireel_prod_backup_gate_operation_records(), ())
                finally:
                    store.close()

    async def test_read_denial_precedes_operation_lookup(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=object,
        )
        query = urlencode(
            {"product": "example-product", "context": "example-product", "instance": "prod"}
        )
        response = await get(
            app,
            f"/v1/production-backup-gates/operations/missing?{query}",
            headers={"Authorization": "Bearer local-operator-token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "Real PostgreSQL test URL is required"
)
class ProductionBackupGatePostgresHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_capture_replays_and_rejects_a_changed_request(self) -> None:
        with _store_for_fresh_head_database() as store:
            binding = _binding()
            store.write_production_backup_target_record(binding.source_target)
            store.write_production_backup_target_record(binding.destination_target)
            store.write_production_backup_policy_record(binding.policy)
            policy_payload = _authz_policy().model_dump(mode="json")
            policy_payload["github_actions"][0]["actions"] += ["production_backup_gate.execute"]
            policy = LaunchplaneAuthzPolicy.model_validate(policy_payload)
            digest = authz_policy_sha256(policy)
            store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
                    source="test:shared-backup-http",
                    updated_at="2026-09-26T00:00:00Z",
                    policy_sha256=digest,
                    policy=policy,
                )
            )
            identity = _identity(
                repository="example/example-product",
                workflow_ref=_WORKFLOW_REF,
                job_workflow_ref=_JOB_WORKFLOW_REF,
                event_name="workflow_dispatch",
                repository_id="1001",
                repository_owner_id="1000",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer valid-token", "Idempotency-Key": "shared-capture"}
            payload = binding.request.model_dump(mode="json")
            accepted = await request(
                app, "POST", "/v1/production-backup-gates", headers=headers, payload=payload
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()["operation_status"], "pending")
            replay = await request(
                app, "POST", "/v1/production-backup-gates", headers=headers, payload=payload
            )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["operation_id"], accepted.json()["operation_id"])
            conflict = await request(
                app,
                "POST",
                "/v1/production-backup-gates",
                headers=headers,
                payload=payload | {"backup_record_id": "different-backup"},
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(len(store.list_verireel_prod_backup_gate_operation_records()), 1)
            operation_id = accepted.json()["operation_id"]
            query = urlencode(
                {"product": "example-product", "context": "example-product", "instance": "prod"}
            )
            read = await get(
                app,
                f"/v1/production-backup-gates/operations/{operation_id}?{query}",
                headers=headers,
            )
            self.assertEqual(read.status_code, 200, read.text)
            self.assertNotIn("proxmox.example.invalid", read.text)
            self.assertNotIn("pbs-production", read.text)
            legacy_cancel = await request(
                app,
                "POST",
                f"/v1/drivers/verireel/prod-backup-gate/operations/{operation_id}/cancel",
                headers=headers,
                payload={"reason": "Wrong route"},
            )
            self.assertEqual(legacy_cancel.status_code, 404, legacy_cancel.text)
            cancelled = await request(
                app,
                "POST",
                f"/v1/production-backup-gates/operations/{operation_id}/cancel?{query}",
                headers=headers,
                payload={"reason": "Cancel pending test capture"},
            )
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertEqual(cancelled.json()["operation_status"], "cancelled")
            self.assertEqual(
                store.read_backup_gate_record(binding.request.backup_record_id).source,
                "launchplane-production-backup-gate-cancellation",
            )

            second = await request(
                app,
                "POST",
                "/v1/production-backup-gates",
                headers=headers | {"Idempotency-Key": "capture-2"},
                payload=payload | {"backup_record_id": "backup-2"},
            )
            self.assertEqual(second.status_code, 200, second.text)
            second_id = second.json()["operation_id"]
            failed = store.read_verireel_prod_backup_gate_operation_record(second_id).model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "finished_at": "2026-09-26T12:00:00Z",
                    "error_code": "operation_authorization_revoked",
                    "error_message": "operation_authorization_revoked",
                    "result": VeriReelProdBackupGateResult(
                        backup_record_id="backup-2",
                        backup_status="fail",
                        error_message="operation_authorization_revoked",
                        evidence={"snapshot_name": "partial-snapshot"},
                    ),
                }
            )
            store.write_verireel_prod_backup_gate_operation_record(failed)
            read_failure = await get(
                app,
                f"/v1/production-backup-gates/operations/{second_id}?{query}",
                headers=headers,
            )
            self.assertEqual(read_failure.status_code, 200, read_failure.text)
            self.assertEqual(read_failure.json()["error_code"], "operation_authorization_revoked")
            self.assertEqual(read_failure.json()["evidence"], {"snapshot_name": "partial-snapshot"})
            cancel_failure = await request(
                app,
                "POST",
                f"/v1/production-backup-gates/operations/{second_id}/cancel?{query}",
                headers=headers,
                payload={"reason": "Cannot cancel terminal work"},
            )
            self.assertEqual(cancel_failure.status_code, 409, cancel_failure.text)


if __name__ == "__main__":
    unittest.main()
