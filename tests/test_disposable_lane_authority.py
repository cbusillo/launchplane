from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import record_safety
from tests.support.http import request
from tests.support.profiles import _generic_site_profile_payload


class DisposableRuntimeAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'state.db'}")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        profile = LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        self.store.write_product_profile_record(
            profile.model_copy(update={"production_use": "live"})
        )
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
                    target_id=f"target-{lane.instance}",
                    updated_at=profile.updated_at,
                )
            )
        self.context = next(lane.context for lane in profile.lanes if lane.instance == "testing")
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=root,
        )

    async def sync(self, *, instance: str = "testing", deploy: bool = False) -> int:
        response = await request(
            self.app,
            "POST",
            "/v1/live-target-runtime/apply",
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": f"sync-{instance}-{deploy}",
            },
            payload={
                "mode": "apply",
                "product": "example-site",
                "context": self.context,
                "instance": instance,
                "deploy": deploy,
            },
        )
        return response.status_code

    async def test_testing_sync_on_live_product_gets_no_production_authority(self) -> None:
        record_safety(self.store, "example-site", self.context, "testing")
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={"tracked_target": {"target_id": "target-testing"}},
        ) as driver:
            self.assertEqual(await self.sync(), 202)
            self.assertEqual(driver.call_args.kwargs["instance_name"], "testing")
            driver.reset_mock()
            self.assertEqual(await self.sync(instance="prod"), 403)
            driver.assert_not_called()

    async def test_client_identity_gets_no_new_permission(self) -> None:
        record_safety(self.store, "example-site", self.context, "testing")
        with (
            scoped_agent_transport(),
            patch(
                "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
            ) as driver,
        ):
            self.assertEqual(await self.sync(), 403)
            driver.assert_not_called()

    async def test_protected_or_unknown_testing_never_reaches_the_driver(self) -> None:
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            for changes in (
                {"usage": "in_use"},
                {"data": "only_copy"},
                {"production_resources": "shared"},
                {"usage": "unknown"},
                {"binding_sha256": "0" * 64},
            ):
                with self.subTest(changes=changes):
                    record_safety(self.store, "example-site", self.context, "testing", **changes)
                    self.assertEqual(await self.sync(), 409)
            now = datetime.now(timezone.utc)
            record_safety(
                self.store,
                "example-site",
                self.context,
                "testing",
                observed_at=now - timedelta(minutes=20),
                expires_at=now - timedelta(minutes=10),
            )
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()

    async def test_production_target_alias_is_refused_even_with_an_isolation_claim(self) -> None:
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        production = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="prod"
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(update={"target_id": production.target_id})
        )
        record_safety(self.store, "example-site", self.context, "testing")
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()

    async def test_replacing_observation_does_not_change_an_inflight_apply_binding(self) -> None:
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord

        record_safety(self.store, "example-site", self.context, "testing")
        effects: list[str] = []

        def drift_then_write(**kwargs: object) -> dict[str, object]:
            self.store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context=self.context,
                    instance="testing",
                    env={"VOLUME_NAME": "new-binding"},
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            record_safety(self.store, "example-site", self.context, "testing")
            callback = kwargs["before_provider_mutation"]
            assert callable(callback)
            callback()
            effects.append("write")
            return {}

        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            side_effect=drift_then_write,
        ):
            self.assertEqual(await self.sync(), 400)
        self.assertEqual(effects, [])

    async def test_separation_stages_new_resources_without_starting_shared_containers(self) -> None:
        record_safety(
            self.store,
            "example-site",
            self.context,
            "testing",
            production_resources="separation_ready",
        )
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={},
        ) as driver:
            self.assertEqual(await self.sync(), 202)
            self.assertFalse(driver.call_args.kwargs["deploy"])
            driver.reset_mock()
            self.assertEqual(await self.sync(deploy=True), 409)
            driver.assert_not_called()
