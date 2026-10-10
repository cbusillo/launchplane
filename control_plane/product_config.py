from __future__ import annotations

import json
from json import JSONDecodeError
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict, cast, get_args

import click

from control_plane import provider_key_adoption
from control_plane import product_secret_copy
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.public_hosts import normalize_public_hosts
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentScope
from control_plane.contracts.runtime_environment_record import (
    ScalarValue,
    normalize_retired_provider_keys,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretClass,
    RuntimeKeySafetyTarget,
)
from control_plane.contracts.secret_record import SecretAuditEvent, SecretRecord, SecretVersion
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.secret_record import SecretScope
from control_plane.contracts.secret_record import SecretSharingReason
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety,
    is_secret_shaped_runtime_key,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
)
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.storage.product_authority_bundle import ProductAuthorityBundleStore
from control_plane.storage.product_authority_bundle import RuntimeEnvironmentWrite
from control_plane.storage.product_authority_bundle import SecretCopySourceExpectation
from control_plane.workflows.ship import utc_now_timestamp


ProductConfigMode = Literal["dry-run", "apply"]
_VALID_SECRET_SCOPES: tuple[SecretScope, ...] = ("global", "context", "context_instance")


class ProductConfigStore(
    control_plane_secrets.SecretWriteStore,
    ProductAuthorityBundleStore,
    Protocol,
):
    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> None: ...

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]: ...


class _SecretBindingLookupKwargs(TypedDict, total=False):
    integration: str
    context_name: str
    instance_name: str
    limit: int | None


class _ProductConfigRuntimeInput(TypedDict):
    scope: str
    context: str
    instance: str
    env: dict[str, object]
    retired_provider_keys: NotRequired[tuple[str, ...] | None]
    adopt_provider_keys: NotRequired[tuple[str, ...] | None]


LaneProviderEnvReader = Callable[[], provider_key_adoption.LaneProviderEnv]


class _ProductConfigSecretWritePlan(TypedDict):
    secret_id: str
    action: str
    updated_at: str
    configured_binding: SecretBinding
    secret_versions: list[SecretVersion]
    secret_records: list[SecretRecord]
    secret_bindings: list[SecretBinding]
    secret_audit_events: list[SecretAuditEvent]


class ProductConfigError(ValueError):
    """Admin-facing product config validation or planning failure."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


def load_product_config_apply_payload(input_file: Path) -> dict[str, object]:
    try:
        payload = json.loads(input_file.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProductConfigError(
            f"Product config input file {input_file} was not found."
        ) from error
    except JSONDecodeError as error:
        raise ProductConfigError(
            f"Product config input file {input_file} is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ProductConfigError("Product config input file must contain a JSON object.")
    validate_product_config_schema_version(payload)
    return payload


def validate_product_config_schema_version(payload: dict[str, object]) -> None:
    schema_version = payload.get("schema_version", 1)
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ProductConfigError("Product config input schema_version must be 1 or 2.")


def required_text(payload: dict[str, object], key: str, *, default: str = "") -> str:
    value = str(payload.get(key, default) or "").strip()
    if not value:
        raise ProductConfigError(f"Product config input requires {key!r}.")
    return value


def optional_text(payload: dict[str, object], key: str) -> str:
    return str(payload.get(key, "") or "").strip()


def product_context(payload: dict[str, object]) -> tuple[str, str, str]:
    validate_product_config_schema_version(payload)
    return (
        required_text(payload, "product"),
        optional_text(payload, "context"),
        optional_text(payload, "instance"),
    )


def summarize_runtime_environment_record(record: RuntimeEnvironmentRecord) -> dict[str, object]:
    return {
        "scope": record.scope,
        "context": record.context,
        "instance": record.instance,
        "updated_at": record.updated_at,
        "source_label": record.source_label,
        "env_keys": sorted(record.env.keys()),
        "env_value_count": len(record.env),
        "retired_provider_keys": list(record.retired_provider_keys),
    }


def normalize_product_config_payload(payload: dict[str, object]) -> dict[str, object]:
    product, context_name, instance_name = product_context(payload)
    runtime_input = _product_config_runtime_input(
        payload,
        context_name=context_name,
        instance_name=instance_name,
    )
    runtime_env = _normalize_product_config_runtime_env(runtime_input["env"])
    secrets = _product_config_secret_inputs(
        payload,
        context_name=context_name,
        instance_name=instance_name,
    )
    normalized_runtime_input: dict[str, object] = {
        "scope": runtime_input["scope"],
        "context": runtime_input["context"],
        "instance": runtime_input["instance"],
        "env": runtime_env,
    }
    if runtime_input.get("retired_provider_keys") is not None:
        normalized_runtime_input["retired_provider_keys"] = runtime_input["retired_provider_keys"]
    if runtime_input.get("adopt_provider_keys") is not None:
        normalized_runtime_input["adopt_provider_keys"] = runtime_input["adopt_provider_keys"]
    normalized: dict[str, object] = {
        "schema_version": payload.get("schema_version", 1),
        "product": product,
        "context": context_name,
        "instance": instance_name,
        "runtime_env": normalized_runtime_input,
        "secrets": [dict(secret) for secret in secrets],
    }
    if "public_hosts" in payload:
        if instance_name != "prod" or not context_name:
            raise ProductConfigError("Public hosts require an exact prod lane.")
        try:
            normalized["public_hosts"] = list(normalize_public_hosts(payload["public_hosts"]))
        except ValueError as error:
            raise ProductConfigError(str(error)) from error
    return normalized


def apply_product_config_bundle(
    *,
    record_store: ProductConfigStore,
    payload: dict[str, object],
    mode: ProductConfigMode,
    actor: str,
    source_label: str,
    lane_provider_env_reader: LaneProviderEnvReader | None = None,
) -> dict[str, object]:
    result, bundle = plan_product_config_authority_bundle(
        record_store=record_store,
        payload=payload,
        mode=mode,
        actor=actor,
        source_label=source_label,
        lane_provider_env_reader=lane_provider_env_reader,
    )
    if mode == "apply":
        record_store.write_product_authority_bundle(bundle)
    return result


def plan_product_config_authority_bundle(
    *,
    record_store: ProductConfigStore,
    payload: dict[str, object],
    mode: ProductConfigMode,
    actor: str,
    source_label: str,
    lane_provider_env_reader: LaneProviderEnvReader | None = None,
    secret_copy_source_authorizer: Callable[[SecretRecord], bool] | None = None,
) -> tuple[dict[str, object], ProductAuthorityBundle]:
    if "public_hosts" in payload:
        raise ProductConfigError(
            "Public hosts require the service product-config dry-run/apply path.",
            code="public_hosts_service_required",
        )
    if mode not in {"dry-run", "apply"}:
        raise ProductConfigError("Product config mode must be 'dry-run' or 'apply'.")
    normalized_payload = normalize_product_config_payload(payload)
    product = str(normalized_payload["product"])
    context_name = str(normalized_payload["context"])
    instance_name = str(normalized_payload["instance"])
    runtime_input = cast(_ProductConfigRuntimeInput, normalized_payload["runtime_env"])
    runtime_env = cast(dict[str, ScalarValue], runtime_input["env"])
    secrets = tuple(cast(list[dict[str, object]], normalized_payload["secrets"]))
    _require_product_config_master_key_if_needed(secrets)
    copy_sources: dict[int, SecretVersion] = {}
    copy_contexts: set[tuple[str, str]] = set()
    copy_expectations: list[SecretCopySourceExpectation] = []
    copy_profile = None
    for index, secret in enumerate(secrets):
        if secret.get("copy_from") is None:
            continue
        try:
            profile, binding, record, version = product_secret_copy.resolve_copy_source(
                cast(product_secret_copy.ProductSecretCopyStore, record_store),
                product=product,
                target_context=context_name,
                target_instance=instance_name,
                binding_key=str(secret["binding_key"]),
                reference=product_secret_copy.ProductSecretCopyFrom.model_validate(
                    secret["copy_from"]
                ),
                authorizer=secret_copy_source_authorizer,
            )
        except product_secret_copy.ProductSecretCopyError as error:
            raise ProductConfigError(str(error), code=error.code) from error
        if copy_profile is not None and profile != copy_profile:
            raise ProductConfigError("Product changed during secret copy planning.")
        copy_profile = profile
        copy_sources[index] = version
        copy_contexts.add((product, record.context))
        copy_expectations.append(SecretCopySourceExpectation(record=record, binding=binding))

    existing_runtime_records = record_store.list_runtime_environment_records()
    adopted_secret_values = _resolve_provider_secret_adoptions(
        record_store=record_store,
        secrets=secrets,
        recorded_keys=_lane_recorded_runtime_keys(
            existing_records=existing_runtime_records,
            context_name=context_name,
            instance_name=instance_name,
        ),
        lane_provider_env_reader=lane_provider_env_reader,
    )
    retired_provider_keys = runtime_input.get("retired_provider_keys")
    adoption = _plan_provider_key_adoption(
        existing_records=existing_runtime_records,
        runtime_input=runtime_input,
        lane_provider_env_reader=lane_provider_env_reader,
    )
    if adoption is not None:
        if mode == "apply" and adoption.refused_keys:
            raise ProductConfigError(
                "Provider key adoption names keys that are missing on the provider or look "
                "like credentials; remove them and review a fresh dry run.",
                code="provider_key_adoption_refused",
            )
        runtime_env = {**runtime_env, **adoption.adopted_env}
        if adoption.template_default_keys:
            current_record = _find_runtime_environment_record(
                existing_records=existing_runtime_records,
                scope=str(runtime_input["scope"]),
                context_name=str(runtime_input["context"]),
                instance_name=str(runtime_input["instance"]),
            )
            base_retired_keys = (
                retired_provider_keys
                if retired_provider_keys is not None
                else (current_record.retired_provider_keys if current_record is not None else ())
            )
            retired_provider_keys = tuple(
                sorted(set(base_retired_keys) | set(adoption.template_default_keys))
            )
    runtime_record, runtime_summary = _plan_product_config_runtime_environment(
        existing_records=existing_runtime_records,
        scope=str(runtime_input["scope"]),
        context_name=str(runtime_input["context"]),
        instance_name=str(runtime_input["instance"]),
        env=runtime_env,
        retired_provider_keys=retired_provider_keys,
        source_label=source_label,
    )
    secret_summaries: list[dict[str, object]] = []
    runtime_key_safety_summary = _evaluate_product_config_runtime_key_safety(
        record_store=record_store,
        context_name=context_name,
        instance_name=instance_name,
        secrets=secrets,
    )
    apply_changes = mode == "apply"
    secret_versions: list[SecretVersion] = []
    secret_records: list[SecretRecord] = []
    secret_bindings: list[SecretBinding] = []
    secret_audit_events: list[SecretAuditEvent] = []
    adopted_secret_ids: list[str] = []
    for index, secret in enumerate(secrets):
        planned_action, existing_secret_id = _product_config_secret_current_action(
            record_store=record_store,
            secret=secret,
        )
        if apply_changes:
            try:
                if index in copy_sources:
                    plaintext_value = product_secret_copy.copy_source_value(copy_sources[index])
                elif index in adopted_secret_values:
                    plaintext_value = adopted_secret_values[index]
                else:
                    plaintext_value = str(secret["value"])
            except product_secret_copy.ProductSecretCopyError as error:
                raise ProductConfigError(str(error), code=error.code) from error
            secret_plan = _plan_product_config_secret_write(
                record_store=record_store,
                scope=cast(SecretScope, str(secret["scope"])),
                integration=str(secret["integration"]),
                name=str(secret["name"]),
                plaintext_value=plaintext_value,
                binding_key=str(secret["binding_key"]),
                context_name=str(secret["context"]),
                instance_name=str(secret["instance"]),
                description=str(secret["description"]),
                declared_secret_class=cast(RuntimeSecretClass | None, secret["secret_class"]),
                sharing_reason=_product_config_sharing_reason(secret, actor=actor),
                actor=actor,
                source_label=source_label,
            )
            secret_id = secret_plan["secret_id"]
            if index in copy_sources:
                for event in secret_plan["secret_audit_events"]:
                    event.metadata.update(
                        {
                            "copy_from_secret_id": copy_sources[index].secret_id,
                            "copy_from_version_id": copy_sources[index].version_id,
                        }
                    )
            if index in adopted_secret_values:
                adopted_secret_ids.append(secret_id)
                for event in secret_plan["secret_audit_events"]:
                    event.metadata.update({"value_source": "provider_env"})
            secret_versions.extend(secret_plan["secret_versions"])
            secret_records.extend(secret_plan["secret_records"])
            secret_bindings.extend(secret_plan["secret_bindings"])
            secret_audit_events.extend(secret_plan["secret_audit_events"])
            secret_bindings.extend(
                _planned_disabled_runtime_secret_placeholder_retirements(
                    record_store=record_store,
                    configured_binding=secret_plan["configured_binding"],
                    updated_at=secret_plan["updated_at"],
                ),
            )
            secret_summaries.append(
                _summarize_product_config_secret_input(
                    action=secret_plan["action"],
                    secret=secret,
                    secret_id=secret_id,
                )
            )
            continue
        secret_summaries.append(
            _summarize_product_config_secret_input(
                action=planned_action,
                secret=secret,
                secret_id=existing_secret_id,
            )
        )
    runtime_environment_writes: tuple[RuntimeEnvironmentWrite, ...] = ()
    if apply_changes and runtime_record is not None:
        expected_record = _find_runtime_environment_record(
            existing_records=existing_runtime_records,
            scope=runtime_record.scope,
            context_name=runtime_record.context,
            instance_name=runtime_record.instance,
        )
        runtime_environment_writes = (
            RuntimeEnvironmentWrite(
                record=runtime_record,
                expected_record=expected_record,
                expected_absent=expected_record is None,
            ),
        )
        if runtime_summary["action"] != "unchanged":
            runtime_summary = {
                **runtime_summary,
                "record": summarize_runtime_environment_record(runtime_record),
            }

    changed_secret_count = sum(
        1 for item in secret_summaries if item["action"] in {"created", "rotated"}
    )
    result: dict[str, object] = {
        "status": "ok",
        "mode": mode,
        "product": product,
        "context": context_name,
        "instance": instance_name,
        "actor": actor,
        "source_label": source_label,
        "runtime_environment": runtime_summary,
        "runtime_key_safety": runtime_key_safety_summary,
        "secrets": secret_summaries,
        "provider_key_adoption": adoption.summary() if adoption is not None else [],
        "summary": {
            "runtime_changed_key_count": len(
                cast(list[object], runtime_summary.get("changed_keys", []))
            ),
            "secret_change_count": changed_secret_count,
        },
    }
    bundle = ProductAuthorityBundle(
        expected_product_profiles=(copy_profile,) if copy_profile is not None else (),
        secret_copy_sources=tuple(copy_expectations),
        absent_secret_ids=tuple(adopted_secret_ids),
        required_context_owners=tuple(sorted(copy_contexts | {(product, context_name)}))
        if copy_profile is not None
        else (),
        runtime_environment_writes=runtime_environment_writes,
        secret_versions=tuple(secret_versions),
        secret_records=tuple(secret_records),
        secret_bindings=tuple(secret_bindings),
        secret_audit_events=tuple(secret_audit_events),
    )
    return result, bundle


def _default_runtime_scope(*, context_name: str, instance_name: str) -> str:
    if instance_name:
        return "instance"
    if context_name:
        return "context"
    return "global"


def _default_secret_scope(*, context_name: str, instance_name: str) -> str:
    if instance_name:
        return "context_instance"
    if context_name:
        return "context"
    return "global"


def _product_config_runtime_input(
    payload: dict[str, object], *, context_name: str, instance_name: str
) -> _ProductConfigRuntimeInput:
    runtime_payload = payload.get("runtime_env", payload.get("runtime_environment", {}))
    if runtime_payload is None:
        return {
            "scope": _default_runtime_scope(context_name=context_name, instance_name=instance_name),
            "context": context_name,
            "instance": instance_name,
            "env": {},
            "retired_provider_keys": None,
        }
    if not isinstance(runtime_payload, dict):
        raise ProductConfigError("Product config runtime_env must be a JSON object.")
    if "env" in runtime_payload:
        raw_env = runtime_payload.get("env")
        if raw_env is None:
            raw_env = {}
        if not isinstance(raw_env, dict):
            raise ProductConfigError("Product config runtime_env.env must be a JSON object.")
    else:
        raw_env = {
            key: value
            for key, value in runtime_payload.items()
            if key
            not in {"scope", "context", "instance", "retired_provider_keys", "adopt_provider_keys"}
        }
    runtime_context = str(runtime_payload.get("context", context_name) or "").strip()
    runtime_instance = str(runtime_payload.get("instance", instance_name) or "").strip()
    expected_scope = _default_runtime_scope(context_name=context_name, instance_name=instance_name)
    scope = str(
        runtime_payload.get(
            "scope",
            expected_scope,
        )
        or ""
    ).strip()
    if scope != expected_scope:
        raise ProductConfigError(
            "Product config runtime_env scope must match the top-level target."
        )
    _validate_product_config_target_alignment(
        target_kind="runtime_env",
        context_name=runtime_context,
        instance_name=runtime_instance,
        expected_context=context_name,
        expected_instance=instance_name,
    )
    retired_keys = None
    if (
        "retired_provider_keys" in runtime_payload
        and runtime_payload["retired_provider_keys"] is not None
    ):
        if payload.get("schema_version", 1) != 2:
            raise ProductConfigError(
                "Provider key retirement requires product-config schema version 2."
            )
        try:
            retired_keys = normalize_retired_provider_keys(runtime_payload["retired_provider_keys"])
        except ValueError as error:
            raise ProductConfigError(str(error)) from error
        if scope != "instance":
            raise ProductConfigError("Provider key retirement requires an instance-scoped request.")
    adopt_keys = None
    if runtime_payload.get("adopt_provider_keys") is not None:
        if payload.get("schema_version", 1) != 2:
            raise ProductConfigError(
                "Provider key adoption requires product-config schema version 2."
            )
        if scope != "instance":
            raise ProductConfigError("Provider key adoption requires an instance-scoped request.")
        try:
            adopt_keys = provider_key_adoption.normalize_adopt_provider_keys(
                runtime_payload["adopt_provider_keys"]
            )
        except provider_key_adoption.ProviderKeyAdoptionError as error:
            raise ProductConfigError(str(error), code=error.code) from error
        configured_keys = {str(key).strip() for key in raw_env} | set(retired_keys or ())
        if configured_keys & set(adopt_keys):
            raise ProductConfigError(
                "A provider key cannot be both adopted and set or retired in the same request."
            )
    return {
        "scope": scope,
        "context": runtime_context,
        "instance": runtime_instance,
        "env": raw_env,
        "retired_provider_keys": retired_keys,
        "adopt_provider_keys": adopt_keys,
    }


def _plan_provider_key_adoption(
    *,
    existing_records: tuple[RuntimeEnvironmentRecord, ...],
    runtime_input: _ProductConfigRuntimeInput,
    lane_provider_env_reader: LaneProviderEnvReader | None,
) -> provider_key_adoption.ProviderKeyAdoptionPlan | None:
    adopt_keys = runtime_input.get("adopt_provider_keys")
    if adopt_keys is None:
        return None
    if lane_provider_env_reader is None:
        raise ProductConfigError(
            "Provider key adoption needs the service's read of the lane's provider env.",
            code="provider_env_unavailable",
        )
    return provider_key_adoption.plan_provider_key_adoption(
        keys=adopt_keys,
        provider=lane_provider_env_reader(),
        recorded_keys=_lane_recorded_runtime_keys(
            existing_records=existing_records,
            context_name=str(runtime_input["context"]),
            instance_name=str(runtime_input["instance"]),
        ),
    )


def _lane_recorded_runtime_keys(
    *,
    existing_records: tuple[RuntimeEnvironmentRecord, ...],
    context_name: str,
    instance_name: str,
) -> frozenset[str]:
    """Keys the lane's own runtime records already supply; the deploy delivers those."""
    return frozenset(
        key
        for record in existing_records
        if (record.scope == "context" and record.context == context_name)
        or (
            record.scope == "instance"
            and record.context == context_name
            and record.instance == instance_name
        )
        for key in record.env
    )


def _resolve_provider_secret_adoptions(
    *,
    record_store: control_plane_secrets.SecretWriteStore,
    secrets: tuple[dict[str, object], ...],
    recorded_keys: frozenset[str],
    lane_provider_env_reader: LaneProviderEnvReader | None,
) -> dict[int, str]:
    """Each adopted secret's value from the lane's provider env, by input index.

    Only a value that lives on the provider alone is adopted: a key a Launchplane
    record already supplies for the lane (a runtime setting, the tracked target's
    env, or a managed secret) is refused, and so is a destination secret that
    already exists under another binding key, so adoption never replaces a recorded
    value. Both modes check, so a dry run shows a refusal before apply. Refusals
    carry fixed messages; no value leaves this function except to the secret write.
    """
    adopted_indexes = [
        index for index, secret in enumerate(secrets) if secret.get("adopt_from_provider")
    ]
    if not adopted_indexes:
        return {}
    if lane_provider_env_reader is None:
        raise ProductConfigError(
            "Provider secret adoption needs the service's read of the lane's provider env.",
            code="provider_env_unavailable",
        )
    provider = lane_provider_env_reader()
    values: dict[int, str] = {}
    for index in adopted_indexes:
        key = str(secrets[index]["binding_key"])
        existing_action, _ = _product_config_secret_current_action(
            record_store=record_store, secret=secrets[index]
        )
        if key in recorded_keys or key in provider.recorded_keys or existing_action != "created":
            raise ProductConfigError(
                "A secret named for provider adoption is already supplied by a Launchplane "
                "record for this lane.",
                code="provider_secret_already_recorded",
            )
        value = provider.env.get(key, "")
        if not value.strip():
            raise ProductConfigError(
                "A secret named for provider adoption is missing or empty in the lane's "
                "provider env.",
                code="provider_secret_missing",
            )
        values[index] = value
    return values


def _normalize_product_config_runtime_env(raw_env: object) -> dict[str, ScalarValue]:
    if not isinstance(raw_env, dict):
        raise ProductConfigError("Product config runtime_env.env must be a JSON object.")
    env: dict[str, ScalarValue] = {}
    for raw_key, raw_value in raw_env.items():
        if not isinstance(raw_key, str):
            raise ProductConfigError("Product config runtime env keys must be strings.")
        key_name = _normalize_runtime_environment_key(raw_key)
        if is_secret_shaped_runtime_key(key_name):
            raise ProductConfigError(
                f"Runtime environment key {key_name!r} must be written as a managed secret."
            )
        if not isinstance(raw_value, (str, int, float, bool)):
            raise ProductConfigError(
                f"Product config runtime env value for {key_name!r} must be a scalar."
            )
        # Preserve the existing key-name rule above; share credential-value detection
        # without expanding it to harmless names containing e.g. KEYCLOAK or TOKENIZER.
        if provider_key_adoption.looks_like_credential("", str(raw_value)):
            raise ProductConfigError(
                f"Runtime environment key {key_name!r} must hold a plain setting; "
                "write credentials as managed secrets.",
                code="runtime_setting_refused",
            )
        env[key_name] = raw_value
    return env


def _product_config_secret_inputs(
    payload: dict[str, object], *, context_name: str, instance_name: str
) -> tuple[dict[str, object], ...]:
    raw_secrets = payload.get("secrets", [])
    if raw_secrets is None:
        return ()
    if not isinstance(raw_secrets, list):
        raise ProductConfigError("Product config secrets must be a JSON array.")
    normalized: list[dict[str, object]] = []
    for index, raw_secret in enumerate(raw_secrets, start=1):
        if not isinstance(raw_secret, dict):
            raise ProductConfigError(f"Product config secret #{index} must be a JSON object.")
        binding_key = str(raw_secret.get("binding_key", raw_secret.get("name", "")) or "").strip()
        name = str(raw_secret.get("name", binding_key) or "").strip()
        plaintext_value = raw_secret.get("value")
        if not binding_key:
            raise ProductConfigError(f"Product config secret #{index} requires binding_key.")
        if not name:
            raise ProductConfigError(f"Product config secret #{index} requires name.")
        copy_from = raw_secret.get("copy_from")
        adopt_from_provider = raw_secret.get("adopt_from_provider")
        if adopt_from_provider is not None:
            if not isinstance(adopt_from_provider, bool) or not adopt_from_provider:
                raise ProductConfigError(
                    f"Product config secret #{index} adopt_from_provider must be true."
                )
            if plaintext_value is not None or copy_from is not None:
                raise ProductConfigError(
                    "A secret adopted from the provider cannot also supply a value or copy_from."
                )
        elif copy_from is not None:
            if plaintext_value is not None:
                raise ProductConfigError("Secret copy cannot also supply a value.")
            try:
                copy_from = product_secret_copy.ProductSecretCopyFrom.model_validate(
                    copy_from
                ).model_dump()
            except ValueError as error:
                raise ProductConfigError("Secret copy reference is invalid.") from error
        elif not isinstance(plaintext_value, str) or not plaintext_value.strip():
            raise ProductConfigError(f"Product config secret #{index} requires a non-empty value.")
        expected_scope = _default_secret_scope(
            context_name=context_name, instance_name=instance_name
        )
        scope = str(
            raw_secret.get(
                "scope",
                expected_scope,
            )
            or ""
        ).strip()
        default_context = "" if scope == "global" else context_name
        default_instance = instance_name if scope == "context_instance" else ""
        secret_context = str(raw_secret.get("context", default_context) or "").strip()
        secret_instance = str(raw_secret.get("instance", default_instance) or "").strip()
        validated_scope = _validate_product_config_secret_scope_route(
            scope=scope,
            context_name=secret_context,
            instance_name=secret_instance,
            index=index,
        )
        context_scope_uses_instance_safety_target = (
            validated_scope == "context" and expected_scope == "context_instance"
        )
        if validated_scope != expected_scope and not context_scope_uses_instance_safety_target:
            raise ProductConfigError(
                f"Product config secret #{index} scope must match the top-level target."
            )
        if context_scope_uses_instance_safety_target:
            if secret_context != context_name:
                raise ProductConfigError(
                    f"Product config secret #{index} target must match the top-level target."
                )
        else:
            _validate_product_config_target_alignment(
                target_kind=f"secret #{index}",
                context_name=secret_context,
                instance_name=secret_instance,
                expected_context=context_name,
                expected_instance=instance_name,
            )
        integration = str(
            raw_secret.get(
                "integration",
                control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            )
            or ""
        ).strip()
        if not integration:
            raise ProductConfigError(f"Product config secret #{index} requires integration.")
        secret_class = _product_config_declared_secret_class(
            raw_secret.get("secret_class"), scope=validated_scope, index=index
        )
        sharing_reason = _product_config_sharing_reason_input(
            raw_secret.get("sharing_reason"),
            secret_class=secret_class,
            instance_name=secret_instance,
            index=index,
        )
        if copy_from is not None and (
            validated_scope != "context_instance"
            or integration != control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
            or secret_class is None
            or sharing_reason is None
        ):
            raise ProductConfigError(
                "Secret copy requires a lane-exact runtime secret, declared class, "
                "sharing reason and evidence."
            )
        if adopt_from_provider is not None and (
            validated_scope != "context_instance"
            or integration != control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
            or secret_class is None
        ):
            raise ProductConfigError(
                "A secret adopted from the provider must be a lane-exact runtime secret "
                "with a declared class."
            )
        normalized.append(
            {
                "scope": validated_scope,
                "integration": integration,
                "name": name,
                "binding_key": binding_key,
                "value": plaintext_value,
                **({"copy_from": copy_from} if copy_from is not None else {}),
                **({"adopt_from_provider": True} if adopt_from_provider is not None else {}),
                "context": secret_context,
                "instance": secret_instance,
                "description": str(raw_secret.get("description", "") or "").strip(),
                "secret_class": secret_class,
                "sharing_reason": sharing_reason,
            }
        )
    return tuple(normalized)


def _product_config_declared_secret_class(
    raw_value: object, *, scope: SecretScope, index: int
) -> RuntimeSecretClass | None:
    if raw_value is None or raw_value == "":
        return None
    if not isinstance(raw_value, str) or raw_value.strip() not in get_args(RuntimeSecretClass):
        allowed = ", ".join(get_args(RuntimeSecretClass))
        raise ProductConfigError(
            f"Product config secret #{index} secret_class must be one of: {allowed}."
        )
    if scope != "context_instance":
        raise ProductConfigError(
            f"Product config secret #{index} secret_class applies only to a secret stored "
            "for one exact lane (scope context_instance)."
        )
    return cast(RuntimeSecretClass, raw_value.strip())


def _product_config_sharing_reason_input(
    raw_value: object,
    *,
    secret_class: RuntimeSecretClass | None,
    instance_name: str,
    index: int,
) -> dict[str, str] | None:
    """Validate why a declared class is safe; who recorded it is added at apply."""
    if raw_value is None:
        return None
    if secret_class is None:
        raise ProductConfigError(
            f"Product config secret #{index} sharing_reason explains a secret_class; "
            "set secret_class too."
        )
    if not isinstance(raw_value, dict) or set(raw_value) - {"kind", "reason", "evidence"}:
        raise ProductConfigError(
            f"Product config secret #{index} sharing_reason must be an object with "
            "kind, reason and evidence."
        )
    try:
        reason = SecretSharingReason.model_validate(raw_value)
    except ValueError as error:
        raise ProductConfigError(
            f"Product config secret #{index} sharing_reason is invalid: kind must be one of "
            "dev_store, read_only_source, pre_live or site_shared, and reason and evidence "
            "are required."
        ) from error
    if reason.kind == "pre_live" and runtime_key_safety_environment_class(instance_name) not in {
        "testing",
        "dev",
    }:
        raise ProductConfigError(
            f"Product config secret #{index} sharing_reason pre_live is only for a testing "
            "or dev lane."
        )
    return {"kind": reason.kind, "reason": reason.reason, "evidence": reason.evidence}


def _product_config_sharing_reason(
    secret: dict[str, object], *, actor: str, recorded_at: str = ""
) -> SecretSharingReason | None:
    sharing_reason = secret["sharing_reason"]
    if sharing_reason is None:
        return None
    return SecretSharingReason.model_validate(
        {
            **cast(dict[str, str], sharing_reason),
            "recorded_by": actor,
            "recorded_at": recorded_at or utc_now_timestamp(),
        }
    )


def _validate_product_config_secret_scope_route(
    *, scope: str, context_name: str, instance_name: str, index: int
) -> SecretScope:
    if scope not in _VALID_SECRET_SCOPES:
        expected_scopes = ", ".join(_VALID_SECRET_SCOPES)
        raise ProductConfigError(
            f"Product config secret #{index} has unsupported scope {scope!r}; "
            f"expected one of {expected_scopes}."
        )
    if scope == "global":
        if context_name or instance_name:
            raise ProductConfigError(
                f"Product config secret #{index} uses global scope and must not set "
                "context or instance."
            )
        return "global"
    if scope == "context":
        if not context_name or instance_name:
            raise ProductConfigError(
                f"Product config secret #{index} uses context scope and must set context "
                "without instance."
            )
        return "context"
    if not context_name or not instance_name:
        raise ProductConfigError(
            f"Product config secret #{index} uses context_instance scope and must set "
            "context and instance."
        )
    return "context_instance"


def _validate_product_config_target_alignment(
    *,
    target_kind: str,
    context_name: str,
    instance_name: str,
    expected_context: str,
    expected_instance: str,
) -> None:
    if context_name != expected_context or instance_name != expected_instance:
        raise ProductConfigError(
            f"Product config {target_kind} target must match the top-level target."
        )


def _require_product_config_master_key_if_needed(secrets: tuple[dict[str, object], ...]) -> None:
    if not secrets:
        return
    try:
        control_plane_secrets.validate_secret_key_configuration()
    except click.ClickException as error:
        raise ProductConfigError(
            "Product config secrets require valid Launchplane secret-key configuration in the "
            "trusted Launchplane context.",
            code="secret_configuration_required",
        ) from error


def _product_config_secret_current_action(
    *, record_store: control_plane_secrets.SecretWriteStore, secret: dict[str, object]
) -> tuple[str, str]:
    existing_record = record_store.find_secret_record(
        scope=str(secret["scope"]),
        integration=str(secret["integration"]),
        name=str(secret["name"]),
        context=str(secret["context"]),
        instance=str(secret["instance"]),
    )
    if existing_record is None:
        return "created", ""
    return "rotated", existing_record.secret_id


def _plan_product_config_secret_write(
    *,
    record_store: control_plane_secrets.SecretWriteStore,
    scope: SecretScope,
    integration: str,
    name: str,
    plaintext_value: str,
    binding_key: str,
    context_name: str = "",
    instance_name: str = "",
    description: str = "",
    declared_secret_class: RuntimeSecretClass | None = None,
    sharing_reason: SecretSharingReason | None = None,
    actor: str = "",
    source_label: str = "manual",
) -> _ProductConfigSecretWritePlan:
    if not plaintext_value.strip():
        raise ProductConfigError("Product config secret values must be non-empty.")
    now = utc_now_timestamp()
    existing_record = record_store.find_secret_record(
        scope=scope,
        integration=integration,
        name=name,
        context=context_name,
        instance=instance_name,
    )
    secret_id = (
        existing_record.secret_id
        if existing_record is not None
        else control_plane_secrets.expected_secret_id(
            integration=integration,
            name=name,
            context=context_name,
            instance=instance_name,
        )
    )
    created_at = existing_record.created_at if existing_record is not None else now
    binding = SecretBinding(
        binding_id=control_plane_secrets.expected_secret_binding_id(
            secret_id=secret_id,
            binding_key=binding_key,
        ),
        secret_id=secret_id,
        integration=integration,
        binding_key=binding_key,
        context=context_name,
        instance=instance_name,
        declared_secret_class=declared_secret_class,
        sharing_reason=sharing_reason,
        created_at=created_at,
        updated_at=now,
    )
    action = "created" if existing_record is None else "rotated"
    version_id = control_plane_secrets._version_id(secret_id=secret_id)
    ciphertext, key_id = control_plane_secrets._encrypt_secret_value(plaintext_value)
    version = SecretVersion(
        version_id=version_id,
        secret_id=secret_id,
        created_at=now,
        created_by=actor,
        key_id=key_id,
        ciphertext=ciphertext,
    )
    record = SecretRecord(
        secret_id=secret_id,
        scope=scope,
        integration=integration,
        name=name,
        context=context_name,
        instance=instance_name,
        description=description,
        current_version_id=version_id,
        created_at=created_at,
        updated_at=now,
        updated_by=actor,
        last_validated_at=existing_record.last_validated_at if existing_record is not None else "",
    )
    event = SecretAuditEvent(
        event_id=control_plane_secrets._audit_event_id(
            secret_id=secret_id,
            event_type=action,
        ),
        secret_id=secret_id,
        event_type="created" if action == "created" else "rotated",
        recorded_at=now,
        actor=actor,
        detail=f"Launchplane {action} managed secret from {source_label}.",
        metadata={"source": source_label, "binding_key": binding_key},
    )
    return {
        "secret_id": secret_id,
        "action": action,
        "updated_at": now,
        "configured_binding": binding,
        "secret_versions": [version],
        "secret_records": [record],
        "secret_bindings": [binding],
        "secret_audit_events": [event],
    }


def _evaluate_product_config_runtime_key_safety(
    *,
    record_store: ProductConfigStore,
    context_name: str,
    instance_name: str,
    secrets: tuple[dict[str, object], ...],
) -> dict[str, object]:
    runtime_secrets = tuple(
        secret
        for secret in secrets
        if secret["integration"] == control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
    )
    if not runtime_secrets:
        return {"required": False, "status": "skipped", "checked_binding_keys": []}
    try:
        policy_record = latest_active_runtime_key_safety_policy(record_store)
    except ValueError as error:
        raise ProductConfigError(
            "Product config runtime key-safety policy is unavailable.",
            code="runtime_key_safety_unavailable",
        ) from error
    target = RuntimeKeySafetyTarget(
        context=context_name,
        instance=instance_name,
        environment_class=runtime_key_safety_environment_class(instance_name),
    )
    evaluation = evaluate_runtime_key_safety(
        target=target,
        required_binding_keys=(str(secret["binding_key"]) for secret in runtime_secrets),
        secret_bindings=_planned_runtime_secret_bindings(
            record_store=record_store,
            secrets=runtime_secrets,
        ),
        secret_rules=policy_record.rules,
        integration_key_markers=policy_record.integration_key_markers,
        unreasoned_shared_integration_keys="refuse",
    )
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
        raise ProductConfigError(
            "Product config runtime key-safety gate failed.",
            code="runtime_key_safety_failed",
        )
    return summary


def _planned_runtime_secret_bindings(
    *,
    record_store: ProductConfigStore,
    secrets: tuple[dict[str, object], ...],
) -> tuple[SecretBinding, ...]:
    existing_runtime_bindings = record_store.list_secret_bindings(
        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        limit=None,
    )
    existing_bindings = {binding.binding_id: binding for binding in existing_runtime_bindings}
    planned_bindings: list[SecretBinding] = []
    for secret in secrets:
        existing_record = record_store.find_secret_record(
            scope=str(secret["scope"]),
            integration=str(secret["integration"]),
            name=str(secret["name"]),
            context=str(secret["context"]),
            instance=str(secret["instance"]),
        )
        secret_id = (
            existing_record.secret_id
            if existing_record is not None
            else control_plane_secrets.expected_secret_id(
                integration=str(secret["integration"]),
                name=str(secret["name"]),
                context=str(secret["context"]),
                instance=str(secret["instance"]),
            )
        )
        binding_id = control_plane_secrets.expected_secret_binding_id(
            secret_id=secret_id,
            binding_key=str(secret["binding_key"]),
        )
        existing_binding = existing_bindings.get(binding_id)
        now = utc_now_timestamp()
        planned_bindings.append(
            SecretBinding(
                binding_id=binding_id,
                secret_id=secret_id,
                integration=str(secret["integration"]),
                binding_key=str(secret["binding_key"]),
                context=str(secret["context"]),
                instance=str(secret["instance"]),
                status="configured",
                declared_secret_class=cast(RuntimeSecretClass | None, secret["secret_class"]),
                sharing_reason=_product_config_sharing_reason(secret, actor="", recorded_at=now),
                created_at=existing_binding.created_at if existing_binding is not None else now,
                updated_at=now,
            )
        )
    planned_binding_ids = {binding.binding_id for binding in planned_bindings}
    planned_routes = {
        (binding.integration, binding.binding_key, binding.context, binding.instance)
        for binding in planned_bindings
    }
    configured_existing_bindings = tuple(
        binding
        for binding in existing_runtime_bindings
        if binding.status == "configured"
        and binding.binding_id not in planned_binding_ids
        and (binding.integration, binding.binding_key, binding.context, binding.instance)
        in planned_routes
    )
    return (*planned_bindings, *configured_existing_bindings)


def _retire_disabled_runtime_secret_placeholders(
    *,
    record_store: ProductConfigStore,
    configured_binding: SecretBinding,
    updated_at: str,
) -> None:
    if (
        configured_binding.integration
        != control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
    ):
        return
    context_name = configured_binding.context.strip()
    instance_name = configured_binding.instance.strip()
    lookup_kwargs: _SecretBindingLookupKwargs = {
        "integration": configured_binding.integration,
        "limit": None,
    }
    if context_name:
        lookup_kwargs["context_name"] = context_name
    if instance_name:
        lookup_kwargs["instance_name"] = instance_name
    for binding in record_store.list_secret_bindings(
        **lookup_kwargs,
    ):
        if binding.binding_id == configured_binding.binding_id:
            continue
        if binding.context != context_name or binding.instance != instance_name:
            continue
        if binding.binding_key != configured_binding.binding_key:
            continue
        if binding.status != "disabled":
            continue
        record_store.write_secret_binding(
            binding.model_copy(
                update={
                    "integration": f"retired:{binding.integration}",
                    "updated_at": updated_at,
                }
            )
        )


def _planned_disabled_runtime_secret_placeholder_retirements(
    *,
    record_store: ProductConfigStore,
    configured_binding: SecretBinding,
    updated_at: str,
) -> tuple[SecretBinding, ...]:
    if (
        configured_binding.integration
        != control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
    ):
        return ()
    context_name = configured_binding.context.strip()
    instance_name = configured_binding.instance.strip()
    lookup_kwargs: _SecretBindingLookupKwargs = {
        "integration": configured_binding.integration,
        "limit": None,
    }
    if context_name:
        lookup_kwargs["context_name"] = context_name
    if instance_name:
        lookup_kwargs["instance_name"] = instance_name
    retirements: list[SecretBinding] = []
    for binding in record_store.list_secret_bindings(**lookup_kwargs):
        if binding.binding_id == configured_binding.binding_id:
            continue
        if binding.context != context_name or binding.instance != instance_name:
            continue
        if binding.binding_key != configured_binding.binding_key:
            continue
        if binding.status != "disabled":
            continue
        retirements.append(
            binding.model_copy(
                update={
                    "integration": f"retired:{binding.integration}",
                    "updated_at": updated_at,
                }
            )
        )
    return tuple(retirements)


def _summarize_product_config_secret_input(
    *, action: str, secret: dict[str, object], secret_id: str = ""
) -> dict[str, object]:
    summary = {
        "action": action,
        "scope": secret["scope"],
        "integration": secret["integration"],
        "name": secret["name"],
        "binding_key": secret["binding_key"],
        "context": secret["context"],
        "instance": secret["instance"],
    }
    if secret["secret_class"] is not None:
        summary["secret_class"] = secret["secret_class"]
    if secret["sharing_reason"] is not None:
        summary["sharing_reason"] = secret["sharing_reason"]
    if secret.get("copy_from") is not None:
        summary["copy_from"] = secret["copy_from"]
    if secret_id:
        summary["secret_id"] = secret_id
    return summary


def _plan_product_config_runtime_environment(
    *,
    existing_records: tuple[RuntimeEnvironmentRecord, ...],
    scope: str,
    context_name: str,
    instance_name: str,
    env: dict[str, ScalarValue],
    source_label: str,
    retired_provider_keys: tuple[str, ...] | None = None,
) -> tuple[RuntimeEnvironmentRecord | None, dict[str, object]]:
    _validate_runtime_environment_scope_route(
        scope=scope,
        context_name=context_name,
        instance_name=instance_name,
    )
    target_record = _find_runtime_environment_record(
        existing_records=existing_records,
        scope=scope,
        context_name=context_name,
        instance_name=instance_name,
    )
    if not env and retired_provider_keys is None:
        return (
            None,
            {
                "action": "skipped",
                "scope": scope,
                "context": context_name,
                "instance": instance_name,
                "keys": [],
                "changed_keys": [],
                "unchanged_keys": [],
                "env_value_count_after": len(target_record.env) if target_record is not None else 0,
            },
        )
    current_values = dict(target_record.env) if target_record is not None else {}
    previous_retired_keys = target_record.retired_provider_keys if target_record is not None else ()
    planned_retired_keys = (
        previous_retired_keys if retired_provider_keys is None else retired_provider_keys
    )
    if set(planned_retired_keys) & (current_values.keys() | env.keys()):
        raise ProductConfigError("A provider key cannot be both configured and retired.")
    if not current_values and not env:
        raise ProductConfigError("Provider key retirement requires existing runtime values.")
    changed_keys = sorted(
        {
            key_name
            for key_name, value in env.items()
            if key_name not in current_values or str(current_values[key_name]) != str(value)
        }
        | (set(previous_retired_keys) ^ set(planned_retired_keys))
    )
    unchanged_keys = sorted(key_name for key_name in env if key_name not in changed_keys)
    action = "created" if target_record is None else "updated"
    if not changed_keys:
        action = "unchanged"
    planned_values: dict[str, ScalarValue] = dict(current_values)
    planned_values.update(env)
    planned_record = (
        target_record
        if not changed_keys and target_record is not None
        else RuntimeEnvironmentRecord(
            schema_version=2 if planned_retired_keys else 1,
            scope=cast(RuntimeEnvironmentScope, scope),
            context=context_name,
            instance=instance_name,
            env=planned_values,
            retired_provider_keys=planned_retired_keys,
            updated_at=utc_now_timestamp(),
            source_label=source_label.strip() or "product-config-apply",
        )
    )
    return (
        planned_record,
        {
            "action": action,
            "scope": scope,
            "context": context_name,
            "instance": instance_name,
            "keys": sorted(env),
            "changed_keys": changed_keys,
            "unchanged_keys": unchanged_keys,
            "env_value_count_after": len(planned_values),
            "retired_provider_keys_before": list(previous_retired_keys),
            "retired_provider_keys_after": list(planned_retired_keys),
        },
    )


def _normalize_runtime_environment_key(raw_key: str) -> str:
    normalized_key = raw_key.strip()
    if not normalized_key:
        raise ProductConfigError("Runtime environment keys must be non-empty.")
    return normalized_key


def _validate_runtime_environment_scope_route(
    *,
    scope: str,
    context_name: str,
    instance_name: str,
) -> None:
    if scope == "global":
        if context_name or instance_name:
            raise ProductConfigError(
                "Global runtime environment records do not accept --context or --instance."
            )
        return
    if scope == "context":
        if not context_name or instance_name:
            raise ProductConfigError(
                "Context runtime environment records require --context and do not accept --instance."
            )
        return
    if scope == "instance":
        if not context_name or not instance_name:
            raise ProductConfigError(
                "Instance runtime environment records require --context and --instance."
            )
        return
    raise ProductConfigError(f"Unsupported runtime environment scope: {scope}")


def _runtime_environment_record_matches(
    record: RuntimeEnvironmentRecord,
    *,
    scope: str,
    context_name: str,
    instance_name: str,
) -> bool:
    return (
        record.scope == scope
        and record.context == context_name
        and record.instance == instance_name
    )


def _find_runtime_environment_record(
    *,
    existing_records: tuple[RuntimeEnvironmentRecord, ...],
    scope: str,
    context_name: str,
    instance_name: str,
) -> RuntimeEnvironmentRecord | None:
    for record in existing_records:
        if _runtime_environment_record_matches(
            record,
            scope=scope,
            context_name=context_name,
            instance_name=instance_name,
        ):
            return record
    return None
