"""Bounded successful-login reads for Q347's in-place testing drill only."""

from datetime import datetime, timedelta, timezone

from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
from control_plane.dokploy import api, source
from control_plane.dokploy.native_copy_stream import inspect_native_testing_receiver
from control_plane.launchplane_mutations import control_plane_root
from control_plane.odoo_runtime_reads import assert_selection_current, select_stable_runtime
from control_plane.odoo_testing_restore import (
    FreshOdooTestingGeneration,
    generation_binding,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH

# Cover a full daily drill interval. Count every account, including administrators
# and portal users; no account list or privileged-user exclusion can hide use.
DRILL_RECENT_USE_SECONDS = 24 * 60 * 60


class DrillUsageUnreadable(ValueError):
    """A read did not establish the successful-login history."""


class DrillCopyAlreadyServing(ValueError):
    """A reclaimed copy may already contain a tester's work."""


def testing_drill_environment(store: PostgresRecordStore, context: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    for scope in ("context", "instance"):
        for record in store.list_runtime_environment_records(context_name=context):
            if record.scope == scope and record.instance == (
                "testing" if scope == "instance" else ""
            ):
                environment.update({key: str(value) for key, value in record.env.items()})
    target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
    environment.update({key: str(value) for key, value in target.env.items()})
    return environment


def read_drill_recent_signins(
    store: PostgresRecordStore,
    generation: FreshOdooTestingGeneration,
    now: float,
    *,
    history_generation: FreshOdooTestingGeneration | None = None,
    history_database: str = "",
    usage_required: bool = True,
    since: datetime | None = None,
) -> bool | None:
    profile = store.read_product_profile_record(generation.product)
    selection = select_stable_runtime(store, profile, "testing")
    if selection.identity.context != generation.context:
        raise ValueError("Testing runtime belongs to another context.")
    target = store.read_dokploy_target_record(
        context_name=generation.context, instance_name="testing"
    )
    target_id = store.read_dokploy_target_id_record(
        context_name=generation.context, instance_name="testing"
    )
    if target.target_type != "compose" or target_id.target_id != generation.target_id:
        raise ValueError("Testing runtime belongs to another target.")
    host, token = source.read_dokploy_config(
        control_plane_root=control_plane_root(), database_url=store.database_url
    )
    provider = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id.target_id
    )
    if str(provider.get("name") or "") != target.target_name:
        raise ValueError("Testing provider target changed.")
    environment = testing_drill_environment(store, generation.context)
    database = environment.get("ODOO_DB_NAME", "")
    history = history_generation or generation
    history_database = history_database or database
    # Preparation precedes any possible testing use. Completion is too late:
    # people can sign in after web opens but before final health is recorded.
    prepared = datetime.fromisoformat(history.prepared_at.replace("Z", "+00:00"))
    completed = datetime.fromisoformat(history.completed_at.replace("Z", "+00:00"))
    moment = datetime.fromtimestamp(now, timezone.utc)
    if (
        prepared.utcoffset() is None
        or completed.utcoffset() is None
        or not prepared <= completed <= moment
        or since is not None
        and (since.utcoffset() is None or since > moment)
    ):
        raise ValueError("Testing copy timestamps are unreadable.")
    frames: list[dict[str, object]] = []
    inspect_native_testing_receiver(
        host=host,
        token=token,
        server_id=str(provider.get("serverId") or ""),
        spec={
            "generation": generation.model_dump(mode="json"),
            "compose_project": str(provider.get("appName") or ""),
            "image_reference": selection.identity.image_reference,
            "database_name": history_database,
            "usage_database_names": [database, history_database],
            "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
            "copy_binding_sha256": generation_binding(history),
            "workflow_environment": {},
            "usage_only": True,
            "usage_required": usage_required,
            "usage_reclaim": history_generation is not None,
            "usage_new_database": generation.drill_database,
            "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
            "usage_since": max(
                prepared,
                moment - timedelta(seconds=DRILL_RECENT_USE_SECONDS),
                since or prepared,
            ).isoformat(),
        },
        receive=frames.append,
    )
    assert_selection_current(store, selection)
    return parse_drill_usage_result(frames)


def parse_drill_usage_result(frames: list[dict[str, object]]) -> bool | None:
    """Keep unavailable occupancy distinct from the fixed receiver's resource proof."""
    if (
        len(frames) == 1
        and frames[0].get("type") == "complete"
        and frames[0].get("new_copy_serving") is True
    ):
        raise DrillCopyAlreadyServing("The new testing copy has already been opened.")
    if frames == [{"type": "complete", "resources_qualified": True}]:
        return None
    if (
        len(frames) != 1
        or frames[0].get("type") != "complete"
        or type(frames[0].get("recent_signins")) is not bool
    ):
        raise DrillUsageUnreadable("Testing successful-login history is unreadable.")
    return bool(frames[0]["recent_signins"])
