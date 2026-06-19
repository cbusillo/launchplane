from __future__ import annotations

from control_plane.workflows.product_onboarding import ProductOnboardingApplyResult


def build_product_onboarding_service_result(
    onboarding_result: ProductOnboardingApplyResult,
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
    return result, driver_result
