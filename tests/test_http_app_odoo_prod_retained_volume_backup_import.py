import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_prod_retained_volume_backup_import_http import (
    OdooProdRetainedVolumeBackupImportOperationActiveError,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _asgi_request
from tests.support.auth import _identity, _StubVerifier
from tests.test_odoo_prod_retained_volume_backup_import import (
    PRODUCT,
    CONTEXT,
    _Store,
    _build_plan,
    _operation,
    _request,
)


class OdooProdRetainedVolumeBackupImportHttpTests(unittest.IsolatedAsyncioTestCase):
    def _policy(self, *, action: str) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        "managed_set_id": "test.retained-import",
                        "managed_rule_id": action,
                        "repository": "cbusillo/launchplane",
                        "repository_id": "1001",
                        "repository_owner_id": "2001",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/odoo-retained-import.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "refs": ["refs/heads/main"],
                        "products": [PRODUCT],
                        "contexts": [CONTEXT],
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
                "cbusillo/launchplane/.github/workflows/odoo-retained-import.yml@refs/heads/main"
            ),
            event_name="workflow_dispatch",
        )

    def _store(self, state_dir: Path) -> FilesystemRecordStore:
        store = FilesystemRecordStore(state_dir=state_dir)
        store.write_product_profile_record(_Store().profile)
        return store

    def _payload(self, *, apply: bool = False) -> dict[str, object]:
        if apply:
            operation = _operation(
                operation_kind="apply",
                operation_id="retained-import-apply-operation-1",
            )
            backup_import = operation.request.model_dump(mode="json")
        else:
            backup_import = _request().model_dump(mode="json")
        return {"product": PRODUCT, "backup_import": backup_import}

    async def test_plan_route_requires_idempotency_and_captures_durable_authz(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(
                    action="odoo_prod_retained_volume_backup_import_plan.execute"
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            missing_key_response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/odoo/prod-retained-volume-backup-import-plan",
                headers={"Authorization": "Bearer valid-token"},
                payload=self._payload(),
            )
            with patch(
                "control_plane.http_app.enqueue_odoo_prod_retained_volume_backup_import_plan_operation",
                return_value=(
                    {
                        "odoo_prod_retained_volume_backup_import_operation_id": (
                            "retained-import-plan-operation-1"
                        )
                    },
                    {
                        "operation_id": "retained-import-plan-operation-1",
                        "operation_kind": "plan",
                        "status": "pending",
                        "phase": "created",
                        "poll_url": (
                            "/v1/drivers/odoo/prod-retained-volume-backup-import/"
                            "operations/retained-import-plan-operation-1"
                        ),
                    },
                ),
            ) as enqueue_mock:
                accepted_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/prod-retained-volume-backup-import-plan",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "retained-plan-1",
                    },
                    payload=self._payload(),
                )

        self.assertEqual(missing_key_response.status_code, 400)
        self.assertEqual(
            missing_key_response.json()["error"]["code"],
            "idempotency_key_required",
        )
        self.assertEqual(accepted_response.status_code, 202)
        authorization = enqueue_mock.call_args.kwargs["authorization"]
        self.assertEqual(
            authorization.action,
            "odoo_prod_retained_volume_backup_import_plan.execute",
        )

    async def test_apply_route_captures_distinct_authz_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(
                    action="odoo_prod_retained_volume_backup_import_apply.execute"
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            with patch(
                "control_plane.http_app.enqueue_odoo_prod_retained_volume_backup_import_apply_operation",
                return_value=(
                    {
                        "odoo_prod_retained_volume_backup_import_operation_id": (
                            "retained-import-apply-operation-1"
                        )
                    },
                    {
                        "operation_id": "retained-import-apply-operation-1",
                        "operation_kind": "apply",
                        "status": "pending",
                        "phase": "created",
                        "poll_url": (
                            "/v1/drivers/odoo/prod-retained-volume-backup-import/"
                            "operations/retained-import-apply-operation-1"
                        ),
                    },
                ),
            ) as enqueue_mock:
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/odoo/prod-retained-volume-backup-import-apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "retained-apply-1",
                    },
                    payload=self._payload(apply=True),
                )

        self.assertEqual(response.status_code, 202)
        authorization = enqueue_mock.call_args.kwargs["authorization"]
        self.assertEqual(
            authorization.action,
            "odoo_prod_retained_volume_backup_import_apply.execute",
        )

    async def test_write_routes_do_not_expose_operation_exception_text(self) -> None:
        cases = (
            (
                False,
                "odoo_prod_retained_volume_backup_import_plan.execute",
                "/v1/drivers/odoo/prod-retained-volume-backup-import-plan",
                "control_plane.http_app.enqueue_odoo_prod_retained_volume_backup_import_plan_operation",
            ),
            (
                True,
                "odoo_prod_retained_volume_backup_import_apply.execute",
                "/v1/drivers/odoo/prod-retained-volume-backup-import-apply",
                "control_plane.http_app.enqueue_odoo_prod_retained_volume_backup_import_apply_operation",
            ),
        )
        for apply, action, route_path, patch_target in cases:
            with self.subTest(action=action), TemporaryDirectory() as temporary_directory_name:
                root = Path(temporary_directory_name)
                store = self._store(root / "state")
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(self._identity()),
                    authz_policy=self._policy(action=action),
                    record_store_factory=lambda: store,
                    control_plane_root_path=root,
                )
                operation_kind = "apply" if apply else "plan"
                error = OdooProdRetainedVolumeBackupImportOperationActiveError(
                    _operation(
                        operation_kind=operation_kind,
                        operation_id=f"retained-import-{operation_kind}-operation-active",
                    )
                )
                error.args = ("private provider path and stack detail",)
                with patch(patch_target, side_effect=error):
                    response = await _asgi_request(
                        app,
                        "POST",
                        route_path,
                        headers={
                            "Authorization": "Bearer valid-token",
                            "Idempotency-Key": f"retained-{operation_kind}-active",
                        },
                        payload=self._payload(apply=apply),
                    )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["error"]["message"],
                "An Odoo retained-volume backup import operation is already active for this lane.",
            )
            self.assertNotIn("private provider path", response.text)

    async def test_operation_status_route_returns_plan_result_and_poll_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = self._store(root / "state")
            operation = _operation(
                operation_kind="plan",
                operation_id="retained-import-plan-operation-1",
            )
            plan = _build_plan(_Store())
            terminal = operation.model_copy(
                update={
                    "status": "pass",
                    "phase": "completed",
                    "updated_at": "2026-07-26T00:05:00Z",
                    "finished_at": "2026-07-26T00:05:00Z",
                    "result": plan,
                }
            )
            store.write_odoo_prod_retained_volume_backup_import_operation_record(terminal)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(self._identity()),
                authz_policy=self._policy(
                    action="odoo_prod_retained_volume_backup_import_plan.execute"
                ),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _asgi_request(
                app,
                "GET",
                (
                    "/v1/drivers/odoo/prod-retained-volume-backup-import/operations/"
                    "retained-import-plan-operation-1"
                ),
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["operation"]["status"], "pass")
        self.assertEqual(payload["result"]["plan_fingerprint"], plan.plan_fingerprint)
        self.assertTrue(payload["operation"]["poll_url"].endswith(operation.operation_id))


if __name__ == "__main__":
    unittest.main()
