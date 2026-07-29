import hashlib
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_onboarding_manifest import (
    ProductOnboardingLaneManifest,
    ProductOnboardingManifest,
    ProductOnboardingSecretBindingManifest,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneProfile,
    ProductOdooPrelaunchRebuildPolicy,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProductAuthorityBundleStore,
    ProviderTargetWrite,
)
from control_plane.workflows.provider_target_dual_write import (
    prepare_provider_target_from_dokploy_records,
)
from control_plane.workflows.ship import utc_now_timestamp


class ProductOnboardingRecordStore(ProductAuthorityBundleStore, Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> None: ...

    def write_dokploy_target_record(self, record: DokployTargetRecord) -> None: ...

    def write_dokploy_target_id_record(self, record: DokployTargetIdRecord) -> None: ...

    def write_provider_target_record(self, record: ProviderTargetRecord) -> None: ...

    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]: ...

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> None: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...

    def write_secret_binding(self, binding: SecretBinding) -> None: ...


class ProductOnboardingApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    product_profile: LaunchplaneProductProfileRecord
    provider_targets: tuple[DokployTargetRecord, ...] = ()
    provider_target_ids: tuple[DokployTargetIdRecord, ...] = ()
    runtime_environments: tuple[RuntimeEnvironmentRecord, ...] = ()
    secret_bindings: tuple[SecretBinding, ...] = ()


class ProductOnboardingSecretBindingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_bindings: tuple[SecretBinding, ...] = ()
    retired_bindings: tuple[SecretBinding, ...] = ()


def build_product_profile_record(
    *,
    manifest: ProductOnboardingManifest,
    updated_at: str,
    existing_profile: LaunchplaneProductProfileRecord | None = None,
) -> LaunchplaneProductProfileRecord:
    historical_contexts = _merged_historical_contexts(
        manifest_contexts=manifest.historical_contexts,
        existing_profile=existing_profile,
    )
    return LaunchplaneProductProfileRecord(
        product=manifest.product,
        display_name=manifest.display_name,
        repository=manifest.repository,
        driver_id=manifest.driver_id,
        image=ProductImageProfile(repository=manifest.image_repository),
        runtime_port=manifest.runtime_port,
        health_path=manifest.health_path,
        lanes=tuple(
            ProductLaneProfile(
                instance=lane.instance,
                context=lane.context,
                base_url=lane.base_url,
                health_url=_lane_health_url(manifest=manifest, lane=lane),
                odoo_stable_bootstrap=lane.odoo_stable_bootstrap,
                odoo_prelaunch_rebuild=_lane_odoo_prelaunch_rebuild(
                    lane=lane,
                    existing_profile=existing_profile,
                ),
                odoo_data_policy=lane.odoo_data_policy,
                health_monitoring=_lane_health_monitoring(
                    lane=lane,
                    existing_profile=existing_profile,
                ),
            )
            for lane in manifest.lanes
        ),
        historical_contexts=historical_contexts,
        preview=ProductPreviewProfile.model_validate(manifest.preview.model_dump(mode="json")),
        promotion_workflow=manifest.promotion_workflow,
        expected_config=manifest.product_expected_config_profile(),
        updated_at=updated_at,
        source=manifest.source_label,
    )


def _lane_health_monitoring(
    *,
    lane: ProductOnboardingLaneManifest,
    existing_profile: LaunchplaneProductProfileRecord | None,
) -> ProductLaneHealthMonitoringPolicy:
    if existing_profile is None:
        return lane.health_monitoring
    existing_lane = next(
        (candidate for candidate in existing_profile.lanes if candidate.instance == lane.instance),
        None,
    )
    return existing_lane.health_monitoring if existing_lane is not None else lane.health_monitoring


def _lane_odoo_prelaunch_rebuild(
    *,
    lane: ProductOnboardingLaneManifest,
    existing_profile: LaunchplaneProductProfileRecord | None,
) -> ProductOdooPrelaunchRebuildPolicy:
    if existing_profile is None:
        return lane.odoo_prelaunch_rebuild
    existing_lane = next(
        (
            candidate
            for candidate in existing_profile.lanes
            if candidate.context == lane.context and candidate.instance == lane.instance
        ),
        None,
    )
    return (
        existing_lane.odoo_prelaunch_rebuild
        if existing_lane is not None
        else lane.odoo_prelaunch_rebuild
    )


def _lane_health_url(
    *, manifest: ProductOnboardingManifest, lane: ProductOnboardingLaneManifest
) -> str:
    health_url = lane.health_url.strip()
    if health_url:
        return health_url
    if manifest.driver_id == "odoo":
        return ""
    return _health_url(lane.base_url, manifest.health_path)


def _merged_historical_contexts(
    *,
    manifest_contexts: tuple[str, ...],
    existing_profile: LaunchplaneProductProfileRecord | None,
) -> tuple[str, ...]:
    historical_contexts: list[str] = []
    for raw_context in (
        *(existing_profile.historical_contexts if existing_profile else ()),
        *manifest_contexts,
    ):
        context = raw_context.strip()
        if context and context not in historical_contexts:
            historical_contexts.append(context)
    return tuple(historical_contexts)


def _read_existing_product_profile(
    *, record_store: ProductOnboardingRecordStore, product: str
) -> LaunchplaneProductProfileRecord | None:
    try:
        return record_store.read_product_profile_record(product)
    except (FileNotFoundError, KeyError):
        return None


def build_provider_target_records(
    *, manifest: ProductOnboardingManifest, updated_at: str
) -> tuple[DokployTargetRecord, ...]:
    return tuple(
        DokployTargetRecord(
            context=target.context,
            instance=target.instance,
            project_name=target.project_name,
            target_type=target.target_type,
            target_name=target.target_name,
            source_git_ref=target.source_git_ref,
            source_type=target.source_type,
            custom_git_url=target.custom_git_url,
            custom_git_branch=target.custom_git_branch,
            compose_path=target.compose_path,
            watch_paths=target.watch_paths,
            enable_submodules=target.enable_submodules,
            require_test_gate=target.require_test_gate,
            require_prod_gate=target.require_prod_gate,
            deploy_timeout_seconds=target.deploy_timeout_seconds,
            healthcheck_enabled=target.healthcheck_enabled,
            healthcheck_path=target.healthcheck_path or manifest.health_path,
            healthcheck_timeout_seconds=target.healthcheck_timeout_seconds,
            env=dict(target.env),
            domains=target.domains,
            updated_at=updated_at,
            source_label=manifest.source_label,
        )
        for target in manifest.provider_targets
    )


def build_provider_target_id_records(
    *, manifest: ProductOnboardingManifest, updated_at: str
) -> tuple[DokployTargetIdRecord, ...]:
    return tuple(
        DokployTargetIdRecord(
            context=target.context,
            instance=target.instance,
            target_id=target.target_id,
            updated_at=updated_at,
            source_label=manifest.source_label,
        )
        for target in manifest.provider_targets
        if target.target_id.strip()
    )


def build_physical_provider_target_records(
    *,
    provider_targets: tuple[DokployTargetRecord, ...],
    provider_target_ids: tuple[DokployTargetIdRecord, ...],
) -> tuple[ProviderTargetRecord, ...]:
    target_ids_by_route = {
        (record.context, record.instance): record for record in provider_target_ids
    }
    return tuple(
        ProviderTargetRecord.from_dokploy_records(
            target_record=target_record,
            target_id_record=target_id_record,
        )
        for target_record in provider_targets
        if (
            target_id_record := target_ids_by_route.get(
                (target_record.context, target_record.instance)
            )
        )
        is not None
    )


def build_runtime_environment_records(
    *, manifest: ProductOnboardingManifest, updated_at: str
) -> tuple[RuntimeEnvironmentRecord, ...]:
    return tuple(
        RuntimeEnvironmentRecord(
            scope=record.scope,
            context=record.context,
            instance=record.instance,
            env=dict(record.env),
            updated_at=updated_at,
            source_label=manifest.source_label,
        )
        for record in manifest.runtime_environments
    )


def build_secret_bindings(
    *,
    manifest: ProductOnboardingManifest,
    updated_at: str,
    existing_bindings: tuple[SecretBinding, ...] = (),
) -> ProductOnboardingSecretBindingPlan:
    existing_bindings_by_id = {binding.binding_id: binding for binding in existing_bindings}
    secret_bindings: list[SecretBinding] = []
    retired_bindings: list[SecretBinding] = []
    for binding in manifest.secret_bindings:
        binding_id = _secret_binding_id(product=manifest.product, binding=binding)
        existing_binding = existing_bindings_by_id.get(binding_id)
        if binding.status != "configured" and _configured_binding_satisfies_onboarding_binding(
            existing_bindings=existing_bindings,
            binding=binding,
        ):
            if existing_binding is not None and existing_binding.status == "disabled":
                retired_bindings.append(
                    existing_binding.model_copy(
                        update={
                            "integration": f"retired:{existing_binding.integration}",
                            "updated_at": updated_at,
                        }
                    )
                )
            continue
        secret_bindings.append(
            SecretBinding(
                binding_id=binding_id,
                secret_id=_secret_id(product=manifest.product, binding=binding),
                integration=binding.integration,
                binding_key=binding.binding_key,
                context=binding.context,
                instance=binding.instance,
                status=binding.status,
                created_at=existing_binding.created_at if existing_binding else updated_at,
                updated_at=updated_at,
            )
        )
    return ProductOnboardingSecretBindingPlan(
        active_bindings=tuple(secret_bindings),
        retired_bindings=tuple(retired_bindings),
    )


def _configured_binding_satisfies_onboarding_binding(
    *,
    existing_bindings: tuple[SecretBinding, ...],
    binding: ProductOnboardingSecretBindingManifest,
) -> bool:
    return any(
        existing.integration == binding.integration
        and existing.binding_key == binding.binding_key
        and existing.status == "configured"
        and _binding_route_satisfies_target(
            binding_context=existing.context,
            binding_instance=existing.instance,
            target_context=binding.context,
            target_instance=binding.instance,
        )
        for existing in existing_bindings
    )


def _binding_route_satisfies_target(
    *, binding_context: str, binding_instance: str, target_context: str, target_instance: str
) -> bool:
    if not binding_context:
        return True
    if binding_context != target_context:
        return False
    if not binding_instance:
        return True
    return binding_instance == target_instance


def apply_product_onboarding_manifest(
    *,
    record_store: ProductOnboardingRecordStore,
    manifest: ProductOnboardingManifest,
    updated_at: str = "",
) -> ProductOnboardingApplyResult:
    result, bundle = plan_product_onboarding_authority_bundle(
        record_store=record_store,
        manifest=manifest,
        updated_at=updated_at,
    )
    record_store.write_product_authority_bundle(bundle)
    return result


def plan_product_onboarding_authority_bundle(
    *,
    record_store: ProductOnboardingRecordStore,
    manifest: ProductOnboardingManifest,
    updated_at: str = "",
) -> tuple[ProductOnboardingApplyResult, ProductAuthorityBundle]:
    recorded_at = updated_at.strip() or manifest.updated_at.strip() or utc_now_timestamp()
    existing_product_profile = _read_existing_product_profile(
        record_store=record_store,
        product=manifest.product,
    )
    product_profile = build_product_profile_record(
        manifest=manifest,
        updated_at=recorded_at,
        existing_profile=existing_product_profile,
    )
    provider_targets = build_provider_target_records(manifest=manifest, updated_at=recorded_at)
    provider_target_ids = build_provider_target_id_records(
        manifest=manifest, updated_at=recorded_at
    )
    physical_provider_targets = build_physical_provider_target_records(
        provider_targets=provider_targets,
        provider_target_ids=provider_target_ids,
    )
    current_provider_targets = {
        (record.context, record.instance): record
        for record in record_store.list_physical_provider_target_records()
    }
    runtime_environments = build_runtime_environment_records(
        manifest=manifest, updated_at=recorded_at
    )
    secret_binding_plan = build_secret_bindings(
        manifest=manifest,
        updated_at=recorded_at,
        existing_bindings=record_store.list_secret_bindings(limit=None),
    )
    secret_bindings = secret_binding_plan.active_bindings
    target_records_by_route = {
        (record.context, record.instance): record for record in provider_targets
    }
    target_id_records_by_route = {
        (record.context, record.instance): record for record in provider_target_ids
    }
    provider_target_pairs = tuple(
        (
            target_records_by_route[(record.context, record.instance)],
            target_id_records_by_route[(record.context, record.instance)],
        )
        for record in physical_provider_targets
    )
    for target_record, target_id_record in provider_target_pairs:
        prepare_provider_target_from_dokploy_records(
            record_store=record_store,
            target_record=target_record,
            target_id_record=target_id_record,
        )

    result = ProductOnboardingApplyResult(
        product=manifest.product,
        product_profile=product_profile,
        provider_targets=provider_targets,
        provider_target_ids=provider_target_ids,
        runtime_environments=runtime_environments,
        secret_bindings=secret_bindings,
    )
    bundle = ProductAuthorityBundle(
        product_profiles=(product_profile,),
        dokploy_targets=provider_targets,
        dokploy_target_ids=provider_target_ids,
        provider_target_writes=tuple(
            ProviderTargetWrite(
                record=record,
                expected_record=current_provider_targets.get((record.context, record.instance)),
                expected_absent=(record.context, record.instance) not in current_provider_targets,
            )
            for record in physical_provider_targets
        ),
        runtime_environments=runtime_environments,
        secret_bindings=(*secret_bindings, *secret_binding_plan.retired_bindings),
    )
    return result, bundle


def _health_url(base_url: str, health_path: str) -> str:
    normalized_base_url = base_url.rstrip("/")
    if not normalized_base_url:
        return ""
    return f"{normalized_base_url}{health_path}"


def _secret_id(*, product: str, binding: ProductOnboardingSecretBindingManifest) -> str:
    if binding.secret_id.strip():
        return binding.secret_id.strip()
    parts = [product, binding.context, binding.instance, binding.binding_key]
    return "secret-" + _stable_slug("-".join(part for part in parts if part.strip()))


def _secret_binding_id(*, product: str, binding: ProductOnboardingSecretBindingManifest) -> str:
    if binding.binding_id.strip():
        return binding.binding_id.strip()
    digest_source = ":".join(
        (
            product.strip(),
            binding.integration.strip(),
            binding.context.strip(),
            binding.instance.strip(),
            binding.binding_key.strip(),
        )
    )
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]
    return f"binding-{_stable_slug(product)}-{digest}"


def _stable_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    return slug or "unnamed"
