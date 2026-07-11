from __future__ import annotations

import click

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget
from control_plane.runtime_key_safety import (
    evaluate_runtime_key_safety_from_store,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
    runtime_secret_binding_matches_target,
)
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore


def enforce_worker_runtime_key_safety(
    *,
    context_name: str,
    instance_name: str,
    allowed_worker_keys: tuple[str, ...],
    operation_name: str,
) -> None:
    database_url = resolve_database_url(None)
    if database_url is None:
        raise click.ClickException(
            f"Runtime key-safety gate requires LAUNCHPLANE_DATABASE_URL for {operation_name}."
        )

    record_store = PostgresRecordStore(database_url=database_url)
    record_store.ensure_schema()
    try:
        target = RuntimeKeySafetyTarget(
            context=context_name,
            instance=instance_name,
            environment_class=runtime_key_safety_environment_class(instance_name),
        )
        bindings = record_store.list_secret_bindings(
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            limit=None,
        )
        allowed_key_set = set(allowed_worker_keys)
        binding_keys = tuple(
            sorted(
                {
                    binding.binding_key
                    for binding in bindings
                    if binding.binding_key in allowed_key_set
                    and runtime_secret_binding_matches_target(binding=binding, target=target)
                }
            )
        )
        if not binding_keys:
            return
        try:
            policy_record = latest_active_runtime_key_safety_policy(record_store)
            evaluation = evaluate_runtime_key_safety_from_store(
                record_store=record_store,
                policy_record=policy_record,
                target=target,
                required_binding_keys=binding_keys,
            )
        except ValueError as exc:
            raise click.ClickException(
                f"Runtime key-safety policy is unavailable for {operation_name}."
            ) from exc
        if evaluation.status == "pass":
            return
        finding_labels = sorted(
            {
                (f"{finding.code}[{finding.binding_key}]" if finding.binding_key else finding.code)
                for finding in evaluation.findings
            }
        )
        suffix = f": {', '.join(finding_labels)}" if finding_labels else ""
        raise click.ClickException(
            f"Runtime key-safety gate failed for {operation_name} under policy "
            f"{policy_record.record_id} ({policy_record.policy_sha256}){suffix}."
        )
    finally:
        record_store.close()
