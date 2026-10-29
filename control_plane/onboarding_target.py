from __future__ import annotations

from typing import TYPE_CHECKING

from control_plane.generic_web_onboarding import (
    GenericWebOnboardingIntent,
    generic_web_onboarding_plan_sha256,
)
from control_plane.http_routes.mutation_support import request_fingerprint
from control_plane.storage.postgres import PostgresRecordStore

if TYPE_CHECKING:
    from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope


def onboarding_target_digests(request: DokployTargetSetupEnvelope) -> dict[str, str]:
    if request.onboarding is None:
        raise ValueError("New-product allocation requires its onboarding intent.")
    return {
        "onboarding_plan_sha256": generic_web_onboarding_plan_sha256(request.onboarding),
        "target_request_sha256": request_fingerprint(
            request.model_dump(
                mode="json",
                exclude={
                    "mode",
                    "reason",
                    "confirmation",
                    "onboarding",
                    "reviewed_onboarding_plan_sha256",
                    "reviewed_target_request_sha256",
                    "retain_allocation",
                    "expected_allocation_custody_sha256",
                },
            )
        ),
    }


def validate_onboarding_target_request(request: DokployTargetSetupEnvelope) -> None:
    intent = request.onboarding
    if intent is None:
        raise ValueError("New-product allocation requires its onboarding intent.")
    if (
        request.operation != "create-application"
        or request.instance != "testing"
        or request.target_type != "application"
        or request.source_type != "raw"
        or request.project_id
        or request.environment_id
        or request.target_id
        or request.app_name
        or request.domains
        or request.custom_git_branch
        or request.state_volume_path
        or request.state_volume_plan_id
        or request.expected_plan_sha256
        or request.replaces_operation_id
        or request.expected_current_provider_target is not None
        or request.browser_review is not None
        or not request.server_id
        or request.environment_name != "testing"
        or request.context != intent.testing_context
        or request.target_name != intent.target_name
        or request.project_name != intent.project_name
        or request.server_id != intent.server_id
        or request.healthcheck_path != intent.health_path
        or request.source_git_ref != f"origin/{intent.default_branch}"
    ):
        raise ValueError(
            "Onboarding only allocates its new empty testing application in new placement."
        )
    digests = onboarding_target_digests(request)
    if request.mode != "dry-run" and (
        request.reviewed_onboarding_plan_sha256 != digests["onboarding_plan_sha256"]
        or request.reviewed_target_request_sha256 != digests["target_request_sha256"]
    ):
        raise ValueError("Onboarding allocation differs from the reviewed target and record plans.")


def require_unallocated_onboarding_contexts(
    store: PostgresRecordStore, intent: GenericWebOnboardingIntent
) -> None:
    from control_plane.application_state_volume import require_no_held_state_volume

    require_no_held_state_volume(store, intent.preview_context)
    contexts = {intent.testing_context, intent.preview_context}
    for profile in store.list_product_profile_records():
        owned = {lane.context for lane in profile.lanes} | set(profile.historical_contexts)
        if profile.preview is not None:
            owned.add(profile.preview.context)
        if profile.product == intent.product or contexts.intersection(owned):
            raise ValueError(
                "Onboarding requires a new product and unowned testing and preview contexts."
            )
    if any(
        record.context in contexts
        for records in (
            store.list_dokploy_target_records(),
            store.list_dokploy_target_id_records(),
            store.list_physical_provider_target_records(),
        )
        for record in records
    ) or any(store.list_deployment_records(context_name=context) for context in contexts):
        raise ValueError("Onboarding contexts already contain target or deployment records.")
