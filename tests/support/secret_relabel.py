import click
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.contracts.secret_record import SecretBinding, SecretAuditEvent
from control_plane.secrets import (
    SecretWriteStore,
    SECRET_STATUS_CONFIGURED,
    _binding_id,
    _audit_event_id,
)


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
