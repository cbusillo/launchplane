import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_prod_backup_restore_http import (
    OdooProdBackupRestoreApplyEnvelope,
    OdooProdBackupRestoreOperationActiveError,
    OdooProdBackupRestoreReplayNotEligibleError,
    enqueue_odoo_prod_backup_restore_operation,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _asgi_request
from tests.support.auth import _identity, _StubVerifier
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.test_odoo_prod_backup_restore_storage import _verification_failed_restore_operation
from tests.test_odoo_stable_operation_worker import _restore_operation


class OdooProdBackupRestoreHttpTests(unittest.IsolatedAsyncioTestCase):
    def _policy(self, *, action: str) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        "managed_set_id": "test.restore",
                        "managed_rule_id": action,
                        "repository": "cbusillo/launchplane",
                        "repository_id": "1001",
                        "repository_owner_id": "2001",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/odoo-prod-backup-restore.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "refs": ["refs/heads/main"],
                        "products": ["odoo-tenant-cm"],
                        "contexts": ["cm"],
                        "instances": ["prod"],
                        "actions": [action],
                    }
                ],
            }
        )

    def _identity(self) -> GitHubActionsIdentity:
        return _identity(
            repository="cbusillo/launchplane",
            workflow_ref=(
                "cbusillo/launchplane/.github/workflows/"
                "odoo-prod-backup-restore.yml@refs/heads/main"
            ),
            event_name="workflow_dispatch",
        )

    def _store(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload_with_prod_lane())
        )
        return store

    def _payload(self, *, apply: bool = False) -> dict[str, object]:
        operation = _restore_operation()
        restore = operation.request.model_dump(mode="json")
        if not apply:
            for key in (
                "plan_fingerprint",
                "confirmation",
                "no_cache",
                "timeout_seconds",
                "health_timeout_seconds",
            ):
                restore.pop(key, None)
        return {"product": "odoo-tenant-cm", "restore": restore}

    async def test_plan_route_returns_reviewable_fingerprint(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_backup_restore_plan.read"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            plan = _restore_operation().plan
            with patch(
                "control_plane.http_app.build_odoo_prod_backup_restore_plan",
                return_value=plan,
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/prod-backup-restore-plan",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=self._payload(),
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["plan_status"], "ready")
        self.assertEqual(payload["result"]["plan_fingerprint"], plan.plan_fingerprint)
        self.assertEqual(payload["records"]["backup_record_id"], plan.backup_record_id)

    async def test_apply_route_requires_idempotency_and_enqueues_destructive_operation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_backup_restore_apply.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            missing_key_response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/odoo/prod-backup-restore-apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=self._payload(apply=True),
            )
            with patch(
                "control_plane.http_app.enqueue_odoo_prod_backup_restore_operation",
                return_value=(
                    {"odoo_prod_backup_restore_operation_id": "restore-operation-1"},
                    {
                        "operation_id": "restore-operation-1",
                        "status": "pending",
                        "phase": "created",
                    },
                ),
            ) as enqueue_mock:
                accepted_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/prod-backup-restore-apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "restore-request-1",
                    },
                    payload=self._payload(apply=True),
                )

        self.assertEqual(missing_key_response.status_code, 400)
        self.assertEqual(
            missing_key_response.json()["error"]["code"],
            "idempotency_key_required",
        )
        self.assertEqual(accepted_response.status_code, 202)
        self.assertEqual(
            accepted_response.json()["records"]["odoo_prod_backup_restore_operation_id"],
            "restore-operation-1",
        )
        authorization = enqueue_mock.call_args.kwargs["authorization"]
        self.assertEqual(
            authorization.action,
            "odoo_prod_backup_restore_apply.execute",
        )

    async def test_apply_route_reports_replay_ineligible_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_backup_restore_apply.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            with patch(
                "control_plane.http_app.enqueue_odoo_prod_backup_restore_operation",
                side_effect=OdooProdBackupRestoreReplayNotEligibleError(
                    "Odoo production backup restore failure is not eligible for replay."
                ),
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/prod-backup-restore-apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "restore-request-ineligible",
                    },
                    payload=self._payload(apply=True),
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "odoo_prod_backup_restore_replay_not_eligible",
        )

    def test_enqueue_requeues_exact_terminal_failed_restore_replay(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            operation = _verification_failed_restore_operation()
            store.write_odoo_prod_backup_restore_operation_record(operation)

            records, payload = enqueue_odoo_prod_backup_restore_operation(
                control_plane_root=root,
                record_store=store,
                request=OdooProdBackupRestoreApplyEnvelope.model_validate(
                    self._payload(apply=True)
                ),
                idempotency_key=operation.idempotency_key,
                idempotency_scope=operation.idempotency_scope,
                request_fingerprint=operation.request_fingerprint,
                created_at="2026-07-25T00:31:00Z",
                authorization=operation.authorization,
            )
            stored = store.read_odoo_prod_backup_restore_operation_record(operation.operation_id)

        self.assertEqual(
            records["odoo_prod_backup_restore_operation_id"],
            operation.operation_id,
        )
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(stored.status, "pending")
        self.assertEqual(stored.phase, "verification_started")
        self.assertEqual(stored.attempt, 1)

    def test_enqueue_maps_ineligible_failed_replay_to_typed_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            operation = _verification_failed_restore_operation().model_copy(update={"attempt": 2})
            store.write_odoo_prod_backup_restore_operation_record(operation)

            with self.assertRaises(OdooProdBackupRestoreReplayNotEligibleError):
                enqueue_odoo_prod_backup_restore_operation(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooProdBackupRestoreApplyEnvelope.model_validate(
                        self._payload(apply=True)
                    ),
                    idempotency_key=operation.idempotency_key,
                    idempotency_scope=operation.idempotency_scope,
                    request_fingerprint=operation.request_fingerprint,
                    created_at="2026-07-25T00:31:00Z",
                    authorization=operation.authorization,
                )

    async def test_apply_route_does_not_expose_operation_exception_text(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(action="odoo_prod_backup_restore_apply.execute"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            error = OdooProdBackupRestoreOperationActiveError(_restore_operation())
            error.args = ("private provider path and stack detail",)
            with patch(
                "control_plane.http_app.enqueue_odoo_prod_backup_restore_operation",
                side_effect=error,
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/prod-backup-restore-apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "restore-request-active",
                    },
                    payload=self._payload(apply=True),
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["message"],
            "An Odoo production backup restore operation is already active for this lane.",
        )
        self.assertNotIn("private provider path", response.text)


if __name__ == "__main__":
    unittest.main()
