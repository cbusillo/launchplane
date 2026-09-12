from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion

if TYPE_CHECKING:
    from control_plane.github_app_identity import GitHubAppIdentity


PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY = (
    "LAUNCHPLANE_PROVIDER_DELIVERY_INSPECTION_GITHUB_APP_ID"
)
PROVIDER_DELIVERY_INSPECTION_INTEGRATION = "provider_delivery_inspection_github_app"
PROVIDER_DELIVERY_INSPECTION_PRIVATE_KEY_BINDING = "private_key"
PROVIDER_DELIVERY_INSPECTION_PROFILE_ID = "provider-delivery-inspection-v1"
PROVIDER_DELIVERY_INSPECTION_PERMISSIONS = (
    "administration:write",
    "contents:read",
    "metadata:read",
)
_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


class ProviderDeliveryInspectionProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedProviderDeliveryInspectionProfile:
    identity: GitHubAppIdentity = field(repr=False)
    profile_id: str
    profile_sha256: str
    app_id: int
    secret_id: str
    secret_binding_id: str
    secret_version_id: str
    permissions: tuple[str, ...]


class ProviderDeliveryInspectionProfileStore(Protocol):
    def list_runtime_environment_records(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]: ...

    def read_secret_version(self, version_id: str) -> SecretVersion: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...


def resolve_provider_delivery_inspection_profile(
    *,
    record_store: ProviderDeliveryInspectionProfileStore,
) -> ResolvedProviderDeliveryInspectionProfile:
    from control_plane.github_app_identity import GitHubAppIdentity

    runtime_records = record_store.list_runtime_environment_records(
        scope="context",
        context_name=_LAUNCHPLANE_SERVICE_CONTEXT,
        instance_name="",
    )
    if len(runtime_records) != 1:
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection requires one exact service runtime record."
        )
    runtime_record = runtime_records[0]
    if (
        runtime_record.scope != "context"
        or runtime_record.context != _LAUNCHPLANE_SERVICE_CONTEXT
        or runtime_record.instance
    ):
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection service runtime record is not exact."
        )
    raw_app_id = runtime_record.env.get(PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY)
    app_id = _positive_app_id(raw_app_id)

    records = tuple(
        record
        for record in record_store.list_secret_records(
            integration=PROVIDER_DELIVERY_INSPECTION_INTEGRATION,
            context_name=_LAUNCHPLANE_SERVICE_CONTEXT,
            instance_name="",
            limit=None,
        )
        if _is_exact_secret_record(record)
    )
    bindings = tuple(
        binding
        for binding in record_store.list_secret_bindings(
            integration=PROVIDER_DELIVERY_INSPECTION_INTEGRATION,
            context_name=_LAUNCHPLANE_SERVICE_CONTEXT,
            instance_name="",
            limit=None,
        )
        if _is_exact_secret_binding(binding)
    )
    if len(records) != 1 or len(bindings) != 1:
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection requires one exact managed-secret binding."
        )
    record = records[0]
    binding = bindings[0]
    if binding.secret_id != record.secret_id:
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection managed-secret binding is ambiguous."
        )
    try:
        version = record_store.read_secret_version(record.current_version_id)
    except FileNotFoundError as error:
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection managed secret is unavailable."
        ) from error
    if version.version_id != record.current_version_id or version.secret_id != record.secret_id:
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection managed-secret version is not current and exact."
        )
    private_key = control_plane_secrets._decrypt_secret_value(
        version.ciphertext, version.key_id
    ).strip()
    if not private_key:
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection managed secret is unavailable."
        )
    profile_payload: dict[str, object] = {
        "schema_version": 1,
        "profile_id": PROVIDER_DELIVERY_INSPECTION_PROFILE_ID,
        "app_id": app_id,
        "secret_id": record.secret_id,
        "secret_binding_id": binding.binding_id,
        "secret_version_id": version.version_id,
        "permissions": PROVIDER_DELIVERY_INSPECTION_PERMISSIONS,
        "repository_scope": "exact_repository_id",
        "endpoint_contract": "provider-delivery-inspection-github-v1",
    }
    return ResolvedProviderDeliveryInspectionProfile(
        identity=GitHubAppIdentity(app_id=app_id, private_key=private_key),
        profile_id=PROVIDER_DELIVERY_INSPECTION_PROFILE_ID,
        profile_sha256=canonical_json_sha256(profile_payload),
        app_id=app_id,
        secret_id=record.secret_id,
        secret_binding_id=binding.binding_id,
        secret_version_id=version.version_id,
        permissions=PROVIDER_DELIVERY_INSPECTION_PERMISSIONS,
    )


def _positive_app_id(value: object) -> int:
    if isinstance(value, bool):
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection GitHub App id is unavailable."
        )
    if isinstance(value, int):
        app_id = value
    elif isinstance(value, str) and value.strip().isdecimal():
        app_id = int(value.strip())
    else:
        app_id = 0
    if not 0 < app_id <= 2**63 - 1:
        raise ProviderDeliveryInspectionProfileError(
            "Provider-delivery inspection GitHub App id is unavailable."
        )
    return app_id


def _is_exact_secret_record(record: SecretRecord) -> bool:
    return (
        record.scope == "context"
        and record.context == _LAUNCHPLANE_SERVICE_CONTEXT
        and not record.instance
        and record.integration == PROVIDER_DELIVERY_INSPECTION_INTEGRATION
        and record.policy == "write_only"
        and record.status == "configured"
    )


def _is_exact_secret_binding(binding: SecretBinding) -> bool:
    return (
        binding.context == _LAUNCHPLANE_SERVICE_CONTEXT
        and not binding.instance
        and binding.integration == PROVIDER_DELIVERY_INSPECTION_INTEGRATION
        and binding.binding_key == PROVIDER_DELIVERY_INSPECTION_PRIVATE_KEY_BINDING
        and binding.status == "configured"
    )
