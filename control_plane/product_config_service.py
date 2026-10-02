from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import click

from control_plane import product_config as control_plane_product_config
from control_plane import provider_key_adoption
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy.compose import odoo_compose_template_defaults
from control_plane.contracts.product_environment_read_model import (
    ProductConfigWritePrerequisites,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    product_config_requirement_applies_to_lane,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.product_config import ProductConfigMode, ProductConfigStore
from control_plane.runtime_key_safety import (
    RuntimeKeySafetyPolicyReadStore,
    evaluate_runtime_key_safety,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import RuntimeEnvironmentConflictError


@dataclass(frozen=True)
class ProductConfigServiceError:
    status_code: int
    code: str
    message: str


def product_config_write_prerequisites(
    record_store: object,
    *,
    profile: LaunchplaneProductProfileRecord | None = None,
    lane: ProductLaneProfile | None = None,
) -> ProductConfigWritePrerequisites:
    storage_ready = isinstance(record_store, PostgresRecordStore)
    try:
        control_plane_secrets.validate_secret_key_configuration()
    except click.ClickException:
        secret_key_ready = False
    else:
        secret_key_ready = True
    runtime_key_safety_ready = _runtime_key_safety_ready(
        record_store=record_store,
        profile=profile,
        lane=lane,
    )
    return ProductConfigWritePrerequisites(
        storage_ready=storage_ready,
        secret_key_ready=secret_key_ready,
        runtime_key_safety_ready=runtime_key_safety_ready,
    )


def _runtime_key_safety_ready(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord | None,
    lane: ProductLaneProfile | None,
) -> bool:
    if profile is None or lane is None:
        return False
    binding_keys = tuple(
        dict.fromkeys(
            requirement.binding_key
            for requirement in profile.expected_config.managed_secret_bindings
            if requirement.integration == "runtime_environment"
            and product_config_requirement_applies_to_lane(
                requirement_context=requirement.context,
                requirement_instance=requirement.instance,
                lane=lane,
            )
        )
    )
    if not binding_keys:
        return True
    try:
        policy_store = cast(RuntimeKeySafetyPolicyReadStore, record_store)
        policy = latest_active_runtime_key_safety_policy(policy_store)
        # A writer's declared class lives on the lane's stored binding; carry it
        # onto the stand-in binding so readiness matches the real evaluation.
        declared_classes = {
            binding.binding_key: binding.declared_secret_class
            for binding in policy_store.list_secret_bindings(
                integration="runtime_environment",
                context_name=lane.context,
                instance_name=lane.instance,
                limit=None,
            )
            if binding.status == "configured"
            and binding.context == lane.context
            and binding.instance == lane.instance
        }
        target = RuntimeKeySafetyTarget(
            context=lane.context,
            instance=lane.instance,
            environment_class=runtime_key_safety_environment_class(lane.instance),
        )
        evaluation = evaluate_runtime_key_safety(
            target=target,
            required_binding_keys=binding_keys,
            secret_bindings=tuple(
                SecretBinding(
                    binding_id=f"product-config-availability:{binding_key}",
                    secret_id=f"product-config-availability:{binding_key}",
                    integration="runtime_environment",
                    binding_key=binding_key,
                    context=lane.context,
                    instance=lane.instance,
                    declared_secret_class=declared_classes.get(binding_key),
                    created_at="1970-01-01T00:00:00Z",
                    updated_at="1970-01-01T00:00:00Z",
                )
                for binding_key in binding_keys
            ),
            secret_rules=policy.rules,
            integration_key_markers=policy.integration_key_markers,
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return evaluation.status == "pass"


class LaneProviderEnvStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...


def read_lane_provider_env(
    *,
    record_store: LaneProviderEnvStore,
    control_plane_root: Path,
    product: str,
    context_name: str,
    instance_name: str,
) -> provider_key_adoption.LaneProviderEnv:
    """The lane's current provider env, read by the service for provider key adoption.

    The values stay inside the service; only the adoption plan's key names and
    dispositions leave it.
    """
    unavailable = control_plane_product_config.ProductConfigError(
        "Launchplane could not read the lane's provider env.",
        code="provider_env_unavailable",
    )
    try:
        profile = record_store.read_product_profile_record(product)
        if not any(
            lane.context == context_name and lane.instance == instance_name
            for lane in profile.lanes
        ):
            raise unavailable
        target_record = record_store.read_dokploy_target_record(
            context_name=context_name, instance_name=instance_name
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=context_name, instance_name=instance_name
        )
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=target_record.target_type,
            target_id=target_id_record.target_id,
        )
    except (FileNotFoundError, click.ClickException) as error:
        raise unavailable from error
    env = dokploy_api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    # Only the Odoo compose template is Launchplane's own; other targets supply no defaults.
    template_defaults = (
        odoo_compose_template_defaults()
        if profile.driver_id == "odoo" and target_record.target_type == "compose"
        else {}
    )
    return provider_key_adoption.LaneProviderEnv(env=env, template_defaults=template_defaults)


def apply_product_config_service_request(
    *,
    record_store: ProductConfigStore,
    payload: dict[str, object],
    mode: ProductConfigMode,
    actor: str,
    source_label: str,
) -> tuple[dict[str, object] | None, ProductConfigServiceError | None]:
    try:
        return (
            control_plane_product_config.apply_product_config_bundle(
                record_store=record_store,
                payload=payload,
                mode=mode,
                actor=actor,
                source_label=source_label,
            ),
            None,
        )
    except (
        control_plane_product_config.ProductConfigError,
        RuntimeEnvironmentConflictError,
    ) as error:
        return None, product_config_service_error(error)


def product_config_service_error(
    error: control_plane_product_config.ProductConfigError | RuntimeEnvironmentConflictError,
) -> ProductConfigServiceError:
    if isinstance(error, RuntimeEnvironmentConflictError):
        return ProductConfigServiceError(
            status_code=409,
            code="runtime_environment_conflict",
            message="Runtime configuration changed before commit. Retry using current configuration.",
        )
    error_code = error.code
    error_message = "Product config request failed validation."
    status_code = 400
    if error_code == "secret_configuration_required":
        status_code = 503
        error_message = "Launchplane service is missing required secret write configuration."
    if error_code == "runtime_key_safety_unavailable":
        status_code = 503
        error_message = "Launchplane runtime key-safety policy is unavailable."
    if error_code == "runtime_key_safety_failed":
        error_message = "Product config runtime key-safety gate failed."
    if error_code == "provider_env_unavailable":
        status_code = 503
        error_message = "Launchplane could not read the lane's provider env."
    if error_code == "provider_key_adoption_refused":
        error_message = (
            "Provider key adoption names keys that are missing on the provider or look like "
            "credentials; review a fresh dry run."
        )
    return ProductConfigServiceError(
        status_code=status_code,
        code=error_code,
        message=error_message,
    )
