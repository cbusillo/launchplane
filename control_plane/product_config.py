from __future__ import annotations

import json
import os
from json import JSONDecodeError
from pathlib import Path
from typing import Literal, Protocol, cast

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentScope
from control_plane.contracts.runtime_environment_record import ScalarValue
from control_plane.contracts.secret_record import SecretScope
from control_plane.workflows.ship import utc_now_timestamp


MASTER_ENCRYPTION_KEY_ENV_KEYS = ("LAUNCHPLANE_MASTER_ENCRYPTION_KEY",)
SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS = {"PASSWORD", "TOKEN", "SECRET", "KEY"}
ProductConfigMode = Literal["dry-run", "apply"]
_VALID_SECRET_SCOPES: tuple[SecretScope, ...] = ("global", "context", "context_instance")


class ProductConfigStore(control_plane_secrets.SecretWriteStore, Protocol):
    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> None: ...


class ProductConfigError(ValueError):
    """Operator-facing product config validation or planning failure."""

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
    if schema_version != 1:
        raise ProductConfigError("Product config input schema_version must be 1.")


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
    }


def apply_product_config_bundle(
    *,
    record_store: ProductConfigStore,
    payload: dict[str, object],
    mode: ProductConfigMode,
    actor: str,
    source_label: str,
) -> dict[str, object]:
    if mode not in {"dry-run", "apply"}:
        raise ProductConfigError("Product config mode must be 'dry-run' or 'apply'.")
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
    _require_product_config_master_key_if_needed(secrets)

    runtime_record, runtime_summary = _plan_product_config_runtime_environment(
        existing_records=record_store.list_runtime_environment_records(),
        scope=str(runtime_input["scope"]),
        context_name=str(runtime_input["context"]),
        instance_name=str(runtime_input["instance"]),
        env=runtime_env,
        source_label=source_label,
    )
    secret_summaries: list[dict[str, object]] = []
    apply_changes = mode == "apply"
    for secret in secrets:
        planned_action, existing_secret_id = _product_config_secret_current_action(
            record_store=record_store,
            secret=secret,
        )
        if apply_changes:
            result = control_plane_secrets.write_secret_value(
                record_store=record_store,
                scope=cast(SecretScope, str(secret["scope"])),
                integration=str(secret["integration"]),
                name=str(secret["name"]),
                plaintext_value=str(secret["value"]),
                binding_key=str(secret["binding_key"]),
                context_name=str(secret["context"]),
                instance_name=str(secret["instance"]),
                description=str(secret["description"]),
                actor=actor,
                source_label=source_label,
            )
            secret_summaries.append(
                _summarize_product_config_secret_input(
                    action=str(result["action"]),
                    secret=secret,
                    secret_id=str(result["secret_id"]),
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
    if apply_changes and runtime_record is not None and runtime_summary["action"] != "unchanged":
        record_store.write_runtime_environment_record(runtime_record)
        runtime_summary = {
            **runtime_summary,
            "record": summarize_runtime_environment_record(runtime_record),
        }

    changed_secret_count = sum(
        1 for item in secret_summaries if item["action"] in {"created", "rotated"}
    )
    return {
        "status": "ok",
        "mode": mode,
        "product": product,
        "context": context_name,
        "instance": instance_name,
        "actor": actor,
        "source_label": source_label,
        "runtime_environment": runtime_summary,
        "secrets": secret_summaries,
        "summary": {
            "runtime_changed_key_count": len(
                cast(list[object], runtime_summary.get("changed_keys", []))
            ),
            "secret_change_count": changed_secret_count,
        },
    }


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
) -> dict[str, object]:
    runtime_payload = payload.get("runtime_env", payload.get("runtime_environment", {}))
    if runtime_payload is None:
        return {
            "scope": _default_runtime_scope(context_name=context_name, instance_name=instance_name),
            "context": context_name,
            "instance": instance_name,
            "env": {},
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
            if key not in {"scope", "context", "instance"}
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
    return {
        "scope": scope,
        "context": runtime_context,
        "instance": runtime_instance,
        "env": raw_env,
    }


def _normalize_product_config_runtime_env(raw_env: object) -> dict[str, ScalarValue]:
    if not isinstance(raw_env, dict):
        raise ProductConfigError("Product config runtime_env.env must be a JSON object.")
    env: dict[str, ScalarValue] = {}
    for raw_key, raw_value in raw_env.items():
        if not isinstance(raw_key, str):
            raise ProductConfigError("Product config runtime env keys must be strings.")
        key_name = _normalize_runtime_environment_key(raw_key)
        if _runtime_environment_key_requires_secret_store(key_name):
            raise ProductConfigError(
                f"Runtime environment key {key_name!r} must be written as a managed secret."
            )
        if not isinstance(raw_value, (str, int, float, bool)):
            raise ProductConfigError(
                f"Product config runtime env value for {key_name!r} must be a scalar."
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
        if not isinstance(plaintext_value, str) or not plaintext_value.strip():
            raise ProductConfigError(f"Product config secret #{index} requires a non-empty value.")
        secret_context = str(raw_secret.get("context", context_name) or "").strip()
        secret_instance = str(raw_secret.get("instance", instance_name) or "").strip()
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
        validated_scope = _validate_product_config_secret_scope_route(
            scope=scope,
            context_name=secret_context,
            instance_name=secret_instance,
            index=index,
        )
        if validated_scope != expected_scope:
            raise ProductConfigError(
                f"Product config secret #{index} scope must match the top-level target."
            )
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
        normalized.append(
            {
                "scope": validated_scope,
                "integration": integration,
                "name": name,
                "binding_key": binding_key,
                "value": plaintext_value,
                "context": secret_context,
                "instance": secret_instance,
                "description": str(raw_secret.get("description", "") or "").strip(),
            }
        )
    return tuple(normalized)


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
    if not any(os.environ.get(key, "").strip() for key in MASTER_ENCRYPTION_KEY_ENV_KEYS):
        expected_keys = " or ".join(MASTER_ENCRYPTION_KEY_ENV_KEYS)
        raise ProductConfigError(
            f"Product config secrets require {expected_keys} in the trusted Launchplane context.",
            code="secret_configuration_required",
        )


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
    current_version = record_store.read_secret_version(existing_record.current_version_id)
    if control_plane_secrets._decrypt_secret_value(current_version.ciphertext) == str(
        secret["value"]
    ):
        return "unchanged", existing_record.secret_id
    return "rotated", existing_record.secret_id


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
    if not env:
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
    changed_keys = sorted(
        key_name
        for key_name, value in env.items()
        if key_name not in current_values or str(current_values[key_name]) != str(value)
    )
    unchanged_keys = sorted(key_name for key_name in env if key_name not in changed_keys)
    action = "created" if target_record is None else "updated"
    if not changed_keys:
        action = "unchanged"
    planned_values: dict[str, ScalarValue] = dict(current_values)
    planned_values.update(env)
    planned_record = RuntimeEnvironmentRecord(
        scope=cast(RuntimeEnvironmentScope, scope),
        context=context_name,
        instance=instance_name,
        env=planned_values,
        updated_at=utc_now_timestamp(),
        source_label=source_label.strip() or "product-config-apply",
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
        },
    )


def _normalize_runtime_environment_key(raw_key: str) -> str:
    normalized_key = raw_key.strip()
    if not normalized_key:
        raise ProductConfigError("Runtime environment keys must be non-empty.")
    return normalized_key


def _runtime_environment_key_requires_secret_store(key_name: str) -> bool:
    return any(
        key_part in SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS
        for key_part in key_name.strip().upper().split("_")
    )


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
