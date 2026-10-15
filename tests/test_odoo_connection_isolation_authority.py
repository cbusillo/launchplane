"""Same authenticated agent observes or repairs only a qualified testing copy."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import record_safety
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class ConnectionIsolationAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.store.write_product_profile_record(profile)
        for lane in profile.lanes:
            self.store.write_dokploy_target_record(
                DokployTargetRecord(
                    context=lane.context, instance=lane.instance, updated_at=profile.updated_at
                )
            )
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=lane.context,
                    instance=lane.instance,
                    target_id=f"fixture-{lane.instance}",
                    updated_at=profile.updated_at,
                )
            )
        self.app = self.make_app()

    def make_app(self, config: BearerIdentityConfig | None = None) -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=config or _local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    async def call(
        self, mode: str | None, *, instance: str = "testing", key: str = "", **changes: Any
    ) -> Any:
        body: dict[str, Any] = {"context": "cm", "instance": instance}
        if mode is not None:
            body["connection_isolation"] = mode
        if mode == "apply":
            body.update(
                reviewed_dry_run=True,
                reviewed_plan_sha256="a" * 64,
                reason="Remove copied connections from the testing fixture",
            )
        body.update(changes)
        return await request(
            self.app,
            "POST",
            "/v1/drivers/odoo/post-deploy",
            headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
            payload={"product": "example-odoo", "post_deploy": body},
        )

    def safety(self, **changes: Any) -> None:
        record_safety(self.store, "example-odoo", "cm", "testing", **changes)

    async def test_same_director_can_observe_without_write_grant_or_disposable_facts(self) -> None:
        for mode in ("dry-run", "readback"):
            with (
                self.subTest(mode=mode),
                patch(
                    "control_plane.http_app.execute_odoo_post_deploy_result",
                    return_value=({}, {"post_deploy_status": "pass"}),
                ) as execute,
            ):
                response = await self.call(mode)
                self.assertEqual(response.status_code, 202, response.text)
                self.assertIsNone(execute.call_args.kwargs["before_connection_isolation_mutation"])
                self.assertEqual(
                    execute.call_args.kwargs["request"].post_deploy.connection_isolation, mode
                )
        self.assertIsNone(self.store.read_disposable_lane_safety("example-odoo", "cm", "testing"))

    async def test_terminal_director_observes_but_has_no_unrelated_maintenance(self) -> None:
        self.app = self.make_app(
            BearerIdentityConfig(
                terminal_agent_token="local-operator-token",
                terminal_agent_subject="fixture-terminal",
                terminal_agent_token_label="fixture",
            )
        )
        with patch(
            "control_plane.http_app.execute_odoo_post_deploy_result",
            return_value=({}, {"post_deploy_status": "pass"}),
        ) as execute:
            self.assertEqual((await self.call("dry-run")).status_code, 202)
            execute.reset_mock()
            self.assertEqual((await self.call(None)).status_code, 403)
            execute.assert_not_called()

    async def test_generic_agent_and_unknown_token_cannot_inherit_director_repair(self) -> None:
        self.safety()
        with (
            scoped_agent_transport(),
            patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute,
        ):
            for mode in ("dry-run", "readback", "apply"):
                response = await self.call(mode, key="scoped")
                self.assertEqual(response.status_code, 403, response.text)
            execute.assert_not_called()
        self.app = self.make_app(BearerIdentityConfig())
        self.assertEqual((await self.call("dry-run")).status_code, 401)

    async def test_director_repair_uses_bounded_driver_and_replays_stable_key(self) -> None:
        self.safety()
        target = DokployTargetDefinition(
            context="cm", instance="testing", target_type="compose", target_id="fixture-testing"
        )

        def repair(**kwargs: Any) -> dict[str, str]:
            self.assertEqual(kwargs["mode"], "apply")
            kwargs["before_mutation"]()
            return {"connection_isolation_ok": "true", "integration_readback_ok": "true"}

        with (
            patch(
                "control_plane.workflows.odoo_post_deploy._resolve_compose_target_definition",
                return_value=target,
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                return_value=("fixture", "fixture"),
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.run_connection_isolation",
                side_effect=repair,
            ) as run,
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update"
            ) as maintenance,
        ):
            first = await self.call("apply", key="reviewed-isolation")
            self.assertEqual(first.status_code, 202, first.text)
            second = await self.call("apply", key="reviewed-isolation")
            self.assertEqual(second.status_code, 202, second.text)
            run.assert_called_once()
            maintenance.assert_not_called()

    async def test_production_unknown_modes_and_maintenance_refuse_before_driver(self) -> None:
        with patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute:
            for mode in ("dry-run", "readback", "apply"):
                for instance in ("prod", "production", " PROD "):
                    with self.subTest(mode=mode, instance=instance):
                        self.assertEqual(
                            (await self.call(mode, instance=instance, key="prod")).status_code, 400
                        )
            self.assertEqual((await self.call("unknown")).status_code, 400)
            self.assertEqual(
                (await self.call("apply", instance="pr-1", key="preview")).status_code, 400
            )
            self.assertEqual((await self.call(None)).status_code, 403)
            self.assertEqual((await self.call("apply")).status_code, 400)
            self.assertEqual(
                (await self.call("apply", key="unreviewed", reviewed_dry_run=False)).status_code,
                400,
            )
            execute.assert_not_called()

    async def test_unknown_protected_and_stale_lanes_refuse_before_driver(self) -> None:
        with patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute:
            response = await self.call("apply", key="unknown")
            self.assertEqual(response.status_code, 409, response.text)
            for field, value in (
                ("usage", "in_use"),
                ("data", "only_copy"),
                ("production_resources", "shared"),
                ("production_resources", "unknown"),
            ):
                with self.subTest(field=field, value=value):
                    self.safety(**{field: value})
                    response = await self.call("apply", key=f"protected-{field}-{value}")
                    self.assertEqual(response.status_code, 409, response.text)
            now = datetime.now(timezone.utc)
            self.safety(
                observed_at=now - timedelta(minutes=10), expires_at=now - timedelta(minutes=5)
            )
            self.assertEqual((await self.call("apply", key="stale")).status_code, 409)
            execute.assert_not_called()

    async def test_target_alias_and_staff_hold_refuse_before_driver(self) -> None:
        self.safety()
        target = self.store.read_dokploy_target_id_record(
            context_name="cm", instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"target_id": "fixture-prod"})
        )
        with patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute:
            self.assertEqual((await self.call("apply", key="alias")).status_code, 409)
            self.store.write_dokploy_target_id_record(target)
            record = self.store.read_dokploy_target_record(
                context_name="cm", instance_name="testing"
            )
            hold = DokployTargetStaffTestingHold(reason="Staff test in progress")
            self.store.write_dokploy_target_record(
                record.model_copy(
                    update={
                        "policies": record.policies.model_copy(update={"staff_testing_hold": hold})
                    }
                )
            )
            self.safety()
            self.assertEqual((await self.call("apply", key="hold")).status_code, 409)
            execute.assert_not_called()

    async def test_safety_is_checked_again_immediately_before_provider_mutation(self) -> None:
        self.safety()

        def changed(**kwargs: Any) -> Any:
            self.safety(usage="in_use")
            kwargs["before_connection_isolation_mutation"]()
            self.fail("Changed protection must stop the repair")

        with patch("control_plane.http_app.execute_odoo_post_deploy_result", side_effect=changed):
            response = await self.call("apply", key="changed-usage")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "disposable_lane_safety_unknown")
