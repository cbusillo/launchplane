from pathlib import Path

from control_plane import retained_volume_capture
from control_plane.dokploy import api, source
from control_plane.odoo_testing_resources import ApplicationTestingResources
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_activation import (
    activation_record,
    completed_application_activation,
    preparation_resources,
)
from control_plane.testing_application_activation import ApplicationActivationAdapter
from control_plane.testing_rebuild_driver import TestingRebuildRefused
from control_plane.testing_rebuild_service import GenericWebTestingRebuildIntent


def capture_rebuild_application_intent(
    root: Path, store: PostgresRecordStore, intent: GenericWebTestingRebuildIntent
) -> GenericWebTestingRebuildIntent:
    from control_plane.generic_web_testing_rebuild import capture_rebuild_application_specification

    intent = intent.model_copy(
        update={"application": capture_rebuild_application_specification(store, intent)}
    )
    previous = activation_record(store, intent.profile.product, intent.before.target.context)
    if (
        previous is None
        or previous.target_id != intent.before.target_id.target_id
        or previous.request.application is None
        or previous.request.application.state.kind != "volume"
    ):
        return intent
    if not completed_application_activation(store, previous):
        raise TestingRebuildRefused(
            "testing_rebuild_state_unverified", "The original state needs its completed activation."
        )
    resources, _ = preparation_resources(store, previous.product, previous.request, previous.scope)
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="application", target_id=previous.target_id
    )
    if (
        not isinstance(resources, ApplicationTestingResources)
        or resources.state_kind != "volume"
        or payload.get("applicationId") != previous.target_id
        or not ApplicationActivationAdapter.mount_matches(previous, payload, resources)
    ):
        raise TestingRebuildRefused(
            "testing_rebuild_state_unverified", "The original application's state mount changed."
        )
    volumes, complete = retained_volume_capture.capture_retained_volumes(
        host=host, token=token, payload=payload
    )
    if not complete or len(volumes) != 1:
        raise TestingRebuildRefused(
            "testing_rebuild_state_unverified", "The original physical volume is unobserved."
        )
    volume = volumes[0]
    if (
        volume.volume_name != resources.volumes["state"]
        or volume.application_id != previous.target_id
        or volume.application_name != payload.get("appName")
        or volume.server_id != payload.get("serverId")
        or not volume.volume_identity_sha256
        or not volume.engine_identity_sha256
    ):
        raise TestingRebuildRefused(
            "testing_rebuild_state_unverified",
            "The original volume custody differs from its allocation.",
        )
    return intent.model_copy(update={"retained_application": previous, "retained_volumes": volumes})
