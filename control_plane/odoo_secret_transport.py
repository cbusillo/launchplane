"""Plan metadata-only Odoo secret transport changes for one atomic commit."""

from dataclasses import dataclass
from uuid import uuid4

from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.secret_record import SecretAuditEvent, SecretBinding, SecretRecord
from control_plane.odoo_secret_metadata import recover_odoo_secret_metadata
from control_plane.odoo_instance_overrides import (
    addon_setting_secret_env_key,
    config_parameter_secret_env_key,
)
from control_plane.secrets import expected_secret_binding_id
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.storage.product_authority_bundle import (
    OdooInstanceOverrideConflictError,
    OdooInstanceOverrideWrite,
    ProductAuthorityBundle,
    SecretBindingSetExpectation,
    odoo_instance_override_records_match,
)


def _binding_references(record: OdooInstanceOverrideRecord) -> set[str]:
    return {
        value.secret_binding_id
        for value in (
            *(override.value for override in record.config_parameters),
            *(override.value for override in record.addon_settings),
        )
        if value.source == "secret_binding"
    }


@dataclass(frozen=True)
class OdooSecretTransportPlan:
    record: OdooInstanceOverrideRecord
    bundle: ProductAuthorityBundle
    changes: list[dict[str, str]]


def plan_odoo_secret_transport(
    *,
    record: OdooInstanceOverrideRecord,
    records: tuple[OdooInstanceOverrideRecord, ...],
    bindings: tuple[SecretBinding, ...],
    source_label: str,
    repair_metadata: bool = False,
    secret_records: tuple[SecretRecord, ...] = (),
    audit_events: tuple[SecretAuditEvent, ...] = (),
) -> OdooSecretTransportPlan:
    if not any(odoo_instance_override_records_match(record, other) for other in records):
        raise OdooInstanceOverrideConflictError("Odoo overrides changed before planning.")
    now = utc_now_timestamp()
    original_bindings = bindings
    repairs, expected_secrets = (
        recover_odoo_secret_metadata(
            record=record,
            bindings=bindings,
            secret_records=secret_records,
            events=audit_events,
            now=now,
        )
        if repair_metadata
        else ((), ())
    )
    repairs_by_id = {binding.binding_id: binding for binding in repairs}
    bindings = tuple(repairs_by_id.get(binding.binding_id, binding) for binding in bindings)
    binding_by_id = {binding.binding_id: binding for binding in bindings}
    writes: dict[str, SecretBinding] = dict(repairs_by_id)
    sources: dict[str, SecretBinding] = {}
    changes: list[dict[str, str]] = []

    def migrate(
        value: OdooOverrideValue, *, key: str, desired_key: str, kind: str
    ) -> OdooOverrideValue:
        if value.source != "secret_binding":
            return value
        binding = binding_by_id.get(value.secret_binding_id)
        if binding is None:
            raise ValueError(
                f"{kind} {key!r} references missing secret binding {value.secret_binding_id!r}."
            )
        desired_id = expected_secret_binding_id(
            secret_id=binding.secret_id, binding_key=desired_key
        )
        target = writes.get(desired_id) or binding_by_id.get(desired_id)
        if binding.binding_key == desired_key:
            desired_id = binding.binding_id
        else:
            replacement = binding.model_copy(
                update={
                    "binding_id": desired_id,
                    "binding_key": desired_key,
                    "status": "configured",
                }
            )
            repaired_target = target
            if target is not None:
                # The former relabel helper omitted these fields. Restore only
                # absent metadata; never replace a destination's differing policy.
                repaired_target = target.model_copy(
                    update={
                        "declared_secret_class": target.declared_secret_class
                        or binding.declared_secret_class,
                        "sharing_reason": target.sharing_reason or binding.sharing_reason,
                    }
                )
            if (
                repaired_target is not None
                and repaired_target.model_copy(
                    update={"created_at": binding.created_at, "updated_at": binding.updated_at}
                )
                != replacement
            ):
                raise ValueError(
                    f"{kind} {key!r} destination binding {desired_id!r} conflicts with source."
                )
            if binding.status != "configured" and target is None:
                raise ValueError(
                    f"{kind} {key!r} secret binding {binding.binding_id!r} is not configured."
                )
            if target is None:
                writes[desired_id] = replacement.model_copy(
                    update={"created_at": now, "updated_at": now}
                )
            elif repaired_target is not None and repaired_target != target:
                writes[desired_id] = repaired_target.model_copy(update={"updated_at": now})
            sources[binding.binding_id] = binding
        changes.append(
            {
                "kind": kind,
                "key": key,
                "action": "unchanged" if desired_id == binding.binding_id else "pending",
                "old_binding_id": binding.binding_id,
                "new_binding_id": desired_id,
                "old_binding_key": binding.binding_key,
                "new_binding_key": desired_key,
            }
        )
        return OdooOverrideValue(source="secret_binding", secret_binding_id=desired_id)

    config_parameters = tuple(
        override.model_copy(
            update={
                "value": migrate(
                    override.value,
                    key=override.key,
                    desired_key=config_parameter_secret_env_key(override.key),
                    kind="config_parameter",
                )
            }
        )
        for override in record.config_parameters
    )
    addon_settings = tuple(
        override.model_copy(
            update={
                "value": migrate(
                    override.value,
                    key=f"{override.addon}.{override.setting}",
                    desired_key=addon_setting_secret_env_key(
                        addon_name=override.addon, setting_name=override.setting
                    ),
                    kind="addon_setting",
                )
            }
        )
        for override in record.addon_settings
    )
    changed = any(change["action"] == "pending" for change in changes)
    updated = (
        record.model_copy(
            update={
                "config_parameters": config_parameters,
                "addon_settings": addon_settings,
                "updated_at": now,
                "source_label": source_label.strip() or "odoo-secret-transport-migration",
            }
        )
        if changed
        else record
    )
    remaining = set().union(
        *(_binding_references(updated if other == record else other) for other in records)
    )
    events: list[SecretAuditEvent] = []
    for binding in sources.values():
        if binding.binding_id not in remaining and binding.status == "configured":
            writes[binding.binding_id] = binding.model_copy(
                update={"status": "disabled", "updated_at": now}
            )
    for change in changes:
        if change["old_binding_id"] == change["new_binding_id"]:
            continue
        binding = sources[change["old_binding_id"]]
        events.append(
            SecretAuditEvent(
                event_id=f"{binding.secret_id}-relabelled-{uuid4().hex}",
                secret_id=binding.secret_id,
                event_type="relabelled",
                recorded_at=now,
                actor=source_label,
                detail="Odoo secret transport and override references migrated atomically.",
                metadata={
                    "source": source_label,
                    **{
                        key: change[key]
                        for key in (
                            "old_binding_id",
                            "old_binding_key",
                            "new_binding_id",
                            "new_binding_key",
                        )
                    },
                },
            )
        )
    for binding in repairs:
        changes.append(
            {
                "kind": "binding_metadata",
                "key": binding.binding_key,
                "action": "metadata_pending",
                "binding_id": binding.binding_id,
            }
        )
        events.append(
            SecretAuditEvent(
                event_id=f"{binding.secret_id}-metadata-{uuid4().hex}",
                secret_id=binding.secret_id,
                event_type="validated",
                recorded_at=now,
                actor=source_label,
                detail="Recovered omitted Odoo relabel metadata from retained provenance.",
                metadata={"binding_id": binding.binding_id, "action": "relabel_metadata_repair"},
            )
        )
    secret_ids = {binding.secret_id for binding in (*sources.values(), *repairs)}
    return OdooSecretTransportPlan(
        record=updated,
        changes=changes,
        bundle=ProductAuthorityBundle(
            odoo_instance_override_writes=(
                (OdooInstanceOverrideWrite(record=updated, expected_record=record),)
                if changed or repairs
                else ()
            ),
            secret_bindings=tuple(writes.values()),
            secret_audit_events=tuple(events),
            expected_secret_records=expected_secrets,
            secret_binding_sets=tuple(
                SecretBindingSetExpectation(
                    secret_id=secret_id,
                    bindings=tuple(
                        binding for binding in original_bindings if binding.secret_id == secret_id
                    ),
                )
                for secret_id in sorted(secret_ids)
            ),
        ),
    )
