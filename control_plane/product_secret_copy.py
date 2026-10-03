"""Product-owned runtime secret references and metadata, never plaintext reads."""

from typing import Protocol

import click
from pydantic import BaseModel, ConfigDict, field_validator

from control_plane import secrets
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.contracts.secret_record import SecretScope, SecretSharingReason
from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretClass


class ProductSecretCopyFrom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    version_id: str

    @field_validator("context", "instance", "version_id")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Secret references require context, instance and version_id.")
        return value.strip()


class ProductSecretCopyError(ValueError):
    """A reference is outside the product's store, unavailable, or stale."""


class ProductSecretBindingMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_key: str
    name: str
    scope: SecretScope
    context: str
    instance: str
    secret_class: RuntimeSecretClass | None = None
    sharing_reason: SecretSharingReason | None = None
    version_id: str


class ProductSecretCopyStore(secrets.SecretReadStore, Protocol):
    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


def require_product_lanes(
    store: ProductSecretCopyStore, *, product: str, routes: tuple[tuple[str, str], ...]
) -> LaunchplaneProductProfileRecord:
    profiles = store.list_product_profile_records()
    profile = next((item for item in profiles if item.product == product), None)
    owners = product_context_owner_map(profiles)
    if profile is None or any(
        instance not in {"prod", "testing", "dev"}
        or not any(lane.context == context and lane.instance == instance for lane in profile.lanes)
        or not is_exclusive_product_context(context=context, product=product, owners=owners)
        for context, instance in routes
    ):
        raise ProductSecretCopyError("Secret references require stable lanes of the same product.")
    return profile


def resolve_copy_source(
    store: ProductSecretCopyStore,
    *,
    product: str,
    target_context: str,
    target_instance: str,
    binding_key: str,
    reference: ProductSecretCopyFrom,
) -> tuple[LaunchplaneProductProfileRecord, SecretBinding, SecretRecord, SecretVersion]:
    profile = require_product_lanes(
        store,
        product=product,
        routes=((target_context, target_instance), (reference.context, reference.instance)),
    )
    if (target_context, target_instance) == (reference.context, reference.instance):
        raise ProductSecretCopyError("Secret copy source must be a different lane.")
    candidates = product_secret_bindings(store, product=product)
    # Use the same lane precedence as delivery, while refusing ambiguous bindings.
    candidates = [
        (binding, record)
        for binding, record in candidates
        if binding.binding_key == binding_key
        and record.context == reference.context
        and (record.scope == "context" or record.instance == reference.instance)
    ]
    exact = [pair for pair in candidates if pair[1].scope == "context_instance"]
    selected = exact or candidates
    if len(selected) != 1:
        raise ProductSecretCopyError("Secret copy source is missing or ambiguous.")
    binding, record = selected[0]
    if record.current_version_id != reference.version_id:
        raise ProductSecretCopyError("Secret copy source changed; read metadata and review again.")
    try:
        version = store.read_secret_version(reference.version_id)
    except FileNotFoundError as error:
        raise ProductSecretCopyError("Secret copy source version is unavailable.") from error
    if version.secret_id != record.secret_id:
        raise ProductSecretCopyError("Secret copy source version does not match its record.")
    return profile, binding, record, version


def copy_source_value(version: SecretVersion) -> str:
    try:
        return secrets._decrypt_secret_value(version.ciphertext, version.key_id)
    except click.ClickException as error:
        raise ProductSecretCopyError(
            "Secret copy source cannot be decrypted by the service."
        ) from error


def product_secret_bindings(
    store: ProductSecretCopyStore, *, product: str
) -> list[tuple[SecretBinding, SecretRecord]]:
    profiles = store.list_product_profile_records()
    profile = next((item for item in profiles if item.product == product), None)
    if profile is None:
        raise ProductSecretCopyError("Product secret bindings require an existing product.")
    owners = product_context_owner_map(profiles)
    contexts = {lane.context for lane in profile.lanes}
    records = {
        record.secret_id: record
        for record in store.list_secret_records(
            integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION, limit=None
        )
        if record.integration == secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION
        and record.status == "configured"
        and record.scope in {"context", "context_instance"}
        and record.context in contexts
        and is_exclusive_product_context(context=record.context, product=product, owners=owners)
    }
    return [
        (binding, records[binding.secret_id])
        for binding in store.list_secret_bindings(
            integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION, limit=None
        )
        if binding.status == "configured"
        and binding.secret_id in records
        and binding.integration == records[binding.secret_id].integration
        and binding.context == records[binding.secret_id].context
        and binding.instance == records[binding.secret_id].instance
    ]


def product_secret_binding_metadata(
    store: ProductSecretCopyStore, *, product: str
) -> list[dict[str, object]]:
    return sorted(
        [
            {
                "binding_key": binding.binding_key,
                "name": record.name,
                "scope": record.scope,
                "context": record.context,
                "instance": record.instance,
                "secret_class": binding.declared_secret_class,
                "sharing_reason": (
                    binding.sharing_reason.model_dump(mode="json")
                    if binding.sharing_reason is not None
                    else None
                ),
                "version_id": record.current_version_id,
            }
            for binding, record in product_secret_bindings(store, product=product)
        ],
        key=lambda item: (str(item["context"]), str(item["instance"]), str(item["binding_key"])),
    )
