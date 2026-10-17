import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.testing_replacement_resources import (
    ROUTE,
    TestingReplacementResourcesRequest,
    resource_request_payload,
    resource_wire_payload,
)
from control_plane.workflows.dokploy_target_adoption import (
    PROJECT_CREATE_PATH,
    ENVIRONMENT_CREATE_PATH,
    COMPOSE_CREATE_PATH,
)
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import prepare_held_rebuild
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class LegacyResourceRequestTests(unittest.TestCase):
    def test_historical_wire_and_identity_preserve_recovery_keys(self) -> None:
        artifact = json.loads(
            (
                Path(__file__).parent / "fixtures" / "legacy_testing_resource_request.json"
            ).read_text()
        )
        request = TestingReplacementResourcesRequest.model_validate(artifact["wire_payload"])
        self.assertEqual(resource_wire_payload(request), artifact["wire_payload"])
        self.assertEqual(resource_request_payload(request), artifact["request_payload"])
        self.assertEqual(
            canonical_json_sha256(resource_request_payload(request)), artifact["request_sha256"]
        )


class TestingReplacementResourceHttpTests(unittest.IsolatedAsyncioTestCase):
    database_url = ""
    idempotency_key = "resource-fixture"

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=self.database_url or f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.store.write_product_profile_record(self.profile)
        for lane in self.profile.lanes:
            target = DokployTargetRecord(
                context=lane.context,
                instance=lane.instance,
                target_name=f"example-{lane.instance}",
                updated_at=self.profile.updated_at,
            )
            target_id = DokployTargetIdRecord(
                context=lane.context,
                instance=lane.instance,
                target_id=f"compose-{lane.instance}",
                updated_at=self.profile.updated_at,
            )
            self.store.write_dokploy_target_record(target)
            self.store.write_dokploy_target_id_record(target_id)
            self.store.write_provider_target_record(
                ProviderTargetRecord.from_dokploy_records(
                    target_record=target,
                    target_id_record=target_id,
                )
            )
        self.context = next(
            lane.context for lane in self.profile.lanes if lane.instance == "testing"
        )
        self.operation = prepare_held_rebuild(
            self.store, self.profile.product, self.context, prepare=False
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    async def post(self, **changes: object) -> Any:
        return await request(
            self.app,
            "POST",
            ROUTE.format(product=self.profile.product),
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": self.idempotency_key,
            },
            payload={
                "context": self.context,
                "operation_id": self.operation.operation_id,
                "reason": "Prepare new isolated testing storage",
                **changes,
            },
        )

    async def test_pending_route_effects_keep_the_retained_target_attached(self) -> None:
        from control_plane.contracts.dokploy_target_record import PublicHostEffect

        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        held = target.model_copy(
            update={
                "pending_public_host_effects": (
                    PublicHostEffect(
                        host="retained-route.example.invalid",
                        provider_host="https://provider.invalid",
                        target_id="compose-testing",
                        runtime_port=8069,
                    ),
                )
            }
        )
        self.store.write_dokploy_target_record(held)
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            held,
        )

    async def test_plan_preserves_every_original_record_and_makes_no_provider_call(self) -> None:
        before = self.store.read_product_profile_record(self.profile.product)
        target = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        with patch(
            "control_plane.testing_replacement_resources.source.read_dokploy_config"
        ) as provider:
            response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(response.json()["result"]["applied"])
        provider.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), before)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ),
            target,
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.profile.product, self.context, "testing")
        )

    async def test_wrong_operation_production_and_client_cannot_prepare(self) -> None:
        with patch(
            "control_plane.testing_replacement_resources.source.read_dokploy_config"
        ) as provider:
            for changes in (
                {"instance": "prod"},
                {"context": "another-context"},
                {"operation_id": "missing"},
            ):
                with self.subTest(changes=changes):
                    self.assertGreaterEqual((await self.post(**changes)).status_code, 400)
            with scoped_agent_transport():
                self.assertEqual((await self.post()).status_code, 403)
        provider.assert_not_called()
        self.assertIsNone(
            self.store.read_disposable_replacement_preparation(self.operation.operation_id)
        )

    async def test_sqlite_rehearsal_cannot_publish_positive_resource_facts(self) -> None:
        plan = await self.post()
        with patch(
            "control_plane.testing_replacement_resources.source.read_dokploy_config"
        ) as provider:
            applied = await self.post(
                mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
            )
            readback = await self.post(mode="readback")
        self.assertEqual(applied.status_code, 503, applied.text)
        self.assertEqual(readback.status_code, 503, readback.text)
        provider.assert_not_called()
        self.assertIsNone(
            self.store.read_disposable_lane_safety(self.profile.product, self.context, "testing")
        )


class TestingResourceScheduleTests(unittest.TestCase):
    def test_real_created_compose_defaults_and_provider_app_name_reach_host_readback(self) -> None:
        from unittest.mock import Mock
        from uuid import uuid4
        from control_plane.odoo_testing_resources import OdooTestingResources
        from control_plane.testing_replacement_resources import run_resource_schedule
        from control_plane.dokploy import post_deploy

        resources = OdooTestingResources(
            product="example",
            context="example",
            operation_id="retained",
            target_id="new",
            namespace=f"lp-testing-{uuid4().hex}",
        )
        project = resources.namespace + "-abc123"
        payload = {
            "composeId": resources.target_id,
            "serverId": "new-server",
            "environmentId": "new-environment",
            "appName": project,
            "sourceType": "github",
            "autoDeploy": True,
            "env": None,
            "composeFile": None,
        }
        target = DokployTargetRecord(
            context="example", instance="testing", updated_at="2026-01-01T00:00:00Z"
        )
        store = Mock()
        store.read_dokploy_target_record.return_value = target
        evidence = {
            "testing_resources_ok": "true",
            "testing_resources_binding_sha256": resources.binding_sha256,
            "schedule_id": "new-schedule",
        }
        with (
            patch(
                "control_plane.testing_replacement_resources.source.read_dokploy_config",
                return_value=("provider.invalid", "fixture"),
            ),
            patch(
                "control_plane.testing_replacement_resources.api.fetch_dokploy_target_payload",
                return_value=payload,
            ),
            patch(
                "control_plane.testing_replacement_resources.api.list_deployments_for_target",
                return_value=(),
            ),
            patch(
                "control_plane.testing_replacement_resources.fetch_dokploy_compose_domains_for_target_setup",
                return_value=(),
            ),
            patch.object(
                post_deploy,
                "_resolve_dokploy_schedule_runtime",
                return_value=("server", "new-server", project, "new-server"),
            ),
            patch(
                "control_plane.testing_replacement_resources.api.find_matching_dokploy_schedule",
                return_value=None,
            ),
            patch.object(post_deploy, "run_manual_data_schedule", return_value=evidence) as run,
        ):
            self.assertEqual(
                run_resource_schedule(
                    root=Path("."),
                    store=store,
                    resources=resources,
                    mode="readback",
                    checkpoint=lambda: None,
                ),
                evidence,
            )
            # The executed artifact carries the actual provider project identity.
            script = run.call_args.kwargs["schedule_payload"]["script"]
            self.assertEqual(script, resources.script("readback", compose_project=project))
            from control_plane.dokploy.source import DokployTargetDefinition

            candidate = DokployTargetDefinition(
                context=resources.context,
                instance="testing",
                target_id=resources.target_id,
                project_name=resources.namespace,
                target_name=resources.namespace,
            )
            store.reset_mock()
            run.reset_mock()
            self.assertEqual(
                run_resource_schedule(
                    root=Path("."),
                    store=store,
                    resources=resources,
                    mode="prepare",
                    checkpoint=lambda: None,
                    candidate_target=candidate,
                    candidate_server_id="new-server",
                ),
                evidence,
            )
            store.assert_not_called()
            self.assertFalse(store.mock_calls)
            self.assertEqual(run.call_args.kwargs["target_definition"], candidate)
            for changes in (
                {"target_id": "serving-target"},
                {"instance": "prod"},
                {"context": "another-lane"},
                {"env": {"ODOO_DB_VOLUME": "retained-db"}},
                {"domains": ("serving.example.invalid",)},
                {"public_hosts": ("serving.example.invalid",)},
            ):
                with self.subTest(candidate=changes):
                    run.reset_mock()
                    with self.assertRaisesRegex(ValueError, "exact empty, unrouted"):
                        run_resource_schedule(
                            root=Path("."),
                            store=store,
                            resources=resources,
                            mode="prepare",
                            checkpoint=lambda: None,
                            candidate_target=candidate.model_copy(update=changes),
                            candidate_server_id="new-server",
                        )
                    run.assert_not_called()
            run.reset_mock()
            with self.assertRaisesRegex(ValueError, "placement changed"):
                run_resource_schedule(
                    root=Path("."),
                    store=store,
                    resources=resources,
                    mode="prepare",
                    checkpoint=lambda: None,
                    candidate_target=candidate,
                    candidate_server_id="another-server",
                )
            run.assert_not_called()
            for field, value in (
                ("customGitSSHKeyId", "existing-key"),
                ("composeFile", "services: {}"),
                ("appName", "foreign-project"),
            ):
                with self.subTest(field=field):
                    changed = {**payload, field: value}
                    with patch(
                        "control_plane.testing_replacement_resources.api.fetch_dokploy_target_payload",
                        return_value=changed,
                    ):
                        with self.assertRaises(ValueError):
                            run_resource_schedule(
                                root=Path("."),
                                store=store,
                                resources=resources,
                                mode="prepare",
                                checkpoint=lambda: None,
                            )


class TestingResourceCaptureRecoveryTests(unittest.TestCase):
    def test_only_realized_prior_phases_allow_missing_next_phase_to_resume(self) -> None:
        from contextlib import nullcontext
        from datetime import datetime, timezone
        from unittest.mock import Mock
        from uuid import uuid4
        from control_plane.contracts.disposable_lane import DisposableTestingResourcePreparation
        from control_plane.testing_replacement_resources import (
            TestingReplacementResourceAdapter,
            TestingReplacementResourcesRequest,
        )

        preparation = DisposableTestingResourcePreparation(
            operation_id="retained",
            product="example",
            context="example",
            retained_target_id="old",
            prepared_by="service",
            recorded_at=datetime.now(timezone.utc),
            resource_namespace=f"lp-testing-{uuid4().hex}",
        )
        store = Mock()
        store.read_disposable_resource_preparation.return_value = preparation
        store.testing_replacement_execution_lock.side_effect = lambda *_: nullcontext(lambda: None)
        adapter = TestingReplacementResourceAdapter(
            Path("."),
            store,
            "example",
            TestingReplacementResourcesRequest(
                context="example", operation_id="retained", reason="recover bounded creation"
            ),
            "fixture-trace",
        )
        for placement, phase, expected in (
            (("", "", ""), "", "absent"),
            (("", "", ""), PROJECT_CREATE_PATH, "unknown"),
            (("project", "", ""), PROJECT_CREATE_PATH, "absent"),
            (("project", "", ""), ENVIRONMENT_CREATE_PATH, "unknown"),
            (("project", "environment", ""), ENVIRONMENT_CREATE_PATH, "absent"),
            (("project", "environment", ""), COMPOSE_CREATE_PATH, "unknown"),
        ):
            with (
                self.subTest(placement=placement, phase=phase),
                patch.object(adapter, "created_placement", return_value=placement),
                patch.object(adapter, "capture_checkpoint"),
            ):
                observation = adapter.recover_created_target(phase)
                assert observation is not None
                self.assertEqual(observation.outcome, expected)
                self.assertEqual(observation.retry_safe, expected == "absent")

    def test_provider_inventory_accepts_its_relation_names_and_refuses_ambiguity(self) -> None:
        from unittest.mock import Mock
        from control_plane.testing_replacement_resources import (
            TestingReplacementResourceAdapter,
            TestingReplacementResourcesRequest,
        )

        adapter = TestingReplacementResourceAdapter(
            Path("."),
            Mock(),
            "example",
            TestingReplacementResourcesRequest(
                context="example", operation_id="retained", reason="read actual placement"
            ),
            "fixture",
        )
        namespace = "service-namespace"
        for relation in ("compose", "composes"):
            project: dict[str, Any] = {
                "name": namespace,
                "projectId": "new-project",
                "environments": [
                    {"name": "provider-default", relation: []},
                    {
                        "name": namespace,
                        "environmentId": "new-environment",
                        relation: [{"name": namespace, "composeId": "new-compose"}],
                    },
                ],
            }
            with (
                self.subTest(relation=relation),
                patch(
                    "control_plane.testing_replacement_resources.source.read_dokploy_config",
                    return_value=("provider.invalid", "fixture"),
                ),
                patch(
                    "control_plane.testing_replacement_resources.api.fetch_dokploy_projects",
                    return_value=(project,),
                ),
            ):
                self.assertEqual(
                    adapter.created_placement(namespace),
                    ("new-project", "new-environment", "new-compose"),
                )
                project["environments"][1]["compose" if relation == "composes" else "composes"] = []
                with self.assertRaises(ValueError):
                    adapter.created_placement(namespace)


if __name__ == "__main__":
    unittest.main()
