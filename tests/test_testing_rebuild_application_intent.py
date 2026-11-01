import unittest

from control_plane import generic_web_testing_rebuild as rebuild
from control_plane.contracts.product_profile_record import ProductGenericWebTestingState
from control_plane.contracts.testing_application import TestingApplicationState
from control_plane.testing_activation import activation_record
from control_plane.testing_publication_storage import read_snapshot
from control_plane.testing_rebuild_service import GenericWebTestingRebuildIntent
from control_plane.testing_rebuild_driver import TestingRebuildRefused
from tests import test_testing_application_flow as application


class TestingRebuildApplicationIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = application.ApplicationFlowTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.flow.addressed()
        self.flow.prepare()
        outcome = self.flow.activate()
        self.assertEqual(outcome.status, "completed")
        self.record = activation_record(self.flow.store, self.flow.product, self.flow.context)
        assert self.record is not None and self.record.request.application is not None
        self.specification = self.record.request.application
        profile = self.flow.store.read_product_profile_record(self.flow.product)
        lane = next(lane for lane in profile.lanes if lane.instance == "testing")
        lane.generic_web_testing_state = ProductGenericWebTestingState.model_validate(
            self.specification.state.model_dump()
        )
        self.flow.store.write_product_profile_record(profile)
        self.intent = GenericWebTestingRebuildIntent(
            operation_id="fresh-rebuild",
            profile=profile,
            before=read_snapshot(
                self.flow.store, product=self.flow.product, context=self.flow.context
            ),
            state=self.specification.state,
        )

    def test_completed_current_recipe_keeps_existing_credential_and_storage_intent(self) -> None:
        captured = rebuild.capture_rebuild_application_specification(self.flow.store, self.intent)
        self.assertEqual(captured.local_credentials, self.specification.local_credentials)
        self.assertEqual(captured.external_credentials, self.specification.external_credentials)
        self.assertEqual(captured.state, self.specification.state)
        self.assertEqual(captured.network_name, self.specification.network_name)
        self.assertEqual(captured.runtime_port, self.intent.profile.runtime_port)
        self.assertEqual(captured.health_path, self.intent.profile.health_path)
        self.assertTrue(captured.local_credentials)
        self.assertEqual(
            activation_record(self.flow.store, self.flow.product, self.flow.context), self.record
        )

    def test_recipe_from_another_recipient_cannot_supply_rebuild_intent(self) -> None:
        rebound = self.intent.before.model_copy(
            update={
                "target_id": self.intent.before.target_id.model_copy(
                    update={"target_id": "another-recipient"}
                )
            }
        )
        with self.assertRaises(TestingRebuildRefused):
            rebuild.capture_rebuild_application_specification(
                self.flow.store, self.intent.model_copy(update={"before": rebound})
            )

    def test_current_state_declaration_does_not_reuse_historical_volume_intent(self) -> None:
        state = TestingApplicationState(kind="none")
        intent = self.intent.model_copy(update={"state": state})
        captured = rebuild.capture_rebuild_application_specification(self.flow.store, intent)
        self.assertEqual(captured.state, state)
        self.assertEqual(captured.local_credentials, self.specification.local_credentials)
        self.assertEqual(captured.network_name, self.specification.network_name)
        self.assertEqual(
            activation_record(self.flow.store, self.flow.product, self.flow.context), self.record
        )


if __name__ == "__main__":
    unittest.main()
