from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import click

from control_plane import product_config as control_plane_product_config
from control_plane import secrets as control_plane_secrets
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
        policy = latest_active_runtime_key_safety_policy(
            cast(RuntimeKeySafetyPolicyReadStore, record_store)
        )
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
                    created_at="1970-01-01T00:00:00Z",
                    updated_at="1970-01-01T00:00:00Z",
                )
                for binding_key in binding_keys
            ),
            secret_rules=policy.rules,
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return evaluation.status == "pass"


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
    except control_plane_product_config.ProductConfigError as error:
        return None, product_config_service_error(error)


def product_config_service_error(
    error: control_plane_product_config.ProductConfigError,
) -> ProductConfigServiceError:
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
    return ProductConfigServiceError(
        status_code=status_code,
        code=error_code,
        message=error_message,
    )
