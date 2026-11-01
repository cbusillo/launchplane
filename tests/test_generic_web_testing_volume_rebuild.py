from pathlib import Path
from typing import Any
from unittest.mock import patch

from control_plane import retained_volume_capture
from control_plane.contracts.product_profile_record import ProductGenericWebTestingState
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.testing_activation import activation_record
from tests import test_generic_web_testing_rebuild as rebuild


class GenericWebTestingVolumeRebuildTests(rebuild.GenericWebTestingRebuildFlowFixture):
    with_unresolved_predecessor = True

    def configure_serving_application(self) -> None:
        self.flow.prepare()
        self.assertEqual(self.flow.activate().status, "completed")
        current = activation_record(self.store, self.flow.product, self.flow.context)
        assert current is not None and current.request.application is not None
        self.original = current
        self.original_volume = next(
            item["volumeName"] for item in self.flow.payloads[current.target_id]["mounts"]
        )
        profile = self.store.read_product_profile_record(self.flow.product)
        lane = next(item for item in profile.lanes if item.instance == "testing")
        lane.generic_web_testing_state = ProductGenericWebTestingState.model_validate(
            current.request.application.state.model_dump()
        )
        self.store.write_product_profile_record(profile)
        self.enterContext(
            patch.object(
                retained_volume_capture, "capture_retained_volumes", side_effect=self.capture_volume
            )
        )

    def capture_volume(self, **kwargs: Any) -> tuple[tuple[RetainedVolumeBinding, ...], bool]:
        payload = kwargs["payload"]
        name = payload["mounts"][0]["volumeName"]
        return (
            RetainedVolumeBinding(
                volume_name=name,
                application_id=payload["applicationId"],
                application_name=payload["appName"],
                server_id=payload["serverId"],
                volume_identity_sha256="a" * 64,
                engine_identity_sha256="b" * 64,
            ),
        ), True

    def test_rebuild_uses_fresh_state_and_retires_the_original_volume_after_health(self) -> None:
        original_path = Path(str(self.flow.storage.volumes[self.original_volume]["Mountpoint"]))
        (original_path / "old-data").write_text("Retained previous testing data")
        result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        current = activation_record(self.store, self.flow.product, self.flow.context)
        assert current is not None
        current_mount = self.flow.payloads[current.target_id]["mounts"][0]
        self.assertNotEqual(current_mount["volumeName"], self.original_volume)
        self.assertNotIn(self.original_volume, self.flow.storage.volumes)
        self.assertNotIn(self.original.target_id, self.flow.payloads)
        self.assertFalse(
            (
                Path(str(self.flow.storage.volumes[current_mount["volumeName"]]["Mountpoint"]))
                / "old-data"
            ).exists()
        )
