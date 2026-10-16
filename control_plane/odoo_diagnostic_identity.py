"""Service-only diagnostic credentials, selected from one exact lane's secret store."""

from typing import Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.secret_record import SecretVersion
from control_plane.secrets import SecretBindingSelectionStore, _decrypt_secret_value

DIAGNOSTIC_SECRET_INTEGRATION = "odoo_diagnostics"
DIAGNOSTIC_IDENTITY_KEY = "ODOO_DIAGNOSTIC_IDENTITY"


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
        records = {
            record.secret_id: record
            for record in store.list_secret_records(integration=DIAGNOSTIC_SECRET_INTEGRATION)
            if record.integration == DIAGNOSTIC_SECRET_INTEGRATION
            and record.scope == "context_instance"
            and record.context == runtime.context
            and record.instance == runtime.instance
            and record.status == "configured"
        }
        pairs = [
            (binding, records[binding.secret_id])
            for binding in store.list_secret_bindings(integration=DIAGNOSTIC_SECRET_INTEGRATION)
            if binding.secret_id in records
            and binding.integration == DIAGNOSTIC_SECRET_INTEGRATION
            and binding.binding_key == DIAGNOSTIC_IDENTITY_KEY
            and binding.context == runtime.context
            and binding.instance == runtime.instance
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
