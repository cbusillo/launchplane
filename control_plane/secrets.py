from __future__ import annotations

import base64
import hashlib
import os
import uuid
from typing import Literal

import click
from cryptography.fernet import Fernet, InvalidToken

from control_plane.contracts.secret_record import SecretAuditEvent, SecretBinding, SecretRecord, SecretScope, SecretVersion
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR = "LAUNCHPLANE_MASTER_ENCRYPTION_KEY"
LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VARS = (LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR,)
DOKPLOY_SECRET_INTEGRATION = "dokploy"
RUNTIME_ENVIRONMENT_SECRET_INTEGRATION = "runtime_environment"
SECRET_STATUS_CONFIGURED = "configured"

SecretWriteAction = Literal["created", "rotated", "unchanged"]


def _secret_slug(value: str) -> str:
    compact = "".join(character.lower() if character.isalnum() else "-" for character in value.strip())
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


def _master_fernet_key(raw_key: str) -> bytes:
    normalized = raw_key.strip()
    if not normalized:
        expected_keys = " or ".join(LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VARS)
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


def _secret_cipher() -> Fernet:
    for environment_key in LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VARS:
        configured_value = os.environ.get(environment_key, "")
        if configured_value.strip():
            return Fernet(_master_fernet_key(configured_value))
    return Fernet(_master_fernet_key(""))


def _encrypt_secret_value(plaintext_value: str) -> str:
    return _secret_cipher().encrypt(plaintext_value.encode("utf-8")).decode("utf-8")


def _decrypt_secret_value(ciphertext: str) -> str:
    try:
        return _secret_cipher().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as error:
        raise click.ClickException(
            "Launchplane could not decrypt a managed secret with the configured master key."
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


def _binding_for_secret(record_store: PostgresRecordStore, *, secret_id: str) -> SecretBinding | None:
    for binding in record_store.list_secret_bindings(limit=None):
        if binding.secret_id == secret_id and binding.status == SECRET_STATUS_CONFIGURED:
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
        candidate_records = [
            record
            for record in store.list_secret_records(integration=integration)
            if record.status == SECRET_STATUS_CONFIGURED
            and _scope_matches_record(record, context_name=context_name, instance_name=instance_name)
        ]
        candidate_records.sort(key=lambda record: (_scope_rank(record.scope), record.updated_at, record.secret_id))
        resolved_values: dict[str, str] = {}
        for record in candidate_records:
            binding = _binding_for_secret(store, secret_id=record.secret_id)
            if binding is None:
                continue
            version = store.read_secret_version(record.current_version_id)
            resolved_values[binding.binding_key] = _decrypt_secret_value(version.ciphertext)
        return resolved_values
    finally:
        store.close()


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


def write_secret_value(
    *,
    record_store: PostgresRecordStore,
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
        raise click.ClickException("Launchplane managed secrets require a non-empty plaintext value.")
    now = utc_now_timestamp()
    existing_record = record_store.find_secret_record(
        scope=scope,
        integration=integration,
        name=name,
        context=context_name,
        instance=instance_name,
    )
    secret_id = existing_record.secret_id if existing_record is not None else _secret_id(
        integration=integration,
        name=name,
        context=context_name,
        instance=instance_name,
    )
    if existing_record is not None:
        current_version = record_store.read_secret_version(existing_record.current_version_id)
        if _decrypt_secret_value(current_version.ciphertext) == plaintext_value:
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
    record_store.write_secret_version(
        SecretVersion(
            version_id=version_id,
            secret_id=secret_id,
            created_at=now,
            created_by=actor,
            ciphertext=_encrypt_secret_value(plaintext_value),
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
            last_validated_at=existing_record.last_validated_at if existing_record is not None else "",
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


def build_secret_status(record_store: PostgresRecordStore, *, secret_id: str) -> dict[str, object]:
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
    record_store: PostgresRecordStore,
    *,
    integration: str = "",
    context_name: str = "",
    instance_name: str = "",
) -> list[dict[str, object]]:
    statuses: list[dict[str, object]] = []
    for record in record_store.list_secret_records(integration=integration or ""):
        if context_name and not _scope_matches_record(record, context_name=context_name, instance_name=instance_name):
            continue
        if not context_name and instance_name:
            continue
        statuses.append(build_secret_status(record_store, secret_id=record.secret_id))
    statuses.sort(key=lambda item: (str(item["updated_at"]), str(item["secret_id"])), reverse=True)
    return statuses
