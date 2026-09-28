from __future__ import annotations

from pathlib import Path
from typing import Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import runtime_platform_credentials
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyTarget,
)
from control_plane.odoo_instance_overrides import (
    LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY,
    LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
    ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
    ODOO_OVERRIDE_SECRET_ENV_PREFIX,
)
from control_plane.runtime_key_safety import (
    RuntimeKeySafetyPolicyReadStore,
    evaluate_runtime_key_safety,
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
    runtime_secret_binding_matches_target,
)
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source


class LiveTargetRuntimeError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "live_target_runtime_failed",
        summary: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.summary = summary or {}


class LiveTargetRuntimeApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: str
    product: str
    context: str
    instance: str
    deploy: bool = False
    no_cache: bool = False
    deploy_timeout_seconds: int | None = Field(default=None, gt=0)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        normalized_value = value.strip().lower()
        if normalized_value not in {"dry-run", "apply"}:
            raise ValueError("Live target runtime mode must be 'dry-run' or 'apply'.")
        return normalized_value

    @model_validator(mode="after")
    def _validate_route(self) -> "LiveTargetRuntimeApplyEnvelope":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        if not self.product:
            raise ValueError("Live target runtime apply requires product.")
        if not self.context:
            raise ValueError("Live target runtime apply requires context.")
        if not self.instance:
            raise ValueError("Live target runtime apply requires instance.")
        if self.mode == "dry-run" and (
            self.deploy or self.no_cache or self.deploy_timeout_seconds is not None
        ):
            raise ValueError("Deploy options require live target runtime mode 'apply'.")
        return self

    @property
    def apply_changes(self) -> bool:
        return self.mode == "apply"


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


def validate_provider_key_retirement(*, retired_keys: set[str], application_keys: set[str]) -> None:
    protected_keys = {
        "PLATFORM_CONTEXT",
        "PLATFORM_INSTANCE",
        "DOCKER_IMAGE_REFERENCE",
        "LAUNCHPLANE_RUNTIME_IDENTITY_JSON",
        "LAUNCHPLANE_DEPLOYMENT_RECORD_ID",
        "LAUNCHPLANE_ARTIFACT_ID",
        "LAUNCHPLANE_SOURCE_GIT_REF",
        "ODOO_DATA_VOLUME",
        "ODOO_LOG_VOLUME",
        "ODOO_DB_VOLUME",
        "ODOO_DB_NAME",
        "ODOO_DB_USER",
        "ODOO_DB_PASSWORD",
        "ODOO_ADDONS_PATH",
        "ODOO_INSTALL_MODULES",
        ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
        LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY,
        LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
    }
    if retired_keys & (application_keys | protected_keys) or any(
        key.startswith(ODOO_OVERRIDE_SECRET_ENV_PREFIX) for key in retired_keys
    ):
        raise LiveTargetRuntimeError(
            "Provider key retirement conflicts with declared application or driver settings.",
            code="runtime_retirement_conflict",
        )


def provider_env_platform_credential_report(
    *, live_env_map: dict[str, str], retired_keys: set[str] | frozenset[str]
) -> dict[str, object]:
    """Report platform credentials already in a lane's provider env; names only.

    A sync preserves provider-only keys, so a credential written before the
    refusal existed stays until an operator retires it. This report finds those
    keys; removal stays with product-config retired_provider_keys.
    """

    findings = runtime_platform_credentials.find_platform_credentials(live_env_map)
    keys = [finding.key for finding in findings]
    return {
        "status": "found" if keys else "clear",
        "keys": keys,
        "retiring_keys": [key for key in keys if key in retired_keys],
        "unretired_keys": [key for key in keys if key not in retired_keys],
    }


def report_lane_provider_env_platform_credentials(
    *,
    control_plane_root: Path,
    database_url: str | None = None,
    context_name: str = "",
    instance_name: str = "",
) -> dict[str, object]:
    """Read-only: list platform credentials in each tracked lane's provider env.

    Reads tracked targets and provider env; writes nothing. Output holds key
    names and lane coordinates only, never values.
    """

    source_of_truth = dokploy_source.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    host, token = dokploy_source.read_dokploy_config(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    lanes: list[dict[str, object]] = []
    for target in source_of_truth.targets:
        if context_name and target.context != context_name:
            continue
        if instance_name and target.instance != instance_name:
            continue
        lane: dict[str, object] = {
            "context": target.context,
            "instance": target.instance,
            "target_type": target.target_type,
            "target_name": target.target_name,
        }
        try:
            target_payload = dokploy_api.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type=target.target_type,
                target_id=target.target_id,
            )
            retired_keys = control_plane_runtime_environments.retired_provider_keys_for_lane(
                context_name=target.context,
                instance_name=target.instance,
                database_url=database_url,
            )
        except click.ClickException as error:
            lane["status"] = "unavailable"
            lane["error"] = str(error)
            lanes.append(lane)
            continue
        lane.update(
            provider_env_platform_credential_report(
                live_env_map=dokploy_api.parse_dokploy_env_text(
                    str(target_payload.get("env") or "")
                ),
                retired_keys=retired_keys,
            )
        )
        lanes.append(lane)
    return {
        "status": "ok",
        "flagged_lane_count": sum(1 for lane in lanes if lane.get("status") == "found"),
        "lanes": lanes,
    }


def runtime_env_live_target_delta(
    *,
    desired_env_map: dict[str, str],
    live_env_map: dict[str, str],
    retired_keys: set[str] | None = None,
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
    retired_keys_present = sorted((retired_keys or set()) & live_env_map.keys())
    result: dict[str, object] = {
        "desired_key_count": len(desired_keys),
        "live_key_count": len(live_env_map),
        "missing_keys": missing_keys,
        "different_keys": different_keys,
        "changed_keys": sorted({*missing_keys, *different_keys, *retired_keys_present}),
        "unchanged_key_count": len(unchanged_keys),
    }
    if retired_keys:
        result["retired_keys_present"] = retired_keys_present
    return result


def evaluate_runtime_key_safety_for_live_target_sync(
    *,
    record_store: RuntimeKeySafetyPolicyReadStore,
    context_name: str,
    instance_name: str,
    require_policy: bool = True,
    required_binding_keys: tuple[str, ...] | None = None,
) -> dict[str, object]:
    target = RuntimeKeySafetyTarget(
        context=context_name,
        instance=instance_name,
        environment_class=runtime_key_safety_environment_class(instance_name),
    )
    all_runtime_bindings = record_store.list_secret_bindings(
        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        limit=None,
    )
    bindings = tuple(
        binding
        for binding in all_runtime_bindings
        if runtime_secret_binding_matches_target(binding=binding, target=target)
    )
    binding_keys = (
        required_binding_keys
        if required_binding_keys is not None
        else tuple(binding.binding_key for binding in bindings)
    )
    if not binding_keys:
        return {"required": False, "status": "skipped", "checked_binding_keys": []}
    try:
        policy_record = latest_active_runtime_key_safety_policy(record_store)
        if required_binding_keys is not None:
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
            summary=summary,
        )
    return summary


def require_product_profile_runtime_keys(
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
    allowed_keys = _declared_runtime_keys(
        profile=profile, context_name=context_name, instance_name=instance_name
    )
    if not allowed_keys:
        raise LiveTargetRuntimeError(
            f"Product {product_name!r} has no expected runtime keys for {context_name}/{instance_name}.",
            code="runtime_environment_empty",
        )
    return allowed_keys


def _declared_runtime_keys(
    *, profile: LaunchplaneProductProfileRecord, context_name: str, instance_name: str
) -> set[str]:
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
    return allowed_keys


class ProductProfileListStore(Protocol):
    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


def declared_runtime_keys(
    *, profile: LaunchplaneProductProfileRecord, context_name: str, instance_name: str
) -> set[str]:
    """Runtime keys one product declares for a lane (settings and managed secrets)."""

    return _declared_runtime_keys(
        profile=profile, context_name=context_name, instance_name=instance_name
    )


def declared_runtime_keys_for_lane(
    *, record_store: ProductProfileListStore, context_name: str, instance_name: str
) -> set[str]:
    """Runtime keys any product declares for this lane (settings and managed secrets)."""

    declared_keys: set[str] = set()
    for profile in record_store.list_product_profile_records():
        if any(
            lane.context == context_name and lane.instance == instance_name
            for lane in profile.lanes
        ):
            declared_keys |= _declared_runtime_keys(
                profile=profile, context_name=context_name, instance_name=instance_name
            )
    return declared_keys


def require_declared_runtime_keys_present(
    *, declared_keys: set[str], available_keys: set[str], target: str
) -> None:
    """Fail closed when a declared runtime key would be missing from what the app gets."""

    # A declared platform-credential name can never reach the app (it is withheld
    # or refused), so it is not a missing application key.
    missing_keys = sorted(
        declared_keys - available_keys - runtime_platform_credentials.PLATFORM_CREDENTIAL_KEYS
    )
    if missing_keys:
        raise click.ClickException(
            f"{target} would run without declared runtime key(s): "
            + ", ".join(missing_keys)
            + ". The site environment does not hold them (global values are not "
            "delivered); store them for this site first."
        )


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


def require_product_profile_runtime_secret_keys(
    *,
    record_store: LiveTargetRuntimeProfileStore,
    product_name: str,
    context_name: str,
    instance_name: str,
) -> set[str]:
    return _require_product_profile_runtime_secret_keys(
        record_store=record_store,
        product_name=product_name,
        context_name=context_name,
        instance_name=instance_name,
    )


def runtime_key_safety_error_message(error: LiveTargetRuntimeError) -> str:
    summary = getattr(error, "summary", None)
    if not isinstance(summary, dict):
        return str(error)
    findings = summary.get("findings")
    if not isinstance(findings, list):
        return str(error)
    binding_keys = sorted(
        {
            str(finding.get("binding_key") or "")
            for finding in findings
            if isinstance(finding, dict) and str(finding.get("binding_key") or "").strip()
        }
    )
    if not binding_keys:
        return str(error)
    return f"{error} Affected binding keys: {', '.join(binding_keys)}."


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


def _product_lane_declared_keys(
    *,
    record_store: LiveTargetRuntimeProfileStore,
    product_name: str,
    context_name: str,
    instance_name: str,
) -> set[str]:
    profile = record_store.read_product_profile_record(product_name)
    if not any(
        lane.context == context_name and lane.instance == instance_name for lane in profile.lanes
    ):
        raise LiveTargetRuntimeError(
            f"Product {product_name!r} has no lane for {context_name}/{instance_name}.",
            code="product_lane_not_found",
        )
    return _declared_runtime_keys(
        profile=profile, context_name=context_name, instance_name=instance_name
    )


def _require_expected_runtime_secret_values(
    *, resolved_secret_keys: frozenset[str], runtime_secret_binding_keys: set[str]
) -> None:
    # A plain setting with the same name does not stand in for a declared managed secret.
    missing_keys = sorted(runtime_secret_binding_keys - resolved_secret_keys)
    if missing_keys:
        raise LiveTargetRuntimeError(
            "Expected managed runtime secret values are missing from the resolved "
            f"Launchplane runtime environment: {', '.join(missing_keys)}.",
            code="runtime_secret_values_missing",
        )


def require_dokploy_target_definition(
    *,
    source_of_truth: dokploy_source.DokploySourceOfTruth,
    context_name: str,
    instance_name: str,
    operation_name: str,
) -> dokploy_source.DokployTargetDefinition:
    target_definition = dokploy_source.find_dokploy_target_definition(
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
    database_url: str | None = None,
    product_name: str = "",
    context_name: str,
    instance_name: str,
    apply_changes: bool,
    deploy: bool,
    no_cache: bool,
    deploy_timeout_seconds: int | None,
    deploy_trigger: DokployDeployTrigger,
) -> dict[str, object]:
    source_of_truth = dokploy_source.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    target_definition = require_dokploy_target_definition(
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
        operation_name="Runtime environment live target apply",
    )
    try:
        site_environment = control_plane_runtime_environments.resolve_site_runtime_environment(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
            database_url=database_url,
        )
    except runtime_platform_credentials.PlatformCredentialRefusedError as error:
        raise LiveTargetRuntimeError(error.message, code=error.code) from error
    except click.ClickException as error:
        raise LiveTargetRuntimeError(str(error), code="runtime_environment_unavailable") from error
    desired_env_map = site_environment.values
    retired_keys = set(site_environment.retired_keys)
    if not desired_env_map:
        raise LiveTargetRuntimeError(
            f"No Launchplane runtime environment values resolved for {context_name}/{instance_name}.",
            code="runtime_environment_empty",
        )

    # Protected driver and identity keys are never retirable, with or without
    # product scoping; the product branch below also checks application keys.
    validate_provider_key_retirement(retired_keys=retired_keys, application_keys=set())
    database_url = resolve_database_url(database_url)
    if product_name.strip():
        if database_url is None:
            raise LiveTargetRuntimeError(
                "Live target runtime product scoping requires LAUNCHPLANE_DATABASE_URL.",
                code="runtime_environment_unavailable",
            )
        postgres_store = PostgresRecordStore(database_url=database_url)
        try:
            postgres_store.ensure_schema()
            declared_keys = _product_lane_declared_keys(
                record_store=postgres_store,
                product_name=product_name.strip(),
                context_name=context_name,
                instance_name=instance_name,
            )
            validate_provider_key_retirement(
                retired_keys=retired_keys,
                application_keys=control_plane_runtime_environments.site_application_keys(
                    site_environment.site_keys
                )
                | declared_keys,
            )
            declared_secret_keys = _require_product_profile_runtime_secret_keys(
                record_store=postgres_store,
                product_name=product_name.strip(),
                context_name=context_name,
                instance_name=instance_name,
            )
            _require_expected_runtime_secret_values(
                resolved_secret_keys=site_environment.secret_keys,
                runtime_secret_binding_keys=declared_secret_keys,
            )
        finally:
            postgres_store.close()

    try:
        host, token = dokploy_source.read_dokploy_config(
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=target_definition.target_type,
            target_id=target_definition.target_id,
        )
    except click.ClickException as error:
        raise LiveTargetRuntimeError(str(error), code="dokploy_target_read_failed") from error
    live_env_map = dokploy_api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    provider_env_platform_credentials = provider_env_platform_credential_report(
        live_env_map=live_env_map, retired_keys=retired_keys
    )
    initial_delta = runtime_env_live_target_delta(
        desired_env_map=desired_env_map,
        live_env_map=live_env_map,
        retired_keys=retired_keys,
    )
    changed_keys = initial_delta["changed_keys"]
    changed_key_count = len(changed_keys) if isinstance(changed_keys, list) else 0
    deploy_result: dict[str, str] | None = None
    verification: dict[str, object] = {
        "status": "skipped",
        "reason": "dry_run" if not apply_changes else "no_runtime_env_changes",
    }

    if apply_changes and changed_key_count:
        if retired_keys:
            if database_url is None:
                raise LiveTargetRuntimeError(
                    "Provider key retirement requires LAUNCHPLANE_DATABASE_URL.",
                    code="runtime_environment_unavailable",
                )
            postgres_store = PostgresRecordStore(database_url=database_url)
            try:
                current_retired_keys = (
                    control_plane_runtime_environments.retired_provider_keys_from_store(
                        record_store=postgres_store,
                        context_name=context_name,
                        instance_name=instance_name,
                    )
                )
                if current_retired_keys != retired_keys:
                    raise LiveTargetRuntimeError(
                        "Provider key retirement changed during execution; review current configuration.",
                        code="runtime_retirement_changed",
                    )
                if product_name.strip():
                    validate_provider_key_retirement(
                        retired_keys=retired_keys,
                        application_keys=control_plane_runtime_environments.site_application_keys(
                            control_plane_runtime_environments.resolve_site_runtime_environment(
                                control_plane_root=control_plane_root,
                                context_name=context_name,
                                instance_name=instance_name,
                                database_url=database_url,
                            ).site_keys
                        )
                        | _product_lane_declared_keys(
                            record_store=postgres_store,
                            product_name=product_name.strip(),
                            context_name=context_name,
                            instance_name=instance_name,
                        ),
                    )
            finally:
                postgres_store.close()
        try:
            refreshed_payload = dokploy_api.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
            )
            refreshed_env_map = dokploy_api.parse_dokploy_env_text(
                str(refreshed_payload.get("env") or "")
            )
            updated_env_map = control_plane_runtime_environments.merge_provider_environment(
                current_env_map=refreshed_env_map,
                desired_env_map=desired_env_map,
                retired_keys=retired_keys,
            )
            dokploy_api.update_dokploy_target_env(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
                target_payload=refreshed_payload,
                env_text=dokploy_api.serialize_dokploy_env_text(updated_env_map),
            )
            persisted_payload = dokploy_api.fetch_dokploy_target_payload(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
            )
        except click.ClickException as error:
            raise LiveTargetRuntimeError(str(error), code="dokploy_target_update_failed") from error
        persisted_env_map = dokploy_api.parse_dokploy_env_text(
            str(persisted_payload.get("env") or "")
        )
        verification_delta = runtime_env_live_target_delta(
            desired_env_map=desired_env_map,
            live_env_map=persisted_env_map,
            retired_keys=retired_keys,
        )
        verification_changed_keys = verification_delta["changed_keys"]
        verification = {
            "status": "pass" if verification_changed_keys == [] else "fail",
            "missing_keys": verification_delta["missing_keys"],
            "different_keys": verification_delta["different_keys"],
            "verified_key_count": verification_delta["unchanged_key_count"],
        }
        if retired_keys:
            verification["retired_keys_still_present"] = verification_delta["retired_keys_present"]
        if verification["status"] != "pass":
            raise LiveTargetRuntimeError(
                "Dokploy target env did not persist the requested values and retired-key absence.",
                code="dokploy_target_verification_failed",
            )

    if apply_changes and deploy:
        try:
            deploy_result = deploy_trigger(
                host=host,
                token=token,
                target_type=target_definition.target_type,
                target_id=target_definition.target_id,
                deploy_timeout_seconds=dokploy_source.resolve_ship_timeout_seconds(
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
        "provider_env_platform_credentials": provider_env_platform_credentials,
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
    latest_before = dokploy_api.latest_deployment_for_target(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    dokploy_api.trigger_deployment(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
        no_cache=no_cache,
    )
    deployment_result = dokploy_api.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
        before_key=dokploy_api.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
    )
    return {"deployment_result": deployment_result}
