from __future__ import annotations

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import product_profile_record_sha256
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.workflows.product_onboarding import ProductOnboardingApplyResult


def build_product_onboarding_service_result(
    onboarding_result: ProductOnboardingApplyResult,
    *,
    manifest: ProductOnboardingManifest | None = None,
    authority_bundle: ProductAuthorityBundle | None = None,
    current_targets: tuple[DokployTargetRecord, ...] = (),
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
                "secret_bindings": {"__all__": {"created_at", "updated_at"}},
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
            previous = current_lanes.pop((lane.context, lane.instance), None)
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
        previous_targets = {(target.context, target.instance): target for target in current_targets}
        target_changes = []
        for target in onboarding_result.provider_targets:
            previous_target = previous_targets.get((target.context, target.instance))
            previous_target_fields = (
                previous_target.model_dump(mode="json") if previous_target else {}
            )
            fields = {
                name
                for name, value in target.model_dump(mode="json", exclude={"updated_at"}).items()
                if previous_target_fields.get(name) != value
            }
            for write in authority_bundle.provider_target_writes:
                if (write.record.context, write.record.instance) == (
                    target.context,
                    target.instance,
                ):
                    if (
                        write.expected_record is None
                        or write.expected_record.target_id != write.record.target_id
                    ):
                        fields.add("target_id")
            if fields:
                target_changes.append(
                    {
                        "context": target.context,
                        "instance": target.instance,
                        "fields": sorted(fields),
                    }
                )
        driver_result.update(
            plan_sha256=canonical_json_sha256(
                {
                    "manifest": manifest.model_dump(mode="json"),
                    "bundle": normalized_bundle,
                    "current_targets": [
                        target.model_dump(mode="json") for target in current_targets
                    ],
                }
            ),
            current_profile_sha256=product_profile_record_sha256(current) if current else None,
            desired_profile_sha256=canonical_json_sha256(desired_profile),
            changed_profile_fields=sorted(
                name
                for name, value in desired_profile.items()
                if current_profile.get(name) != value
            ),
            lane_changes=lane_changes,
            removed_lanes=[
                {"context": context, "instance": instance}
                for context, instance in sorted(current_lanes)
            ],
            provider_target_changes=target_changes,
            runtime_environment_changes=[
                {
                    "scope": write.record.scope,
                    "context": write.record.context,
                    "instance": write.record.instance,
                    "changed_key_count": sum(
                        key not in write.expected_record.env
                        or write.expected_record.env[key] != value
                        for key, value in write.record.env.items()
                    )
                    if write.expected_record
                    else len(write.record.env),
                }
                for write in authority_bundle.runtime_environment_writes
            ],
            secret_binding_changes=[
                {
                    "status": binding.status,
                    "retired": binding.integration.startswith("retired:"),
                }
                for binding in authority_bundle.secret_bindings
            ],
        )
    return result, driver_result
