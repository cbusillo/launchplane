import hashlib
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_onboarding_manifest import (
    ProductOnboardingManifest,
    ProductOnboardingSecretBindingManifest,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.workflows.ship import utc_now_timestamp


class ProductOnboardingRecordStore(Protocol):
    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> None: ...

    def write_dokploy_target_record(self, record: DokployTargetRecord) -> None: ...

    def write_dokploy_target_id_record(self, record: DokployTargetIdRecord) -> None: ...

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> None: ...

    def write_secret_binding(self, binding: SecretBinding) -> None: ...


class ProductOnboardingApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    product_profile: LaunchplaneProductProfileRecord
    dokploy_targets: tuple[DokployTargetRecord, ...] = ()
    dokploy_target_ids: tuple[DokployTargetIdRecord, ...] = ()
    runtime_environments: tuple[RuntimeEnvironmentRecord, ...] = ()
    secret_bindings: tuple[SecretBinding, ...] = ()


def build_product_profile_record(
    *, manifest: ProductOnboardingManifest, updated_at: str
) -> LaunchplaneProductProfileRecord:
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
                health_url=lane.health_url or _health_url(lane.base_url, manifest.health_path),
            )
            for lane in manifest.lanes
        ),
        preview=ProductPreviewProfile.model_validate(manifest.preview.model_dump(mode="json")),
        promotion_workflow=manifest.promotion_workflow,
        updated_at=updated_at,
        source=manifest.source_label,
    )


def build_dokploy_target_records(
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
        for target in manifest.dokploy_targets
    )


def build_dokploy_target_id_records(
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
        for target in manifest.dokploy_targets
        if target.target_id.strip()
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
    *, manifest: ProductOnboardingManifest, updated_at: str
) -> tuple[SecretBinding, ...]:
    return tuple(
        SecretBinding(
            binding_id=_secret_binding_id(product=manifest.product, binding=binding),
            secret_id=_secret_id(product=manifest.product, binding=binding),
            integration=binding.integration,
            binding_key=binding.binding_key,
            context=binding.context,
            instance=binding.instance,
            status=binding.status,
            created_at=updated_at,
            updated_at=updated_at,
        )
        for binding in manifest.secret_bindings
    )


def apply_product_onboarding_manifest(
    *,
    record_store: ProductOnboardingRecordStore,
    manifest: ProductOnboardingManifest,
    updated_at: str = "",
) -> ProductOnboardingApplyResult:
    recorded_at = updated_at.strip() or manifest.updated_at.strip() or utc_now_timestamp()
    product_profile = build_product_profile_record(manifest=manifest, updated_at=recorded_at)
    dokploy_targets = build_dokploy_target_records(manifest=manifest, updated_at=recorded_at)
    dokploy_target_ids = build_dokploy_target_id_records(manifest=manifest, updated_at=recorded_at)
    runtime_environments = build_runtime_environment_records(
        manifest=manifest, updated_at=recorded_at
    )
    secret_bindings = build_secret_bindings(manifest=manifest, updated_at=recorded_at)

    record_store.write_product_profile_record(product_profile)
    for target_record in dokploy_targets:
        record_store.write_dokploy_target_record(target_record)
    for target_id_record in dokploy_target_ids:
        record_store.write_dokploy_target_id_record(target_id_record)
    for runtime_record in runtime_environments:
        record_store.write_runtime_environment_record(runtime_record)
    for binding in secret_bindings:
        record_store.write_secret_binding(binding)

    return ProductOnboardingApplyResult(
        product=manifest.product,
        product_profile=product_profile,
        dokploy_targets=dokploy_targets,
        dokploy_target_ids=dokploy_target_ids,
        runtime_environments=runtime_environments,
        secret_bindings=secret_bindings,
    )


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
