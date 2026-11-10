from __future__ import annotations

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import product_profile_record_sha256
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.workflows.product_onboarding import ProductOnboardingApplyResult


def build_product_onboarding_service_result(
    onboarding_result: ProductOnboardingApplyResult,
    *,
    manifest: ProductOnboardingManifest | None = None,
    authority_bundle: ProductAuthorityBundle | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    result: dict[str, object] = {
        "product_profile": onboarding_result.product_profile.product,
        "provider_target_count": len(onboarding_result.provider_targets),
        "provider_target_id_count": len(onboarding_result.provider_target_ids),
        "runtime_environment_record_count": len(onboarding_result.runtime_environments),
        "secret_binding_count": len(onboarding_result.secret_bindings),
    }
    target_ids_by_route = {
        (record.context, record.instance): record
        for record in onboarding_result.provider_target_ids
    }
    provider_targets = [
        {
            "context": record.context,
            "instance": record.instance,
            "target_type": record.target_type,
            "target_name": record.target_name,
            "target_id_recorded": bool(target_ids_by_route.get((record.context, record.instance))),
            "updated_at": record.updated_at,
        }
        for record in onboarding_result.provider_targets
    ]
    provider_target_ids = [
        {
            "context": record.context,
            "instance": record.instance,
            "target_id_recorded": bool(record.target_id.strip()),
            "updated_at": record.updated_at,
        }
        for record in onboarding_result.provider_target_ids
    ]
    driver_result: dict[str, object] = {
        "product": onboarding_result.product,
        "product_profile": {
            "product": onboarding_result.product_profile.product,
            "updated_at": onboarding_result.product_profile.updated_at,
        },
        "provider_targets": provider_targets,
        "provider_target_ids": provider_target_ids,
    }
    if manifest is not None and authority_bundle is not None:
        normalized_bundle = authority_bundle.model_dump(
            mode="json",
            exclude={
                "product_profiles": {"__all__": {"updated_at"}},
                "dokploy_targets": {"__all__": {"updated_at"}},
                "dokploy_target_ids": {"__all__": {"updated_at"}},
                "provider_target_writes": {"__all__": {"record": {"updated_at"}}},
                "runtime_environment_writes": {"__all__": {"record": {"updated_at"}}},
                "secret_bindings": {"__all__": {"updated_at"}},
            },
        )
        current = next(iter(authority_bundle.expected_product_profiles), None)
        desired_profile = onboarding_result.product_profile.model_dump(
            mode="json", exclude={"updated_at"}
        )
        current_profile = current.model_dump(mode="json", exclude={"updated_at"}) if current else {}
        current_lanes = (
            {(lane.context, lane.instance): lane for lane in current.lanes} if current else {}
        )
        lane_changes = []
        for lane in onboarding_result.product_profile.lanes:
            previous = current_lanes.get((lane.context, lane.instance))
            previous_fields = previous.model_dump(mode="json") if previous else {}
            changed_fields = sorted(
                name
                for name, value in lane.model_dump(mode="json").items()
                if previous_fields.get(name) != value
            )
            if changed_fields:
                lane_changes.append(
                    {"context": lane.context, "instance": lane.instance, "fields": changed_fields}
                )
        driver_result.update(
            plan_sha256=canonical_json_sha256(
                {"manifest": manifest.model_dump(mode="json"), "bundle": normalized_bundle}
            ),
            current_profile_sha256=product_profile_record_sha256(current) if current else None,
            desired_profile_sha256=canonical_json_sha256(desired_profile),
            changed_profile_fields=sorted(
                name
                for name, value in desired_profile.items()
                if current_profile.get(name) != value
            ),
            lane_changes=lane_changes,
        )
    return result, driver_result
