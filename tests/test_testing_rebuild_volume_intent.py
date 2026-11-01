import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from control_plane import retained_volume_capture
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import ProductGenericWebTestingState
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.testing_activation import activation_record, preparation_resources
from control_plane.testing_rebuild_driver import TestingRebuildRefused
from control_plane.testing_rebuild_service import (
    GenericWebTestingRebuildIntent,
    capture_rebuild_intent,
)
from tests import test_testing_application_flow as application


class TestingRebuildVolumeIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = application.ApplicationFlowTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.flow.addressed()
        self.flow.prepare()
        self.assertEqual(self.flow.activate().status, "completed")
        record = activation_record(self.flow.store, self.flow.product, self.flow.context)
        assert record is not None and record.request.application is not None
        self.record = record
        profile = self.flow.store.read_product_profile_record(self.flow.product)
        lane = next(item for item in profile.lanes if item.instance == "testing")
        lane.generic_web_testing_state = ProductGenericWebTestingState.model_validate(
            record.request.application.state.model_dump()
        )
        self.flow.store.write_product_profile_record(profile)
        resources, _ = preparation_resources(
            self.flow.store, record.product, record.request, record.scope
        )
        payload = self.flow.payloads[record.target_id]
        self.volume = RetainedVolumeBinding(
            volume_name=resources.volumes["state"],
            server_id=payload["serverId"],
            application_id=record.target_id,
            application_name=payload["appName"],
            volume_identity_sha256="a" * 64,
            engine_identity_sha256="b" * 64,
        )
        stamp = datetime.now(UTC).isoformat()
        self.operation = OdooStableTargetReplacementOperationRecord(
            operation_id="volume-rebuild",
            product=self.flow.product,
            context=self.flow.context,
            instance="testing",
            idempotency_key="volume-key",
            request_fingerprint="volume-request",
            request=OdooStableTargetReplacementApplyRequest(
                product=self.flow.product,
                instance="testing",
                strategy="replace-and-cutover",
                data_source_mode="empty",
                allow_empty_data=True,
                artifact_id=record.image_reference,
                source_git_ref=record.request.source_commit,
                reason="Rebuild an independently owned testing volume",
            ),
            created_at=stamp,
            updated_at=stamp,
        )

    def test_original_volume_and_native_custody_are_captured_without_provider_writes(self) -> None:
        writes = tuple(self.flow.writes)
        with patch.object(
            retained_volume_capture, "capture_retained_volumes", return_value=((self.volume,), True)
        ):
            intent = capture_rebuild_intent(self.flow.root, self.flow.store, self.operation)
        assert isinstance(intent, GenericWebTestingRebuildIntent)
        self.assertEqual(intent.retained_application, self.record)
        self.assertEqual(intent.retained_volumes, (self.volume,))
        self.assertEqual(tuple(self.flow.writes), writes)

    def test_incomplete_or_foreign_volume_custody_cannot_be_admitted(self) -> None:
        for volumes, complete in (
            ((self.volume,), False),
            ((), True),
            ((self.volume, self.volume), True),
            ((self.volume.model_copy(update={"volume_name": "another-volume"}),), True),
            ((self.volume.model_copy(update={"server_id": "another-server"}),), True),
            ((self.volume.model_copy(update={"application_id": "another-application"}),), True),
            ((self.volume.model_copy(update={"volume_identity_sha256": ""}),), True),
            ((self.volume.model_copy(update={"engine_identity_sha256": ""}),), True),
        ):
            with self.subTest(volumes=volumes, complete=complete):
                with patch.object(
                    retained_volume_capture,
                    "capture_retained_volumes",
                    return_value=(volumes, complete),
                ):
                    with self.assertRaises(TestingRebuildRefused):
                        capture_rebuild_intent(self.flow.root, self.flow.store, self.operation)


if __name__ == "__main__":
    unittest.main()
