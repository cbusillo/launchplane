"""Bounded successful-login reads for Q347's in-place testing drill only."""

from datetime import datetime, timedelta, timezone

from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
from control_plane.dokploy.native_copy_stream import inspect_native_testing_receiver
from control_plane.launchplane_mutations import control_plane_root
from control_plane.odoo_runtime_reads import (
    assert_selection_current,
    connect_runtime,
    inspect_runtime_container,
    select_stable_runtime,
)
from control_plane.odoo_testing_restore import (
    FreshOdooTestingGeneration,
    generation_binding,
)
from control_plane.storage.postgres import PostgresRecordStore

# Cover a full daily drill interval. Count every account, including administrators
# and portal users; no account list or privileged-user exclusion can hide use.
DRILL_RECENT_USE_SECONDS = 24 * 60 * 60


def read_drill_recent_signins(
    store: PostgresRecordStore, generation: FreshOdooTestingGeneration, now: float
) -> bool:
    profile = store.read_product_profile_record(generation.product)
    selection = select_stable_runtime(store, profile, "testing")
    if selection.identity.context != generation.context:
        raise ValueError("Testing runtime belongs to another context.")
    connection = connect_runtime(
        store=store,
        selection=selection,
        control_plane_root=control_plane_root(),
        database_url=store.database_url,
    )
    if connection.evidence.target_id != generation.target_id:
        raise ValueError("Testing runtime belongs to another target.")
    environment = inspect_runtime_container(
        connection.host,
        connection.token,
        connection.server_id,
        connection.evidence.container_id,
        connection.app_name,
        selection.identity,
    )
    frames: list[dict[str, object]] = []
    inspect_native_testing_receiver(
        host=connection.host,
        token=connection.token,
        server_id=connection.server_id,
        spec={
            "generation": generation.model_dump(mode="json"),
            "compose_project": connection.app_name,
            "image_reference": selection.identity.image_reference,
            "database_name": connection.credentials["ODOO_DB_NAME"],
            "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
            "expected_overrides_payload": environment.get(
                "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", ""
            ),
            "copy_binding_sha256": generation_binding(generation),
            "workflow_environment": {},
            "usage_only": True,
            "usage_since": (
                datetime.fromtimestamp(now, timezone.utc)
                - timedelta(seconds=DRILL_RECENT_USE_SECONDS)
            ).isoformat(),
        },
        receive=frames.append,
    )
    assert_selection_current(store, selection)
    if (
        len(frames) != 1
        or frames[0].get("type") != "complete"
        or type(frames[0].get("recent_signins")) is not bool
    ):
        raise ValueError("Testing successful-login history is unreadable.")
    return bool(frames[0]["recent_signins"])
