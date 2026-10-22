"""One atomic activation bundle installs local credentials."""

import base64
import secrets as random_secrets

from control_plane.contracts.testing_activation import TestingActivationRecord
from control_plane.contracts.testing_application import TestingApplicationSpecification
from control_plane.contracts.product_profile_record import (
    product_config_requirement_applies_to_lane,
)
from control_plane.product_config import (
    _evaluate_product_config_runtime_key_safety,
    _plan_product_config_secret_write,
    normalize_product_config_payload,
)
from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    SecretDeliverySetExpectation,
)

STATE_SECRET_INTEGRATION = "testing_application_state"


def state_secret_name(record: TestingActivationRecord, purpose: str) -> str:
    return record.namespace + "-" + purpose


def credential_binding_names(
    store: PostgresRecordStore,
    product: str,
    context: str,
    specification: TestingApplicationSpecification,
) -> dict[str, str]:
    """The reviewed local intent cannot reinterpret an externally issued credential."""
    profile = store.read_product_profile_record(product)
    lane = next(
        item for item in profile.lanes if (item.context, item.instance) == (context, "testing")
    )
    requirements = tuple(
        item
        for item in profile.expected_config.managed_secret_bindings
        if item.integration == "runtime_environment"
        and product_config_requirement_applies_to_lane(
            requirement_context=item.context,
            requirement_instance=item.instance,
            lane=lane,
        )
    )
    delivered = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=context, instance_name="testing"
    )
    local_keys = {item.binding_key for item in specification.local_credentials}
    keys = set(local_keys)
    if (
        not local_keys.issubset({item.binding_key for item in requirements})
        or not local_keys.issubset(delivered)
        or any(
            item.owner_input is not None or item.external_provider is not None
            for item in requirements
            if item.binding_key in keys
        )
    ):
        raise ValueError(
            "Local generation requires declared delivered binding names without external owner input."
        )
    names = {key: key for key in keys}
    for key, binding in delivered.items():
        if key not in keys:
            if not (
                binding.declared_secret_class == "shared_safe"
                and binding.sharing_reason is not None
                and binding.sharing_reason.kind == "read_only_source"
            ):
                raise ValueError(
                    "A carried writable external capability needs an independent provider resource."
                )
            continue
        existing = store.read_secret_record(binding.secret_id)
        names[key] = existing.name if existing.scope == "context_instance" else key
    return names


def generation_bundle(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> ProductAuthorityBundle:
    specification = record.request.application
    if specification is None:
        raise ValueError("Application credentials require their reviewed specification.")
    names = credential_binding_names(store, record.product, record.context, specification)
    values: dict[str, str] = {}
    for item in specification.local_credentials:
        raw = random_secrets.token_bytes(32)
        values[item.binding_key] = (
            base64.b64encode(raw).decode()
            if item.encoding == "base64"
            else raw.hex()
            if item.encoding == "hex"
            else base64.urlsafe_b64encode(raw).decode().rstrip("=")
        )
    entries = [
        {
            "binding_key": key,
            "name": names[key],
            "generate": "testing_local",
            "secret_class": "testing",
        }
        for key in sorted(values)
    ]
    normalized = normalize_product_config_payload(
        {
            "product": record.product,
            "context": record.context,
            "instance": "testing",
            "secrets": entries,
        }
    )
    from typing import cast

    _evaluate_product_config_runtime_key_safety(
        record_store=store,
        context_name=record.context,
        instance_name="testing",
        secrets=tuple(cast(list[dict[str, object]], normalized["secrets"])),
    )
    plans = [
        _plan_product_config_secret_write(
            record_store=store,
            scope="context_instance",
            integration=integration,
            name=name,
            binding_key=key,
            plaintext_value=value,
            context_name=record.context,
            instance_name="testing",
            declared_secret_class="testing",
            value_source="generated",
            actor="service:testing-activation",
            source_label="service:testing-activation",
        )
        for integration, name, key, value in (
            *(
                (STATE_SECRET_INTEGRATION, state_secret_name(record, "runtime_" + key), key, value)
                for key, value in values.items()
            ),
        )
    ]
    for planned in plans:
        for event in planned["secret_audit_events"]:
            event.metadata.update(
                generation=record.preparation_record_id, value_source="service_generated"
            )
    return ProductAuthorityBundle(
        secret_delivery_read_sets=tuple(
            SecretDeliverySetExpectation(
                integration=integration,
                context=record.context,
                instance="testing",
                records=store.list_secret_records(
                    integration=integration, context_name=record.context, limit=None
                ),
                bindings=store.list_secret_bindings(
                    integration=integration, context_name=record.context, limit=None
                ),
            )
            for integration in ("runtime_environment", STATE_SECRET_INTEGRATION)
        ),
        secret_records=tuple(value for item in plans for value in item["secret_records"]),
        secret_versions=tuple(value for item in plans for value in item["secret_versions"]),
        secret_bindings=tuple(value for item in plans for value in item["secret_bindings"]),
        secret_audit_events=tuple(value for item in plans for value in item["secret_audit_events"]),
    )


def state_password(
    store: PostgresRecordStore, record: TestingActivationRecord, purpose: str
) -> str:
    from control_plane.secrets import _decrypt_secret_value

    secret = store.find_secret_record(
        scope="context_instance",
        integration=STATE_SECRET_INTEGRATION,
        name=state_secret_name(record, purpose),
        context=record.context,
        instance="testing",
    )
    if secret is None:
        raise ValueError("The original state credential is unavailable.")
    expected = record.state_recipient.get(purpose + "_version")
    if not expected or secret.current_version_id != expected:
        raise ValueError("The original state credential version changed.")
    version = store.read_secret_version(expected)
    if version.secret_id != secret.secret_id:
        raise ValueError("The original state credential version changed.")
    return _decrypt_secret_value(version.ciphertext, version.key_id)


def staged_runtime_values(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> dict[str, str]:
    specification = record.request.application
    assert specification is not None
    keys = {item.binding_key for item in specification.local_credentials}
    return {key: state_password(store, record, "runtime_" + key) for key in keys}


def runtime_activation_bundle(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> ProductAuthorityBundle:
    """Only final canonical publication makes the staged versions site-deliverable."""
    specification = record.request.application
    assert specification is not None
    names = credential_binding_names(store, record.product, record.context, specification)
    plans = [
        _plan_product_config_secret_write(
            record_store=store,
            scope="context_instance",
            integration="runtime_environment",
            name=names[key],
            binding_key=key,
            plaintext_value=value,
            context_name=record.context,
            instance_name="testing",
            declared_secret_class="testing",
            value_source="generated",
            actor="service:testing-activation",
            source_label="service:testing-activation",
        )
        for key, value in staged_runtime_values(store, record).items()
    ]
    for planned in plans:
        for event in planned["secret_audit_events"]:
            event.metadata.update(
                generation=record.preparation_record_id, value_source="service_generated"
            )
    return ProductAuthorityBundle(
        secret_delivery_read_sets=tuple(
            SecretDeliverySetExpectation(
                integration=integration,
                context=record.context,
                instance="testing",
                records=store.list_secret_records(
                    integration=integration, context_name=record.context, limit=None
                ),
                bindings=store.list_secret_bindings(
                    integration=integration, context_name=record.context, limit=None
                ),
            )
            for integration in ("runtime_environment", STATE_SECRET_INTEGRATION)
        ),
        secret_records=tuple(value for item in plans for value in item["secret_records"]),
        secret_versions=tuple(value for item in plans for value in item["secret_versions"]),
        secret_bindings=tuple(value for item in plans for value in item["secret_bindings"]),
        secret_audit_events=tuple(value for item in plans for value in item["secret_audit_events"]),
    )
