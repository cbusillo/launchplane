"""Service-only diagnostic credentials, selected from one exact lane's secret store."""

from typing import Protocol
from collections.abc import Iterable

import click
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretVersion
from control_plane.secrets import SecretBindingSelectionStore, _decrypt_secret_value

DIAGNOSTIC_SECRET_INTEGRATION = "odoo_diagnostics"
DIAGNOSTIC_IDENTITY_KEY = "ODOO_DIAGNOSTIC_IDENTITY"
DIAGNOSTIC_IDENTITY_UNAVAILABLE = "odoo_diagnostic_identity_unavailable"


def diagnostic_preview_secret_name(instance: str) -> str:
    return "preview-" + instance


def diagnostic_preview_context_owned(
    profiles: Iterable[LaunchplaneProductProfileRecord], product: str, context: str
) -> bool:
    profiles = tuple(profiles)
    return any(
        profile.product == product
        and profile.is_active
        and profile.driver_id == "odoo"
        and profile.preview.enabled
        and profile.preview.context == context
        for profile in profiles
    ) and not any(
        profile.product != product
        and (
            context in {lane.context.strip() for lane in profile.lanes}
            or context in profile.historical_contexts
            or (profile.preview.enabled and profile.preview.context == context)
        )
        for profile in profiles
    )


def is_diagnostic_preview_config(payload: dict[str, object]) -> bool:
    """A context write may store only individually named preview identities."""
    runtime = payload.get("runtime_env")
    secrets = payload.get("secrets")
    if (
        payload.get("instance")
        or "public_hosts" in payload
        or not isinstance(runtime, dict)
        or runtime.get("env")
        or runtime.get("retired_provider_keys") is not None
        or runtime.get("adopt_provider_keys") is not None
        or not isinstance(secrets, list)
        or not secrets
    ):
        return False
    for secret in secrets:
        if not isinstance(secret, dict) or (
            secret.get("integration") != DIAGNOSTIC_SECRET_INTEGRATION
            or secret.get("binding_key") != DIAGNOSTIC_IDENTITY_KEY
            or secret.get("scope") != "context"
            or secret.get("context") != payload.get("context")
            or secret.get("instance")
            or secret.get("copy_from") is not None
            or secret.get("adopt_from_provider") is not None
        ):
            return False
        try:
            identity = OdooDiagnosticIdentity.model_validate_json(secret["value"])
        except (ValueError, KeyError, TypeError):
            return False
        if (
            identity.product != payload.get("product")
            or identity.context != payload.get("context")
            or secret.get("name") != diagnostic_preview_secret_name(identity.instance)
        ):
            return False
    return True


class OdooDiagnosticIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    instance: str = Field(min_length=1)
    database: str = Field(min_length=1)
    login: str = Field(min_length=1, repr=False)
    password: SecretStr = Field(repr=False)
    user_id: int = Field(gt=0)


class OdooDiagnosticIdentityStore(SecretBindingSelectionStore, Protocol):
    def read_secret_version(self, version_id: str) -> SecretVersion: ...


def read_diagnostic_identity(
    store: OdooDiagnosticIdentityStore, runtime: RuntimeIdentity, database: str
) -> OdooDiagnosticIdentity:
    """No inherited bindings, ambiguous selection, runtime env, or admin fallback."""
    try:
        preview = runtime.environment_kind == "preview"
        records = {
            record.secret_id: record
            for record in store.list_secret_records(integration=DIAGNOSTIC_SECRET_INTEGRATION)
            if record.integration == DIAGNOSTIC_SECRET_INTEGRATION
            and record.scope == ("context" if preview else "context_instance")
            and record.context == runtime.context
            and record.instance == ("" if preview else runtime.instance)
            and (not preview or record.name == diagnostic_preview_secret_name(runtime.instance))
            and record.status == "configured"
        }
        pairs = [
            (binding, records[binding.secret_id])
            for binding in store.list_secret_bindings(integration=DIAGNOSTIC_SECRET_INTEGRATION)
            if binding.secret_id in records
            and binding.integration == DIAGNOSTIC_SECRET_INTEGRATION
            and binding.binding_key == DIAGNOSTIC_IDENTITY_KEY
            and binding.context == runtime.context
            and binding.instance == ("" if preview else runtime.instance)
            and binding.status == "configured"
        ]
        if len(pairs) != 1:
            raise ValueError
        _, record = pairs[0]
        version = store.read_secret_version(record.current_version_id)
        if version.secret_id != record.secret_id or version.version_id != record.current_version_id:
            raise ValueError
        identity = OdooDiagnosticIdentity.model_validate_json(
            _decrypt_secret_value(version.ciphertext, version.key_id)
        )
        if (
            (identity.product, identity.context, identity.instance, identity.database)
            != (runtime.product, runtime.context, runtime.instance, database)
            or not identity.password.get_secret_value()
            or not identity.login.strip()
            or identity.login.strip().casefold() == "admin"
            or identity.user_id <= 2
        ):
            raise ValueError
        return identity
    except (OSError, ValueError, KeyError, TypeError, click.ClickException):
        # Validation and crypto exceptions can contain private input. Neither
        # their text nor the encrypted payload belongs in a diagnostic receipt.
        raise ValueError("The lane's Odoo diagnostic identity is unavailable.") from None
