"""The OPW day's independent refusals must be visible in one supported read."""

from contextlib import ExitStack
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from httpx2 import Response
from typing import Literal
from control_plane.product_path_check import PathCheckStep

from control_plane.contracts.product_profile_record import ProductOdooLaneDataPolicy
from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
from control_plane.odoo_walkthrough import WalkthroughPrerequisite
from control_plane.testing_replacement_bootstrap import BootstrapPrerequisiteCode
from control_plane.odoo_testing_usage import OdooTestingUsage
from control_plane.secrets import write_secret_value, LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR
from tests import test_testing_replacement_resources as resources
from tests.support.http import request


class TestingPathCheckHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = resources.TestingReplacementResourceHttpTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch(
                "control_plane.testing_initial_generation.read_odoo_testing_usage",
                return_value=OdooTestingUsage(),
            )
        )
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "synthetic-preflight-key",
                    "LAUNCHPLANE_SECRET_KEYS_JSON": "",
                },
            )
        )
        for lane in self.fixture.profile.lanes:
            for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS:
                write_secret_value(
                    record_store=self.store,
                    scope="context_instance",
                    integration="runtime_environment",
                    name=key.lower(),
                    plaintext_value="synthetic-overlap-" + key,
                    binding_key=key,
                    context_name=lane.context,
                    instance_name=lane.instance,
                )
        # Legacy successful replacement is not an eligible stranded-operation anchor.
        self.store.write_odoo_stable_target_replacement_operation_record(
            self.fixture.operation.model_copy(update={"status": "pass", "phase": "completed"})
        )
        self.query = urlencode(
            {
                "path": "testing",
                "intent": "provision",
                "operation_id": self.fixture.operation.operation_id,
                "task_id": "a" * 32,
            }
        )

    async def read(self) -> Response:
        return await request(
            self.fixture.app,
            "GET",
            f"/v1/products/{self.fixture.profile.product}/path-check?{self.query}",
            headers={"Authorization": "Bearer local-operator-token"},
        )

    async def test_default_testing_read_preserves_deploy_scope_without_copy_probes(self) -> None:
        with (
            patch(
                "control_plane.product_path_check._testing_reconcile_plan",
                return_value={
                    "deployed_operation_id": "deployed-fixture",
                },
            ),
            patch("control_plane.testing_path_check.read_testing_prerequisites") as prerequisites,
        ):
            response = await request(
                self.fixture.app,
                "GET",
                f"/v1/products/{self.fixture.profile.product}/path-check?path=testing",
                headers={"Authorization": "Bearer local-operator-token"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["check"]["state"], "clear")
        prerequisites.assert_not_called()

    async def test_copy_hold_and_unreadable_source_are_not_cleared(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        target = self.store.read_dokploy_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="fixture staging", purpose="isolation_staging"
                            ),
                        }
                    )
                }
            )
        )
        with patch(
            "control_plane.testing_replacement_bootstrap.native_testing_source_authority",
            side_effect=RuntimeError("private-source"),
        ):
            response = await self.read()
        steps = {step["step_id"]: step for step in response.json()["check"]["steps"]}
        self.assertEqual(steps["testing_generation_use"]["state"], "blocked")
        self.assertEqual(steps["testing_restore_source"]["state"], "unknown")
        self.assertNotIn("private-source", response.text)

    async def test_four_opw_blockers_are_reported_together_without_writes(self) -> None:
        # Keep real DB records and encrypted credentials; no planner result is stubbed.
        with ExitStack() as stack:
            writes = []
            for name in (
                "write_secret_record",
                "write_secret_version",
                "write_secret_binding",
                "prepare_initial_testing_generation",
                "activate_initial_testing_preparation",
                "prepare_disposable_testing_replacement",
                "write_fresh_odoo_testing_generation",
            ):
                writes.append(
                    stack.enter_context(
                        patch.object(self.store, name, side_effect=AssertionError(name))
                    )
                )
            response = await self.read()
            for write in writes:
                write.assert_not_called()
        self.assertEqual(response.status_code, 200, response.text)
        check = response.json()["check"]
        failures = {step["code"]: step for step in check["steps"] if step["state"] == "blocked"}
        for code in (
            WalkthroughPrerequisite.GENERATION_MISSING.value,
            "testing_resources_refused",
            "testing_restore_policy_unconfigured",
            BootstrapPrerequisiteCode.CREDENTIALS_OVERLAP,
        ):
            self.assertIn(code, failures)
            self.assertTrue(failures[code]["description"])
            self.assertNotEqual(failures[code]["fix"], "none")
        for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS:
            self.assertNotIn("synthetic-overlap-" + key, response.text)
        self.assertEqual(check["state"], "blocked")
        steps = {step["step_id"]: step for step in check["steps"]}
        self.assertEqual(steps["testing_task_physical_copy_unverified"]["state"], "unknown")

        # Fix only policy. The other independent blockers remain visible in the next read.
        profile = self.fixture.profile
        production = next(lane for lane in profile.lanes if lane.instance == "prod")
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(
                            update={
                                "odoo_data_policy": ProductOdooLaneDataPolicy(
                                    data_authority="resettable",
                                    allowed_rebuild_sources=("upstream_restore",),
                                    upstream_source=f"{profile.product}:{production.context}:prod",
                                )
                            }
                        )
                        if lane.instance == "testing"
                        else lane
                        for lane in profile.lanes
                    )
                }
            )
        )
        response = await self.read()
        steps = {step["step_id"]: step for step in response.json()["check"]["steps"]}
        self.assertEqual(steps["testing_restore_policy"]["state"], "clear")
        self.assertEqual(steps["testing_credentials"]["code"], "testing_credentials_overlap")
        self.assertEqual(steps["testing_resource_allocation"]["state"], "blocked")

    async def test_unreadable_credentials_do_not_hide_policy_and_missing_generation(self) -> None:
        with patch.object(
            self.store, "read_secret_version", side_effect=RuntimeError("private token")
        ):
            response = await self.read()
        self.assertEqual(response.status_code, 200, response.text)
        steps = {step["step_id"]: step for step in response.json()["check"]["steps"]}
        self.assertEqual(steps["testing_credentials"]["state"], "unknown")
        self.assertEqual(steps["testing_restore_policy"]["state"], "blocked")
        self.assertEqual(steps["testing_task_generation_unverified"]["state"], "blocked")
        self.assertNotIn("private token", response.text)


class TestingResourceResumePathTests(unittest.TestCase):
    def test_source_intent_must_match_qualified_initial_resources(self) -> None:
        from tests.test_testing_replacement_bootstrap import FreshTestingBootstrapScenario
        from control_plane.testing_path_check import read_testing_prerequisites

        fixture = FreshTestingBootstrapScenario()
        fixture.seed("", initial_generation=True)
        self.addCleanup(fixture.doCleanups)
        preparation = fixture.store.read_disposable_resource_preparation(
            fixture.request.operation_id
        )
        assert preparation is not None

        def allocation(source_mode: Literal["ssh", "container-export"]) -> PathCheckStep:
            steps = read_testing_prerequisites(
                intent="generation",
                store=fixture.store,
                profile=fixture.profile,
                root=fixture.fixture.root,
                source_mode=source_mode,
                task_id=None,
                operation_id=fixture.request.operation_id,
                action_allowed=lambda *args: True,
                caller_can_provision=True,
                caller_can_generate=True,
            )
            return next(step for step in steps if step.step_id == "testing_resource_allocation")

        with patch(
            "control_plane.testing_initial_generation.read_odoo_testing_usage",
            return_value=OdooTestingUsage(usage="unused"),
        ) as probe:
            self.assertEqual(allocation("ssh").state, "clear")
            self.assertEqual(allocation("container-export").state, "blocked")
            self.assertEqual(probe.call_args.kwargs["target"], preparation.retained_target_record)
