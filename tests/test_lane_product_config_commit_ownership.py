import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _identity, _StubVerifier, local_operator_policy
from tests.support.http import request as http_request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.stores import sqlite_database_url
from tests.test_odoo_addon_settings_override import _seed_lane
from tests.test_odoo_addon_settings_override import _request_payload as addon_payload
from tests.test_integration_allowances import _request_payload as allowances_payload
from tests.test_testing_lane_hold import _payload as hold_payload


class LaneProductConfigCommitOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_addon_settings_refuse_context_reassignment_at_commit(self) -> None:
        await self._apply_route(
            "odoo-addon-settings", addon_payload(), "write_odoo_instance_override_record"
        )

    async def test_integration_allowances_refuse_context_reassignment_at_commit(self) -> None:
        await self._apply_route(
            "integration-allowances",
            allowances_payload(),
            "compare_and_write_dokploy_target_record",
        )

    async def test_testing_hold_refuses_context_reassignment_at_commit(self) -> None:
        await self._apply_route(
            "testing-hold", hold_payload(), "compare_and_write_dokploy_target_record"
        )

    async def _apply_route(self, route: str, payload: dict[str, object], writer_name: str) -> None:
        for store_type in (FilesystemRecordStore, PostgresRecordStore):
            for reassigned in (False, True):
                with (
                    self.subTest(route=route, store=store_type.__name__, reassigned=reassigned),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    store = (
                        FilesystemRecordStore(state_dir=root)
                        if store_type is FilesystemRecordStore
                        else PostgresRecordStore(
                            database_url=sqlite_database_url(root / "records.sqlite3")
                        )
                    )
                    if isinstance(store, PostgresRecordStore):
                        store.ensure_schema()
                    profile = LaunchplaneProductProfileRecord.model_validate(
                        _odoo_profile_payload_with_prod_lane()
                    )
                    store.write_product_profile_record(profile)
                    _seed_lane(store)
                    app = create_launchplane_fastapi_app(
                        verifier=_StubVerifier(_identity()),
                        authz_policy=local_operator_policy(
                            actions=("product_config.plan", "product_config.apply"),
                            products=(profile.product,),
                            contexts=("cm",),
                        ),
                        record_store_factory=lambda: store,
                        control_plane_root_path=root,
                        bearer_identity_config=BearerIdentityConfig(
                            local_operator_token="test-operator-token",
                            local_operator_subject="local-owner-agent",
                            local_operator_token_label="local-owner-write",
                        ),
                    )
                    headers = {"Authorization": "Bearer test-operator-token"}
                    path = f"/v1/product-config/{route}/apply"
                    dry_run = await http_request(
                        app, "POST", path, headers=headers, payload=payload
                    )
                    self.assertEqual(dry_run.status_code, 202, dry_run.text)
                    apply_payload = {
                        **payload,
                        "mode": "apply",
                        "reviewed_plan_sha256": dry_run.json()["result"]["plan_sha256"],
                    }
                    before = store.read_dokploy_target_record(
                        context_name="cm", instance_name="testing"
                    )
                    original_writer = getattr(store, writer_name)

                    def write_after_reassignment(*args: object, **kwargs: object) -> object:
                        self.assertEqual(
                            kwargs.get("required_context_owner"), (profile.product, "cm")
                        )
                        if reassigned:
                            store.write_product_profile_record(
                                profile.model_copy(update={"product": "other-product"})
                            )
                        return original_writer(*args, **kwargs)

                    with patch.object(
                        store, writer_name, side_effect=write_after_reassignment
                    ) as writer:
                        response = await http_request(
                            app,
                            "POST",
                            path,
                            headers={**headers, "Idempotency-Key": f"ownership-{route}"},
                            payload=apply_payload,
                        )
                    writer.assert_called_once()
                    self.assertEqual(
                        response.status_code, 403 if reassigned else 202, response.text
                    )
                    if reassigned:
                        self.assertEqual(
                            response.json()["error"]["code"], "local_operator_lane_scope_required"
                        )
                        self.assertEqual(
                            store.read_dokploy_target_record(
                                context_name="cm", instance_name="testing"
                            ),
                            before,
                        )
                        self.assertEqual(store.list_odoo_instance_override_records(), ())
                    else:
                        self.assertTrue(response.json()["result"]["applied"])
                    if isinstance(store, PostgresRecordStore):
                        self.assertEqual(store.list_product_reconcile_requests(), ())
                        store.close()
