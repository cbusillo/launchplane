"""Real collection and repair admission; provider IO uses the fixed receiver fixture."""

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_testing_usage import OdooTestingUsage
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    build_authz_policy_record_id,
    authz_policy_sha256,
)
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.disposable_lanes import require_disposable_lane, DisposableLaneSafetyUnknown
from tests.http_app_test_support import _RejectingVerifier, _local_operator_bearer_config
from tests.support.down_lanes import record_lane_health
from tests.support.http import request
from tests.test_odoo_testing_drill import TestingDrillScenario
from tests import test_odoo_testing_restore as restore_fixtures


class DownLaneCollectionTests(unittest.IsolatedAsyncioTestCase):
    database_url = ""

    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.fixture = TestingDrillScenario()
        self.fixture.seed(self.database_url or f"sqlite+pysqlite:///{root / 'state.db'}")
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        policy = LaunchplaneAuthzPolicy()
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=1, policy_sha256=authz_policy_sha256(policy)
                ),
                revision=1,
                status="active",
                source="test:down-lane-collection",
                updated_at=self.fixture.profile.updated_at,
                policy=policy,
            )
        )
        self.product, self.context = self.fixture.profile.product, self.fixture.context
        self.store.clear_disposable_lane_safety(self.product, self.context, "testing")
        self.native = restore_fixtures.FreshTestingHostProgramTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        assert inventory.artifact_identity is not None
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": RuntimeIdentity(
                        product=self.product,
                        context=self.context,
                        instance="testing",
                        deployment_record_id=inventory.deployment_record_id,
                        artifact_id=inventory.artifact_identity.artifact_id,
                        source_git_ref=inventory.source_git_ref,
                        image_reference=self.native.spec["image_reference"],
                    )
                }
            )
        )
        record_lane_health(self.store, self.product, self.context, datetime.now(timezone.utc))
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=root,
        )
        module = "control_plane.odoo_testing_drill_usage."
        self.enterContext(
            patch(
                module + "source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture-token"),
            )
        )
        self.enterContext(
            patch(
                module + "api.fetch_dokploy_target_payload",
                return_value={
                    "name": "fixture-testing",
                    "appName": self.fixture.gen.namespace,
                    "serverId": "fixture-server",
                },
            )
        )
        self.usage_reader = self.enterContext(
            patch(
                "control_plane.http_app.read_odoo_testing_usage",
                return_value=OdooTestingUsage(reason_code="web_configuration_unavailable"),
            )
        )
        self.transport = self.enterContext(
            patch(module + "inspect_native_testing_receiver", side_effect=self.inspect)
        )

    def inspect(self, **kwargs: Any) -> None:
        spec = kwargs["spec"]
        self.native.spec.update(spec, inspection_only=True)
        for volume in self.native.volumes.values():
            volume["Labels"]["io.launchplane.testing-resources"] = spec["generation"][
                "resources_binding_sha256"
            ]
        for container in self.native.containers.values():
            container["Config"]["Image"] = spec["image_reference"]
            container["Config"]["Env"] = [
                "ODOO_DB_NAME=" + spec["database_name"]
                if item.startswith("ODOO_DB_NAME=")
                else item
                for item in container["Config"]["Env"]
            ]
        status, output = self.native.run_program()
        if status:
            raise ValueError("Native resource proof unavailable")
        for line in output.splitlines():
            if line.startswith("fixture:"):
                kwargs["receive"](json.loads(line.removeprefix("fixture:")))

    async def observe(self, **changes: object) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/disposable-lanes/observations",
            headers={"Authorization": "Bearer local-operator-token"},
            payload={
                "product": self.product,
                "context": self.context,
                "instance": "testing",
                "mode": "inspect",
                **changes,
            },
        )

    async def test_collect_produces_real_resource_facts_then_admits_down_runtime_repair(
        self,
    ) -> None:
        inspected = await self.observe()
        self.assertEqual(inspected.status_code, 202, inspected.text)
        collected = await self.observe(
            mode="collect", expected_binding_sha256=inspected.json()["result"]["binding_sha256"]
        )
        self.assertEqual(collected.status_code, 202, collected.text)
        safety = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        assert safety is not None
        self.assertEqual(
            (safety.usage, safety.data, safety.production_resources),
            ("unknown", "reproducible", "isolated"),
        )

        def provider(**kwargs: Any) -> dict[str, object]:
            kwargs["before_provider_mutation"]()
            return {}

        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            side_effect=provider,
        ) as driver:
            repaired = await request(
                self.app,
                "POST",
                "/v1/live-target-runtime/apply",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "fixture-down-repair",
                },
                payload={
                    "mode": "apply",
                    "product": self.product,
                    "context": self.context,
                    "instance": "testing",
                    "deploy": False,
                },
            )
        self.assertEqual(repaired.status_code, 202, repaired.text)
        self.assertTrue(driver.called)

    async def collect(self) -> Any:
        inspected = await self.observe()
        self.assertEqual(inspected.status_code, 202, inspected.text)
        return await self.observe(
            mode="collect", expected_binding_sha256=inspected.json()["result"]["binding_sha256"]
        )

    async def test_expired_positive_usage_is_retained_but_does_not_block_fresh_down_repair(
        self,
    ) -> None:
        self.usage_reader.return_value = OdooTestingUsage("in_use", True, True, "complete")
        first = await self.collect()
        self.assertEqual(first.status_code, 202, first.text)
        before = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        assert before is not None
        self.store.write_disposable_lane_safety(
            before.model_copy(
                update={
                    "observed_at": before.observed_at - timedelta(hours=1),
                    "expires_at": before.expires_at - timedelta(hours=1),
                }
            )
        )
        self.usage_reader.return_value = OdooTestingUsage(
            reason_code="web_configuration_unavailable"
        )
        current = await self.collect()
        self.assertEqual(current.status_code, 202, current.text)
        self.assertTrue(current.json()["result"]["historical_usage_retained"])
        safety = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        assert safety is not None
        self.assertEqual(
            (safety.usage, safety.usage_evidence), (before.usage, before.usage_evidence)
        )
        self.assertEqual(
            require_disposable_lane(
                self.store, product=self.product, context=self.context, instance="testing"
            ),
            safety.binding_sha256,
        )

    async def test_needed_copy_and_failed_physical_inspection_still_refuse(self) -> None:
        first = await self.collect()
        self.assertEqual(first.status_code, 202, first.text)
        previous = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        assert previous is not None
        self.store.write_disposable_lane_safety(previous.model_copy(update={"data": "only_copy"}))
        current = await self.collect()
        self.assertEqual(current.status_code, 202, current.text)
        self.assertEqual(current.json()["result"]["data"], "only_copy")
        with self.assertRaises(DisposableLaneSafetyUnknown):
            require_disposable_lane(
                self.store, product=self.product, context=self.context, instance="testing"
            )
        self.store.write_disposable_lane_safety(previous)
        self.native.containers["web"]["HostConfig"]["PortBindings"] = {"8069/tcp": [{}]}
        failed = await self.collect()
        self.assertEqual(failed.status_code, 202, failed.text)
        self.assertEqual(
            (failed.json()["result"]["data"], failed.json()["result"]["production_resources"]),
            ("unknown", "unknown"),
        )
        with self.assertRaises(DisposableLaneSafetyUnknown):
            require_disposable_lane(
                self.store, product=self.product, context=self.context, instance="testing"
            )

    async def test_resource_denial_and_binding_drift_do_not_publish(self) -> None:
        self.transport.side_effect = NativeSourceAuthorizationDenied("private-provider-detail")
        denied = await self.collect()
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertNotIn("private-provider-detail", denied.text)
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        )

        def drift(**kwargs: Any) -> None:
            self.inspect(**kwargs)
            target = self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            )
            self.store.write_dokploy_target_record(
                target.model_copy(update={"env": {**target.env, "FIXTURE_DRIFT": "changed"}})
            )

        self.transport.side_effect = drift
        changed = await self.collect()
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        )

    async def test_recorded_shared_resources_survive_passing_inspection(self) -> None:
        self.assertEqual((await self.collect()).status_code, 202)
        previous = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
        assert previous is not None
        for protection in ("shared", "separation_ready"):
            with self.subTest(protection=protection):
                self.store.write_disposable_lane_safety(
                    previous.model_copy(update={"production_resources": protection})
                )
                current = await self.collect()
                self.assertEqual(current.status_code, 202, current.text)
                self.assertEqual(current.json()["result"]["production_resources"], protection)
                with self.assertRaises(DisposableLaneSafetyUnknown):
                    require_disposable_lane(
                        self.store, product=self.product, context=self.context, instance="testing"
                    )

    async def test_positive_usage_reads_are_distinct_but_retained_history_stays_stable(
        self,
    ) -> None:
        self.usage_reader.return_value = OdooTestingUsage("in_use", True, True, "complete")
        first = await self.collect()
        second = await self.collect()
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertNotEqual(
            first.json()["result"]["usage_evidence"], second.json()["result"]["usage_evidence"]
        )
        self.usage_reader.return_value = OdooTestingUsage(
            reason_code="web_configuration_unavailable"
        )
        retained = await self.collect()
        self.assertEqual(retained.status_code, 202, retained.text)
        self.assertEqual(
            retained.json()["result"]["usage_evidence"], second.json()["result"]["usage_evidence"]
        )
