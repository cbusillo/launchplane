from __future__ import annotations

from control_plane.workflows.product_onboarding import ProductOnboardingApplyResult


def build_product_onboarding_service_result(
    onboarding_result: ProductOnboardingApplyResult,
) -> tuple[dict[str, object], dict[str, object]]:
    result: dict[str, object] = {
        "product_profile": onboarding_result.product_profile.product,
        "dokploy_target_count": len(onboarding_result.dokploy_targets),
        "dokploy_target_id_count": len(onboarding_result.dokploy_target_ids),
        "runtime_environment_record_count": len(onboarding_result.runtime_environments),
        "secret_binding_count": len(onboarding_result.secret_bindings),
    }
    driver_result: dict[str, object] = {
        "product": onboarding_result.product,
        "product_profile": onboarding_result.product_profile.model_dump(mode="json"),
        "dokploy_targets": [
            record.model_dump(mode="json") for record in onboarding_result.dokploy_targets
        ],
        "dokploy_target_ids": [
            {
                "context": record.context,
                "instance": record.instance,
                "target_id_recorded": bool(record.target_id.strip()),
                "updated_at": record.updated_at,
                "source_label": record.source_label,
            }
            for record in onboarding_result.dokploy_target_ids
        ],
        "runtime_environment_records": [
            {
                "scope": record.scope,
                "context": record.context,
                "instance": record.instance,
                "env_keys": sorted(record.env),
                "updated_at": record.updated_at,
                "source_label": record.source_label,
            }
            for record in onboarding_result.runtime_environments
        ],
        "secret_bindings": [
            {
                "binding_id": binding.binding_id,
                "integration": binding.integration,
                "binding_key": binding.binding_key,
                "context": binding.context,
                "instance": binding.instance,
                "status": binding.status,
                "updated_at": binding.updated_at,
            }
            for binding in onboarding_result.secret_bindings
        ],
    }
    return result, driver_result
