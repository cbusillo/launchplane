"""Read the native rebuild's inputs without reserving a copy or dispatching effects."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

import click
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.odoo_target_replacement_apply_http import (
    _validate_target_replacement_apply_artifact_authority,
)
from control_plane.odoo_target_replacement_plan_http import (
    resolve_odoo_target_replacement_plan_lane,
)
from control_plane.odoo_testing_native_source import native_testing_source_authority
from control_plane.testing_candidate_runtime import candidate_credential_authority
from control_plane.runtime_environments import resolve_site_runtime_environment
from control_plane.dokploy import post_deploy, api
from control_plane.dokploy.native_copy_stream import (
    NativeSourceAuthorizationDenied,
    NativeSourceUnavailable,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_rebuild_service import (
    capture_rebuild_intent,
    check_testing_rebuild_protection,
    TestingRebuildRefused,
)
from control_plane.workflows.ship import utc_now_timestamp


class TestingRebuildPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pass"] = "pass"
    product: str
    context: str
    instance: Literal["testing"] = "testing"
    base_url: str
    artifact_id: str
    source_git_ref: str
    data_source_mode: Literal["empty", "production_restore", "upstream_restore"]
    observed_at: str
    # This is admission evidence. The worker still proves the actual independent
    # candidate's isolation, credentials, restore, health and cutover before use.
    candidate_verification: Literal["pending"] = "pending"


def inspect_testing_rebuild(
    *,
    root: Path,
    store: PostgresRecordStore,
    request: OdooStableTargetReplacementRequest,
    verified_manifest: ArtifactIdentityManifest | None = None,
) -> TestingRebuildPreflight:
    if request.strategy != "replace-and-cutover":
        raise ValueError("Native preflight requires the testing copy lifecycle.")
    apply = OdooStableTargetReplacementApplyRequest.model_validate(request.model_dump())
    lane = resolve_odoo_target_replacement_plan_lane(
        record_store=store, product=apply.product, instance=apply.instance
    )
    _validate_target_replacement_apply_artifact_authority(
        operation_store=store,
        replacement_request=apply,
        context=lane.context,
        verified_manifest=verified_manifest,
    )
    now = utc_now_timestamp()
    # A local read identity, never persisted or passed to a provider executor.
    operation = OdooStableTargetReplacementOperationRecord(
        operation_id="preflight-" + uuid4().hex,
        product=apply.product,
        context=lane.context,
        instance="testing",
        idempotency_key="read-only",
        request_fingerprint="read-only",
        request=apply,
        created_at=now,
        updated_at=now,
    )
    try:
        intent = capture_rebuild_intent(root, store, operation)
        check_testing_rebuild_protection(root, store, operation, intent)
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied(
                "The provider refused the native preflight read."
            ) from error
        raise TestingRebuildRefused(
            "testing_rebuild_inputs_unverified",
            "The retained testing inputs could not be verified.",
        ) from error
    except NativeSourceUnavailable as error:
        raise TestingRebuildRefused(
            "testing_rebuild_observation_unavailable",
            "The retained testing observation is unavailable.",
        ) from error
    except (NativeSourceAuthorizationDenied, TestingRebuildRefused):
        raise
    except (ValueError, click.ClickException, FileNotFoundError) as error:
        raise TestingRebuildRefused(
            "testing_rebuild_inputs_unverified",
            "The retained testing inputs could not be verified.",
        ) from error
    try:
        candidate_credential_authority(store, lane.context)
    except NativeSourceAuthorizationDenied:
        raise
    except (ValueError, click.ClickException, FileNotFoundError) as error:
        raise TestingRebuildRefused(
            "testing_credentials_unverified",
            "The generated testing passwords or their lane bindings are unavailable.",
        ) from error
    active = store.list_odoo_stable_target_replacement_operation_records(
        product=apply.product,
        context_name=lane.context,
        instance_name="testing",
        statuses=("pending", "running", "reconciliation_required"),
        limit=1,
    )
    if active:
        raise TestingRebuildRefused(
            "testing_rebuild_active", "The testing lane already has an unfinished rebuild."
        )
    try:
        if apply.data_source_mode == "production_restore":
            native_testing_source_authority(
                store, profile=intent.profile, testing_context=lane.context
            )
        elif apply.data_source_mode == "upstream_restore":
            environment = resolve_site_runtime_environment(
                control_plane_root=root,
                database_url=store.database_url,
                context_name=lane.context,
                instance_name="testing",
                target_environment_override={},
                secret_binding_keys=frozenset(),
            )
            post_deploy.resolve_upstream_restore_workflow_environment(
                desired_env_map={**environment.values, **intent.before.target.env}
            )
    except NativeSourceAuthorizationDenied:
        raise
    except (ValueError, click.ClickException, FileNotFoundError) as error:
        raise TestingRebuildRefused(
            "testing_restore_source_unconfigured",
            "The selected testing restore source is not configured or available.",
        ) from error
    # Configuration is not a completed or healthy copy. The worker verifies the
    # real source and independent candidate before exposing it at the address.
    assert apply.data_source_mode in {"empty", "production_restore", "upstream_restore"}
    return TestingRebuildPreflight(
        product=apply.product,
        context=lane.context,
        base_url=lane.base_url,
        artifact_id=apply.artifact_id,
        source_git_ref=apply.source_git_ref,
        data_source_mode=apply.data_source_mode,
        observed_at=now,
    )
