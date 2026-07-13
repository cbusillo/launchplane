from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol

import click
from cryptography.fernet import Fernet, InvalidToken

from control_plane.child_process_errors import redact_untrusted_text
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretRotationWrite,
    SecretScope,
    SecretVersion,
)
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR = "LAUNCHPLANE_SECRET_KEYS_JSON"
LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR = "LAUNCHPLANE_MASTER_ENCRYPTION_KEY"
LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VARS = (LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR,)
DOKPLOY_SECRET_INTEGRATION = "dokploy"
RUNTIME_ENVIRONMENT_SECRET_INTEGRATION = "runtime_environment"
SECRET_STATUS_CONFIGURED = "configured"
LEGACY_SECRET_KEY_ID = "launchplane-master-key"
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MIN_UNIQUE_KEY_BYTES = 16

SecretWriteAction = Literal["created", "rotated", "unchanged"]
SecretBindingRelabelAction = Literal["relabelled", "unchanged"]


class SecretReadStore(Protocol):
    def read_secret_record(self, secret_id: str) -> SecretRecord: ...

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]: ...

    def read_secret_version(self, version_id: str) -> SecretVersion: ...

    def list_secret_versions(self, *, secret_id: str) -> tuple[SecretVersion, ...]: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[SecretAuditEvent, ...]: ...


class SecretWriteStore(SecretReadStore, Protocol):
    def find_secret_record(
        self,
        *,
        scope: str,
        integration: str,
        name: str,
        context: str = "",
        instance: str = "",
    ) -> SecretRecord | None: ...

    def write_secret_record(self, record: SecretRecord) -> None: ...

    def write_secret_version(self, version: SecretVersion) -> None: ...

    def write_secret_binding(self, binding: SecretBinding) -> None: ...

    def write_secret_audit_event(self, event: SecretAuditEvent) -> None: ...


class SecretRotationStore(SecretWriteStore, Protocol):
    def write_secret_rotations(self, rotations: tuple[SecretRotationWrite, ...]) -> None: ...


def _secret_slug(value: str) -> str:
    compact = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    )
    normalized = "-".join(part for part in compact.split("-") if part)
    return normalized or "secret"


def _secret_id(*, integration: str, name: str, context: str, instance: str) -> str:
    parts = ["secret", integration, name]
    if context.strip():
        parts.append(context)
    if instance.strip():
        parts.append(instance)
    return "-".join(_secret_slug(part) for part in parts)


def _binding_id(*, secret_id: str, binding_key: str) -> str:
    return f"{secret_id}-binding-{_secret_slug(binding_key)}"


def _version_id(*, secret_id: str) -> str:
    return f"{secret_id}-version-{uuid.uuid4().hex[:12]}"


def _audit_event_id(*, secret_id: str, event_type: str) -> str:
    return f"{secret_id}-event-{_secret_slug(event_type)}-{uuid.uuid4().hex[:12]}"


def _scope_rank(scope: str) -> int:
    if scope == "global":
        return 0
    if scope == "context":
        return 1
    return 2


@dataclass(frozen=True)
class KeyRing:
    active_key_id: str
    keys: Mapping[str, Fernet]
    legacy_compatibility_key_loaded: bool = False


def _validated_key_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _KEY_ID_PATTERN.fullmatch(value):
        raise click.ClickException(
            f"Invalid {LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR} configuration: "
            f"{label} must match {_KEY_ID_PATTERN.pattern!r}."
        )
    return value


def _canonical_fernet_key(raw_key: object, *, key_id: str) -> bytes:
    if not isinstance(raw_key, str) or raw_key != raw_key.strip():
        raise click.ClickException(
            f"Invalid canonical key material for key {key_id!r}: expected an exact string."
        )
    try:
        encoded = raw_key.encode("ascii")
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise click.ClickException(
            f"Invalid canonical key material for key {key_id!r}: expected URL-safe base64."
        ) from error
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded) != encoded:
        raise click.ClickException(
            f"Invalid canonical key material for key {key_id!r}: expected 32 canonical bytes."
        )
    if len(set(decoded)) < _MIN_UNIQUE_KEY_BYTES:
        raise click.ClickException(
            f"Invalid canonical key material for key {key_id!r}: key material lacks entropy."
        )
    return encoded


def _get_key_ring() -> KeyRing:
    keys_json = os.environ.get(LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR, "").strip()
    if keys_json:
        try:
            parsed = json.loads(keys_json)
            if not isinstance(parsed, dict):
                raise ValueError("configuration must be a JSON object")
            unexpected_fields = sorted(set(parsed) - {"active_key_id", "keys"})
            if unexpected_fields:
                raise ValueError(f"unexpected fields: {', '.join(unexpected_fields)}")
            active_key_id = _validated_key_id(parsed["active_key_id"], label="active_key_id")
            keys_dict = parsed["keys"]
            if not isinstance(keys_dict, dict) or not keys_dict:
                raise ValueError("keys must be a non-empty object")
        except (KeyError, ValueError, TypeError) as error:
            raise click.ClickException(
                f"Invalid {LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR} configuration: {error}"
            ) from error

        encoded_keys: dict[str, bytes] = {}
        for key_id, raw_key in keys_dict.items():
            validated_key_id = _validated_key_id(key_id, label="key id")
            encoded_keys[validated_key_id] = _canonical_fernet_key(raw_key, key_id=validated_key_id)

        if active_key_id not in encoded_keys:
            raise click.ClickException(f"Active key id '{active_key_id}' not found in keys.")

        legacy_compatibility_key_loaded = False
        legacy_value = os.environ.get(LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR, "").strip()
        if legacy_value:
            legacy_key = _master_fernet_key(legacy_value)
            configured_legacy_key = encoded_keys.get(LEGACY_SECRET_KEY_ID)
            if configured_legacy_key is not None and configured_legacy_key != legacy_key:
                raise click.ClickException(
                    f"{LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR} key {LEGACY_SECRET_KEY_ID!r} "
                    f"does not match {LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR}."
                )
            if configured_legacy_key is None:
                encoded_keys[LEGACY_SECRET_KEY_ID] = legacy_key
                legacy_compatibility_key_loaded = True

        return KeyRing(
            active_key_id=active_key_id,
            keys={key_id: Fernet(encoded) for key_id, encoded in encoded_keys.items()},
            legacy_compatibility_key_loaded=legacy_compatibility_key_loaded,
        )

    for environment_key in LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VARS:
        configured_value = os.environ.get(environment_key, "")
        if configured_value.strip():
            fernet = Fernet(_master_fernet_key(configured_value))
            return KeyRing(
                active_key_id=LEGACY_SECRET_KEY_ID,
                keys={LEGACY_SECRET_KEY_ID: fernet},
                legacy_compatibility_key_loaded=True,
            )

    expected_keys = " or ".join(
        (LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR, *LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VARS)
    )
    raise click.ClickException(
        f"Launchplane managed secrets require {expected_keys} to read or write encrypted values."
    )


def _master_fernet_key(raw_key: str) -> bytes:
    normalized = raw_key.strip()
    if not normalized:
        expected_keys = f"{LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR} or " + " or ".join(
            LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VARS
        )
        raise click.ClickException(
            f"Launchplane managed secrets require {expected_keys} to read or write encrypted values."
        )
    encoded = normalized.encode("utf-8")
    try:
        Fernet(encoded)
        return encoded
    except (ValueError, TypeError):
        digest = hashlib.sha256(encoded).digest()
        return base64.urlsafe_b64encode(digest)


def validate_secret_key_configuration() -> None:
    _get_key_ring()


def _encrypt_secret_value(plaintext_value: str) -> tuple[str, str]:
    key_ring = _get_key_ring()
    cipher = key_ring.keys[key_ring.active_key_id]
    return cipher.encrypt(plaintext_value.encode("utf-8")).decode("utf-8"), key_ring.active_key_id


def _decrypt_secret_value(ciphertext: str, key_id: str) -> str:
    key_ring = _get_key_ring()
    cipher = key_ring.keys.get(key_id)
    if not cipher:
        raise click.ClickException(
            f"Launchplane could not decrypt a managed secret: key_id '{key_id}' is unknown or retired."
        )
    try:
        return cipher.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as error:
        raise click.ClickException(
            f"Launchplane could not decrypt a managed secret with key_id '{key_id}'."
        ) from error


def _open_secret_store(database_url: str | None = None) -> PostgresRecordStore | None:
    resolved_database_url = resolve_database_url(database_url)
    if resolved_database_url is None:
        return None
    return PostgresRecordStore(database_url=resolved_database_url)


def _scope_matches_record(
    record: SecretRecord,
    *,
    context_name: str,
    instance_name: str,
) -> bool:
    if record.scope == "global":
        return True
    if record.scope == "context":
        return record.context == context_name
    return record.context == context_name and record.instance == instance_name


def _binding_for_secret(
    record_store: SecretReadStore,
    *,
    secret_id: str,
    integration: str | None = None,
    context_name: str | None = None,
    instance_name: str | None = None,
) -> SecretBinding | None:
    for binding in record_store.list_secret_bindings(
        integration=integration or "",
        context_name=context_name or "",
        instance_name=instance_name or "",
        limit=None,
    ):
        if binding.secret_id != secret_id or binding.status != SECRET_STATUS_CONFIGURED:
            continue
        if integration is not None and binding.integration != integration:
            continue
        if context_name is not None and binding.context != context_name:
            continue
        if instance_name is not None and binding.instance != instance_name:
            continue
        return binding
    return None


def resolve_secret_values_for_integration(
    *,
    integration: str,
    context_name: str = "",
    instance_name: str = "",
    database_url: str | None = None,
) -> dict[str, str]:
    store = _open_secret_store(database_url)
    if store is None:
        return {}
    try:
        return resolve_secret_values_for_integration_from_store(
            record_store=store,
            integration=integration,
            context_name=context_name,
            instance_name=instance_name,
        )
    finally:
        store.close()


def resolve_secret_values_for_integration_from_store(
    *,
    record_store: SecretReadStore,
    integration: str,
    context_name: str = "",
    instance_name: str = "",
) -> dict[str, str]:
    candidate_records = [
        record
        for record in record_store.list_secret_records(integration=integration)
        if record.status == SECRET_STATUS_CONFIGURED
        and _scope_matches_record(record, context_name=context_name, instance_name=instance_name)
    ]
    candidate_records.sort(
        key=lambda record: (_scope_rank(record.scope), record.updated_at, record.secret_id)
    )
    resolved_values: dict[str, str] = {}
    for record in candidate_records:
        binding = _binding_for_secret(
            record_store,
            secret_id=record.secret_id,
            integration=record.integration,
            context_name=record.context,
            instance_name=record.instance,
        )
        if binding is None:
            continue
        version = record_store.read_secret_version(record.current_version_id)
        resolved_values[binding.binding_key] = _decrypt_secret_value(
            version.ciphertext, version.key_id
        )
    return resolved_values


def overlay_dokploy_environment_values(
    *,
    environment_values: dict[str, str],
    database_url: str | None = None,
) -> dict[str, str]:
    managed_values = resolve_secret_values_for_integration(
        integration=DOKPLOY_SECRET_INTEGRATION,
        database_url=database_url,
    )
    if not managed_values:
        return environment_values
    merged_values = dict(environment_values)
    merged_values.update(managed_values)
    return merged_values


def overlay_runtime_environment_secret_values(
    *,
    environment_values: dict[str, str],
    context_name: str,
    instance_name: str = "",
    database_url: str | None = None,
) -> dict[str, str]:
    managed_values = resolve_secret_values_for_integration(
        integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context_name,
        instance_name=instance_name,
        database_url=database_url,
    )
    if not managed_values:
        return environment_values
    merged_values = dict(environment_values)
    merged_values.update(managed_values)
    return merged_values


def overlay_runtime_environment_secret_values_from_store(
    *,
    environment_values: dict[str, str],
    record_store: SecretReadStore,
    context_name: str,
    instance_name: str = "",
) -> dict[str, str]:
    managed_values = resolve_secret_values_for_integration_from_store(
        record_store=record_store,
        integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context_name,
        instance_name=instance_name,
    )
    if not managed_values:
        return environment_values
    merged_values = dict(environment_values)
    merged_values.update(managed_values)
    return merged_values


def write_secret_value(
    *,
    record_store: SecretWriteStore,
    scope: SecretScope,
    integration: str,
    name: str,
    plaintext_value: str,
    binding_key: str,
    context_name: str = "",
    instance_name: str = "",
    description: str = "",
    actor: str = "",
    source_label: str = "manual",
) -> dict[str, str]:
    if not plaintext_value.strip():
        raise click.ClickException(
            "Launchplane managed secrets require a non-empty plaintext value."
        )
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
        else _secret_id(
            integration=integration,
            name=name,
            context=context_name,
            instance=instance_name,
        )
    )
    if existing_record is not None:
        current_version = record_store.read_secret_version(existing_record.current_version_id)
        if (
            _decrypt_secret_value(current_version.ciphertext, current_version.key_id)
            == plaintext_value
        ):
            binding = SecretBinding(
                binding_id=_binding_id(secret_id=secret_id, binding_key=binding_key),
                secret_id=secret_id,
                integration=integration,
                binding_key=binding_key,
                context=context_name,
                instance=instance_name,
                created_at=existing_record.created_at,
                updated_at=now,
            )
            record_store.write_secret_binding(binding)
            return {"status": "ok", "secret_id": secret_id, "action": "unchanged"}
    action: SecretWriteAction = "created" if existing_record is None else "rotated"
    version_id = _version_id(secret_id=secret_id)
    ciphertext, key_id = _encrypt_secret_value(plaintext_value)
    record_store.write_secret_version(
        SecretVersion(
            version_id=version_id,
            secret_id=secret_id,
            created_at=now,
            created_by=actor,
            key_id=key_id,
            ciphertext=ciphertext,
        )
    )
    created_at = existing_record.created_at if existing_record is not None else now
    record_store.write_secret_record(
        SecretRecord(
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
            last_validated_at=existing_record.last_validated_at
            if existing_record is not None
            else "",
        )
    )
    record_store.write_secret_binding(
        SecretBinding(
            binding_id=_binding_id(secret_id=secret_id, binding_key=binding_key),
            secret_id=secret_id,
            integration=integration,
            binding_key=binding_key,
            context=context_name,
            instance=instance_name,
            created_at=created_at,
            updated_at=now,
        )
    )
    record_store.write_secret_audit_event(
        SecretAuditEvent(
            event_id=_audit_event_id(secret_id=secret_id, event_type=action),
            secret_id=secret_id,
            event_type="created" if action == "created" else "rotated",
            recorded_at=now,
            actor=actor,
            detail=f"Launchplane {action} managed secret from {source_label}.",
            metadata={"source": source_label, "binding_key": binding_key},
        )
    )
    return {"status": "ok", "secret_id": secret_id, "action": action, "version_id": version_id}


def expected_secret_binding_id(*, secret_id: str, binding_key: str) -> str:
    return _binding_id(secret_id=secret_id, binding_key=binding_key)


def expected_secret_id(
    *, integration: str, name: str, context: str = "", instance: str = ""
) -> str:
    return _secret_id(integration=integration, name=name, context=context, instance=instance)


def relabel_secret_binding(
    *,
    record_store: SecretWriteStore,
    binding_id: str,
    binding_key: str,
    actor: str = "",
    source_label: str = "manual",
) -> dict[str, str]:
    normalized_binding_id = binding_id.strip()
    normalized_binding_key = binding_key.strip()
    if not normalized_binding_id or not normalized_binding_key:
        raise click.ClickException("Secret binding relabel requires binding id and binding key.")

    bindings = record_store.list_secret_bindings(limit=None)
    existing_binding = next(
        (binding for binding in bindings if binding.binding_id == normalized_binding_id), None
    )
    if existing_binding is None:
        raise click.ClickException(f"Secret binding {normalized_binding_id!r} was not found.")

    desired_binding_id = _binding_id(
        secret_id=existing_binding.secret_id, binding_key=normalized_binding_key
    )
    target_binding = next(
        (binding for binding in bindings if binding.binding_id == desired_binding_id), None
    )
    if existing_binding.binding_key == normalized_binding_key:
        return {
            "status": "ok",
            "action": "unchanged",
            "secret_id": existing_binding.secret_id,
            "binding_id": existing_binding.binding_id,
            "binding_key": existing_binding.binding_key,
        }
    if existing_binding.status != SECRET_STATUS_CONFIGURED:
        if (
            target_binding is not None
            and target_binding.status == SECRET_STATUS_CONFIGURED
            and target_binding.secret_id == existing_binding.secret_id
            and target_binding.binding_key == normalized_binding_key
        ):
            return {
                "status": "ok",
                "action": "unchanged",
                "secret_id": existing_binding.secret_id,
                "binding_id": target_binding.binding_id,
                "binding_key": target_binding.binding_key,
            }
        raise click.ClickException(f"Secret binding {normalized_binding_id!r} is not configured.")

    now = utc_now_timestamp()
    record_store.write_secret_binding(
        existing_binding.model_copy(update={"status": "disabled", "updated_at": now})
    )
    record_store.write_secret_binding(
        SecretBinding(
            binding_id=desired_binding_id,
            secret_id=existing_binding.secret_id,
            integration=existing_binding.integration,
            binding_key=normalized_binding_key,
            context=existing_binding.context,
            instance=existing_binding.instance,
            status="configured",
            created_at=target_binding.created_at if target_binding is not None else now,
            updated_at=now,
        )
    )
    record_store.write_secret_audit_event(
        SecretAuditEvent(
            event_id=_audit_event_id(secret_id=existing_binding.secret_id, event_type="relabelled"),
            secret_id=existing_binding.secret_id,
            event_type="relabelled",
            recorded_at=now,
            actor=actor,
            detail=f"Launchplane relabelled managed secret binding from {source_label}.",
            metadata={
                "source": source_label,
                "old_binding_id": existing_binding.binding_id,
                "old_binding_key": existing_binding.binding_key,
                "new_binding_id": desired_binding_id,
                "new_binding_key": normalized_binding_key,
            },
        )
    )
    return {
        "status": "ok",
        "action": "relabelled",
        "secret_id": existing_binding.secret_id,
        "binding_id": desired_binding_id,
        "binding_key": normalized_binding_key,
    }


def build_secret_status(record_store: SecretReadStore, *, secret_id: str) -> dict[str, object]:
    record = record_store.read_secret_record(secret_id)
    versions = record_store.list_secret_versions(secret_id=secret_id)
    binding = _binding_for_secret(record_store, secret_id=secret_id)
    audit_events = record_store.list_secret_audit_events(secret_id=secret_id)
    return {
        "secret_id": record.secret_id,
        "scope": record.scope,
        "integration": record.integration,
        "name": record.name,
        "context": record.context,
        "instance": record.instance,
        "policy": record.policy,
        "status": record.status,
        "description": record.description,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "updated_by": record.updated_by,
        "last_validated_at": record.last_validated_at,
        "current_version_id": record.current_version_id,
        "version_count": len(versions),
        "current_version_created_at": versions[0].created_at if versions else "",
        "binding": (
            {
                "binding_id": binding.binding_id,
                "binding_type": binding.binding_type,
                "binding_key": binding.binding_key,
                "status": binding.status,
                "context": binding.context,
                "instance": binding.instance,
                "updated_at": binding.updated_at,
            }
            if binding is not None
            else None
        ),
        "recent_audit_events": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "recorded_at": event.recorded_at,
                "actor": event.actor,
                "detail": event.detail,
                "metadata": event.metadata,
            }
            for event in audit_events[:5]
        ],
    }


def list_secret_statuses(
    record_store: SecretReadStore,
    *,
    integration: str = "",
    context_name: str = "",
    instance_name: str = "",
) -> list[dict[str, object]]:
    statuses: list[dict[str, object]] = []
    for record in record_store.list_secret_records(integration=integration or ""):
        if context_name and not _scope_matches_record(
            record, context_name=context_name, instance_name=instance_name
        ):
            continue
        if not context_name and instance_name:
            continue
        statuses.append(build_secret_status(record_store, secret_id=record.secret_id))
    statuses.sort(key=lambda item: (str(item["updated_at"]), str(item["secret_id"])), reverse=True)
    return statuses


def reencrypt_secrets(
    *,
    record_store: SecretRotationStore,
    apply: bool = False,
    expected_plan_digest: str = "",
    actor: str = "cli",
    source_label: str = "manual",
    reason: str = "",
) -> dict[str, object]:
    key_ring = _get_key_ring()
    active_key_id = key_ring.active_key_id

    records = tuple(
        sorted(
            (
                record
                for record in record_store.list_secret_records(limit=None)
                if record.status == SECRET_STATUS_CONFIGURED
            ),
            key=lambda item: item.secret_id,
        )
    )
    versions = tuple(
        record_store.read_secret_version(record.current_version_id) for record in records
    )
    plan_entries = tuple(
        {
            "secret_id": record.secret_id,
            "current_version_id": version.version_id,
            "key_id": version.key_id,
        }
        for record, version in zip(records, versions, strict=True)
    )
    plan_digest = hashlib.sha256(
        json.dumps(
            {"active_key_id": active_key_id, "entries": plan_entries},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    key_usage: dict[str, int] = {}
    for version in versions:
        key_usage[version.key_id] = key_usage.get(version.key_id, 0) + 1

    errors: list[str] = []
    for record, version in zip(records, versions, strict=True):
        try:
            _decrypt_secret_value(version.ciphertext, version.key_id)
        except click.ClickException as error:
            errors.append(f"Managed secret {record.secret_id} is not decryptable: {error}")

    candidate_count = sum(version.key_id != active_key_id for version in versions)
    unchanged_count = len(versions) - candidate_count
    retirement_blocked_key_ids = sorted(
        key_id
        for key_id in key_ring.keys
        if key_id != active_key_id and key_usage.get(key_id, 0) > 0
    )
    retirement_ready_key_ids = sorted(
        key_id
        for key_id in key_ring.keys
        if key_id != active_key_id and key_usage.get(key_id, 0) == 0
    )

    result: dict[str, object] = {
        "status": "ok" if not errors else "error",
        "dry_run": not apply,
        "plan_digest": plan_digest,
        "rotated_count": candidate_count,
        "unchanged_count": unchanged_count,
        "error_count": len(errors),
        "errors": errors,
        "active_key_id": active_key_id,
        "retirement_blocked_key_ids": retirement_blocked_key_ids,
        "retirement_ready_key_ids": retirement_ready_key_ids,
        "legacy_compatibility_key_loaded": key_ring.legacy_compatibility_key_loaded,
    }
    if errors or not apply:
        return result
    if not reason.strip():
        return {
            **result,
            "status": "error",
            "error_count": 1,
            "errors": ["Managed-secret re-encryption apply requires a reason."],
        }
    if expected_plan_digest != plan_digest:
        return {
            **result,
            "status": "error",
            "error_count": 1,
            "errors": ["Managed-secret re-encryption apply requires the matching dry-run digest."],
        }

    safe_reason = redact_untrusted_text(
        reason,
        fallback="Operator-requested managed-secret rotation.",
        maximum_length=160,
    )
    safe_source_label = redact_untrusted_text(
        source_label,
        fallback="managed-secret-rotation",
        maximum_length=96,
    )
    rotations: list[SecretRotationWrite] = []
    for record, current_version in zip(records, versions, strict=True):
        if current_version.key_id == active_key_id:
            continue
        plaintext = _decrypt_secret_value(current_version.ciphertext, current_version.key_id)
        ciphertext, new_key_id = _encrypt_secret_value(plaintext)
        now = utc_now_timestamp()
        version_id = _version_id(secret_id=record.secret_id)
        new_version = SecretVersion(
            version_id=version_id,
            secret_id=record.secret_id,
            created_at=now,
            created_by=actor,
            key_id=new_key_id,
            ciphertext=ciphertext,
        )
        updated_record = record.model_copy(
            update={"current_version_id": version_id, "updated_at": now, "updated_by": actor}
        )
        audit_event = SecretAuditEvent(
            event_id=_audit_event_id(secret_id=record.secret_id, event_type="rotated"),
            secret_id=record.secret_id,
            event_type="rotated",
            recorded_at=now,
            actor=actor,
            detail="Launchplane re-encrypted a managed secret under the active key.",
            metadata={
                "source": safe_source_label,
                "reason": safe_reason,
                "rotation_plan_digest": plan_digest,
                "old_key_id": current_version.key_id,
                "new_key_id": new_key_id,
                "old_version_id": current_version.version_id,
                "new_version_id": version_id,
            },
        )
        rotations.append(
            SecretRotationWrite(
                expected_current_version_id=current_version.version_id,
                record=updated_record,
                version=new_version,
                audit_event=audit_event,
            )
        )

    try:
        record_store.write_secret_rotations(tuple(rotations))
    except (FileNotFoundError, ValueError):
        return {
            **result,
            "status": "error",
            "error_count": 1,
            "errors": ["Managed-secret state changed after the matching dry-run."],
        }
    return result
