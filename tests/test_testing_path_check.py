"""The OPW day's independent refusals must be visible in one supported read."""

from contextlib import ExitStack
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from httpx2 import Response

from control_plane.contracts.product_profile_record import ProductOdooLaneDataPolicy
from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
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

    async def test_four_opw_blockers_are_reported_together_without_writes(self) -> None:
        # Keep real DB records and encrypted credentials; no planner result is stubbed.
        with ExitStack() as stack:
            for name in (
                "write_secret_record",
                "write_secret_version",
                "write_secret_binding",
                "prepare_initial_testing_generation",
                "activate_initial_testing_preparation",
                "prepare_disposable_testing_replacement",
                "write_fresh_odoo_testing_generation",
            ):
                stack.enter_context(
                    patch.object(self.store, name, side_effect=AssertionError(name))
                )
            response = await self.read()
        self.assertEqual(response.status_code, 200, response.text)
        check = response.json()["check"]
        failures = {step["code"]: step for step in check["steps"] if step["state"] == "blocked"}
        for code in (
            "testing_task_generation_missing",
            "testing_resources_refused",
            "testing_restore_policy_unconfigured",
            "testing_credentials_overlap",
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
        self.assertEqual(steps["testing_task_generation_missing"]["state"], "blocked")
        self.assertNotIn("private token", response.text)
