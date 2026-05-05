from __future__ import annotations

import click

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeEnvironmentClass,
    RuntimeKeySafetyTarget,
)
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
)
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore


def _runtime_key_safety_environment_class(instance_name: str) -> RuntimeEnvironmentClass:
    normalized_instance = instance_name.strip().lower()
    if normalized_instance in {"prod", "production"}:
        return "prod"
    if normalized_instance in {"testing", "test", "staging", "stage"}:
        return "testing"
    if normalized_instance in {"preview", "pr"} or normalized_instance.startswith("pr-"):
        return "preview"
    if normalized_instance in {"dev", "local", "development"}:
        return "dev"
    return "unknown"


def enforce_worker_runtime_key_safety(
    *,
    context_name: str,
    instance_name: str,
    allowed_worker_keys: tuple[str, ...],
    operation_name: str,
) -> None:
    database_url = resolve_database_url(None)
    if database_url is None:
        return

    record_store = PostgresRecordStore(database_url=database_url)
    record_store.ensure_schema()
    try:
        bindings = record_store.list_secret_bindings(
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            context_name=context_name,
            instance_name=instance_name,
            limit=None,
        )
        allowed_key_set = set(allowed_worker_keys)
        binding_keys = tuple(
            binding.binding_key for binding in bindings if binding.binding_key in allowed_key_set
        )
        if not binding_keys:
            return
        try:
            policy_record = latest_active_runtime_key_safety_policy(record_store)
            evaluation = evaluate_runtime_key_safety_from_store(
                record_store=record_store,
                policy_record=policy_record,
                target=RuntimeKeySafetyTarget(
                    context=context_name,
                    instance=instance_name,
                    environment_class=_runtime_key_safety_environment_class(instance_name),
                ),
                required_binding_keys=binding_keys,
            )
        except ValueError as exc:
            raise click.ClickException(
                f"Runtime key-safety policy is unavailable for {operation_name}."
            ) from exc
        if evaluation.status == "pass":
            return
        finding_codes = sorted({finding.code for finding in evaluation.findings})
        suffix = f": {', '.join(finding_codes)}" if finding_codes else ""
        raise click.ClickException(f"Runtime key-safety gate failed for {operation_name}{suffix}.")
    finally:
        record_store.close()
