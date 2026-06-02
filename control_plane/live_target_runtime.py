from __future__ import annotations

from pathlib import Path
from typing import Protocol

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyTarget,
)
from control_plane.runtime_key_safety import (
    RuntimeKeySafetyPolicyReadStore,
    evaluate_runtime_key_safety,
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
)
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore


class LiveTargetRuntimeError(Exception):
    def __init__(self, message: str, *, code: str = "live_target_runtime_failed") -> None:
        super().__init__(message)
        self.code = code


class DokployDeployTrigger(Protocol):
    def __call__(
        self,
        *,
        host: str,
        token: str,
        target_type: str,
        target_id: str,
        deploy_timeout_seconds: int,
        no_cache: bool,
    ) -> dict[str, str]: ...


class LiveTargetRuntimeProfileStore(RuntimeKeySafetyPolicyReadStore, Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...


def runtime_env_live_target_delta(
    *,
    desired_env_map: dict[str, str],
    live_env_map: dict[str, str],
) -> dict[str, object]:
    desired_keys = sorted(desired_env_map)
    missing_keys = [env_key for env_key in desired_keys if env_key not in live_env_map]
    different_keys = [
        env_key
        for env_key in desired_keys
        if env_key in live_env_map and live_env_map[env_key] != desired_env_map[env_key]
    ]
    unchanged_keys = [
        env_key
        for env_key in desired_keys
        if env_key in live_env_map and live_env_map[env_key] == desired_env_map[env_key]
    ]
    return {
        "desired_key_count": len(desired_keys),
        "live_key_count": len(live_env_map),
        "missing_keys": missing_keys,
        "different_keys": different_keys,
        "changed_keys": sorted({*missing_keys, *different_keys}),
        "unchanged_key_count": len(unchanged_keys),
    }


def evaluate_runtime_key_safety_for_live_target_sync(
    *,
    record_store: RuntimeKeySafetyPolicyReadStore,
    context_name: str,
    instance_name: str,
    require_policy: bool = True,
    required_binding_keys: tuple[str, ...] = (),
) -> dict[str, object]:
    all_runtime_bindings = record_store.list_secret_bindings(
        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        limit=None,
    )
    bindings = tuple(
        binding
        for binding in all_runtime_bindings
        if _runtime_secret_binding_matches_target(
            binding_context=binding.context,
            binding_instance=binding.instance,
            context_name=context_name,
            instance_name=instance_name,
        )
    )
    binding_keys = required_binding_keys or tuple(binding.binding_key for binding in bindings)
    if not binding_keys:
        return {"required": False, "status": "skipped", "checked_binding_keys": []}
    try:
        policy_record = latest_active_runtime_key_safety_policy(record_store)
        target = RuntimeKeySafetyTarget(
            context=context_name,
            instance=instance_name,
            environment_class=runtime_key_safety_environment_class(instance_name),
        )
        if required_binding_keys:
            evaluation = evaluate_runtime_key_safety(
                target=target,
                required_binding_keys=binding_keys,
                secret_bindings=bindings,
                secret_rules=policy_record.rules,
            )
        else:
            evaluation = evaluate_runtime_key_safety_from_store(
                record_store=record_store,
                policy_record=policy_record,
                target=target,
                required_binding_keys=binding_keys,
            )
    except ValueError as error:
        if not require_policy:
            return {
                "required": True,
                "status": "unavailable",
                "checked_binding_keys": list(binding_keys),
            }
        raise LiveTargetRuntimeError(
            "Runtime key-safety policy is unavailable for live target runtime sync.",
            code="runtime_key_safety_unavailable",
        ) from error
    summary: dict[str, object] = {
        "required": True,
        "status": evaluation.status,
        "policy_record_id": policy_record.record_id,
        "policy_sha256": policy_record.policy_sha256,
        "target": evaluation.target.model_dump(mode="json"),
        "checked_binding_keys": list(evaluation.checked_binding_keys),
        "findings": [finding.model_dump(mode="json") for finding in evaluation.findings],
    }
    if evaluation.status != "pass":
        finding_codes = sorted({finding.code for finding in evaluation.findings})
        suffix = f": {', '.join(finding_codes)}" if finding_codes else ""
        raise LiveTargetRuntimeError(
            f"Runtime key-safety gate failed for live target sync{suffix}.",
            code="runtime_key_safety_failed",
        )
    return summary


def skipped_runtime_key_safety_summary() -> dict[str, object]:
    return {"required": False, "status": "skipped", "checked_binding_keys": []}


def _runtime_secret_binding_matches_target(
    *, binding_context: str, binding_instance: str, context_name: str, instance_name: str
) -> bool:
    if not binding_context:
        return True
    if binding_context != context_name:
        return False
    if not binding_instance:
        return True
    return binding_instance == instance_name


def _require_product_profile_runtime_keys(
    *,
    record_store: LiveTargetRuntimeProfileStore,
    product_name: str,
    context_name: str,
    instance_name: str,
) -> set[str]:
    profile = record_store.read_product_profile_record(product_name)
    lane = next(
        (
            candidate
            for candidate in profile.lanes
            if candidate.context == context_name and candidate.instance == instance_name
        ),
        None,
    )
    if lane is None:
        raise LiveTargetRuntimeError(
            f"Product {product_name!r} has no lane for {context_name}/{instance_name}.",
            code="product_lane_not_found",
        )
    allowed_keys: set[str] = set()
    for runtime_requirement in profile.expected_config.runtime_environment_keys:
        if _expected_config_route_matches(
            requirement_context=runtime_requirement.context,
            requirement_instance=runtime_requirement.instance,
            context_name=context_name,
            instance_name=instance_name,
        ):
            allowed_keys.add(runtime_requirement.key)
    for secret_requirement in profile.expected_config.managed_secret_bindings:
        if (
            secret_requirement.integration
            != control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
        ):
            continue
        if _expected_config_route_matches(
            requirement_context=secret_requirement.context,
            requirement_instance=secret_requirement.instance,
            context_name=context_name,
            instance_name=instance_name,
        ):
            allowed_keys.add(secret_requirement.binding_key)
    if not allowed_keys:
        raise LiveTargetRuntimeError(
            f"Product {product_name!r} has no expected runtime keys for {context_name}/{instance_name}.",
            code="runtime_environment_empty",
        )
    return allowed_keys


def _require_product_profile_runtime_secret_keys(
    *,
    record_store: LiveTargetRuntimeProfileStore,
    product_name: str,
    context_name: str,
    instance_name: str,
) -> set[str]:
    profile = record_store.read_product_profile_record(product_name)
    return {
        requirement.binding_key
        for requirement in profile.expected_config.managed_secret_bindings
        if requirement.integration == control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
        and _expected_config_route_matches(
            requirement_context=requirement.context,
            requirement_instance=requirement.instance,
            context_name=context_name,
            instance_name=instance_name,
        )
    }


def _expected_config_route_matches(
    *,
    requirement_context: str,
    requirement_instance: str,
    context_name: str,
    instance_name: str,
) -> bool:
    if requirement_instance:
        return requirement_context == context_name and requirement_instance == instance_name
    if requirement_context:
        return requirement_context == context_name
    return True


def _filter_runtime_environment_to_product_keys(
    *, desired_env_map: dict[str, str], allowed_keys: set[str]
) -> dict[str, str]:
    return {key: value for key, value in desired_env_map.items() if key in allowed_keys}


def _require_expected_runtime_secret_values(
    *, desired_env_map: dict[str, str], runtime_secret_binding_keys: set[str]
) -> None:
    missing_keys = sorted(key for key in runtime_secret_binding_keys if key not in desired_env_map)
    if missing_keys:
        raise LiveTargetRuntimeError(
            "Expected managed runtime secret values are missing from the resolved "
            f"Launchplane runtime environment: {', '.join(missing_keys)}.",
            code="runtime_secret_values_missing",
        )


def require_dokploy_target_definition(
    *,
    source_of_truth: control_plane_dokploy.DokploySourceOfTruth,
    context_name: str,
    instance_name: str,
    operation_name: str,
) -> control_plane_dokploy.DokployTargetDefinition:
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
    )
    if target_definition is None:
        raise LiveTargetRuntimeError(
            f"{operation_name} target {context_name}/{instance_name} is missing from the DB-backed tracked Dokploy targets.",
            code="tracked_target_not_found",
        )
    return target_definition


def apply_live_target_runtime_environment(
    *,
    control_plane_root: Path,
    product_name: str = "",
    context_name: str,
    instance_name: str,
    apply_changes: bool,
    deploy: bool,
    no_cache: bool,
    deploy_timeout_seconds: int | None,
    deploy_trigger: DokployDeployTrigger,
) -> dict[str, object]:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = require_dokploy_target_definition(
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
        operation_name="Runtime environment live target apply",
    )
    try:
        desired_env_map = control_plane_runtime_environments.resolve_runtime_environment_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
        )
    except click.ClickException as error:
        raise LiveTargetRuntimeError(str(error), code="runtime_environment_unavailable") from error
    if not desired_env_map:
        raise LiveTargetRuntimeError(
            f"No Launchplane runtime environment values resolved for {context_name}/{instance_name}.",
            code="runtime_environment_empty",
        )

    database_url = resolve_database_url(None)
    runtime_secret_binding_keys: set[str] = set()
    if product_name.strip():
        if database_url is None:
            raise LiveTargetRuntimeError(
                "Live target runtime product scoping requires LAUNCHPLANE_DATABASE_URL.",
                code="runtime_environment_unavailable",
            )
        postgres_store = PostgresRecordStore(database_url=database_url)
        try:
            postgres_store.ensure_schema()
            desired_env_map = _filter_runtime_environment_to_product_keys(
                desired_env_map=desired_env_map,
                allowed_keys=_require_product_profile_runtime_keys(
                    record_store=postgres_store,
                    product_name=product_name.strip(),
                    context_name=context_name,
                    instance_name=instance_name,
                ),
            )
            runtime_secret_binding_keys = _require_product_profile_runtime_secret_keys(
                record_store=postgres_store,
                product_name=product_name.strip(),
                context_name=context_name,
                instance_name=instance_name,
            )
            _require_expected_runtime_secret_values(
                desired_env_map=desired_env_map,
                runtime_secret_binding_keys=runtime_secret_binding_keys,
            )
        finally:
            postgres_store.close()
        if not desired_env_map:
            raise LiveTargetRuntimeError(
                f"No expected Launchplane runtime environment values resolved for {product_name}/{context_name}/{instance_name}.",
                code="runtime_environment_empty",
            )

    try:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=target_definition.target_type,
            target_id=target_definition.target_id,
        )
    except click.ClickException as error:
        raise LiveTargetRuntimeError(str(error), code="dokploy_target_read_failed") from error
    live_env_map = control_plane_dokploy.parse_dokploy_env_text(
        str(target_payload.get("env") or "")
    )
    initial_delta = runtime_env_live_target_delta(
        desired_env_map=desired_env_map,
        live_env_map=live_env_map,
    )
    changed_keys = initial_delta["changed_keys"]
    changed_key_count = len(changed_keys) if isinstance(changed_keys, list) else 0
    runtime_key_safety = skipped_runtime_key_safety_summary()
    deploy_result: dict[str, str] | None = None
    verification: dict[str, object] = {
        "status": "skipped",
        "reason": "dry_run" if not apply_changes else "no_runtime_env_changes",
    }

    if apply_changes and database_url is None:
        raise LiveTargetRuntimeError(
            "Live target runtime apply requires LAUNCHPLANE_DATABASE_URL for DB-backed "
            "runtime key-safety evaluation.",
            code="runtime_key_safety_unavailable",
        )
    if database_url is not None and (not apply_changes or changed_key_count or deploy):
        postgres_store = PostgresRecordStore(database_url=database_url)
        try:
            postgres_store.ensure_schema()
            runtime_key_safety = evaluate_runtime_key_safety_for_live_target_sync(
                record_store=postgres_store,
                context_name=context_name,
                instance_name=instance_name,
                require_policy=apply_changes,
                required_binding_keys=tuple(sorted(runtime_secret_binding_keys)),
            )
        finally:
            postgres_store.close()

    if apply_changes and changed_key_count:
        try:
            refreshed_payload = control_plane_dokploy.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
            )
            refreshed_env_map = control_plane_dokploy.parse_dokploy_env_text(
                str(refreshed_payload.get("env") or "")
            )
            updated_env_map = dict(refreshed_env_map)
            updated_env_map.update(desired_env_map)
            control_plane_dokploy.update_dokploy_target_env(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
                target_payload=refreshed_payload,
                env_text=control_plane_dokploy.serialize_dokploy_env_text(updated_env_map),
            )
            persisted_payload = control_plane_dokploy.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
            )
        except click.ClickException as error:
            raise LiveTargetRuntimeError(str(error), code="dokploy_target_update_failed") from error
        persisted_env_map = control_plane_dokploy.parse_dokploy_env_text(
            str(persisted_payload.get("env") or "")
        )
        verification_delta = runtime_env_live_target_delta(
            desired_env_map=desired_env_map,
            live_env_map=persisted_env_map,
        )
        verification_changed_keys = verification_delta["changed_keys"]
        verification = {
            "status": "pass" if verification_changed_keys == [] else "fail",
            "missing_keys": verification_delta["missing_keys"],
            "different_keys": verification_delta["different_keys"],
            "verified_key_count": verification_delta["unchanged_key_count"],
        }
        if verification["status"] != "pass":
            raise LiveTargetRuntimeError(
                "Dokploy target env did not persist all Launchplane runtime environment keys.",
                code="dokploy_target_verification_failed",
            )

    if apply_changes and deploy:
        try:
            deploy_result = deploy_trigger(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
                deploy_timeout_seconds=control_plane_dokploy.resolve_ship_timeout_seconds(
                    timeout_override_seconds=deploy_timeout_seconds,
                    target_definition=target_definition,
                ),
                no_cache=no_cache,
            )
        except click.ClickException as error:
            raise LiveTargetRuntimeError(str(error), code="dokploy_deploy_failed") from error

    return {
        "status": "ok",
        "mode": "apply" if apply_changes else "dry-run",
        "context": context_name,
        "instance": instance_name,
        "tracked_target": {
            "target_id": target_definition.target_id,
            "target_type": target_definition.target_type,
            "target_name": target_definition.target_name,
        },
        "runtime_environment": initial_delta,
        "runtime_key_safety": runtime_key_safety,
        "apply": {
            "applied": apply_changes,
            "env_updated": bool(apply_changes and changed_key_count),
            "verification": verification,
        },
        "deploy": {
            "requested": deploy,
            "triggered": deploy_result is not None,
            "result": deploy_result,
        },
    }


def trigger_and_wait_for_dokploy_target_deploy(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    deploy_timeout_seconds: int,
    no_cache: bool,
) -> dict[str, str]:
    if deploy_timeout_seconds <= 0:
        raise click.ClickException(
            "Launchplane service deploy timeout must be greater than zero seconds."
        )
    latest_before = control_plane_dokploy.latest_deployment_for_target(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
        no_cache=no_cache,
    )
    deployment_result = control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
    )
    return {"deployment_result": deployment_result}
