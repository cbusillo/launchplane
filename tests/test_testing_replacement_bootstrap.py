"""Fresh generation continuation against real record transactions and fake provider IO."""

from pathlib import Path
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import ProductOdooLaneDataPolicy
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.odoo_testing_restore import TestingCredentialsOverlap
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from control_plane.testing_replacement_bootstrap import (
    TestingReplacementBootstrapAdapter,
    TestingReplacementBootstrapRequest,
    TestingBootstrapPrerequisiteError,
    bootstrap_plan,
)
from control_plane.testing_replacement_resources import (
    TestingReplacementResourceAdapter,
    TestingReplacementResourcesRequest,
)
from tests import test_testing_replacement_resources as resource_tests


class FreshTestingBootstrapScenario(unittest.TestCase):
    def seed(self, store_url: str) -> None:
        self.fixture = resource_tests.TestingReplacementResourceHttpTests()
        self.fixture.database_url = store_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.store = fixture.store
        profile = self.store.read_product_profile_record(fixture.profile.product)
        source_lane = next(lane for lane in profile.lanes if lane.instance == "prod")
        self.profile = profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_data_policy": ProductOdooLaneDataPolicy(
                                data_authority="resettable",
                                allowed_rebuild_sources=("upstream_restore",),
                                upstream_source=f"{profile.product}:{source_lane.context}:prod",
                            )
                        }
                    )
                    if lane.instance == "testing"
                    else lane
                    for lane in profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.production_before = self.store.read_dokploy_target_record(
            context_name=fixture.context, instance_name="prod"
        )
        self.assertTrue(
            self.store.prepare_disposable_testing_replacement(fixture.operation, "fixture-director")
        )
        allocation = self.store.read_disposable_resource_preparation(fixture.operation.operation_id)
        assert allocation is not None
        assigned = allocation.model_copy(update={"resource_namespace": "lp-testing-" + "a" * 32})
        self.store.compare_and_write_disposable_resource_preparation(
            expected=allocation, replacement=assigned
        )
        target = DokployTargetRecord(
            context=fixture.context,
            instance="testing",
            target_name=assigned.resource_namespace,
            updated_at=profile.updated_at,
        )
        target_id = DokployTargetIdRecord(
            context=fixture.context,
            instance="testing",
            target_id="fresh-compose",
            updated_at=profile.updated_at,
        )
        provider = ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        )
        with self.store.capture_disposable_resource_adoption(assigned, checkpoint=lambda: None):
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    dokploy_targets=(target,),
                    dokploy_target_ids=(target_id,),
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=provider,
                            expected_record=self.store.read_provider_target_record(
                                context_name=fixture.context, instance_name="testing"
                            ),
                        ),
                    ),
                )
            )
        self.resources = OdooTestingResources(
            product=profile.product,
            context=fixture.context,
            operation_id=fixture.operation.operation_id,
            target_id=target_id.target_id,
            namespace=assigned.resource_namespace,
        )
        resource_adapter = TestingReplacementResourceAdapter(
            fixture.root,
            self.store,
            profile.product,
            TestingReplacementResourcesRequest(
                context=fixture.context,
                operation_id=fixture.operation.operation_id,
                reason="fixture isolated resources",
            ),
            "fixture",
        )
        resource_adapter.publish(self.resources, "fixture-schedule")
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context=fixture.context,
                env={
                    "ODOO_UPSTREAM_HOST": "source.invalid",
                    "ODOO_UPSTREAM_USER": "restore-reader",
                    "ODOO_UPSTREAM_DB_USER": "postgres",
                    "ODOO_UPSTREAM_DB_NAME": "source_db",
                    "ODOO_UPSTREAM_FILESTORE_PATH": "/source/filestore/source_db",
                },
                updated_at=profile.updated_at,
            )
        )
        self.request = TestingReplacementBootstrapRequest(
            context=fixture.context,
            operation_id=fixture.operation.operation_id,
            domain_host="fresh.example.invalid",
            reason="prepare fixture sanitized source copy",
        )
        self.routes: list[dict[str, Any]] = []
        self.route_writes = 0
        self.lease = Mock(spec=["assert_current", "checkpoint_effect"])

    def route(self, **kwargs: Any) -> str:
        self.route_writes += 1
        self.routes[:] = [
            {
                "domainId": "fresh-domain",
                "host": kwargs["domain_host"],
                "composeId": kwargs["compose_id"],
                "serviceName": "web",
                "port": kwargs["runtime_port"],
                "https": True,
                "certificateType": kwargs["certificate_type"],
                "path": "/",
                "internalPath": "/",
                "stripPath": False,
            }
        ]
        return "fresh-domain"

    def patches(self) -> None:
        self.enterContext(
            patch("control_plane.odoo_testing_restore.require_distinct_testing_credentials")
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.require_distinct_testing_credentials"
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.source.read_dokploy_config",
                return_value=("provider.invalid", "fixture-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.fetch_dokploy_compose_domains_for_target_setup",
                side_effect=lambda **_: tuple(self.routes),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.compose.ensure_compose_web_domain_route",
                side_effect=self.route,
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.run_resource_schedule",
                side_effect=lambda **kw: kw["checkpoint"](),
            )
        )

    def adapter(self) -> TestingReplacementBootstrapAdapter:
        return TestingReplacementBootstrapAdapter(
            self.fixture.root, self.store, self.profile.product, self.request, "fixture-trace"
        )

    def apply(self) -> Any:
        return self.adapter().apply("fixture-original-key", self.lease)

    def review(self) -> None:
        plan = bootstrap_plan(self.store, self.profile.product, self.request)
        self.request = self.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
        )

    def scenario(self, fault: str) -> None:
        self.patches()
        self.review()
        original_bundle = self.store.write_product_public_hosts_bundle
        original_queue = self.store.request_fresh_testing_reconcile
        if fault == "route-receipt":

            def interrupted(bundle: Any, **kwargs: Any) -> None:
                def lose() -> None:
                    raise RuntimeError("fixture lost route reply")

                original_bundle(bundle, **{**kwargs, "verify_provider": lose})

            with patch.object(
                self.store, "write_product_public_hosts_bundle", side_effect=interrupted
            ):
                with self.assertRaises(RuntimeError):
                    self.apply()
        elif fault == "fence":
            with patch.object(
                self.store,
                "supersede_disposable_testing_replacement",
                side_effect=RuntimeError("fixture crash before supersession"),
            ):
                with self.assertRaises(RuntimeError):
                    self.apply()
        elif fault == "queue":
            with patch.object(
                self.store,
                "request_fresh_testing_reconcile",
                side_effect=RuntimeError("fixture crash before queue"),
            ):
                with self.assertRaises(RuntimeError):
                    self.apply()
        elif fault == "queue-commit":

            def lost_queue(generation: Any) -> None:
                original_queue(generation)
                raise RuntimeError("fixture lost queue reply")

            with patch.object(
                self.store, "request_fresh_testing_reconcile", side_effect=lost_queue
            ):
                with self.assertRaises(RuntimeError):
                    self.apply()
            self.assertEqual(
                self.adapter()
                .observe("key", "bind_independent_testing_route", "reconcile")
                .outcome,
                "present",
            )
            return
        if fault != "normal":
            from control_plane.odoo_testing_restore import read_current_testing_generation

            with self.assertRaises(ValueError):
                read_current_testing_generation(
                    self.store,
                    product=self.profile.product,
                    context=self.fixture.context,
                    instance="testing",
                )
            observed = self.adapter().observe("key", "bind_independent_testing_route", "reconcile")
            self.assertEqual(observed.outcome, "absent")
            self.assertTrue(observed.retry_safe)
        self.apply()
        self.assertEqual(self.route_writes, 1)
        result = self.adapter().readback()
        self.assertTrue(result["applied"])
        held = self.store.read_odoo_stable_target_replacement_operation_record(
            self.fixture.operation.operation_id
        )
        self.assertEqual(held.status, "superseded")
        self.assertIsNone(held.recovery_journal)
        self.assertEqual(held.request, self.fixture.operation.request)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="prod"
            ),
            self.production_before,
        )
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        assert generation is not None
        self.assertTrue(generation.reconcile_requested_at)
        queued = self.store.read_product_reconcile_request(f"{self.profile.product}:testing")
        self.store.request_fresh_testing_reconcile(generation)
        self.assertEqual(self.store.read_product_reconcile_request(queued.target_key), queued)


class FreshTestingBootstrapPrerequisiteScenario(FreshTestingBootstrapScenario):
    def credential_comparison_failure(self) -> None:
        self.patches()
        with (
            patch(
                "control_plane.testing_replacement_bootstrap.require_distinct_testing_credentials",
                side_effect=TestingCredentialsOverlap("private fixture credential"),
            ),
            patch("control_plane.testing_replacement_bootstrap.encoded_restore_source") as source,
            self.assertRaises(TestingBootstrapPrerequisiteError) as caught,
        ):
            bootstrap_plan(self.store, self.profile.product, self.request)
        self.assertEqual(
            caught.exception.code,
            TestingBootstrapPrerequisiteError("testing_credentials_overlap").code,
        )
        self.assertNotIn("private fixture credential", str(caught.exception))
        source.assert_not_called()
        self.assertIsNone(
            self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            )
        )
        self.assertEqual(self.routes, [])
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="prod"
            ),
            self.production_before,
        )

    def missing_source_failure(self) -> None:
        self.patches()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context=self.fixture.context,
                env={"FIXTURE_SETTING": "present"},
                updated_at=self.profile.updated_at,
            )
        )
        with self.assertRaises(TestingBootstrapPrerequisiteError) as caught:
            bootstrap_plan(self.store, self.profile.product, self.request)
        self.assertEqual(
            caught.exception.code,
            TestingBootstrapPrerequisiteError("testing_restore_source_unconfigured").code,
        )
        self.assertIsNone(
            self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            )
        )
        self.assertEqual(self.routes, [])


class FreshTestingBootstrapObservationTests(unittest.TestCase):
    def test_unknown_dispatched_route_is_never_blindly_repeated(self) -> None:
        store = Mock(spec=PostgresRecordStore)
        from tests.test_odoo_testing_restore import generation

        store.read_fresh_odoo_testing_generation.return_value = generation()
        adapter = TestingReplacementBootstrapAdapter(
            Path("."),
            store,
            "example-odoo",
            TestingReplacementBootstrapRequest(
                context="example",
                operation_id="held-example",
                domain_host="fresh.example.invalid",
                reason="recover fixture route",
            ),
            "fixture",
        )
        with (
            patch.object(adapter, "readback", side_effect=ValueError),
            patch.object(adapter, "_domains", return_value=()),
            patch("control_plane.testing_replacement_bootstrap.bootstrap_plan"),
        ):
            observation = adapter.observe("key", "bind_independent_testing_route", "reconcile")
            self.assertEqual(observation.outcome, "unknown")
            self.assertFalse(observation.retry_safe)


class TestingBootstrapHttpRefusalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = resource_tests.TestingReplacementResourceHttpTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    async def post(self, **changes: object) -> Any:
        from tests.support.http import request
        from control_plane.testing_replacement_bootstrap import ROUTE

        return await request(
            self.fixture.app,
            "POST",
            ROUTE.format(product=self.fixture.profile.product),
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "original-key",
            },
            payload={
                "context": self.fixture.context,
                "operation_id": self.fixture.operation.operation_id,
                "domain_host": "fresh.example.invalid",
                "reason": "fixture copy",
                **changes,
            },
        )

    async def test_wrong_context_operation_production_and_client_never_reach_provider(self) -> None:
        from tests.support.auth import scoped_agent_transport

        with patch(
            "control_plane.testing_replacement_bootstrap.source.read_dokploy_config"
        ) as provider:
            for changes in (
                {"instance": "prod"},
                {"context": "another"},
                {"operation_id": "missing"},
            ):
                with self.subTest(changes=changes):
                    self.assertGreaterEqual((await self.post(**changes)).status_code, 400)
            with scoped_agent_transport():
                self.assertEqual((await self.post()).status_code, 403)
            provider.assert_not_called()
        self.assertIsNone(
            self.fixture.store.read_fresh_odoo_testing_generation(
                self.fixture.profile.product, self.fixture.context
            )
        )

    async def test_sqlite_cannot_publish_bootstrap_or_private_failure_details(self) -> None:
        for mode in ("apply", "readback"):
            result = await self.post(mode=mode, reviewed_plan_sha256="a" * 64)
            self.assertEqual(result.status_code, 503, result.text)
        with patch(
            "control_plane.http_routes.testing_replacement_bootstrap.bootstrap_plan",
            side_effect=ValueError("private-value"),
        ):
            result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertNotIn("private-value", result.text)

    async def test_classified_prerequisite_refusal_remains_inert_and_value_free(self) -> None:
        prerequisite = TestingBootstrapPrerequisiteError("testing_credentials_unverified")
        with (
            patch(
                "control_plane.http_routes.testing_replacement_bootstrap.bootstrap_plan",
                side_effect=prerequisite,
            ),
            patch(
                "control_plane.testing_replacement_bootstrap.source.read_dokploy_config"
            ) as provider,
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], prerequisite.code)
        provider.assert_not_called()
        self.assertIsNone(
            self.fixture.store.read_fresh_odoo_testing_generation(
                self.fixture.profile.product, self.fixture.context
            )
        )
