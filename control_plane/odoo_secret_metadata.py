"""Recover omitted relabel metadata only from an unchanged historical source."""

from datetime import datetime

from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.secret_record import SecretAuditEvent, SecretBinding, SecretRecord
from control_plane.odoo_instance_overrides import (
    addon_setting_secret_env_key,
    config_parameter_secret_env_key,
)
from control_plane.secrets import expected_secret_binding_id


def recover_odoo_secret_metadata(
    *,
    record: OdooInstanceOverrideRecord,
    bindings: tuple[SecretBinding, ...],
    secret_records: tuple[SecretRecord, ...],
    events: tuple[SecretAuditEvent, ...],
    now: str,
    binding_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[SecretBinding, ...], tuple[SecretRecord, ...]]:
    """Return metadata writes and current-version expectations; never read values."""
    by_id = {binding.binding_id: binding for binding in bindings}
    secrets = {secret.secret_id: secret for secret in secret_records}
    targets: dict[str, SecretBinding] = {}
    referenced_ids: set[str] = set()
    for value, key in (
        *(
            (item.value, config_parameter_secret_env_key(item.key))
            for item in record.config_parameters
        ),
        *(
            (
                item.value,
                addon_setting_secret_env_key(addon_name=item.addon, setting_name=item.setting),
            )
            for item in record.addon_settings
        ),
    ):
        if value.source != "secret_binding" or value.secret_binding_id not in by_id:
            continue
        binding = by_id[value.secret_binding_id]
        target = (
            binding
            if binding.binding_key == key
            else by_id.get(expected_secret_binding_id(secret_id=binding.secret_id, binding_key=key))
        )
        if target is not None:
            referenced_ids.add(target.binding_id)
        if (
            target is not None
            and (not binding_ids or target.binding_id in binding_ids)
            and (target.declared_secret_class is None or target.value_source is None)
        ):
            targets[target.binding_id] = target
    if binding_ids - referenced_ids:
        raise ValueError(
            "Metadata recovery binding ids must identify this lane's referenced destinations."
        )

    writes: list[SecretBinding] = []
    expectations: dict[str, SecretRecord] = {}
    for target in targets.values():

        def refuse(reason: str = "ambiguous or stale relabel provenance") -> ValueError:
            return ValueError(
                f"Binding {target.binding_id!r} has {reason}; retain the current records. "
                "Use the supported product-config-dry-run/product-config-apply value-writing "
                "path with separately authorized supplied value and secret_class to re-establish "
                "origin/classification. No metadata-only force repair is supported. "
                "Use --repair-binding-id to isolate other recoverable destinations."
            )

        candidates = [
            event
            for event in events
            if event.event_type == "relabelled"
            and event.metadata.get("new_binding_id") == target.binding_id
        ]
        if not candidates:
            raise refuse("no retained relabel provenance")
        if len(candidates) != 1:
            raise refuse()
        event = candidates[0]
        source = by_id.get(event.metadata.get("old_binding_id", ""))
        secret = secrets.get(target.secret_id)
        if source is None or secret is None:
            raise refuse()
        if not (
            event.secret_id == source.secret_id == target.secret_id
            and secret.integration == source.integration == target.integration
            and source.binding_type == target.binding_type == "env"
            and source.context == target.context == record.context
            and source.instance == target.instance == record.instance
            and source.status == "disabled"
            and target.status == secret.status == "configured"
            and event.metadata.get("old_binding_key") == source.binding_key
            and event.metadata.get("new_binding_key") == target.binding_key
            and target.binding_id
            == expected_secret_binding_id(
                secret_id=source.secret_id, binding_key=target.binding_key
            )
            and source.updated_at == target.created_at == target.updated_at == event.recorded_at
        ):
            raise refuse()
        try:
            if datetime.fromisoformat(secret.updated_at) > datetime.fromisoformat(
                event.recorded_at
            ):
                raise refuse()
        except (ValueError, TypeError):
            raise refuse() from None
        classification = target.declared_secret_class or source.declared_secret_class
        value_source = target.value_source or source.value_source
        if classification is None or value_source is None:
            raise refuse()
        sharing = target.sharing_reason
        if sharing is None and classification == source.declared_secret_class:
            sharing = source.sharing_reason
        repaired = SecretBinding.model_validate(
            {
                **target.model_dump(),
                "declared_secret_class": classification,
                "value_source": value_source,
                "sharing_reason": sharing,
                "updated_at": now,
            }
        )
        writes.append(repaired)
        expectations[secret.secret_id] = secret
    return tuple(writes), tuple(expectations.values())
