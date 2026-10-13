import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import click
from pydantic import ValidationError

from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.generic_web_testing_exclusion import (
    GENERIC_WEB_TESTING_EXCLUSION_ROUTE,
    GenericWebTestingExclusionResponse,
)
from control_plane.generic_web_testing_exclusion import (
    inspect_generic_web_testing_exclusion,
    TestingExclusionRefused,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from tests.support.auth import local_operator_policy, StubVerifier, identity
from tests.support.http import request
from tests import test_testing_event_deploy_recovery as recovery_fixture


class GenericWebTestingExclusionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = recovery_fixture.TestingEventDeployRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.root = self.fixture.root
        self.product = self.fixture.profile.product
        self.context = self.fixture.context
        self.reservation = self.fixture.reserve(phase="deploy_trigger")
        self.hold()
        config = patch(
            "control_plane.dokploy.source.read_dokploy_config",
            return_value=("https://provider.test", "fixture-token"),
        )
        config.start()
        self.addCleanup(config.stop)
        self.fixture.responses["/api/deployment.allByType"] = [
            {
                "deploymentId": "old-failed",
                "composeId": self.fixture.target.resolved_target.target_id,
                "status": "error",
                "startedAt": "2026-10-01T10:00:00Z",
                "finishedAt": "2026-10-01T10:01:00Z",
                "title": "untitled historical deploy",
                "errorMessage": "secret private log",
            }
        ]

    def hold(self, enabled: bool = True) -> None:
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="private staff reason"
                            )
                            if enabled
                            else None
                        }
                    )
                }
            )
        )

    def inspect(self) -> GenericWebTestingExclusionResponse:
        return inspect_generic_web_testing_exclusion(
            store=self.store,
            root=self.root,
            product=self.product,
            context=self.context,
            instance="testing",
            trace_id="launchplane_req_fixture",
        )

    async def test_standing_read_without_original_inputs_is_inert_and_complete(self) -> None:
        before = self.store.list_held_provider_target_reservations()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=local_operator_policy(actions=("product_environment.read",)),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        result = await request(
            app,
            "GET",
            f"{GENERIC_WEB_TESTING_EXCLUSION_ROUTE}?product={self.product}&context={self.context}",
            headers={"Authorization": "Bearer fixture"},
        )
        self.assertEqual(result.status_code, 200, result.text)
        observed = GenericWebTestingExclusionResponse.model_validate(result.json())
        self.assertEqual(observed.status, "excluded")
        self.assertEqual(observed.historical_settlement, "not_evaluated")
        deployments = self.fixture.responses["/api/deployment.allByType"]
        assert isinstance(deployments, list)
        self.assertEqual(observed.deployments.count, len(deployments))
        self.assertEqual(self.store.list_held_provider_target_reservations(), before)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            target,
        )
        self.assertFalse(self.store.list_deployment_records())
        self.assertFalse(self.store.list_product_reconcile_requests())
        for private in (
            "private staff reason",
            "secret private log",
            "old-failed",
            self.fixture.target.resolved_target.target_id,
            "untitled historical deploy",
        ):
            self.assertNotIn(private, result.text)
        paths = {call.kwargs["path"] for call in self.fixture.read_mock.call_args_list}
        self.assertEqual(
            paths, {"/api/deployment.allByType", "/api/deployment.queueList", "/api/schedule.list"}
        )

    def test_missing_hold_does_not_establish_sustained_exclusion(self) -> None:
        self.hold(False)
        result = self.inspect()
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.hold.code, "hold_absent")

    def test_held_target_without_reservation_is_not_a_manual_dispatch_fence(self) -> None:
        with patch.object(self.store, "list_held_provider_target_reservations", return_value=()):
            result = self.inspect()
        self.assertEqual(result.execution.code, "reservation_fence_absent")
        self.assertEqual(result.status, "unknown")

    def test_active_old_executor_does_not_become_excluded_from_lease_expiry(self) -> None:
        with self.store.testing_replacement_execution_lock(
            testing_event_execution_lock_name(self.fixture.target_key)
        ) as check:
            self.assertIsNotNone(check)
            result = self.inspect()
        self.assertEqual(result.execution.code, "execution_lock_busy")
        self.assertEqual(result.status, "unknown")

    def test_unknown_inventory_does_not_hide_other_inspection_results(self) -> None:
        for path in (
            "/api/deployment.allByType",
            "/api/deployment.queueList",
            "/api/schedule.list",
        ):
            original = self.fixture.responses[path]
            for invalid in (None, {"items": [], "hasMore": True}, [None]):
                with self.subTest(path=path, invalid=invalid):
                    self.fixture.responses[path] = invalid
                    result = self.inspect()
                    self.assertEqual(result.status, "unknown")
            self.fixture.responses[path] = original

    def test_active_jobs_schedules_and_deployments_remain_unproved(self) -> None:
        target_id = self.fixture.target.resolved_target.target_id
        cases = {
            "/api/deployment.allByType": [
                {"deploymentId": "new", "composeId": target_id, "status": "running"}
            ],
            "/api/deployment.queueList": [{"data": {"composeId": target_id}, "state": "waiting"}],
            "/api/schedule.list": [{"enabled": False}],
        }
        for path, rows in cases.items():
            with self.subTest(path=path):
                original = self.fixture.responses[path]
                self.fixture.responses[path] = rows
                result = self.inspect()
                self.assertEqual(result.status, "unknown")
                facet = (
                    result.deployments
                    if "allByType" in path
                    else result.queue
                    if "queueList" in path
                    else result.schedules
                )
                self.assertEqual(facet.code, "provider_effect_active")
                self.fixture.responses[path] = original

    def test_unreadable_queue_retains_complete_history_and_schedule_observations(self) -> None:
        original = self.fixture.read

        def read(**kwargs: object) -> object:
            if kwargs["path"] == "/api/deployment.queueList":
                raise click.ClickException("private provider error")
            return original(**kwargs)

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=read):
            result = self.inspect()
        self.assertEqual(result.queue.code, "provider_read_unavailable")
        self.assertTrue(result.deployments.complete)
        self.assertTrue(result.schedules.complete)

    def test_target_drift_during_reads_invalidates_exclusion(self) -> None:
        def read(**kwargs: object) -> object:
            self.fixture.payload["appName"] = "repointed"
            return self.fixture.read(**kwargs)

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=read):
            result = self.inspect()
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.execution.code, "binding_changed")

    def test_hold_lift_during_reads_invalidates_exclusion(self) -> None:
        def read(**kwargs: object) -> object:
            self.hold(False)
            return self.fixture.read(**kwargs)

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=read):
            result = self.inspect()
        self.assertEqual(result.execution.code, "binding_changed")
        self.assertEqual(result.status, "unknown")

    def test_production_and_cross_product_scopes_are_refused_before_provider_read(self) -> None:
        with self.assertRaises(TestingExclusionRefused):
            inspect_generic_web_testing_exclusion(
                store=self.store,
                root=self.root,
                product=self.product,
                context=self.context,
                instance="prod",
                trace_id="launchplane_req_fixture",
            )
        from control_plane.product_config import ProductConfigError

        with self.assertRaises(ProductConfigError):
            inspect_generic_web_testing_exclusion(
                store=self.store,
                root=self.root,
                product=self.product,
                context="another-product",
                instance="testing",
                trace_id="launchplane_req_fixture",
            )
        self.fixture.read_mock.assert_not_called()

    def test_consumer_rejects_forged_clearance_and_extra_private_fields(self) -> None:
        payload = self.inspect().model_dump(mode="json")
        for edit in (
            {"provider_target_id": "private"},
            {
                "execution": {
                    "state": "unknown",
                    "code": "execution_lock_busy",
                    "complete": False,
                    "count": None,
                }
            },
        ):
            with self.subTest(edit=edit), self.assertRaises(ValidationError):
                GenericWebTestingExclusionResponse.model_validate(payload | edit)

    def test_consumer_outputs_bound_observation_and_unknown_without_raw_payloads(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "exclusion_consumer",
            Path(__file__).parents[1] / "scripts/read-generic-web-testing-exclusion.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = self.inspect().model_dump(mode="json")
        helper = {
            "public_identifier": lambda value: value,
            "prepare_operator_settings": lambda **_: {
                "service_url": "https://service.test",
                "token": "private-token",
            },
            "request_launchplane_read": lambda **_: payload,
            "assert_public_safe_shape": lambda _: None,
        }
        for expected in ("excluded", "unknown"):
            with self.subTest(expected=expected):
                payload["status"] = expected
                with (
                    patch(
                        "sys.argv",
                        [
                            "read",
                            "--operator-helper",
                            "helper.py",
                            "--product",
                            self.product,
                            "--context",
                            self.context,
                        ],
                    ),
                    patch.object(module.runpy, "run_path", return_value=helper),
                    patch("sys.stdout", new_callable=io.StringIO) as output,
                ):
                    code = module.main()
                self.assertEqual(code, 0 if expected == "excluded" else 1)
                self.assertEqual(json.loads(output.getvalue()), payload)
                self.assertNotIn("private-token", output.getvalue())


class TestingExclusionConsumerTests(unittest.TestCase):
    def test_consumer_uses_only_maintained_get_and_rejects_wrong_lane(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "exclusion_consumer",
            Path(__file__).parents[1] / "scripts/read-generic-web-testing-exclusion.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []

        def read(**kwargs: object) -> object:
            calls.append(kwargs)
            return {"status": "excluded", "product": "another-product", "token": "secret"}

        helper = {
            "public_identifier": lambda value: value,
            "prepare_operator_settings": lambda **_: {
                "service_url": "https://service.test",
                "token": "secret",
            },
            "request_launchplane_read": read,
        }
        with (
            patch(
                "sys.argv",
                [
                    "read",
                    "--operator-helper",
                    "helper.py",
                    "--product",
                    "site",
                    "--context",
                    "lane",
                ],
            ),
            patch.object(module.runpy, "run_path", return_value=helper),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(module.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "unknown")
        self.assertNotIn("secret", output.getvalue())
        self.assertEqual(calls[0]["path"], GENERIC_WEB_TESTING_EXCLUSION_ROUTE)
        self.assertEqual(
            calls[0]["query"], {"product": "site", "context": "lane", "instance": "testing"}
        )
